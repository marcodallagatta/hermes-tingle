"""Typed, authenticated owner-DM setup. Drafts are not runtime policy or grants."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
import tempfile
import time
from pathlib import Path

try:
    from . import hermes_tingle_resources as resources
    from .hermes_tingle_config import load_settings, validate_settings
    from .hermes_tingle_policy import DEFAULTS, NAME, snapshot, write_policy, EVALUATION_DISCLOSURE, evaluation_consent
    from . import setup_jobs
except ImportError:
    import hermes_tingle_resources as resources
    from hermes_tingle_config import load_settings, validate_settings
    from hermes_tingle_policy import DEFAULTS, NAME, snapshot, write_policy, EVALUATION_DISCLOSURE, evaluation_consent
    import setup_jobs

PROVISIONING = (
    'No Tingle owner is configured here. New profiles take the owner from the configured home channel, '
    'so `/tingle setup` in the owner DM is enough. Older or broken profiles need operator repair; use '
    '`setup-owner.json` only when the home channel is not a DM. Nothing changed.'
)
OWNER = 'setup-owner.json'
STATE = 'setup-state.json'
TTL = 1800
FIELDS = ('destination', 'timezone', 'review', 'deadline', 'date-only', 'availability', 'sources', 'evaluation', 'exclusions')
EXCLUSIONS = ('calendar_exclude_substrings', 'workstream_exclude_values', 'source_exclude_substrings', 'ticket_exclude_patterns')
# Detect an active-process profile switch even when the new profile has no draft.
# Persistent drafts also bind the exact path/inode and authority below.
_active_profiles: dict[tuple, Path] = {}


class OwnerRefused(ValueError):
    """Safe public refusal, distinct from filesystem permission failures."""


def _identity(path):
    info = path.stat()
    return [info.st_dev, info.st_ino]


def _read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('Setup files must be regular files')
        return stream.read()


def _config_document(home):
    """Parse the real profile config with Hermes's YAML policy, or fail closed."""
    config = resources.safe(home / 'config.yaml', home)
    try:
        import hermes_yaml
        document = hermes_yaml.safe_load(config.read_text())
    except Exception:
        return None, None
    if not isinstance(document, dict):
        return None, None
    return document, config


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate manifest key')
        result[key] = value
    return result


def _reject_json_constant(value):
    raise ValueError('non-standard JSON constant')


def _strict_manifest(home):
    """Reject duplicate keys and malformed entries before trusting provenance."""
    path = resources.safe(home / resources.MANIFEST, home)
    data = json.loads(path.read_text(), object_pairs_hook=_no_duplicate_keys)
    if (not isinstance(data, dict) or data.get('version') != 1
            or data.get('home') != str(Path(home).resolve(strict=True))
            or any(not isinstance(data.get(k), dict) for k in ('files', 'directories', 'jobs'))):
        raise ValueError('invalid manifest')
    for group in ('files', 'directories'):
        for value in data[group].values():
            if (not isinstance(value, dict) or value.get('origin') != 'created'
                    or not isinstance(value.get('device'), int) or not isinstance(value.get('inode'), int)):
                raise ValueError('invalid manifest provenance')
    for value in data['jobs'].values():
        if not isinstance(value, dict) or not isinstance(value.get('definition'), dict):
            raise ValueError('invalid manifest job provenance')
    return data


def _prior_tingle_state(home):
    """Distinguish an old install from admitted-chat metadata in a new DB."""
    if (home / 'hermes-tingle.json').exists() or list(home.glob('.tingle-policy-backup-*')):
        return True
    if (home / '.tingle-removal-recovery').exists():
        return True
    # Older installations may predate the provenance manifest. Existing copied
    # helpers or profile Cron entries are still evidence of prior Tingle state.
    for name in ('hermes_tingle_snapshot.py', 'hermes_tingle_due.py'):
        helper = resources.safe(home / 'scripts' / name, home)
        if helper.exists():
            return True
    cron_path = resources.safe(home / 'cron' / 'jobs.json', home)
    if cron_path.exists():
        try:
            cron = json.loads(_read(cron_path).decode('utf-8-sig'),
                              object_pairs_hook=_no_duplicate_keys,
                              parse_constant=_reject_json_constant)
            jobs = cron.get('jobs') if isinstance(cron, dict) else None
            if not isinstance(jobs, list):
                return True
            for job in jobs:
                if (not isinstance(job, dict) or not isinstance(job.get('id'), str)
                        or not isinstance(job.get('name'), str)
                        or not isinstance(job.get('script'), (str, type(None)))):
                    return True
                toolsets = []
                for key in ('enabled_toolsets', 'toolsets'):
                    configured = job.get(key)
                    if configured is None:
                        continue
                    if not isinstance(configured, list) or any(not isinstance(value, str) for value in configured):
                        return True
                    toolsets.extend(configured)
                if ('tingle' in job['name'].casefold()
                        or Path(str(job.get('script') or '')).name in
                        ('hermes_tingle_snapshot.py', 'hermes_tingle_due.py')
                        or 'hermes_tingle' in toolsets):
                    return True
        except Exception:
            # An unreadable or ambiguous scheduler inventory cannot prove this
            # is a fresh profile; fail closed rather than bootstrap authority.
            return True
    manifest = home / resources.MANIFEST
    if manifest.exists():
        # The first setup reply records only its private draft. Any other
        # created path or any recorded job proves this is not a fresh install.
        try:
            data = _strict_manifest(home)
        except Exception:
            return True
        wizard_paths = {
            'plugin-data/hermes-tingle/setup-state.json',
            'plugin-data/hermes-tingle/setup.lock',
        }
        files = data.get('files', {})
        directories = data.get('directories', {})
        if data.get('jobs') or any(path not in wizard_paths for path in files):
            return True
        if any(path != 'plugin-data/hermes-tingle' for path in directories):
            return True
    for namespace in ('hermes-tingle-lookback', 'hermes-tingle-body-audit'):
        root = home / 'plugin-data' / namespace
        try:
            if root.exists() and any(root.iterdir()):
                return True
        except OSError:
            return True
    db = home / 'plugin-data' / 'hermes-tingle' / 'data.db'
    if not db.exists():
        return False
    try:
        import sqlite3
        conn = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=0)
        try:
            tables = {row[0] for row in conn.execute("select name from sqlite_master where type='table'")}
            checks = (
                ('hermes_tingle_items', ''),
                ('hermes_tingle_source_optins', "where enabled=1 or generation>0 or enabled_at is not null or note != ''"),
                ('chat_source_consents', ''),
                ('onboarding_item_journal', ''),
                ('plugin_meta', "where key='onboarding_state'"),
                ('hermes_tingle_steps', ''),
            )
            for table, clause in checks:
                if table in tables and conn.execute(f'select 1 from {table} {clause} limit 1').fetchone():
                    return True
        finally:
            conn.close()
    except Exception:
        return True
    return False


def _profile_owner(home, binding):
    """Fresh-profile owner from the configured Telegram home channel, never from a stranger DM."""
    platform, user, _chat, _thread = binding
    # Other gateways have no verified home-channel DM predicate here.
    if platform != 'telegram':
        return None
    document, config = _config_document(home)
    if document is None or config is None:
        return None
    platforms = document.get('platforms')
    if not isinstance(platforms, dict):
        return None
    entry = platforms.get(platform)
    if not isinstance(entry, dict):
        return None
    channel = entry.get('home_channel')
    if not isinstance(channel, dict):
        return None
    if str(channel.get('platform', '')).casefold() != platform:
        return None
    owner_user = str(channel.get('user_id', '') or '').strip()
    owner_chat = str(channel.get('chat_id', '') or '').strip()
    if not owner_user or not owner_chat or owner_user.casefold() != user.casefold():
        return None
    # Telegram DM chat IDs are positive and match the sender; groups never own.
    if not re.fullmatch(r'\d+', owner_chat) or owner_chat != owner_user:
        return None
    info = config.stat()
    config_identity = [info.st_dev, info.st_ino, hashlib.sha256(config.read_bytes()).hexdigest()]
    return [f'{platform}:{owner_user}'], config_identity


def _default_authority(owners, config_identity):
    raw = json.dumps({'owner_principals': owners, 'default': True,
                      'config_identity': config_identity}, ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest(), config_identity


def _current_authority(home, data_dir, source, binding, owners, authority, owner_identity):
    """Revalidate either the provisioned file or the unchanged home-channel default."""
    if _binding(source) != binding:
        return False
    owner_path = resources.safe(data_dir / OWNER, home)
    if owner_path.exists():
        return _authority(home, data_dir, source)[2:] == (authority, owner_identity)
    default = _profile_owner(home, binding)
    if default is None:
        return False
    current_owners, config_identity = default
    current_authority, current_identity = _default_authority(current_owners, config_identity)
    return ((current_owners, current_authority, current_identity)
            == (owners, authority, owner_identity))


def _binding(source):
    platform = getattr(source, 'platform', None)
    platform = str(getattr(platform, 'value', platform) or '')
    user = str(getattr(source, 'user_id', '') or '')
    chat = str(getattr(source, 'chat_id', '') or '')
    # Do not infer a DM from an absent/unknown chat_type, nor a chat from user_id.
    if (source is None or getattr(source, 'is_bot', False) or not platform or not user or not chat
            or str(getattr(source, 'chat_type', '')).casefold() not in ('dm', 'private')):
        raise ValueError('Setup requires an authenticated human in a verified direct message; nothing changed.')
    return (platform.casefold(), user.casefold(), chat, str(getattr(source, 'thread_id', '') or ''))


def _authority(home, data_dir, source):
    binding = _binding(source)
    owner_path = resources.safe(data_dir / OWNER, home)
    if owner_path.exists():
        raw = _read(owner_path)
        data = json.loads(raw)
        owners = data.get('owner_principals') if isinstance(data, dict) else None
        if (not isinstance(data, dict) or data.get('fresh_profile') is not True
                or not isinstance(owners, list) or not owners or any(
                not isinstance(v, str) or not re.fullmatch(r'[a-z0-9_]+:[^\s:]+', v, re.I) for v in owners)):
            raise ValueError(PROVISIONING)
    else:
        # No operator file: a genuinely fresh profile inherits the configured
        # home-channel owner. Callers have already excluded existing policy and
        # a consumed bootstrap; nobody self-enrolls from an arbitrary DM.
        if _prior_tingle_state(home):
            raise ValueError(PROVISIONING)
        default = _profile_owner(home, binding)
        if default is None:
            raise ValueError(PROVISIONING)
        owners, config_identity = default
        authority, config_identity = _default_authority(owners, config_identity)
        return binding, owners, authority, config_identity
    if ':'.join(binding[:2]) not in {v.casefold() for v in owners}:
        raise OwnerRefused('Setup refused: this sender is not a configured owner principal. Nothing changed.')
    return binding, owners, hashlib.sha256(raw).hexdigest(), _identity(owner_path)


def _save(home, path, state):
    resources.safe(path, home)
    fd, temp = tempfile.mkstemp(prefix='.setup-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(state, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        resources.record(path, home=home)
    finally:
        Path(temp).unlink(missing_ok=True)


def _prompt(state):
    field = FIELDS[state['step']]
    hints = {
        'destination': 'Where should reminders go? Reply `/tingle setup here` to use this chat (`'
                       + ':'.join((state['binding'][0], state['binding'][2])) + '`), or give an explicit '
                       'destination like `telegram:<chat-id>[:<topic-id>]`. `/tingle identity` shows IDs; '
                       'your sender ID is not always the chat destination.',
        'timezone': 'Which IANA timezone? Example: Europe/Berlin (default suggestion: Etc/UTC).',
        'review': 'Which review cron? Suggestion: 0 0,8,16 * * * (00:00, 08:00, 16:00 in your timezone).',
        'deadline': 'How many minutes between script-only deadline checks? Suggestion: 30; range 1–1440.',
        'date-only': 'What local time for date-only deadlines? HH:MM; suggestion: 09:00.',
        'availability': 'What same-day availability window? Two HH:MM times, e.g. 09:00 18:00.',
        'sources': 'Gmail, Google Calendar and chat are proposed ON (all). Reply all to approve minimal connection checks and grants only after WRITE, or none to keep them off. Gmail is bounded mail/sent follow-ups; Calendar is read-only (including organizer/count context); chat covers this profile’s known chats, with /tingle chat exclude available later. Google Drive is not a source. A failed check stays off. No account/message read has happened.',
        'evaluation': EVALUATION_DISCLOSURE + ' Enable private evaluation? Reply yes for the recommended €5 cap, no-cap for NO Tingle cap, or no for OFF; default suggestion: no (OFF).',
        'exclusions': 'Which source/work exclusions? Reply with one JSON object containing all four string lists: ' + json.dumps({k: [] for k in EXCLUSIONS}) + '. Empty lists exclude nothing.',
    }
    return hints[field] + '\nReply `/tingle setup ' + field + ' <value>`. Cancel with `/tingle setup cancel`. Draft expires 30 minutes after start.'


def _preview(state):
    return ('Review every value (nothing applied yet):\n' + json.dumps(state['policy'], indent=2, ensure_ascii=False)
            + '\nSource choice: ' + state['sources']
            + '\nPrivate evaluation consent: ' + ('enabled' if 'private_chat_evaluation' in state['policy'] else 'disabled')
            + '\n' + EVALUATION_DISCLOSURE
            + '\nTracker data stays on this host. Approved source content and tracked details may go to your configured '
              'model provider, Hermes history/logs and chosen messaging gateway. No Tingle-operated telemetry service is added. '
              'Revocation does not erase prior copies. See README Privacy and data. '
              'WRITE saves global profile policy, the selected evaluation consent, and consents to the selected source connection checks/grants; '
              'helpers and jobs require separate Create both jobs consent. No test message is sent.\n'
              'Retype the exact destination with `/tingle setup confirm-destination <value>`. '
              'Cancel with `/tingle setup cancel`.')


def _apply(state, field, value):
    policy = state['policy']
    keys = {'destination': 'delivery', 'timezone': 'timezone', 'review': 'daily_schedule',
            'deadline': 'deadline_poll_minutes', 'date-only': 'date_only_time'}
    if field == 'destination' and str(value).strip().casefold() == 'here':
        value = ':'.join((state['binding'][0], state['binding'][2]))
    if field in keys:
        policy[keys[field]] = int(value) if field == 'deadline' else value
    elif field == 'availability':
        policy['availability_start'], policy['availability_end'] = value.split()
    elif field == 'sources':
        if value not in ('all', 'none'):
            raise ValueError('Unsupported source choice')
        state['sources'] = value
    elif field == 'evaluation':
        if value not in ('yes', 'no', 'no-cap'):
            raise ValueError('Evaluation consent requires exactly yes, no, or no-cap')
        if value in ('yes', 'no-cap'):
            policy['private_chat_evaluation'] = evaluation_consent(':'.join(state['binding'][:2]), no_cap=value == 'no-cap')
        else:
            policy.pop('private_chat_evaluation', None)
    elif field == 'exclusions':
        exclusions = json.loads(value)
        if not isinstance(exclusions, dict) or set(exclusions) != set(EXCLUSIONS):
            raise ValueError('Explicit exclusion lists required')
        policy.update(exclusions)
    validate_settings(policy)
    if field == 'destination':
        setup_jobs._check_delivery_platform(value)
    if field == 'review':
        parser, _ = setup_jobs._cron_api()
        if parser(value).get('kind') != 'cron':
            raise ValueError('A recurring cron expression is required')
    state['step'] += 1


def handle(ctx, raw, source):
    """Run before ordinary policy gating; never trust owners from invalid policy."""
    import hermes_constants
    home_path = Path(hermes_constants.get_hermes_home()).absolute()
    home = home_path.resolve(strict=True)
    data_dir = home / 'plugin-data' / 'hermes-tingle'
    state_path = data_dir / STATE
    field, _, value = raw.strip().partition(' ')
    value = value.strip()
    state = None
    activation = None
    write_attempted = False
    try:
        binding = _binding(source)
    except ValueError as exc:
        return str(exc)
    try:
        previous = _active_profiles.get(binding)
        if previous is not None and previous != home:
            # Invalidate only this sender's old pending draft, never another profile's policy.
            old_state = resources.safe(previous / 'plugin-data' / 'hermes-tingle' / STATE, previous)
            with resources.mutation(previous):
                old_state.unlink(missing_ok=True)
            _active_profiles.pop(binding, None)
            return 'Setup stopped: active profile changed. Start /tingle setup again; no policy, sources or jobs changed.'
        resources.safe(data_dir, home)
        if field == 'refresh-review' and not value:
            if state_path.exists():
                return 'A setup draft is active; finish or cancel it before refreshing the review prompt. Nothing changed.'
            if snapshot(home) is None:
                return 'No valid Tingle profile setup is available for this refresh. Nothing changed.'
            try:
                load_settings(home)
            except ValueError:
                return PROVISIONING
            return setup_jobs.refresh_review_prompt_from_command(ctx, source)
        # A draft must be checked before valid-policy activation: a policy appearing
        # mid-flow is a stale flow, not permission to skip to job creation.
        if state_path.exists():
            state = json.loads(_read(state_path))
            if tuple(state['binding']) != binding:
                return 'Setup refused: draft belongs to a different owner or chat. Nothing changed.'
            if field == 'cancel' and not value:
                with resources.mutation(home):
                    state_path.unlink()
                _active_profiles.pop(binding, None)
                return 'Setup cancelled; draft discarded. No policy, sources or jobs changed.'
        elif snapshot(home) is not None:
            try:
                load_settings(home)
            except ValueError:
                return PROVISIONING
            if field:
                return 'No setup draft in this profile. Existing valid policy was not edited; /tingle setup previews job activation.'
            return setup_jobs.setup_from_command(ctx, source=source)
        consumed = resources.safe(data_dir / 'setup-consumed.json', home)
        if consumed.exists():
            return PROVISIONING
        binding, owners, authority, owner_identity = _authority(home, data_dir, source)
        with resources.mutation(home):
            binding, owners, authority, owner_identity = _authority(home, data_dir, source)
            # Every reply is serialized with policy writes/removal and other replies.
            # Re-read after acquiring the fence; never write an older draft over a reply.
            state = json.loads(_read(state_path)) if state_path.exists() else None
            # Exact-argument shortcut only: `/tingle setup here` on the first field.
            # Any extra argument stays an unsupported reply, never a silent accept.
            if state is not None and field == 'here' and not value and state['step'] == 0:
                field, value = 'destination', 'here'
            if state is None:
                if field:
                    return 'No setup draft in this profile. Start /tingle setup in the owner DM; nothing changed.'
                if snapshot(home) is not None:
                    raise ValueError('Profile policy changed')
                if consumed.exists():
                    return PROVISIONING
                # The operator explicitly attests a fresh install; admission may
                # already have initialized chat metadata in the plugin database.
                candidate = copy.deepcopy(DEFAULTS)
                candidate.update(owner_principals=owners, delivery='telegram:0')
                state = {'home': str(home), 'home_identity': _identity(home), 'binding': binding,
                         'authority': authority, 'owner_identity': owner_identity, 'expires': time.time() + TTL,
                         'epoch': resources._mutation_epochs.get(home, 0),
                         'step': 0, 'policy': candidate, 'sources': 'none'}
                _active_profiles[binding] = home
            else:
                if (state['home'] != str(home) or state['home_identity'] != _identity(home)
                        or tuple(state['binding']) != binding or state['authority'] != authority
                        or state['owner_identity'] != owner_identity or snapshot(home) is not None
                        or state['epoch'] != resources._mutation_epochs.get(home, 0)
                        or home_path.resolve(strict=True) != home or time.time() >= state['expires']):
                    raise ValueError('Profile, authority or policy changed, or draft expired')
                if not field:
                    state_path.unlink()
                    return 'Setup stopped: unsupported reply; draft discarded. Start /tingle setup again. Nothing applied.'
                if state['step'] < len(FIELDS):
                    if field != FIELDS[state['step']] or not value:
                        raise ValueError('Unsupported or out-of-order reply')
                    _apply(state, field, value)
                elif state['step'] == len(FIELDS):
                    if field != 'confirm-destination' or value != state['policy']['delivery']:
                        raise ValueError('Destination retype did not match exactly')
                    state['step'] += 1
                else:
                    if field != 'confirm' or value != 'WRITE':
                        raise ValueError('Final confirmation must be exactly WRITE')
                    validate_settings(state['policy'])
                    # Check again at the actual commit boundary, not just at dispatch.
                    if (_authority(home, data_dir, source)[2:] != (authority, owner_identity)
                            or Path(hermes_constants.get_hermes_home()).absolute() != home_path
                            or home_path.resolve(strict=True) != home):
                        raise ValueError('Profile or authority changed')
                    if consumed.exists():
                        raise ValueError('Setup bootstrap already consumed')
                    write_attempted = True
                    # Consume the one-shot fresh-install authority *before* the
                    # write. A crash requires operator inspection, not defaults.
                    _save(home, consumed, {'authority': authority})
                    write_policy(home, state['policy'], None)
                    state_path.unlink()
                    _active_profiles.pop(binding, None)
                    activation = state
            if activation is None:
                _save(home, state_path, state)
                if state['step'] < len(FIELDS):
                    return _prompt(state)
                if state['step'] == len(FIELDS):
                    return _preview(state)
                return 'Destination matched. Write this global profile policy? Reply `/tingle setup confirm WRITE` (uppercase, exact). Or `/tingle setup cancel`.'
        # Never hold the policy/removal fence while a person is choosing. The
        # existing activation path captures and rechecks its own preview fence.
        saved = activation['policy']

        def authorize():
            if (not _current_authority(home, data_dir, source, binding, owners, authority, owner_identity)
                    or home_path.resolve(strict=True) != home
                    or Path(hermes_constants.get_hermes_home()).absolute() != home_path
                    or load_settings(home) != saved):
                raise ValueError('Profile, authority or policy changed; restart setup')
        result = setup_jobs._activate_jobs(ctx, authorize=authorize, first_run_sources=activation['sources'])
        return 'Validated policy saved. ' + result
    except OwnerRefused as exc:
        if state is None:
            return str(exc)
        with resources.mutation(home):
            resources.safe(state_path, home).unlink(missing_ok=True)
        _active_profiles.pop(binding, None)
        return 'Setup stopped: owner authority changed; draft discarded. Nothing applied.'
    except (ValueError, OSError, KeyError, TypeError, ImportError):
        # Never echo malformed source text, provider diagnostics or private file contents.
        if write_attempted:
            return 'Policy write/activation stopped; policy may already be saved. Inspect the profile with your operator before retrying. No automatic retry was made.'
        _active_profiles.pop(binding, None)
        if state is not None:
            try:
                with resources.mutation(home):
                    resources.safe(state_path, home).unlink(missing_ok=True)
            except (ValueError, OSError):
                return 'Setup stopped; draft cleanup unavailable during maintenance/path change. Ask the operator before restarting setup. Nothing applied.'
            return 'Setup stopped: unsupported reply, destination/WRITE mismatch, expired draft, or profile/authority/policy changed. Draft discarded; nothing applied. Start /tingle setup again.'
        return PROVISIONING
