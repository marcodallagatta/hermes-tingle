"""Policy-only owner-DM controls; never resolve a route, scan sources or call a model."""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

try:
    from . import hermes_tingle_policy as policy
    from . import hermes_tingle_resources as resources
    from .hermes_tingle_config import private_chat_evaluation_policy, validate_settings
except ImportError:
    import hermes_tingle_policy as policy
    import hermes_tingle_resources as resources
    from hermes_tingle_config import private_chat_evaluation_policy, validate_settings


USAGE = 'Usage: `/tingle evaluation status|enable|disable` (configured owner, verified human DM only).'


def _binding(source):
    platform = getattr(source, 'platform', None)
    platform = str(getattr(platform, 'value', platform) or '').casefold()
    user = str(getattr(source, 'user_id', '') or '').casefold()
    chat = str(getattr(source, 'chat_id', '') or '')
    if (source is None or getattr(source, 'is_bot', False)
            or str(getattr(source, 'chat_type', '')).casefold() not in ('dm', 'private')
            or not chat.strip() or not re.fullmatch(r'[a-z0-9_]+', platform)
            or not re.fullmatch(r'[^\s:]+', user)):
        raise ValueError('Unverified direct human message')
    return platform, user, chat, str(getattr(source, 'thread_id', '') or '')


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate policy key')
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError('Non-JSON policy constant')


def _authorized_snapshot(home, binding):
    resources.safe(home / policy.NAME, home)
    raw = policy.snapshot(home)
    if raw is None:
        raise ValueError('Missing policy')
    settings = validate_settings(json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_invalid_constant))
    principal = ':'.join(binding[:2])
    if principal not in {v.strip().casefold() for v in settings.get('owner_principals', [])}:
        raise ValueError('Not a configured owner')
    # Do not show or repair a malformed consent as if it were a valid disabled policy.
    if 'private_chat_evaluation' in settings and private_chat_evaluation_policy(settings) is None:
        raise ValueError('Malformed evaluation policy')
    return raw, settings


def handle(ctx, source, args):
    # This gate precedes all profile policy reads, including generic command observers.
    try:
        binding = _binding(source)
    except ValueError:
        return 'Evaluation requires a configured owner in a positively verified human direct message; nothing changed.'
    if len(args) != 1 or args[0] not in ('status', 'enable', 'disable'):
        return USAGE
    write_attempted = False
    try:
        import hermes_constants
        home_path = Path(hermes_constants.get_hermes_home()).absolute()
        home = home_path.resolve(strict=True)
        identity = (home.stat().st_dev, home.stat().st_ino)
        resources.active(home)
        expected, settings = _authorized_snapshot(home, binding)
        epoch = resources._mutation_epochs.get(home, 0)

        def check_identity():
            if (_binding(source) != binding
                    or Path(hermes_constants.get_hermes_home()).absolute() != home_path
                    or home_path.resolve(strict=True) != home
                    or (home.stat().st_dev, home.stat().st_ino) != identity
                    or resources._mutation_epochs.get(home, 0) != epoch):
                raise ValueError('Profile or sender changed')
            resources.active(home)

        def revalidate():
            check_identity()
            if _authorized_snapshot(home, binding)[0] != expected:
                raise ValueError('Policy changed')
            check_identity()

        revalidate()  # Authenticate the exact snapshot before any policy disclosure.
        action = args[0]
        choice = None
        if action == 'status':
            return 'Private-chat filter: ' + ('on.' if private_chat_evaluation_policy(settings) else 'off.')
        if action == 'enable':
            chooser = getattr(ctx, 'ask_choice', None)
            if not callable(chooser):
                return 'Private-chat filter confirmation unavailable; nothing changed.'
            try:
                choice = chooser(policy.EVALUATION_DISCLOSURE + '\nEnable the filter for private-chat suggestions?',
                                 [{'value': 'enable', 'label': 'Enable — €5/month cap'},
                                  {'value': 'no-cap', 'label': 'Enable — no monthly spending cap'},
                                  {'value': 'cancel', 'label': 'Cancel'}])
            except Exception:
                return 'Private-chat filter confirmation unavailable; nothing changed.'
            if choice not in ('enable', 'no-cap'):
                return 'Private-chat filter not enabled; nothing changed.'
        updated = copy.deepcopy(settings)
        if action == 'enable':
            value = policy.evaluation_consent(':'.join(binding[:2]), no_cap=choice == 'no-cap')
            # Re-consenting must not silently raise an owner's existing lower limit.
            previous = private_chat_evaluation_policy(settings)
            if previous and previous['monthly_limit_microeur'] > 0 and choice == 'enable':
                value['monthly_limit_microeur'] = min(value['monthly_limit_microeur'], previous['monthly_limit_microeur'])
            updated['private_chat_evaluation'] = value
        else:
            updated.pop('private_chat_evaluation', None)
        with resources.mutation(home):
            revalidate()
            write_attempted = True
            # The shared writer validates, compares exact bytes, atomically writes,
            # preserves a private backup, and verifies readback before returning.
            policy.write_policy(home, updated, expected)
        if action == 'enable':
            return 'Private-chat filter enabled. No scan or model request was made. Sources, jobs and other settings are unchanged.'
        return ('Private-chat filter disabled. An already-sent excerpt may remain in provider or Hermes history. '
                'No new requests will start. Sources, jobs and other settings are unchanged.')
    except (ValueError, OSError, KeyError, TypeError, ImportError):
        if write_attempted:
            return ('Private-chat filter update stopped; policy may already be saved. '
                    'Ask your operator to inspect this profile before retrying; no automatic retry was made.')
        return 'Private-chat filter change stopped: valid policy, active profile and configured owner are required; nothing changed.'
