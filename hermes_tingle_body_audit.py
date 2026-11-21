"""Minimal shared body-read audit, separate from either source pipeline's state.

All callers hold the profile's existing sources lock. IDs/timestamps only, never
content. Legacy standing records are consulted, never rewritten by look-back.
"""
from __future__ import annotations
import sqlite3
import hashlib
from datetime import timedelta
try:
    from . import hermes_tingle_store as store
except ImportError:
    import hermes_tingle_store as store


def connect():
    conn = store.open_database('hermes-tingle-body-audit')
    conn.row_factory = sqlite3.Row
    conn.execute('CREATE TABLE IF NOT EXISTS reads (account_key TEXT NOT NULL,message_id TEXT NOT NULL,read_at TEXT NOT NULL,origin TEXT NOT NULL,PRIMARY KEY(account_key,message_id))')
    return conn


def recent(message_id, account_key, *, now=None):
    message_id = hashlib.sha256(str(message_id).encode()).hexdigest()
    stamp = store._now(now)
    cutoff = (stamp-timedelta(days=store.BODY_READ_DEDUPE_DAYS)).isoformat(timespec='seconds')
    conn = connect()
    try:
        return conn.execute('SELECT 1 FROM reads WHERE account_key=? AND message_id=? AND read_at>=?',(account_key,message_id,cutoff)).fetchone() is not None
    finally:
        conn.close()


def claim(message_id, account_key, *, now=None, origin='lookback'):
    stamp = store._now(now)
    cutoff = (stamp-timedelta(days=store.BODY_READ_DEDUPE_DAYS)).isoformat(timespec='seconds')
    legacy = store._connect()
    try:
        if origin != 'standing' and legacy.execute('SELECT 1 FROM email_body_reads WHERE source_id=? AND read_at>=?',(message_id,cutoff)).fetchone():
            return False
    finally:
        legacy.close()
    message_id = hashlib.sha256(str(message_id).encode()).hexdigest()
    conn = connect()
    try:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('DELETE FROM reads WHERE read_at<?',(cutoff,))
        if conn.execute('SELECT 1 FROM reads WHERE account_key=? AND message_id=? AND read_at>=?',(account_key,message_id,cutoff)).fetchone():
            conn.rollback();return False
        conn.execute('INSERT OR REPLACE INTO reads VALUES(?,?,?,?)',(account_key,message_id,stamp.isoformat(timespec='seconds'),origin))
        conn.commit();return True
    finally:
        conn.close()


def clear(*, origin=None):
    """Revocation clears its audit scope; never touches email content or items."""
    conn = connect()
    try:
        if origin is None:
            conn.execute('DELETE FROM reads')
        else:
            conn.execute('DELETE FROM reads WHERE origin=?',(origin,))
        conn.commit()
    finally:
        conn.close()
