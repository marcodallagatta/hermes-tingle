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
    'Tingle needs reviewed profile policy and a pre-provisioned owner. An installer/operator must '
    'provision the owner principal for this profile once; chat cannot enroll an owner. '
    'See docs/onboarding.md#operator-provisioning (and its local repair section for existing invalid policy). '
    'Then the owner can use /tingle setup in a verified DM. Nothing was changed.'
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
    raw = _read(resources.safe(data_dir / OWNER, home))
    data = json.loads(raw)
    owners = data.get('owner_principals') if isinstance(data, dict) else None
    if (not isinstance(data, dict) or data.get('fresh_profile') is not True
            or not isinstance(owners, list) or not owners or any(
            not isinstance(v, str) or not re.fullmatch(r'[a-z0-9_]+:[^\s:]+', v, re.I) for v in owners)):
        raise ValueError(PROVISIONING)
    if ':'.join(binding[:2]) not in {v.casefold() for v in owners}:
        raise OwnerRefused('Setup refused: this sender is not a configured owner principal. Nothing changed.')
    return binding, owners, hashlib.sha256(raw).hexdigest(), _identity(data_dir / OWNER)


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
        'destination': 'Where should reminders go? Use an explicit gateway destination, e.g. telegram:<chat-id>[:<topic-id>]. /tingle identity displays IDs; sender ID is not always the destination.',
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
            if (_binding(source) != binding or home_path.resolve(strict=True) != home
                    or Path(hermes_constants.get_hermes_home()).absolute() != home_path
                    or _authority(home, data_dir, source)[2:] != (authority, owner_identity)
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
