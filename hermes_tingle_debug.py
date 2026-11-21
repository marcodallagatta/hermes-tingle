"""Opt-in local diagnostics. Closed schemas only; never source content or model input.

Records are hidden after seven days and physically pruned on the next record,
mode change or explicit purge. No background cleanup job is created. A local
operator has the same access as for the rest of this profile's private state.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

try:
    from . import hermes_tingle_store as store
except ImportError:
    import hermes_tingle_store as store

TTL_DAYS = 7
MAX_RECORDS = 200
MAX_COUNT = 1000000
KEY = 'private_debug_enabled'
SCHEMAS = {
    'assessment': frozenset(('considered', 'assessed', 'qualified', 'failed_calls', 'over_cap',
                            'excluded', 'plan_faults', 'staged', 'suppressed_routine', 'skipped_draft',
                            'skipped_owner_last', 'capped', 'request_failed', 'request_not_sent',
                            'no_valid_verdict', 'model_not_action', 'pick_unsupported', 'pick_changed',
                            'pick_skipped_by_rule', 'pick_listed', 'accepted')),
    'snapshot': frozenset(('emails', 'calendar_events', 'due_items', 'errors', 'checkpoint_checks',
                          'reply_checks', 'case_items', 'chat_matches')),
}


def _now(value=None):
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise ValueError('diagnostic clock must be timezone-aware')
    return value.astimezone(timezone.utc)


def _connect():
    conn = store._connect()
    conn.execute('CREATE TABLE IF NOT EXISTS private_debug_records '
                 '(seq INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT NOT NULL, '
                 'event TEXT NOT NULL, counts TEXT NOT NULL)')
    return conn


def enabled():
    return store.get_plugin_meta(KEY) == '1'


@store.serialized('private_debug')
def set_enabled(value: bool, *, now=None):
    if type(value) is not bool:
        raise ValueError('debug mode must be on or off')
    stamp = _now(now)
    conn = _connect()
    try:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('INSERT INTO plugin_meta(key,value) VALUES(?,?) '
                     'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (KEY, '1' if value else '0'))
        if value:
            conn.execute('DELETE FROM private_debug_records WHERE created<=?',
                         ((stamp-timedelta(days=TTL_DAYS)).isoformat(),))
        else:
            conn.execute('DELETE FROM private_debug_records')
        conn.commit()
    finally:
        conn.close()


@store.serialized('private_debug')
def purge():
    conn = _connect()
    try:
        conn.execute('DELETE FROM private_debug_records')
        conn.commit()
    finally:
        conn.close()


@store.serialized('private_debug')
def record(event, counts, *, now=None):
    """Fail closed on unknown keys/text. Callers supply counts, never arbitrary payloads."""
    if not enabled():
        return
    if event not in SCHEMAS or not isinstance(counts, dict) or set(counts)-SCHEMAS[event]:
        raise ValueError('unknown diagnostic schema')
    if any(type(v) is not int or not 0 <= v <= MAX_COUNT for v in counts.values()):
        raise ValueError('diagnostics accept bounded nonnegative integer counts only')
    stamp = _now(now)
    conn = _connect()
    try:
        conn.execute('BEGIN IMMEDIATE')
        # Recheck inside the serialized write; off/purge cannot race a late record.
        row = conn.execute('SELECT value FROM plugin_meta WHERE key=?', (KEY,)).fetchone()
        if not row or row[0] != '1':
            conn.rollback()
            return
        conn.execute('DELETE FROM private_debug_records WHERE created<=?',
                     ((stamp-timedelta(days=TTL_DAYS)).isoformat(),))
        conn.execute('INSERT INTO private_debug_records(created,event,counts) VALUES(?,?,?)',
                     (stamp.isoformat(), event, json.dumps(counts, sort_keys=True)))
        conn.execute('DELETE FROM private_debug_records WHERE seq NOT IN '
                     '(SELECT seq FROM private_debug_records ORDER BY seq DESC LIMIT ?)', (MAX_RECORDS,))
        conn.commit()
    finally:
        conn.close()


def safe_record(event, counts):
    """Observability must never break tracking. Never echo a diagnostic exception."""
    try:
        record(event, counts)
    except Exception:
        return


def read(*, limit=20, now=None):
    """Fresh-process local read API; no provider/model calls, no retention mutations.

    Existing private profile state is the operator authority boundary. Chat access
    is separately guarded by the authenticated owner-DM command.
    """
    if type(limit) is not int or not 1 <= limit <= MAX_RECORDS:
        raise ValueError('diagnostic limit must be 1 through 200')
    stamp = _now(now)
    # Do not create a table or prune records on this read path.
    conn = store._connect()
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='private_debug_records'").fetchone():
            return []
        rows = conn.execute('SELECT created,event,counts FROM private_debug_records '
                            'WHERE created>? ORDER BY seq DESC LIMIT ?',
                            ((stamp-timedelta(days=TTL_DAYS)).isoformat(), limit)).fetchall()
        result = []
        for row in rows:
            event = row['event']
            counts = json.loads(row['counts'])
            if event not in SCHEMAS or not isinstance(counts, dict) or set(counts)-SCHEMAS[event]:
                continue
            if any(type(v) is not int or not 0 <= v <= MAX_COUNT for v in counts.values()):
                continue
            result.append({'created': row['created'], 'event': event, 'counts': counts,
                           'meaning': 'observed counters, not proof of model judgment or source coverage'})
        return result
    finally:
        conn.close()
