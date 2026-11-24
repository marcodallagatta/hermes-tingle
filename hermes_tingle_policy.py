"""Shared policy defaults and checked atomic writer (no account or job access)."""
from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path

try:
    from .hermes_tingle_config import load_settings, validate_settings
    from . import hermes_tingle_resources as resources
except ImportError:
    from hermes_tingle_config import load_settings, validate_settings
    import hermes_tingle_resources as resources

NAME = 'hermes-tingle.json'
EVALUATION_DISCLOSURE = (
    'Private chat filtering is OFF unless you enable it. If enabled, Tingle sends a short, bounded excerpt '
    'from your private chats to this profile’s default AI model in a separate request with no tools; your '
    'provider may see that excerpt. Tingle sends it only when Hermes confirms that the route and its pricing '
    'are supported. Otherwise, it keeps using local text checks and sends no filter request. '
    'For supported routes, Tingle adds a 1.5 EUR/USD safety margin to its cost estimate; that estimate is '
    'not your provider bill. Choose a €5 monthly spending cap (UTC month; or the lower limit already saved), '
    'or choose no monthly Tingle spending cap. With no cap, provider charges can exceed €5. Usage figures are estimates. '
    'Request and token sizes are limited. If the filter fails, Tingle does not retry or switch providers and '
    'saves no suggestion. This does not grant chat access: if chat is off, no excerpt is sent. Turning the '
    'filter off cannot recall an excerpt already sent or remove copies from provider or Hermes history.'
)


def evaluation_consent(principal: str, *, no_cap=False) -> dict:
    """Exact runtime-default consent, only after the caller authenticates the owner."""
    return {'enabled': True, 'consent': 'owner-dm-excerpts-v2' if no_cap else 'owner-dm-excerpts-v1',
            'approved_by': principal, 'route': 'runtime-default',
            'monthly_limit_microeur': 0 if no_cap else 5_000_000}


DEFAULTS = {
    'timezone': 'Etc/UTC', 'daily_schedule': '0 0,8,16 * * *', 'deadline_poll_minutes': 30,
    'date_only_time': '09:00', 'availability_start': '09:00', 'availability_end': '18:00',
    'calendar_exclude_substrings': [], 'workstream_exclude_values': [],
    'source_exclude_substrings': [], 'ticket_exclude_patterns': [],
    'owner_principals': [],
}


def snapshot(home: Path) -> bytes | None:
    path = home / NAME
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, 'rb') as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError('Configuration must be a regular file, not a link/device')
        return handle.read()


def write_policy(home: Path, data: dict, expected: bytes | None) -> Path | None:
    with resources.mutation(home):
        return _write_policy(home, data, expected)


def _write_policy(home: Path, data: dict, expected: bytes | None) -> Path | None:
    """Validate; no-clobber creation or checked atomic repair with private backup.

    Cooperative locks and exact-byte checks detect concurrent edits, not a
    hostile process with the same OS access. Used by operator and owner setup.
    """
    validate_settings(data)
    home = Path(home).resolve(strict=True)
    resources.active(home)
    if not home.is_dir():
        raise ValueError('Profile home must be an existing directory')
    import fcntl
    lock_existed = (home / '.tingle-init.lock').exists()
    lock = os.open(home / '.tingle-init.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    temp = None
    try:
        if not stat.S_ISREG(os.fstat(lock).st_mode):
            raise ValueError('Wizard lock must be a regular file')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if snapshot(home) != expected:
            raise ValueError('Profile configuration changed; restart and review it again')
        fd, temp = tempfile.mkstemp(prefix='.tingle-init-', dir=home)
        with os.fdopen(fd, 'wb') as handle:
            handle.write((json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode())
            handle.flush()
            os.fsync(handle.fileno())
        backup = None
        if expected is None:
            os.link(temp, home / NAME)
        else:
            bfd, backup_name = tempfile.mkstemp(prefix='.tingle-policy-backup-', dir=home)
            backup = Path(backup_name)
            with os.fdopen(bfd, 'wb') as handle:
                handle.write(expected)
                handle.flush()
                os.fsync(handle.fileno())
            if snapshot(home) != expected:
                raise ValueError('Profile configuration changed; restart and review it again')
            os.replace(temp, home / NAME)
        directory = os.open(home, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        if load_settings(home) != data:
            raise ValueError('Post-write validation mismatch; inspect the profile before continuing')
        resources.record(home / NAME, home=home)
        if backup is not None:
            resources.record(backup, home=home)
        if not lock_existed:
            resources.record(home / '.tingle-init.lock', home=home)
        return backup
    finally:
        if temp is not None:
            Path(temp).unlink(missing_ok=True)
        os.close(lock)
