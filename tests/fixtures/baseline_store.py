"""Profile-scoped durable store for the Hermes Tingle plugin."""
from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
from datetime import date, datetime, time, timedelta, timezone

from typing import Any, Iterable

from plugins.plugin_storage import plugin_db

try:
    from .hermes_tingle_config import load_settings, local_timezone
except ImportError:  # pragma: no cover
    from hermes_tingle_config import load_settings, local_timezone

PLUGIN_ID = "hermes-tingle"

MAX_EMAIL_CANDIDATES = 100
BODY_READ_DEDUPE_DAYS = 14
_STATUSES = {"open", "waiting", "blocked", "done", "dismissed"}
_ACTIVE_STATUSES = ("open", "waiting", "blocked")
_SOURCE_TYPES = {"conversation", "email", "calendar", "manual"}
_PRIORITIES = {"low", "normal", "high"}
_COMPLETION_SIGNALS = {"", "source_email_trashed", "source_email_reply_received"}
_FEEDBACK_CATEGORIES = {"general", "email", "calendar", "links", "triage", "formatting", "reminders", "other"}
_FEEDBACK_STATUSES = {"pending", "approved", "dismissed"}
_FEEDBACK_ID_RE = re.compile(r"^FB-[A-F0-9]{6}$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _connect() -> sqlite3.Connection:
    conn = plugin_db(PLUGIN_ID)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    ensure_schema(conn)
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    existing_tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "open_loops" in existing_tables:
        if "hermes_tingle_items" in existing_tables:
            raise RuntimeError("both legacy and Hermes Tingle item tables exist; refusing to merge automatically")
        conn.execute("ALTER TABLE open_loops RENAME TO hermes_tingle_items")
        conn.execute("DROP INDEX IF EXISTS idx_open_loops_source_ref")
        conn.execute("DROP INDEX IF EXISTS idx_open_loops_due")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS hermes_tingle_items (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'open',
            owner TEXT NOT NULL DEFAULT 'me',
            priority TEXT NOT NULL DEFAULT 'normal',
            source_type TEXT NOT NULL DEFAULT 'conversation',
            source_ref TEXT NOT NULL DEFAULT '',
            source_url TEXT NOT NULL DEFAULT '',
            completion_signal TEXT NOT NULL DEFAULT '',
            reply_interval_seconds INTEGER,
            due_at TEXT,
            next_check_at TEXT,
            snoozed_until TEXT,
            reminder_count INTEGER NOT NULL DEFAULT 0,
            last_notified_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_hermes_tingle_items_source_ref
            ON hermes_tingle_items(source_type, source_ref) WHERE source_ref <> '';
        CREATE INDEX IF NOT EXISTS idx_hermes_tingle_items_due
            ON hermes_tingle_items(status, next_check_at);
        CREATE TABLE IF NOT EXISTS source_seen (
            source_type TEXT NOT NULL,
            source_id TEXT NOT NULL,
            source_version TEXT NOT NULL DEFAULT '',
            reviewed_at TEXT NOT NULL,
            PRIMARY KEY (source_type, source_id, source_version)
        );
        CREATE TABLE IF NOT EXISTS email_candidates (
            source_id TEXT PRIMARY KEY,
            registered_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS email_body_reads (
            source_id TEXT PRIMARY KEY,
            read_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS plugin_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS gmail_pending_messages (
            message_id TEXT PRIMARY KEY,
            queued_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS feedback_candidates (
            id TEXT PRIMARY KEY,
            rule_text TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'general',
            status TEXT NOT NULL DEFAULT 'pending',
            fingerprint TEXT NOT NULL UNIQUE,
            occurrences INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            approved_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_feedback_candidates_status
            ON feedback_candidates(status, created_at);
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(hermes_tingle_items)")}
    if "completion_signal" not in columns:
        conn.execute("ALTER TABLE hermes_tingle_items ADD COLUMN completion_signal TEXT NOT NULL DEFAULT ''")
    if "reply_interval_seconds" not in columns:
        conn.execute("ALTER TABLE hermes_tingle_items ADD COLUMN reply_interval_seconds INTEGER")
    conn.commit()


def _parse_time(value: Any, *, field: str = "timestamp") -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime.combine(value, time.fromisoformat(load_settings()["date_only_time"]))
    elif isinstance(value, str):
        raw = value.strip()
        if _ISO_DATE.fullmatch(raw):
            dt = datetime.combine(date.fromisoformat(raw), time.fromisoformat(load_settings()["date_only_time"]))
        else:
            try:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(f"{field} must be ISO-8601 or YYYY-MM-DD") from exc
    else:
        raise ValueError(f"{field} must be ISO-8601 or YYYY-MM-DD")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=local_timezone())
        if dt.utcoffset() != dt.replace(fold=1).utcoffset():
            raise ValueError(f"{field} is ambiguous or nonexistent; provide an explicit offset")
    return dt.astimezone(timezone.utc)


def _stamp(value: Any, *, field: str = "timestamp") -> str | None:
    dt = _parse_time(value, field=field)
    return dt.isoformat(timespec="seconds") if dt else None


def _now(value: Any = None) -> datetime:
    return _parse_time(value or datetime.now(timezone.utc), field="now")  # type: ignore[return-value]


def _public(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    result["deduplicated"] = False
    return result


def _next_check(
    *, now: datetime, due_at: datetime | None, owner: str, priority: str, status: str,
) -> datetime:
    if due_at is not None:
        remaining = due_at - now
        if remaining > timedelta(days=1):
            return due_at - timedelta(days=1)
        if remaining > timedelta(hours=1):
            return due_at - timedelta(hours=1)
        return max(now + timedelta(minutes=15), due_at)
    if status == "waiting" or owner == "other":
        return now + timedelta(days=3)
    if priority == "high":
        return now + timedelta(days=1)
    if priority == "low":
        return now + timedelta(days=14)
    return now + timedelta(days=7)


def add_item(
    *,
    title: str,
    summary: str = "",
    status: str = "open",
    owner: str = "me",
    priority: str = "normal",
    source_type: str = "conversation",
    source_ref: str = "",
    source_url: str = "",
    completion_signal: str = "",
    due_at: Any = None,
    next_check_at: Any = None,
    now: Any = None,
) -> dict[str, Any]:
    title = str(title or "").strip()
    if not title:
        raise ValueError("title is required")
    if len(title) > 180:
        raise ValueError("title must be at most 180 characters")
    summary = str(summary or "").strip()
    if len(summary) > 1200:
        raise ValueError("summary must be at most 1200 characters")
    if status not in _STATUSES - {"done", "dismissed"}:
        raise ValueError("new items must have status open, waiting, or blocked")
    if owner not in {"me", "other", "joint", "unknown"}:
        raise ValueError("owner must be me, other, joint, or unknown")
    if priority not in _PRIORITIES:
        raise ValueError("priority must be low, normal, or high")
    if source_type not in _SOURCE_TYPES:
        raise ValueError("unsupported source_type")
    if completion_signal not in _COMPLETION_SIGNALS:
        raise ValueError("unsupported completion_signal")
    source_ref = str(source_ref or "").strip()[:256]
    if completion_signal == "source_email_reply_received":
        if source_type != "email" or status != "waiting" or owner != "other":
            raise ValueError("reply-watch items must be waiting email items owned by another person")
        if not source_ref.startswith("gmail:") or "#" not in source_ref[6:] or not source_ref.rsplit("#", 1)[-1]:
            raise ValueError("reply-watch source_ref must include a Gmail thread and sent-message anchor")
        if next_check_at in (None, ""):
            raise ValueError("reply-watch items require a context-specific next_check_at")
    source_url = str(source_url or "").strip()[:2048]
    now_dt = _now(now)
    now_s = now_dt.isoformat(timespec="seconds")
    due_dt = _parse_time(due_at, field="due_at")
    check_dt = _parse_time(next_check_at, field="next_check_at")
    if check_dt is None:
        check_dt = _next_check(now=now_dt, due_at=due_dt, owner=owner, priority=priority, status=status)
    reply_interval_seconds = None
    if completion_signal == "source_email_reply_received":
        reply_interval_seconds = max(2 * 24 * 60 * 60, int((check_dt - now_dt).total_seconds()))

    conn = _connect()
    try:
        if source_ref:
            existing = conn.execute(
                "SELECT * FROM hermes_tingle_items WHERE source_type=? AND source_ref=?",
                (source_type, source_ref),
            ).fetchone()
            if existing:
                result = dict(existing)
                result["deduplicated"] = True
                return result
        if completion_signal != "source_email_reply_received":
            norm = " ".join(title.casefold().split())
            candidates = conn.execute(
                "SELECT * FROM hermes_tingle_items WHERE status IN ('open','waiting','blocked') AND owner=?",
                (owner,),
            ).fetchall()
            for row in candidates:
                same_title = " ".join(str(row["title"]).casefold().split()) == norm
                same_due = (row["due_at"] or None) == (due_dt.isoformat(timespec="seconds") if due_dt else None)
                if same_title and same_due:
                    result = dict(row)
                    result["deduplicated"] = True
                    return result

        item_id = "HT-" + secrets.token_hex(3).upper()
        due_s = due_dt.isoformat(timespec="seconds") if due_dt else None
        check_s = check_dt.isoformat(timespec="seconds") if check_dt else None
        conn.execute(
            """INSERT INTO hermes_tingle_items
               (id,title,summary,status,owner,priority,source_type,source_ref,source_url,completion_signal,reply_interval_seconds,
                due_at,next_check_at,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (item_id,title,summary,status,owner,priority,source_type,source_ref,source_url,
             completion_signal,reply_interval_seconds,due_s,check_s,now_s,now_s),
        )
        conn.commit()
        return _public(conn.execute("SELECT * FROM hermes_tingle_items WHERE id=?", (item_id,)).fetchone())  # type: ignore[return-value]
    finally:
        conn.close()


def get_item(item_id: str) -> dict[str, Any] | None:
    conn = _connect()
    try:
        return _public(conn.execute("SELECT * FROM hermes_tingle_items WHERE id=?", (str(item_id).upper(),)).fetchone())
    finally:
        conn.close()


def list_items(*, status: str = "active", limit: int = 20) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 100))
    conn = _connect()
    try:
        if status == "active":
            rows = conn.execute(
                """SELECT * FROM hermes_tingle_items WHERE status IN ('open','waiting','blocked')
                   ORDER BY CASE WHEN due_at IS NULL THEN 1 ELSE 0 END, due_at, next_check_at LIMIT ?""",
                (limit,),
            ).fetchall()
        elif status in _STATUSES:
            rows = conn.execute(
                "SELECT * FROM hermes_tingle_items WHERE status=? ORDER BY updated_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        elif status == "all":
            rows = conn.execute("SELECT * FROM hermes_tingle_items ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        else:
            raise ValueError("status must be active, all, or a known item status")
        return [dict(row) for row in rows]
    finally:
        conn.close()


def due_items(*, now: Any = None, limit: int = 50) -> list[dict[str, Any]]:
    now_s = _stamp(now or datetime.now(timezone.utc), field="now")
    limit = max(1, min(int(limit), 100))
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT * FROM hermes_tingle_items
               WHERE status IN ('open','waiting','blocked')
                 AND next_check_at IS NOT NULL AND next_check_at <= ?
                 AND (snoozed_until IS NULL OR snoozed_until <= ?)
               ORDER BY next_check_at, due_at LIMIT ?""",
            (now_s, now_s, limit),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def update_item(item_id: str, **updates: Any) -> dict[str, Any]:
    allowed = {"title", "summary", "status", "owner", "priority", "due_at", "next_check_at", "source_url", "completion_signal"}
    if not updates or set(updates) - allowed:
        raise ValueError("update requires only supported fields")
    normalized: dict[str, Any] = {}
    for key, value in updates.items():
        if key == "title":
            value = str(value).strip()
            if not value or len(value) > 180:
                raise ValueError("title must be 1-180 characters")
        elif key == "summary":
            value = str(value or "").strip()
            if len(value) > 1200:
                raise ValueError("summary must be at most 1200 characters")
        elif key == "status" and value not in _STATUSES:
            raise ValueError("unknown status")
        elif key == "owner" and value not in {"me", "other", "joint", "unknown"}:
            raise ValueError("unknown owner")
        elif key == "priority" and value not in _PRIORITIES:
            raise ValueError("unknown priority")
        elif key in {"due_at", "next_check_at"}:
            value = _stamp(value, field=key)
        elif key == "source_url":
            value = str(value or "").strip()[:2048]
        elif key == "completion_signal":
            value = str(value or "")
            if value not in _COMPLETION_SIGNALS:
                raise ValueError("unsupported completion_signal")
        normalized[key] = value
    now_dt = _now()
    now_s = now_dt.isoformat(timespec="seconds")
    conn = _connect()
    try:
        current = conn.execute(
            "SELECT source_type,source_ref,completion_signal,status,owner,next_check_at FROM hermes_tingle_items WHERE id=?",
            (str(item_id).upper(),),
        ).fetchone()
        if current is None:
            raise KeyError("Hermes Tingle item not found")
        signal = normalized.get("completion_signal", current["completion_signal"])
        state = normalized.get("status", current["status"])
        owner = normalized.get("owner", current["owner"])
        source_type = current["source_type"]
        source_ref = str(current["source_ref"] or "")
        check_value = normalized.get("next_check_at", current["next_check_at"])
        if signal == "source_email_reply_received":
            if source_type != "email" or not source_ref.startswith("gmail:") or "#" not in source_ref[6:] or not source_ref.rsplit("#", 1)[-1]:
                raise ValueError("reply-watch items require a Gmail thread and sent-message anchor")
            if state in _ACTIVE_STATUSES and (state != "waiting" or owner != "other"):
                raise ValueError("active reply-watch items must remain waiting on another person")
            next_dt = _parse_time(check_value, field="next_check_at")
            if state in _ACTIVE_STATUSES and next_dt is None:
                raise ValueError("active reply-watch items require a context-specific next_check_at")
            if "next_check_at" in normalized or current["completion_signal"] != signal:
                normalized["reply_interval_seconds"] = max(
                    2 * 24 * 60 * 60,
                    int((next_dt - now_dt).total_seconds()) if next_dt else 2 * 24 * 60 * 60,
                )
        fields = ",".join(f"{key}=?" for key in normalized)
        cur = conn.execute(f"UPDATE hermes_tingle_items SET {fields}, updated_at=? WHERE id=?", (*normalized.values(), now_s, str(item_id).upper()))
        if not cur.rowcount:
            raise KeyError("Hermes Tingle item not found")
        if normalized.get("status") == "done":
            conn.execute("UPDATE hermes_tingle_items SET completed_at=? WHERE id=?", (now_s, str(item_id).upper()))
        conn.commit()
        return _public(conn.execute("SELECT * FROM hermes_tingle_items WHERE id=?", (str(item_id).upper(),)).fetchone())  # type: ignore[return-value]
    finally:
        conn.close()


def complete_item(item_id: str, *, note: str = "", now: Any = None) -> dict[str, Any]:
    note = str(note or "").strip()[:1200]
    now_s = _stamp(now or datetime.now(timezone.utc))
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE hermes_tingle_items SET status='done',summary=CASE WHEN ?='' THEN summary ELSE ? END,updated_at=?,completed_at=? WHERE id=? AND status IN ('open','waiting','blocked')",
            (note, note, now_s, now_s, str(item_id).upper()),
        )
        if not cur.rowcount:
            raise KeyError("Hermes Tingle item not found or already closed")
        conn.commit()
        return _public(conn.execute("SELECT * FROM hermes_tingle_items WHERE id=?", (str(item_id).upper(),)).fetchone())  # type: ignore[return-value]
    finally:
        conn.close()


def dismiss_item(item_id: str, *, now: Any = None) -> dict[str, Any]:
    return update_item(item_id, status="dismissed")


def snooze_item(item_id: str, *, until: Any, now: Any = None) -> dict[str, Any]:
    until_dt = _parse_time(until, field="until")
    now_dt = _now(now)
    if until_dt is None or until_dt <= now_dt:
        raise ValueError("snooze time must be in the future")
    now_s = now_dt.isoformat(timespec="seconds")
    until_s = until_dt.isoformat(timespec="seconds")
    conn = _connect()
    try:
        cur = conn.execute(
            """UPDATE hermes_tingle_items SET snoozed_until=?,next_check_at=?,reminder_count=0,
               last_notified_at=NULL,updated_at=?
               WHERE id=? AND status IN ('open','waiting','blocked')""",
            (until_s, until_s, now_s, str(item_id).upper()),
        )
        if not cur.rowcount:
            raise KeyError("Hermes Tingle item not found or already closed")
        conn.commit()
        return _public(conn.execute("SELECT * FROM hermes_tingle_items WHERE id=?", (str(item_id).upper(),)).fetchone())  # type: ignore[return-value]
    finally:
        conn.close()


def mark_notified(item_ids: Iterable[str], *, now: Any = None) -> list[dict[str, Any]]:
    now_dt = _now(now)
    now_s = now_dt.isoformat(timespec="seconds")
    ids = [str(x).upper() for x in item_ids][:100]
    if not ids:
        return []
    conn = _connect()
    results = []
    try:
        for item_id in ids:
            row = conn.execute("SELECT * FROM hermes_tingle_items WHERE id=?", (item_id,)).fetchone()
            if row is None or row["status"] not in _ACTIVE_STATUSES:
                continue
            count = int(row["reminder_count"]) + 1
            due_dt = _parse_time(row["due_at"], field="due_at") if row["due_at"] else None
            if count >= 4:
                next_dt = None  # stop repetitive nudges; the item remains open and queryable
            elif row["completion_signal"] == "source_email_reply_received" and row["reply_interval_seconds"]:
                next_dt = now_dt + timedelta(seconds=int(row["reply_interval_seconds"]))
            elif due_dt and due_dt > now_dt:
                next_dt = due_dt
            else:
                delay_days = 2 if count == 1 else 7 if count == 2 else 14
                next_dt = now_dt + timedelta(days=delay_days)
            next_s = next_dt.isoformat(timespec="seconds") if next_dt else None
            conn.execute(
                "UPDATE hermes_tingle_items SET last_notified_at=?,reminder_count=?,next_check_at=?,updated_at=? WHERE id=?",
                (now_s,count,next_s,now_s,item_id),
            )
        conn.commit()
        for item_id in ids:
            row = conn.execute("SELECT * FROM hermes_tingle_items WHERE id=?", (item_id,)).fetchone()
            if row is not None:
                results.append(dict(row))
        return results
    finally:
        conn.close()


def mark_source_reviewed(source_type: str, source_id: str, source_version: str = "", *, now: Any = None) -> bool:
    if source_type not in {"email", "calendar"}:
        raise ValueError("source_type must be email or calendar")
    source_id = str(source_id or "").strip()[:256]
    source_version = str(source_version or "").strip()[:256]
    if not source_id:
        raise ValueError("source_id is required")
    reviewed_at = _stamp(now or datetime.now(timezone.utc))
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO source_seen(source_type,source_id,source_version,reviewed_at) VALUES(?,?,?,?)",
            (source_type,source_id,source_version,reviewed_at),
        )
        if source_type == "email":
            conn.execute("DELETE FROM gmail_pending_messages WHERE message_id=?", (source_id,))
        conn.commit()
        return bool(cur.rowcount)
    finally:
        conn.close()


def source_reviewed(source_type: str, source_id: str, source_version: str = "") -> bool:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM source_seen WHERE source_type=? AND source_id=? AND source_version=?",
            (source_type,str(source_id),str(source_version)),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def gmail_sync_state() -> dict[str, str | None]:
    """Return minimal Gmail sync cursors; never stores email contents."""
    conn = _connect()
    try:
        rows = {
            str(row["key"]): str(row["value"])
            for row in conn.execute(
                "SELECT key,value FROM plugin_meta WHERE key IN ('gmail_history_id','gmail_last_full_reconcile_at')"
            )
        }
        return {
            "history_id": rows.get("gmail_history_id"),
            "last_full_reconcile_at": rows.get("gmail_last_full_reconcile_at"),
        }
    finally:
        conn.close()


def stage_gmail_sync(
    *, history_id: str | None, message_ids: Iterable[str], full_reconcile_at: Any = None, now: Any = None,
    expected_history_id: str | None = None,
) -> bool:
    """Queue changed message IDs and advance the history cursor in one local transaction."""
    ids = list(dict.fromkeys(str(value).strip()[:256] for value in message_ids if str(value).strip()))
    if len(ids) > 5000:
        raise ValueError("a single Gmail sync cannot stage more than 5000 pending message IDs")
    history_id = str(history_id or "").strip()[:256]
    now_s = _stamp(now or datetime.now(timezone.utc))
    full_s = _stamp(full_reconcile_at, field="full_reconcile_at") if full_reconcile_at else None
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany(
            "INSERT OR IGNORE INTO gmail_pending_messages(message_id,queued_at) VALUES(?,?)",
            [(message_id, now_s) for message_id in ids],
        )
        if expected_history_id is not None:
            row = conn.execute("SELECT value FROM plugin_meta WHERE key='gmail_history_id'").fetchone()
            if (str(row[0]) if row else "") != expected_history_id:
                conn.commit()
                return False
        if history_id:
            conn.execute(
                "INSERT INTO plugin_meta(key,value) VALUES('gmail_history_id',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (history_id,),
            )
        if full_s:
            conn.execute(
                "INSERT INTO plugin_meta(key,value) VALUES('gmail_last_full_reconcile_at',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (full_s,),
            )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def pending_gmail_message_ids(*, limit: int = 100) -> list[str]:
    limit = max(1, min(int(limit), 5000))
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT message_id FROM gmail_pending_messages ORDER BY queued_at,message_id LIMIT ?",
            (limit,),
        ).fetchall()
        return [str(row["message_id"]) for row in rows]
    finally:
        conn.close()


def defer_gmail_pending(message_ids: Iterable[str], *, now: Any = None) -> None:
    """Keep unreadable IDs, but let later queued messages be reviewed first."""
    ids = list(dict.fromkeys(str(value).strip()[:256] for value in message_ids if str(value).strip()))
    if not ids:
        return
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT MAX(queued_at) FROM gmail_pending_messages").fetchone()
        last = datetime.fromisoformat(row[0]) if row and row[0] else _now(now)
        retry_at = (max(_now(now), last) + timedelta(seconds=1)).isoformat(timespec="seconds")
        conn.executemany("UPDATE gmail_pending_messages SET queued_at=? WHERE message_id=?",
                         [(retry_at, value) for value in ids])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def clear_gmail_pending(message_ids: Iterable[str]) -> None:
    ids = list(dict.fromkeys(str(value).strip()[:256] for value in message_ids if str(value).strip()))
    if not ids:
        return
    conn = _connect()
    try:
        conn.executemany("DELETE FROM gmail_pending_messages WHERE message_id=?", [(value,) for value in ids])
        conn.commit()
    finally:
        conn.close()


def register_email_candidates(source_ids: Iterable[str], *, now: Any = None) -> None:
    now_dt = _now(now)
    now_s = now_dt.isoformat(timespec="seconds")
    candidate_cutoff = (now_dt - timedelta(hours=24)).isoformat(timespec="seconds")
    body_read_cutoff = (now_dt - timedelta(days=BODY_READ_DEDUPE_DAYS)).isoformat(timespec="seconds")
    ids = list(dict.fromkeys(str(source_id).strip()[:256] for source_id in source_ids if str(source_id).strip()))[:MAX_EMAIL_CANDIDATES]
    conn = _connect()
    try:
        conn.execute("DELETE FROM email_candidates WHERE registered_at < ?", (candidate_cutoff,))
        conn.execute("DELETE FROM email_body_reads WHERE read_at < ?", (body_read_cutoff,))
        conn.executemany(
            "INSERT OR REPLACE INTO email_candidates(source_id,registered_at) VALUES(?,?)",
            [(source_id, now_s) for source_id in ids],
        )
        conn.commit()
    finally:
        conn.close()


def claim_email_body_read(source_id: str, *, now: Any = None) -> bool:
    """Allow one body read per recent scanned message ID during the 14-day source window."""
    source_id = str(source_id or "").strip()[:256]
    if not source_id:
        return False
    now_dt = _now(now)
    now_s = now_dt.isoformat(timespec="seconds")
    candidate_cutoff = (now_dt - timedelta(hours=24)).isoformat(timespec="seconds")
    body_read_cutoff = (now_dt - timedelta(days=BODY_READ_DEDUPE_DAYS)).isoformat(timespec="seconds")
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        candidate = conn.execute(
            "SELECT 1 FROM email_candidates WHERE source_id=? AND registered_at >= ?",
            (source_id, candidate_cutoff),
        ).fetchone()
        if candidate is None:
            conn.commit()
            return False
        existing_read = conn.execute(
            "SELECT 1 FROM email_body_reads WHERE source_id=? AND read_at >= ?",
            (source_id, body_read_cutoff),
        ).fetchone()
        if existing_read:
            conn.commit()
            return False
        conn.execute("INSERT OR REPLACE INTO email_body_reads(source_id,read_at) VALUES(?,?)", (source_id, now_s))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _feedback_public(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in (
        "id", "rule_text", "category", "status", "occurrences", "created_at", "updated_at", "approved_at",
    )}


def capture_feedback_candidate(
    rule_text: str, *, category: str = "general", now: Any = None,
) -> dict[str, Any]:
    rule_text = " ".join(str(rule_text or "").split())
    if not rule_text:
        raise ValueError("feedback_text is required")
    if len(rule_text) > 500:
        raise ValueError("feedback_text must be at most 500 characters")
    category = str(category or "general").strip().casefold()
    if category not in _FEEDBACK_CATEGORIES:
        raise ValueError("unsupported feedback_category")
    normalized = " ".join(rule_text.casefold().split())
    fingerprint = hashlib.sha256(f"{category}|{normalized}".encode("utf-8")).hexdigest()
    now_s = _now(now).isoformat(timespec="seconds")
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute("SELECT * FROM feedback_candidates WHERE fingerprint=?", (fingerprint,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE feedback_candidates SET occurrences=occurrences+1, updated_at=? WHERE id=?",
                (now_s, existing["id"]),
            )
            conn.commit()
            result = _feedback_public(conn.execute(
                "SELECT * FROM feedback_candidates WHERE id=?", (existing["id"],),
            ).fetchone())
            result["deduplicated"] = True  # type: ignore[index]
            return result  # type: ignore[return-value]
        feedback_id = "FB-" + secrets.token_hex(3).upper()
        conn.execute(
            """INSERT INTO feedback_candidates
               (id,rule_text,category,status,fingerprint,occurrences,created_at,updated_at,approved_at)
               VALUES (?,?,?,?,?,1,?,?,NULL)""",
            (feedback_id, rule_text, category, "pending", fingerprint, now_s, now_s),
        )
        conn.commit()
        result = _feedback_public(conn.execute(
            "SELECT * FROM feedback_candidates WHERE id=?", (feedback_id,),
        ).fetchone())
        result["deduplicated"] = False  # type: ignore[index]
        return result  # type: ignore[return-value]
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_feedback_candidates(*, status: str = "pending", limit: int = 20) -> list[dict[str, Any]]:
    status = str(status or "pending").strip().casefold()
    if status not in _FEEDBACK_STATUSES | {"all"}:
        raise ValueError("feedback_status must be pending, approved, dismissed, or all")
    limit = max(1, min(int(limit), 100))
    conn = _connect()
    try:
        if status == "all":
            rows = conn.execute(
                "SELECT * FROM feedback_candidates ORDER BY created_at DESC LIMIT ?", (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM feedback_candidates WHERE status=? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        return [_feedback_public(row) for row in rows]  # type: ignore[misc]
    finally:
        conn.close()


def resolve_feedback_candidate(feedback_id: str, decision: str, *, now: Any = None) -> dict[str, Any]:
    feedback_id = str(feedback_id or "").strip().upper()
    if not _FEEDBACK_ID_RE.fullmatch(feedback_id):
        raise ValueError("feedback_id must look like FB-1A2B3C")
    decision = str(decision or "").strip().casefold()
    if decision not in {"approved", "dismissed"}:
        raise ValueError("feedback_decision must be approved or dismissed")
    now_s = _now(now).isoformat(timespec="seconds")
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute("SELECT 1 FROM feedback_candidates WHERE id=?", (feedback_id,)).fetchone()
        if existing is None:
            raise KeyError("feedback candidate not found")
        conn.execute(
            "UPDATE feedback_candidates SET status=?, updated_at=?, approved_at=? WHERE id=?",
            (decision, now_s, now_s if decision == "approved" else None, feedback_id),
        )
        conn.commit()
        return _feedback_public(conn.execute(
            "SELECT * FROM feedback_candidates WHERE id=?", (feedback_id,),
        ).fetchone())  # type: ignore[return-value]
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def approved_feedback_rules(*, limit: int = 50) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 100))
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id,rule_text,category,approved_at FROM feedback_candidates "
            "WHERE status='approved' ORDER BY approved_at ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()
