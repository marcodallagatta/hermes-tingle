"""Profile-scoped durable store for the Hermes Tingle plugin."""
from __future__ import annotations

import hashlib
import functools
import threading
import time as clock
try:
    import fcntl
except ImportError:  # Windows Hermes installations
    fcntl = None
    import msvcrt
from contextlib import contextmanager
from pathlib import Path

import calendar
import json
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
_STATUSES = {"open", "waiting", "blocked", "done", "dismissed", "missed", "merged", "auto_closed"}
_ACTIVE_STATUSES = ("open", "waiting", "blocked")
_SOURCE_TYPES = {"conversation", "email", "calendar", "manual"}
_PRIORITIES = {"low", "normal", "high"}
_COMPLETION_SIGNALS = {"", "source_email_trashed", "source_email_reply_received", "source_email_checkpoint"}
_FEEDBACK_CATEGORIES = {"general", "email", "calendar", "links", "triage", "formatting", "reminders", "other"}
_FEEDBACK_STATUSES = {"pending", "approved", "dismissed"}
# Approved triage rules go to the model on every scan. The limits are enforced here, when a rule is
# captured or approved, so a rule is never silently clipped or dropped later (see hermes_tingle_assess).
TRIAGE_RULE_CHARS = 200
TRIAGE_RULES_MAX = 20
# Rules in these categories steer scans. Chat-captured preferences often default to "general", so they count too.
TRIAGE_CATEGORIES = ("triage", "general", "email")
_FEEDBACK_ID_RE = re.compile(r"^FB-[A-F0-9]{6}$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def open_database(name):
    import hermes_constants
    try:
        from . import hermes_tingle_resources as resources
    except ImportError:
        import hermes_tingle_resources as resources
    home=Path(hermes_constants.get_hermes_home()).resolve()
    with resources.mutation(home):
        resources.active(home)
        root=home/'plugin-data'/name
        existed=root.exists()
        conn=plugin_db(name)
        try:
            resources.active(home)
            actual=next((row[2] for row in conn.execute('PRAGMA database_list') if row[1]=='main'),'')
            if name in resources.NAMES and actual and Path(actual).resolve()==root/'data.db' and not existed:
                resources.record(root,home=home,directory=True)
            return conn
        except BaseException:
            conn.close()
            raise


def _connect() -> sqlite3.Connection:
    conn = open_database(PLUGIN_ID)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        ensure_schema(conn)
        return conn
    except BaseException:
        conn.close()
        raise


def ensure_schema(conn: sqlite3.Connection) -> None:
    # Serialize BEFORE schema inspection. A deferred read transaction cannot
    # safely upgrade to a writer while another initializer is doing the same.
    own_transaction = not conn.in_transaction
    if own_transaction:
        conn.execute("BEGIN IMMEDIATE")
    conn.execute('SAVEPOINT tingle_schema')
    try:
        _ensure_schema(conn)
        conn.execute('RELEASE SAVEPOINT tingle_schema')
        if own_transaction:
            conn.commit()
    except Exception:
        conn.execute('ROLLBACK TO SAVEPOINT tingle_schema')
        conn.execute('RELEASE SAVEPOINT tingle_schema')
        if own_transaction:
            conn.rollback()
        raise


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS case_merge_proposals (
        id TEXT PRIMARY KEY, state TEXT NOT NULL, created TEXT NOT NULL, expires TEXT NOT NULL,
        policy TEXT NOT NULL, principal TEXT NOT NULL, payload TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS case_merge_members (
        item_id TEXT PRIMARY KEY, canonical_id TEXT NOT NULL, merge_id TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS onboarding_item_journal (
        item_id TEXT PRIMARY KEY, scan_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'imported')""")
    existing_tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "open_loops" in existing_tables:
        if "hermes_tingle_items" in existing_tables:
            raise RuntimeError("both legacy and Hermes Tingle item tables exist; refusing to merge automatically")
        conn.execute("ALTER TABLE open_loops RENAME TO hermes_tingle_items")
        conn.execute("DROP INDEX IF EXISTS idx_open_loops_source_ref")
        conn.execute("DROP INDEX IF EXISTS idx_open_loops_due")
    schema_sql = (
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
            recur_interval INTEGER CHECK(recur_interval > 0),
            recur_unit TEXT CHECK(recur_unit IN ('day','month','year')),
            recur_lead_days INTEGER CHECK(recur_lead_days >= 0),
            recur_anchor TEXT,
            snoozed_until TEXT,
            reminder_count INTEGER NOT NULL DEFAULT 0,
            last_notified_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS hermes_tingle_retirement (
            item_id TEXT PRIMARY KEY REFERENCES hermes_tingle_items(id) ON DELETE CASCADE,
            retirement_class TEXT NOT NULL DEFAULT '',
            last_activity_at TEXT,
            auto_close_at TEXT,
            evidence_key TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_tingle_retirement_due
            ON hermes_tingle_retirement(auto_close_at);
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
        CREATE TABLE IF NOT EXISTS calendar_transition_alerts (
            pair_key TEXT PRIMARY KEY CHECK(length(pair_key)=64),
            alerted_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS calendar_transition_receipts (
            receipt_hash TEXT PRIMARY KEY CHECK(length(receipt_hash)=64),
            pair_key TEXT NOT NULL CHECK(length(pair_key)=64),
            source_generation INTEGER NOT NULL,
            policy_key TEXT NOT NULL,
            expires_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_calendar_transition_receipts_pair
            ON calendar_transition_receipts(pair_key, expires_at);
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
        CREATE TABLE IF NOT EXISTS recurrence_completions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id TEXT NOT NULL,
            occurrence_due_at TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS alert_emissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_ids TEXT NOT NULL,
            emitted_at TEXT NOT NULL,
            acked_at TEXT,
            surfaced_at TEXT
        );
        CREATE TABLE IF NOT EXISTS approval_requests (
            id TEXT PRIMARY KEY,
            action_type TEXT NOT NULL,
            target TEXT NOT NULL,
            parameters_json TEXT NOT NULL,
            parameters_hash TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','approved','rejected','expired','consumed')),
            requested_by TEXT NOT NULL DEFAULT '',
            approver_platform TEXT,
            approver_user_id TEXT,
            approver_chat_id TEXT,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            decided_at TEXT,
            approval_evidence TEXT NOT NULL DEFAULT '',
            consumed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_approval_requests_status
            ON approval_requests(status, created_at);
        CREATE TABLE IF NOT EXISTS action_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            approval_id TEXT,
            action_type TEXT NOT NULL,
            target TEXT NOT NULL,
            parameters_hash TEXT NOT NULL,
            outcome TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        """
    )
    for statement in schema_sql.split(';'):
        if statement.strip():
            conn.execute(statement)
    candidate_columns = {row[1] for row in conn.execute("PRAGMA table_info(email_candidates)")}
    if "account_key" not in candidate_columns:
        conn.execute("ALTER TABLE email_candidates ADD COLUMN account_key TEXT NOT NULL DEFAULT ''")
    if "policy_key" not in candidate_columns:
        conn.execute("ALTER TABLE email_candidates ADD COLUMN policy_key TEXT NOT NULL DEFAULT ''")
    if "generation" not in candidate_columns:
        conn.execute("ALTER TABLE email_candidates ADD COLUMN generation INTEGER NOT NULL DEFAULT -1")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(hermes_tingle_items)")}
    legacy_retirement = {"retirement_class", "retirement_last_activity_at", "auto_close_at", "retirement_evidence_key"} & columns
    if legacy_retirement:
        defaults = {
            "retirement_class": "''",
            "retirement_last_activity_at": "created_at",
            "auto_close_at": "NULL",
            "retirement_evidence_key": "''",
        }
        # A legacy marker may legitimately be NULL. Coalesce every nullable
        # legacy value before INSERT so one NULL cannot silently skip the whole
        # row and then lose its original activity/date to DROP COLUMN.
        select_expr = ["id"] + [
            "COALESCE(" + name + "," + defaults[name] + ")" if name in legacy_retirement else defaults[name]
            for name in ("retirement_class", "retirement_last_activity_at", "auto_close_at", "retirement_evidence_key")
        ]
        conn.execute(
            "INSERT OR IGNORE INTO hermes_tingle_retirement(item_id,retirement_class,last_activity_at,auto_close_at,evidence_key) "
            "SELECT " + ",".join(select_expr) + " FROM hermes_tingle_items"
        )
        conn.execute("DROP INDEX IF EXISTS idx_tingle_auto_close")
        for column in sorted(legacy_retirement):
            conn.execute("ALTER TABLE hermes_tingle_items DROP COLUMN " + column)
        columns -= legacy_retirement
    if 'topic_emoji' not in {r[1] for r in conn.execute('PRAGMA table_info(hermes_tingle_items)')}:
        conn.execute("ALTER TABLE hermes_tingle_items ADD COLUMN topic_emoji TEXT NOT NULL DEFAULT '📌'")
    if "completion_signal" not in columns:
        conn.execute("ALTER TABLE hermes_tingle_items ADD COLUMN completion_signal TEXT NOT NULL DEFAULT ''")
    # Retirement state lives in its own table: SELECT * on items must stay stable.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tingle_retirement_due ON hermes_tingle_retirement(auto_close_at)")
    if "email_review_plan" not in columns:
        conn.execute("ALTER TABLE hermes_tingle_items ADD COLUMN email_review_plan TEXT NOT NULL DEFAULT ''")
    if "reply_interval_seconds" not in columns:
        conn.execute("ALTER TABLE hermes_tingle_items ADD COLUMN reply_interval_seconds INTEGER")
    if "recur_interval" not in columns:
        conn.execute("ALTER TABLE hermes_tingle_items ADD COLUMN recur_interval INTEGER CHECK(recur_interval > 0)")
    if "recur_unit" not in columns:
        conn.execute("ALTER TABLE hermes_tingle_items ADD COLUMN recur_unit TEXT CHECK(recur_unit IN ('day','month','year'))")
    if "recur_lead_days" not in columns:
        conn.execute("ALTER TABLE hermes_tingle_items ADD COLUMN recur_lead_days INTEGER CHECK(recur_lead_days >= 0)")
    if "recur_anchor" not in columns:
        conn.execute("ALTER TABLE hermes_tingle_items ADD COLUMN recur_anchor TEXT")
        conn.execute("UPDATE hermes_tingle_items SET recur_anchor=due_at WHERE recur_interval IS NOT NULL AND recur_anchor IS NULL")
    if "origin_platform" not in columns:
        conn.execute("ALTER TABLE hermes_tingle_items ADD COLUMN origin_platform TEXT")
    if "origin_chat_id" not in columns:
        conn.execute("ALTER TABLE hermes_tingle_items ADD COLUMN origin_chat_id TEXT")
    if "origin_message_id" not in columns:
        conn.execute("ALTER TABLE hermes_tingle_items ADD COLUMN origin_message_id TEXT")
    if "reminder_revision" not in columns:
        conn.execute("ALTER TABLE hermes_tingle_items ADD COLUMN reminder_revision INTEGER NOT NULL DEFAULT 0")
    if "reminder_cycle" not in columns:
        conn.execute("ALTER TABLE hermes_tingle_items ADD COLUMN reminder_cycle TEXT NOT NULL DEFAULT ''")
    # Separate cycle identity from content/advice revision. Assign once, not on edit.
    conn.execute("UPDATE hermes_tingle_items SET reminder_cycle=lower(hex(randomblob(16))) WHERE reminder_cycle=''")
    conn.execute("CREATE TRIGGER IF NOT EXISTS reminder_cycle_insert AFTER INSERT ON hermes_tingle_items WHEN NEW.reminder_cycle='' BEGIN UPDATE hermes_tingle_items SET reminder_cycle=lower(hex(randomblob(16))) WHERE id=NEW.id; END")
    if "hard_deadline" not in columns:
        conn.execute("ALTER TABLE hermes_tingle_items ADD COLUMN hard_deadline INTEGER NOT NULL DEFAULT 0")
    if "watch_ref" not in columns:
        conn.execute("ALTER TABLE hermes_tingle_items ADD COLUMN watch_ref TEXT")
    if "watch_state" not in columns:
        conn.execute("ALTER TABLE hermes_tingle_items ADD COLUMN watch_state TEXT")
    if "watch_updated_at" not in columns:
        conn.execute("ALTER TABLE hermes_tingle_items ADD COLUMN watch_updated_at TEXT")
    if "watch_max_age_hours" not in columns:
        conn.execute("ALTER TABLE hermes_tingle_items ADD COLUMN watch_max_age_hours INTEGER CHECK(watch_max_age_hours > 0)")
    emission_columns = {row[1] for row in conn.execute("PRAGMA table_info(alert_emissions)")}
    if "emitted_to_gateway_at" not in emission_columns:
        conn.execute("ALTER TABLE alert_emissions ADD COLUMN emitted_to_gateway_at TEXT")
    for column, declaration in (("receipt_token", "TEXT"), ("channel", "TEXT NOT NULL DEFAULT 'script'"), ("released_at", "TEXT"), ("item_versions", "TEXT"), ("item_cycles", "TEXT")):
        if column not in emission_columns:
            conn.execute(f"ALTER TABLE alert_emissions ADD COLUMN {column} {declaration}")
    if not conn.execute("SELECT 1 FROM plugin_meta WHERE key='reminder_cycle_provenance_v1'").fetchone():
        # Preserve provable pre-upgrade batch-to-current-occurrence bindings.
        # A stale/missing version cannot identify its old occurrence; do not guess.
        for batch in conn.execute("SELECT id,item_versions FROM alert_emissions WHERE item_cycles IS NULL AND item_versions IS NOT NULL").fetchall():
            cycles = {}
            for item_id, revision in json.loads(batch["item_versions"]).items():
                item = conn.execute("SELECT reminder_cycle FROM hermes_tingle_items WHERE id=? AND reminder_revision=?", (item_id,revision)).fetchone()
                if item:
                    cycles[item_id] = item[0]
            conn.execute("UPDATE alert_emissions SET item_cycles=? WHERE id=?", (json.dumps(cycles),batch["id"]))
        conn.execute("INSERT INTO plugin_meta(key,value) VALUES('reminder_cycle_provenance_v1','1')")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reminder_cycle ON hermes_tingle_items(reminder_cycle)")
    # Attempt budget is independent of positive owner receipts. Legacy pending
    # batches are converted lazily under the reminder lock before selection.
    if "reminder_attempt_count" not in columns:
        conn.execute("ALTER TABLE hermes_tingle_items ADD COLUMN reminder_attempt_count INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE hermes_tingle_items SET reminder_attempt_count=reminder_count")
    if "reminder_budget_uncertain" not in columns:
        conn.execute("ALTER TABLE hermes_tingle_items ADD COLUMN reminder_budget_uncertain INTEGER NOT NULL DEFAULT 0")
    if "budget_reserved" not in emission_columns:
        conn.execute("ALTER TABLE alert_emissions ADD COLUMN budget_reserved INTEGER NOT NULL DEFAULT 0")
    approval_columns = {row[1] for row in conn.execute("PRAGMA table_info(approval_requests)")}
    for _col in ("approver_platform", "approver_user_id", "approver_chat_id"):
        if _col not in approval_columns:
            conn.execute(f"ALTER TABLE approval_requests ADD COLUMN {_col} TEXT")
    conn.execute("""CREATE TABLE IF NOT EXISTS hermes_tingle_steps (
        goal_id TEXT NOT NULL REFERENCES hermes_tingle_items(id),
        position INTEGER NOT NULL CHECK(position > 0),
        title TEXT NOT NULL,
        why TEXT NOT NULL,
        done INTEGER NOT NULL DEFAULT 0 CHECK(done IN (0, 1)),
        skipped INTEGER NOT NULL DEFAULT 0 CHECK(skipped IN (0, 1)),
        wait_item_id TEXT UNIQUE REFERENCES hermes_tingle_items(id),
        PRIMARY KEY(goal_id, position)
    )""")
    conn.execute('''CREATE TABLE IF NOT EXISTS hermes_tingle_documents (
        item_id TEXT NOT NULL REFERENCES hermes_tingle_items(id),
        position INTEGER NOT NULL CHECK(position > 0),
        name TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'needed'
            CHECK(state IN ('needed', 'collected', 'submitted', 'accepted', 'rejected', 'expired')),
        valid_until TEXT,
        note TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(item_id, position)
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS hermes_tingle_requirements (
        item_id TEXT NOT NULL REFERENCES hermes_tingle_items(id),
        position INTEGER NOT NULL CHECK(position > 0),
        text TEXT NOT NULL,
        must INTEGER NOT NULL DEFAULT 0 CHECK(must IN (0, 1)),
        met INTEGER NOT NULL DEFAULT 0 CHECK(met IN (0, 1)),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(item_id, position)
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS hermes_tingle_options (
        item_id TEXT NOT NULL REFERENCES hermes_tingle_items(id),
        position INTEGER NOT NULL CHECK(position > 0),
        name TEXT NOT NULL,
        detail TEXT DEFAULT '',
        price_text TEXT DEFAULT '',
        state TEXT NOT NULL DEFAULT 'candidate'
            CHECK(state IN ('candidate', 'shortlisted', 'rejected', 'proposed')),
        note TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(item_id, position)
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS hermes_tingle_bookings (
        item_id TEXT NOT NULL REFERENCES hermes_tingle_items(id),
        position INTEGER NOT NULL CHECK(position > 0),
        kind TEXT NOT NULL DEFAULT 'other'
            CHECK(kind IN ('flight', 'train', 'lodging', 'rental', 'event', 'other')),
        name TEXT NOT NULL,
        detail TEXT DEFAULT '',
        state TEXT NOT NULL DEFAULT 'researched'
            CHECK(state IN ('researched', 'booked', 'confirmed', 'cancelled')),
        cancel_by TEXT,
        cancel_item_id TEXT,
        confirmation_ref TEXT DEFAULT '',
        note TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(item_id, position)
    )''')
    feedback_cols = {row[1] for row in conn.execute("PRAGMA table_info(feedback_candidates)")}
    if "source_ref" not in feedback_cols:
        conn.execute("ALTER TABLE feedback_candidates ADD COLUMN source_ref TEXT NOT NULL DEFAULT ''")
    if "decided_by" not in feedback_cols:
        conn.execute("ALTER TABLE feedback_candidates ADD COLUMN decided_by TEXT NOT NULL DEFAULT ''")
    if "decision_evidence" not in feedback_cols:
        conn.execute("ALTER TABLE feedback_candidates ADD COLUMN decision_evidence TEXT NOT NULL DEFAULT ''")
    pending_cols = {row[1] for row in conn.execute("PRAGMA table_info(gmail_pending_messages)")}
    if "attempts" not in pending_cols:
        conn.execute("ALTER TABLE gmail_pending_messages ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
    if "next_retry_at" not in pending_cols:
        conn.execute("ALTER TABLE gmail_pending_messages ADD COLUMN next_retry_at TEXT")
    if "last_error_kind" not in pending_cols:
        conn.execute("ALTER TABLE gmail_pending_messages ADD COLUMN last_error_kind TEXT NOT NULL DEFAULT ''")
    if "quarantined" not in pending_cols:
        conn.execute("ALTER TABLE gmail_pending_messages ADD COLUMN quarantined INTEGER NOT NULL DEFAULT 0 CHECK(quarantined IN (0, 1))")
    if "quarantine_cycles" not in pending_cols:
        conn.execute("ALTER TABLE gmail_pending_messages ADD COLUMN quarantine_cycles INTEGER NOT NULL DEFAULT 0")
    if "quarantined_at" not in pending_cols:
        conn.execute("ALTER TABLE gmail_pending_messages ADD COLUMN quarantined_at TEXT")
    conn.execute("""CREATE TABLE IF NOT EXISTS gmail_unreadable (
        message_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        attempts INTEGER NOT NULL,
        first_failed_at TEXT NOT NULL,
        last_failed_at TEXT NOT NULL,
        note TEXT NOT NULL DEFAULT ''
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS hermes_tingle_source_optins (
        kind TEXT PRIMARY KEY CHECK(kind IN ('email','calendar','document','chat','meeting')),
        enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0, 1)),
        lookback_days INTEGER CHECK(lookback_days IS NULL OR (lookback_days >= 1 AND lookback_days <= 30)),
        enabled_by TEXT NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT '',
        enabled_at TEXT,
        disabled_at TEXT
    )""")
    source_columns = {row[1] for row in conn.execute("PRAGMA table_info(hermes_tingle_source_optins)")}
    if "generation" not in source_columns:
        conn.execute("ALTER TABLE hermes_tingle_source_optins ADD COLUMN generation INTEGER NOT NULL DEFAULT 0")
    if not conn.execute("SELECT 1 FROM plugin_meta WHERE key='missed_schedule_recovery_v2'").fetchone():
        conn.execute("UPDATE hermes_tingle_items SET next_check_at=COALESCE(next_check_at,updated_at,created_at),completed_at=NULL,reminder_count=0 WHERE status='missed'")
        conn.execute("INSERT INTO plugin_meta(key,value) VALUES('missed_schedule_recovery_v2','1')")
    if "calendar_context" not in source_columns:
        conn.execute("ALTER TABLE hermes_tingle_source_optins ADD COLUMN calendar_context INTEGER CHECK(calendar_context IN (0,1))")
    _initialize_source_optins(conn)
    conn.execute("""CREATE TABLE IF NOT EXISTS chat_source_consents (
        chat_ref TEXT PRIMARY KEY,
        lookback_days INTEGER NOT NULL DEFAULT 7 CHECK(lookback_days >= 1 AND lookback_days <= 30),
        granted_by TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        revoked_at TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS known_chats (
        chat_ref TEXT PRIMARY KEY,
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        origin TEXT NOT NULL DEFAULT ''
    )""")
    if 'owner_dm_principal' not in {row[1] for row in conn.execute('PRAGMA table_info(known_chats)')}:
        conn.execute('ALTER TABLE known_chats ADD COLUMN owner_dm_principal TEXT')
    conn.execute("""CREATE TABLE IF NOT EXISTS hermes_tingle_chat_discovery_candidates (
        id TEXT PRIMARY KEY, action_label TEXT NOT NULL, source_locator TEXT NOT NULL,
        source_version TEXT NOT NULL, binding TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
        UNIQUE(source_locator,source_version,binding))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS discovery_evaluation_ledger (
        key TEXT PRIMARY KEY, outcome TEXT NOT NULL CHECK(outcome IN ('keep','drop','uncertain')),
        expires_at TEXT NOT NULL)""")
    # Supports zero-rate reservations and optional comparison estimates. Rebuild
    # the two content-free tables atomically; preserve every old cap reservation.
    # Downgrades require writer quiescence: old code cannot enforce v2 policy.
    for table in ('discovery_evaluation_reservations', 'discovery_evaluation_spend'):
        old = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if old and ('cost_microeur>0' in old[0] or 'input_rate>0' in old[0]):
            conn.execute('ALTER TABLE ' + table + ' RENAME TO ' + table + '_v1')
    conn.execute("""CREATE TABLE IF NOT EXISTS discovery_evaluation_reservations (
        id TEXT PRIMARY KEY, month TEXT NOT NULL, cost_microeur INTEGER NOT NULL CHECK(cost_microeur>=0),
        created_at TEXT NOT NULL, state TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS discovery_evaluation_spend (
        request_id TEXT PRIMARY KEY, input_rate INTEGER NOT NULL CHECK(input_rate>=0),
        output_rate INTEGER NOT NULL CHECK(output_rate>=0),
        input_tokens INTEGER, output_tokens INTEGER,
        estimated_microeur INTEGER CHECK(estimated_microeur>=0),
        basis TEXT NOT NULL CHECK(basis IN ('reservation_bound','usage_bound')),
        comparison_input_rate INTEGER CHECK(comparison_input_rate>=0),
        comparison_output_rate INTEGER CHECK(comparison_output_rate>=0),
        comparison_microeur INTEGER CHECK(comparison_microeur>=0))""")
    for table, columns in (
            ('discovery_evaluation_reservations', 'id,month,cost_microeur,created_at,state'),
            ('discovery_evaluation_spend', 'request_id,input_rate,output_rate,input_tokens,output_tokens,estimated_microeur,basis')):
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table + '_v1',)).fetchone():
            conn.execute('INSERT INTO ' + table + ' (' + columns + ') SELECT ' + columns + ' FROM ' + table + '_v1')
            conn.execute('DROP TABLE ' + table + '_v1')
    conn.execute("""CREATE TABLE IF NOT EXISTS discovery_evaluation_prices (
        request_id TEXT PRIMARY KEY, provenance TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS discovery_evaluation_claims (
        key TEXT PRIMARY KEY, request_id TEXT NOT NULL, expires_at TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS chat_source_exclusions (
        chat_ref TEXT PRIMARY KEY,
        excluded_by TEXT NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    )""")
    step_columns = {row[1] for row in conn.execute("PRAGMA table_info(hermes_tingle_steps)")}
    if "skipped" not in step_columns:
        conn.execute("ALTER TABLE hermes_tingle_steps ADD COLUMN skipped INTEGER NOT NULL DEFAULT 0 CHECK(skipped IN (0, 1))")
    _migrate_legacy_item_ids(conn)

    # Convert inside schema initialization's write transaction BEFORE any public
    # mutation can invalidate legacy item revisions. Content edits are not cycles.
    _convert_legacy_reminder_budgets(conn)

    # Current goal steps link only to separate reply-waits, never other goals.
    # Revision invalidation commits with every dependency mutation, including
    # direct store callers. No time-based equality or application-only cache.
    conn.execute("""CREATE TRIGGER IF NOT EXISTS reminder_linked_wait_update
        AFTER UPDATE OF title,summary,status,owner,snoozed_until,completion_signal
        ON hermes_tingle_items BEGIN
        UPDATE hermes_tingle_items SET reminder_revision=reminder_revision+1
        WHERE id IN (SELECT goal_id FROM hermes_tingle_steps WHERE wait_item_id=NEW.id);
        END""")
    for event, ref in (("INSERT", "NEW"), ("UPDATE", "NEW"), ("DELETE", "OLD")):
        conn.execute(f"CREATE TRIGGER IF NOT EXISTS reminder_steps_{event.lower()} AFTER {event} ON hermes_tingle_steps BEGIN UPDATE hermes_tingle_items SET reminder_revision=reminder_revision+1 WHERE id={ref}.goal_id; END")
    # Contextual substate a review can rely on (documents, shopping requirements,
    # trip/booking options) invalidates the reviewed revision in the same
    # transaction. The cycle budget and attempt counters are never reset here.
    for table in ("hermes_tingle_documents", "hermes_tingle_requirements", "hermes_tingle_options"):
        for event, ref in (("INSERT", "NEW"), ("UPDATE", "NEW"), ("DELETE", "OLD")):
            conn.execute(f"CREATE TRIGGER IF NOT EXISTS reminder_{table}_{event.lower()} AFTER {event} ON {table} BEGIN UPDATE hermes_tingle_items SET reminder_revision=reminder_revision+1 WHERE id={ref}.item_id; END")


    # Derived expiry metadata has its own queue/log and cleanup, never ordinary attempts.
    try:
        from . import hermes_tingle_expiry as expiry
    except ImportError:
        import hermes_tingle_expiry as expiry
    expiry.ensure_schema(conn)


def _initialize_source_optins(conn: sqlite3.Connection) -> None:
    """Fresh/ambiguous installs require consent; preserve explicit recorded grants.

    Old migration defaults were not grants and are disabled for owner review.
    No tracked item or session content is removed by this policy migration.
    """
    for kind in ("email", "calendar", "chat", "meeting", "document"):
        conn.execute(
            "INSERT OR IGNORE INTO hermes_tingle_source_optins(kind,enabled) VALUES(?,0)", (kind,))
    ungranted = "enabled=1 AND (enabled_by='migration:install-default' OR trim(enabled_by)='' OR enabled_at IS NULL OR trim(enabled_at)='')"
    for row in conn.execute("SELECT kind FROM hermes_tingle_source_optins WHERE " + ungranted).fetchall():
        _purge_source_retained(conn, row[0])
    conn.execute("""UPDATE hermes_tingle_source_optins SET enabled=0, generation=generation+1,
        calendar_context=CASE WHEN kind='calendar' THEN 0 ELSE calendar_context END,
        note='No complete recorded grant; explicit owner opt-in required.'
        WHERE """ + ungranted)

    # One displayed Calendar source, preserving the exact old field scope. A
    # meeting-only grant never enables Calendar. Retain the legacy row as evidence,
    # but do not read it after this idempotent per-calendar migration.
    conn.execute("""UPDATE hermes_tingle_source_optins SET calendar_context=CASE WHEN EXISTS (
        SELECT 1 FROM hermes_tingle_source_optins WHERE kind='meeting' AND enabled=1
        AND trim(enabled_by)<>'' AND enabled_at IS NOT NULL AND trim(enabled_at)<>''
        ) THEN 1 ELSE 0 END WHERE kind='calendar' AND calendar_context IS NULL""")



# File locks cover processes (cron and gateway); RLocks cover threads and nested
# source calls. No database transaction is held while a network call runs.
_operation_locks: dict[tuple[str, str], threading.RLock] = {}
_operation_locks_guard = threading.Lock()
_operation_depth = threading.local()


class OperationBusy(ValueError):
    """Operation did not run because another operation is active."""


def _file_lock(handle, *, unlock: bool = False) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN if unlock else fcntl.LOCK_EX | fcntl.LOCK_NB)
    else:  # pragma: no cover - exercised by Windows CI/operators, not Linux runs
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK if unlock else msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if not unlock:
                raise BlockingIOError() from exc
            raise


@contextmanager
def serialized_operation(category: str):
    conn = _connect()
    try:
        path = next((row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main"), "")
    finally:
        conn.close()
    if not path:
        raise RuntimeError("serialized operations require a file-backed plugin database")
    key = (str(Path(path).resolve()), category)
    with _operation_locks_guard:
        lock = _operation_locks.setdefault(key, threading.RLock())
    deadline = clock.monotonic() + 30
    if not lock.acquire(timeout=max(0, deadline-clock.monotonic())):
        raise OperationBusy("another operation is still active; no change completed, retry later")
    try:
        depths = getattr(_operation_depth, "values", None)
        if depths is None:
            depths = _operation_depth.values = {}
        if depths.get(key, 0):
            depths[key] += 1
            try:
                yield
            finally:
                depths[key] -= 1
            return
        # The lock contains no account data. OS releases it even after a crash.
        with open(key[0] + "." + category + ".lock", "a+b") as handle:
            if handle.seek(0, 2) == 0:
                handle.write(b"0")
                handle.flush()
            while True:
                try:
                    _file_lock(handle)
                    break
                except BlockingIOError:
                    if clock.monotonic() >= deadline:
                        raise OperationBusy("another operation is still active; no change completed, retry later")
                    clock.sleep(0.01)
            previous_versions = getattr(_operation_depth, "source_versions", None)
            if category == "sources":
                conn = _connect()
                try:
                    _operation_depth.source_versions = {
                        row["kind"]: row["generation"] for row in conn.execute(
                            "SELECT kind,generation FROM hermes_tingle_source_optins")}
                finally:
                    conn.close()
            depths[key] = 1
            try:
                yield
            finally:
                if category == "sources":
                    _operation_depth.source_versions = previous_versions
                depths.pop(key, None)
                _file_lock(handle, unlock=True)
    finally:
        lock.release()


def serialized(category: str):
    def decorate(fn):
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            with serialized_operation(category):
                return fn(*args, **kwargs)
        return wrapped
    return decorate


def source_generation(kind: str) -> int | None:
    """Opaque grant epoch carried with snapshots; never infer it for an old result."""
    conn = _connect()
    try:
        row = conn.execute("SELECT generation FROM hermes_tingle_source_optins WHERE kind=? AND enabled=1", (kind,)).fetchone()
        return int(row[0]) if row else None
    finally:
        conn.close()


def assert_source_grant(kind: str) -> None:
    """Check the outer operation's epoch immediately before an external read."""
    conn = _connect()
    try:
        _require_source(conn, kind)
    finally:
        conn.close()


def _require_source(conn: sqlite3.Connection, kind: str) -> None:
    """Recheck inside the same write transaction as every source-derived write."""
    row = conn.execute("SELECT enabled,generation FROM hermes_tingle_source_optins WHERE kind=?", (kind,)).fetchone()
    expected = getattr(_operation_depth, "source_versions", None)
    if not row or not row[0] or (expected is not None and expected.get(kind) != row[1]):
        raise SourceOptInRevoked(f"{kind} source is disabled; operation stopped")


def _migrate_legacy_item_ids(conn: sqlite3.Connection) -> int:
    """Renumber legacy OL- item IDs to fresh HT- IDs, preserving every row and
    its relational references (steps, linked waits, documents, requirements,
    options, bookings, cancellation links, recurrence completions, alert
    batches). Pending approvals that bind an old ID's exact parameters are
    expired with an audit note - their hash must keep matching the original
    words, so they cannot be rewritten. Idempotent: no OL- rows, no-op."""
    rows = conn.execute("SELECT id FROM hermes_tingle_items WHERE id LIKE 'OL-%' ORDER BY created_at").fetchall()
    if not rows:
        return 0
    now_s = datetime.now(timezone.utc).isoformat(timespec="seconds")
    taken = {r[0] for r in conn.execute("SELECT id FROM hermes_tingle_items")}
    mapping: dict[str, str] = {}
    for (old_id,) in rows:
        new_id = None
        for _ in range(100):
            candidate = "HT-" + secrets.token_hex(3).upper()
            if candidate not in taken:
                new_id = candidate
                break
        if new_id is None:
            raise RuntimeError("could not allocate a fresh HT- item ID after 100 attempts")
        mapping[old_id] = new_id
        taken.add(new_id)
    references = (
        ("hermes_tingle_steps", "goal_id"),
        ("hermes_tingle_steps", "wait_item_id"),
        ("hermes_tingle_documents", "item_id"),
        ("hermes_tingle_requirements", "item_id"),
        ("hermes_tingle_options", "item_id"),
        ("hermes_tingle_bookings", "item_id"),
        ("hermes_tingle_bookings", "cancel_item_id"),
        ("recurrence_completions", "item_id"),
    )
    item_columns = [r[1] for r in conn.execute("PRAGMA table_info(hermes_tingle_items)")]
    # FK- and unique-safe order, no PRAGMA change or deferral: free each old
    # row's (source_type, source_ref) slot first (the partial unique index only
    # covers source_ref <> ''), insert the new parent copy carrying the
    # ORIGINAL values, re-point every child, then delete the old parent. All
    # phases run inside the schema savepoint, so any failure rolls back.
    originals = {}
    for old_id in mapping:
        originals[old_id] = conn.execute("SELECT * FROM hermes_tingle_items WHERE id=?", (old_id,)).fetchone()
        conn.execute("UPDATE hermes_tingle_items SET source_ref='' WHERE id=?", (old_id,))
    for old_id, new_id in mapping.items():
        row = originals[old_id]
        values = [new_id if column == "id" else row[i] for i, column in enumerate(item_columns)]
        conn.execute(
            f"INSERT INTO hermes_tingle_items ({','.join(item_columns)}) VALUES ({','.join('?' * len(item_columns))})",
            values)
    for old_id, new_id in mapping.items():
        for table, column in references:
            conn.execute(f"UPDATE {table} SET {column}=? WHERE {column}=?", (new_id, old_id))
        # The new parent already exists, so the side-table FK stays valid while
        # lifecycle state follows a legacy identifier into its canonical ID.
        conn.execute("UPDATE hermes_tingle_retirement SET item_id=? WHERE item_id=?", (new_id, old_id))
    for old_id in mapping:
        conn.execute("DELETE FROM hermes_tingle_items WHERE id=?", (old_id,))
    for old_id, new_id in mapping.items():
        for emission in conn.execute("SELECT id, item_ids FROM alert_emissions WHERE item_ids LIKE ?", (f'%"{old_id}"%',)).fetchall():
            remapped = [mapping.get(item, item) for item in json.loads(emission[1])]
            conn.execute("UPDATE alert_emissions SET item_ids=? WHERE id=?", (json.dumps(remapped), emission[0]))
        for column in ("item_versions", "item_cycles"):
            for emission in conn.execute(f"SELECT id,{column} FROM alert_emissions WHERE {column} IS NOT NULL").fetchall():
                values = json.loads(emission[1])
                remapped = {mapping.get(key,key): value for key,value in values.items()}
                if values != remapped:
                    conn.execute(f"UPDATE alert_emissions SET {column}=? WHERE id=?", (json.dumps(remapped),emission[0]))
        # Pending AND approved-but-unconsumed approvals binding the old ID can
        # never execute honestly after renumbering; expire both. Consumed,
        # rejected and already-expired approvals stay immutable history.
        for approval in conn.execute(
                "SELECT id, action_type, target, parameters_hash, status FROM approval_requests WHERE status IN ('pending','approved') AND (parameters_json LIKE ? OR target LIKE ?)",
                (f"%{old_id}%", f"%{old_id}%")).fetchall():
            conn.execute("UPDATE approval_requests SET status='expired', decided_at=? WHERE id=? AND status=?", (now_s, approval[0], approval[4]))
            conn.execute(
                "INSERT INTO action_audit(approval_id, action_type, target, parameters_hash, outcome, detail, created_at) VALUES(?,?,?,?,?,?,?)",
                (approval[0], approval[1], approval[2], approval[3], "expired",
                 f"Expired by the legacy ID migration (was {approval[4]}): item {old_id} was renumbered to {new_id} and the approval binds the old ID's exact parameters. A fresh approval is required.", now_s))
    prior = conn.execute("SELECT value FROM plugin_meta WHERE key='legacy_id_migration'").fetchone()
    prior_mapping = json.loads(prior[0]).get("mapping", {}) if prior else {}
    mapping = {**prior_mapping, **mapping}
    conn.execute(
        "INSERT OR REPLACE INTO plugin_meta(key, value) VALUES('legacy_id_migration', ?)",
        (json.dumps({"migrated": len(mapping), "mapping": mapping, "at": now_s}),))
    return len(mapping)


def resolve_item_id(item_id: Any) -> str:
    value = str(item_id or "").upper()
    if not value.startswith("OL-"):
        return value
    conn = _connect()
    try:
        row = conn.execute("SELECT value FROM plugin_meta WHERE key='legacy_id_migration'").fetchone()
        mapping = json.loads(row[0]).get("mapping", {}) if row else {}
        return str(mapping.get(value, value))
    finally:
        conn.close()


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


def _retirement_for(conn: sqlite3.Connection, item_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM hermes_tingle_retirement WHERE item_id=?", (item_id,)).fetchone()


def retirement_state(item_id: str) -> dict[str, Any] | None:
    """Internal read seam for retirement policy state; never included in item payloads."""
    conn = _connect()
    try:
        row = _retirement_for(conn, resolve_item_id(item_id))
        return dict(row) if row is not None else None
    finally:
        conn.close()

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


_RETIREMENT_DAYS = {"deadline": 7, "waiting": 270, "default": 90}
_MAX_ITEM_AGE_DAYS = 365
_DEADLINE_REMINDERS_BEFORE_CLOSE = 3


def _retirement_class(*, due_at: datetime | None, status: str, owner: str,
                      hard_deadline: bool = False, has_open_wait: bool = False) -> str:
    if due_at is not None or hard_deadline:
        return "deadline"
    if status == "waiting" or owner in {"other", "joint"} or has_open_wait:
        return "waiting"
    return "default"


def _has_open_wait(conn: sqlite3.Connection, item_id: str) -> bool:
    return bool(conn.execute(
        """SELECT 1 FROM hermes_tingle_steps s JOIN hermes_tingle_items w ON w.id=s.wait_item_id
           WHERE s.goal_id=? AND COALESCE(s.skipped,0)=0 AND COALESCE(s.done,0)=0
             AND w.status IN ('open','waiting','blocked','missed')
             AND w.completion_signal='source_email_reply_received' LIMIT 1""",
        (item_id,),
    ).fetchone())


_RETIREMENT_EVIDENCE_BITS = 4096
_RETIREMENT_EVIDENCE_PREFIX = "rb1:"


def _evidence_positions(key: str) -> tuple[int, ...]:
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return tuple(int.from_bytes(digest[offset:offset + 2], "big") % _RETIREMENT_EVIDENCE_BITS
                 for offset in (0, 2, 4, 6))


def _evidence_mask(key: str) -> int:
    mask = 0
    for position in _evidence_positions(key):
        mask |= 1 << position
    return mask


def _decode_retirement_evidence(value: Any) -> int:
    """Decode the bounded no-false-negative seen-key Bloom filter.

    Accept the early single-hash and JSON-list encodings additively during upgrade.
    """
    if not value:
        return 0
    text = str(value)
    if text.startswith(_RETIREMENT_EVIDENCE_PREFIX):
        try:
            raw = bytes.fromhex(text[len(_RETIREMENT_EVIDENCE_PREFIX):])
            if len(raw) == _RETIREMENT_EVIDENCE_BITS // 8:
                return int.from_bytes(raw, "big")
        except ValueError:
            pass
        return 0
    try:
        legacy = json.loads(text)
        if isinstance(legacy, list):
            bits = 0
            for key in legacy:
                if isinstance(key, str):
                    bits |= _evidence_mask(key)
            return bits
    except ValueError:
        pass
    return _evidence_mask(text[:128])


def _remember_retirement_evidence(bits: int, key: str) -> tuple[bool, str]:
    mask = _evidence_mask(key)
    seen = bits & mask == mask
    updated = bits if seen else bits | mask
    encoded = (_RETIREMENT_EVIDENCE_PREFIX
               + updated.to_bytes(_RETIREMENT_EVIDENCE_BITS // 8, "big").hex())
    return seen, encoded


def _auto_close_at(*, retirement_class: str, created_at: datetime, last_activity_at: datetime,
                   due_at: datetime | None) -> datetime:
    """Return a stable item close date, bounded by one year from creation.

    A deadline item closes a fixed grace after its deadline; later owner edits
    do not move that deadline. Other classes count inactivity from the last
    meaningful owner/evidence change.
    """
    if retirement_class == "deadline" and due_at is not None:
        target = due_at + timedelta(days=_RETIREMENT_DAYS["deadline"])
    else:
        days = _RETIREMENT_DAYS.get(retirement_class, _RETIREMENT_DAYS["default"])
        target = last_activity_at + timedelta(days=days)
    return min(target, created_at + timedelta(days=_MAX_ITEM_AGE_DAYS))


def _touch_retirement_activity(conn: sqlite3.Connection, item_id: str, now: Any) -> None:
    """Sync retirement state after a real content change. Callers must only invoke
    this after a structured write that actually changed content."""
    row = conn.execute(
        "SELECT created_at,due_at,status,owner,hard_deadline FROM hermes_tingle_items WHERE id=?",
        (item_id,),
    ).fetchone()
    if row is None:
        return
    existing = _retirement_for(conn, item_id)
    created_at = _parse_time(row["created_at"], field="created_at")
    if created_at is None:
        return
    due_at = _parse_time(row["due_at"], field="due_at") if row["due_at"] else None
    activity_at = _now(now)
    retirement_class = _retirement_class(due_at=due_at, status=str(row["status"]),
                                         owner=str(row["owner"]), hard_deadline=bool(row["hard_deadline"]),
                                         has_open_wait=_has_open_wait(conn, item_id))
    auto_close_at = _auto_close_at(retirement_class=retirement_class, created_at=created_at,
                                   last_activity_at=activity_at, due_at=due_at).isoformat(timespec="seconds")
    if existing is None:
        _ensure_retirement_row(
            conn, item_id, created_at=row["created_at"], retirement_class=retirement_class,
            last_activity_at=activity_at.isoformat(timespec="seconds"), auto_close_at=auto_close_at,
        )
    else:
        conn.execute(
            "UPDATE hermes_tingle_retirement SET retirement_class=?,last_activity_at=?,auto_close_at=? WHERE item_id=?",
            (retirement_class, activity_at.isoformat(timespec="seconds"), auto_close_at, item_id),
        )


def _ensure_retirement_row(conn: sqlite3.Connection, item_id: str, *, created_at: str,
                           retirement_class: str, last_activity_at: str, auto_close_at: str) -> None:
    conn.execute(
        """INSERT INTO hermes_tingle_retirement(item_id,retirement_class,last_activity_at,auto_close_at)
           VALUES(?,?,?,?) ON CONFLICT(item_id) DO NOTHING""",
        (item_id, retirement_class, last_activity_at, auto_close_at),
    )


_RECUR_UNITS = {"day", "month", "year"}


_RECUR_NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}
_RECUR_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def parse_recurrence(text: Any) -> tuple[int, str]:
    """Parse a natural-language recurrence phrase into (interval, unit).

    FT-03: users say "every Monday", "every 2 weeks" or "quarterly", not
    interval/unit pairs. The model passes the user's phrase through unchanged;
    this parser is the only place phrases become recurrence parameters, so the
    mapping is deterministic and testable rather than model-invented.

    Weeks are mapped onto the day unit (weekly -> every 7 days) because the
    engine's units are day/month/year; a 7-day series anchored on the chosen
    weekday keeps that weekday permanently. Month-end clamping is documented on
    _advance_recurrence.

    Raises ValueError on a phrase with no recognizable recurrence and on
    contradictory phrases ("every monday monthly"); the model must then ask
    rather than guess.
    """
    phrase = " ".join(str(text or "").strip().casefold().split())
    if not phrase:
        raise ValueError("recur_text is empty")
    matches: list[tuple[int, str]] = []

    def _add(interval: int, unit: str) -> None:
        pair = (interval, unit)
        if pair not in matches:
            matches.append(pair)

    for weekday in _RECUR_WEEKDAYS:
        if re.search(rf"\b{weekday}s?\b", phrase):
            _add(7, "day")
    if re.search(r"\bevery\s+(day|morning|evening|night)\b|\bdaily\b", phrase):
        _add(1, "day")
    if re.search(r"\bfortnightly\b|\bevery\s+(other|second)\s+week\b", phrase):
        _add(14, "day")
    if re.search(r"\bevery\s+(other|second)\s+month\b", phrase):
        _add(2, "month")
    if re.search(r"\bquarterly\b|\bevery\s+quarter\b", phrase):
        _add(3, "month")
    for match in re.finditer(
            r"\bevery\s+(\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s+(day|week|month|year)s?\b",
            phrase):
        count_s, unit_s = match.groups()
        count = int(count_s) if count_s.isdigit() else _RECUR_NUMBER_WORDS[count_s]
        _add(count * 7 if unit_s == "week" else count, "day" if unit_s == "week" else unit_s)
    if re.search(r"\bweekly\b|\bevery\s+week\b|\bevery\s+weekend\b", phrase):
        _add(7, "day")
    if re.search(r"\bmonthly\b|\bevery\s+month\b", phrase):
        _add(1, "month")
    if re.search(r"\byearly\b|\bannually\b|\bevery\s+year\b|\bevery\s+anniversary\b", phrase):
        _add(1, "year")

    if not matches:
        raise ValueError(f"no recognizable recurrence in {phrase!r}")
    if len(matches) > 1:
        raise ValueError(
            f"contradictory recurrence phrases in {phrase!r} ({matches}); ask the user which one applies")
    return matches[0]


def resolve_recurrence_args(recur_text: Any, recur_interval: Any, recur_unit: Any) -> tuple[Any, Any]:
    """Merge a natural-language recur_text with explicit interval/unit params.

    Explicit parameters win when identical; a conflict between the user's own
    words and explicit parameters is refused rather than silently resolved.
    """
    if recur_text in (None, ""):
        return recur_interval, recur_unit
    parsed_interval, parsed_unit = parse_recurrence(recur_text)
    if recur_interval in (None, "") and recur_unit in (None, ""):
        return parsed_interval, parsed_unit
    if (recur_interval, recur_unit) != (parsed_interval, parsed_unit):
        raise ValueError(
            f"recur_text implies every {parsed_interval} {parsed_unit}(s) but explicit "
            "recur_interval/recur_unit say otherwise; use one source of truth")
    return recur_interval, recur_unit


def _validate_recurrence(interval: Any, unit: Any, lead_days: Any, due_dt: Any) -> int | None:
    """Recurring obligations (FT-03): interval+unit anchored on a real deadline.

    Returns the explicit lead override, or None to derive it from the initial
    due_at/next_check_at gap. The lead is stored on the row so it survives the
    reminder series rewriting next_check_at onto the deadline.
    """
    if interval in (None, "") and unit in (None, ""):
        if lead_days not in (None, ""):
            raise ValueError("recur_lead_days requires a recurrence")
        return None
    if isinstance(interval, bool) or not isinstance(interval, int) or interval < 1:
        raise ValueError("recur_interval must be a positive integer")
    if unit not in _RECUR_UNITS:
        raise ValueError("recur_unit must be day, month, or year")
    if due_dt is None:
        raise ValueError("recurring items require a due_at anchor")
    if lead_days in (None, ""):
        return None
    if isinstance(lead_days, bool) or not isinstance(lead_days, int) or lead_days < 0:
        raise ValueError("recur_lead_days must be a non-negative integer")
    return lead_days


def _advance_recurrence(due_dt: datetime, interval: int, unit: str, now_dt: datetime,
                        anchor_dt: datetime | None = None) -> datetime:
    """Next occurrence on the profile-local wall clock, always strictly future.

    Days are wall-clock days in the profile timezone (a 09:00 reminder stays
    09:00 across DST changes). Months/years jump k whole calendar intervals
    from the persisted ORIGINAL anchor (not the clamped previous occurrence),
    so a Jan 31 monthly series runs Feb 28, Mar 31, Apr 30, May 31 and a
    Feb 29 yearly series lands back on Feb 29 every leap year, with no
    accumulated clamp drift and always at least one interval forward.
    """
    zone = local_timezone()
    due_local = due_dt.astimezone(zone)
    local_now = now_dt.astimezone(zone)
    if unit == "day":
        step = timedelta(days=interval)
        candidate = due_local + step
        while candidate <= local_now:
            candidate = candidate + step
        return candidate
    anchor_local = (anchor_dt or due_dt).astimezone(zone)
    months = interval if unit == "month" else interval * 12
    floor = max(local_now, due_local)
    k = 1
    candidate = _add_months(anchor_local, months)
    while candidate <= floor:
        k += 1
        candidate = _add_months(anchor_local, k * months)
    return candidate


def _add_months(dt: datetime, months: int) -> datetime:
    """Calendar month addition with month-end clamping (Jan 31 + 1 = Feb 28)."""
    month_index = dt.month - 1 + months
    year = dt.year + month_index // 12
    month = month_index % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def validate_source_ref(value):
    value = str(value or "")
    if value != value.strip() or len(value) > 1024:
        raise ValueError("source_ref must be exact, with no surrounding whitespace, and at most 1024 characters")
    return value


def add_item(
    *,
    title: str,
    topic_emoji: str = "📌",
    onboarding_scan: str | None = None,
    review_plan: Any = None,
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
    recur_interval: Any = None,
    recur_unit: Any = None,
    recur_lead_days: Any = None,
    origin: Any = None,
    hard_deadline: Any = False,
    now: Any = None,
    _conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    try:
        from .hermes_tingle_topic import safe_icon
    except ImportError:
        from hermes_tingle_topic import safe_icon
    topic_emoji = safe_icon(topic_emoji)
    if status == 'merged': raise ValueError('Merged state is only available through owner-reviewed case merge.')
    title = str(title or "").strip()
    if not title:
        raise ValueError("title is required")
    origin_platform = origin_chat_id = origin_message_id = None
    if origin is not None:
        try:
            origin_platform, origin_chat_id, origin_message_id = (
                str(origin[0] or "").strip().casefold() or None,
                str(origin[1] or "").strip() or None,
                str(origin[2] or "").strip() or None,
            )
        except (TypeError, IndexError):
            raise ValueError("origin must be a (platform, chat_id, message_id) tuple")
        if not (origin_platform and origin_chat_id and origin_message_id):
            origin_platform = origin_chat_id = origin_message_id = None
    hard = 1 if hard_deadline and not isinstance(hard_deadline, str) else 0
    if isinstance(hard_deadline, str):
        hard = 1 if hard_deadline.strip().casefold() in {"1", "true", "yes"} else 0
    if hard and due_at is None:
        raise ValueError("a hard deadline (cancellation window) requires a due_at anchor")
    if len(title) > 180:
        raise ValueError("title must be at most 180 characters")
    summary = str(summary or "").strip()
    if len(summary) > 1200:
        raise ValueError("summary must be at most 1200 characters")
    if status not in {"open", "waiting", "blocked"}:
        raise ValueError("new items must have status open, waiting, or blocked")
    if owner not in {"me", "other", "joint", "unknown"}:
        raise ValueError("owner must be me, other, joint, or unknown")
    if priority not in _PRIORITIES:
        raise ValueError("priority must be low, normal, or high")
    if source_type not in _SOURCE_TYPES:
        raise ValueError("unsupported source_type")
    if completion_signal not in _COMPLETION_SIGNALS:
        raise ValueError("unsupported completion_signal")
    source_ref = validate_source_ref(source_ref)
    if completion_signal == "source_email_reply_received":
        if source_type != "email" or status != "waiting" or owner != "other":
            raise ValueError("reply-watch items must be waiting email items owned by another person")
        if not source_ref.startswith("gmail:") or "#" not in source_ref[6:] or not source_ref.rsplit("#", 1)[-1]:
            raise ValueError("reply-watch source_ref must include a Gmail thread and sent-message anchor")
        if next_check_at in (None, ""):
            raise ValueError("reply-watch items require a context-specific next_check_at")
    plan = None
    if completion_signal == 'source_email_checkpoint':
        try:
            from . import hermes_tingle_checkpoint as checkpoint
        except ImportError:
            import hermes_tingle_checkpoint as checkpoint
        if source_type != 'email' or not re.fullmatch(r'gmail:[a-f0-9]{64}/[^#]+#[^#]+', source_ref):
            raise ValueError('email checkpoints require a verified account/thread/message source')
        plan = checkpoint.item_plan(review_plan)
        owner = plan['actor']
        status = 'waiting' if owner == 'other' else 'open'
        if next_check_at in (None, '') and not plan['review_needed']:
            next_check_at = plan['review_at']
    source_url = str(source_url or "").strip()[:2048]
    now_dt = _now(now)
    now_s = now_dt.isoformat(timespec="seconds")
    due_dt = _parse_time(due_at, field="due_at")
    if hard and due_dt is None:
        raise ValueError("a hard deadline requires a due_at anchor")
    check_dt = _parse_time(next_check_at, field="next_check_at")
    recur_lead = _validate_recurrence(recur_interval, recur_unit, recur_lead_days, due_dt)
    recur_days = None
    if check_dt is None and (completion_signal != 'source_email_checkpoint' or due_dt is not None):
        check_dt = _next_check(now=now_dt, due_at=due_dt, owner=owner, priority=priority, status=status)
    if recur_interval not in (None, ""):
        recur_days = recur_interval
        if recur_lead is None:
            gap_seconds = (due_dt - check_dt).total_seconds()
            recur_lead = max(0, int(gap_seconds // 86400))
    reply_interval_seconds = None
    if completion_signal == "source_email_reply_received":
        interval = int((check_dt - now_dt).total_seconds())
        reply_interval_seconds = interval if interval > 0 else 2 * 24 * 60 * 60
    retirement_class = _retirement_class(due_at=due_dt, status=status, owner=owner, hard_deadline=bool(hard))
    retirement_last_activity_at = now_s
    auto_close_at = _auto_close_at(retirement_class=retirement_class, created_at=now_dt,
                                   last_activity_at=now_dt, due_at=due_dt).isoformat(timespec="seconds")

    conn = _conn if _conn is not None else _connect()
    try:
        if source_ref:
            existing = conn.execute(
                "SELECT * FROM hermes_tingle_items WHERE source_type=? AND source_ref=?",
                (source_type, source_ref),
            ).fetchone()
            if existing:
                if (" ".join(str(existing['title']).casefold().split()) != " ".join(title.casefold().split())
                        or existing['summary'] != summary or existing['completion_signal'] != completion_signal
                        or existing['owner'] != owner or existing['status'] != status
                        or existing['priority'] != priority or existing['source_url'] != source_url
                        or existing['due_at'] != (due_dt.isoformat(timespec='seconds') if due_dt else None)
                        or existing['recur_interval'] != recur_interval or existing['recur_unit'] != (recur_unit if recur_interval else None)
                        or bool(existing['hard_deadline']) != bool(hard)
                        or (plan is not None and existing['email_review_plan'] != json.dumps(plan))
                        or (recur_lead is not None and existing['recur_lead_days'] != recur_lead)
                        or (next_check_at not in (None, '') and existing['next_check_at'] != check_dt.isoformat(timespec='seconds'))):
                    raise ValueError('source already belongs to a different item; use its existing ID')
                result = dict(existing)
                result["deduplicated"] = True
                return result
        if completion_signal != "source_email_reply_received" and not source_ref:
            norm = " ".join(title.casefold().split())
            candidates = conn.execute(
                "SELECT * FROM hermes_tingle_items WHERE status IN ('open','waiting','blocked') AND owner=?",
                (owner,),
            ).fetchall()
            for row in candidates:
                same_title = " ".join(str(row["title"]).casefold().split()) == norm
                same_due = (row["due_at"] or None) == (due_dt.isoformat(timespec="seconds") if due_dt else None)
                if same_title and same_due and row['summary'] == summary and not row['source_ref']:
                    if (row['recur_interval'] != recur_interval or row['recur_unit'] != (recur_unit if recur_interval else None)
                            or bool(row['hard_deadline']) != bool(hard)
                            or (recur_lead is not None and row['recur_lead_days'] != recur_lead)):
                        raise ValueError('matching item has incompatible recurrence or deadline semantics; use its existing ID')
                    result = dict(row)
                    result["deduplicated"] = True
                    return result

        due_s = due_dt.isoformat(timespec="seconds") if due_dt else None
        check_s = check_dt.isoformat(timespec="seconds") if check_dt else None
        for _ in range(16):
            item_id = "HT-" + secrets.token_hex(3).upper()
            try:
                conn.execute(
                    """INSERT INTO hermes_tingle_items
                       (id,title,summary,status,owner,priority,source_type,source_ref,source_url,completion_signal,reply_interval_seconds,
                        due_at,next_check_at,recur_interval,recur_unit,recur_lead_days,recur_anchor,
                        origin_platform,origin_chat_id,origin_message_id,hard_deadline,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (item_id,title,summary,status,owner,priority,source_type,source_ref,source_url,
                     completion_signal,reply_interval_seconds,due_s,check_s,recur_days,
                     recur_unit if recur_days else None, recur_lead if recur_days else None,
                     due_s if recur_days else None, origin_platform, origin_chat_id, origin_message_id,
                     hard, now_s, now_s),
                )
                _ensure_retirement_row(
                    conn, item_id, created_at=now_s, retirement_class=retirement_class,
                    last_activity_at=now_s, auto_close_at=auto_close_at,
                )
                break
            except sqlite3.IntegrityError as exc:
                if not _is_primary_key_collision(exc):
                    raise
        else:
            raise RuntimeError("Unable to allocate a unique Tingle item ID")
        conn.execute("UPDATE hermes_tingle_items SET topic_emoji=?,email_review_plan=? WHERE id=?", (topic_emoji,json.dumps(plan) if plan is not None else '',item_id))
        if onboarding_scan:
            conn.execute("INSERT INTO onboarding_item_journal(item_id,scan_id,fingerprint) VALUES(?,?,?)",
                         (item_id, str(onboarding_scan), _onboarding_fingerprint(conn, item_id)))
            _save_onboarding_in_transaction(conn, {'state': 'partial', 'scan_id': str(onboarding_scan),
                                                   'at': now_s, 'items': [item_id]})

        if _conn is None:
            conn.commit()
        return _public(conn.execute("SELECT * FROM hermes_tingle_items WHERE id=?", (item_id,)).fetchone())  # type: ignore[return-value]
    finally:
        if _conn is None:
            conn.close()


def _is_primary_key_collision(exc: sqlite3.IntegrityError) -> bool:
    """True for a primary-key ID collision. Python < 3.11 has no sqlite_errorname, so fall back to the message."""
    name = getattr(exc, "sqlite_errorname", "")
    if name:
        return name == "SQLITE_CONSTRAINT_PRIMARYKEY"
    text = str(exc)
    return text.startswith("UNIQUE constraint failed:") and text.rstrip().endswith(".id")


def _steps(conn: sqlite3.Connection, goal_id: str) -> list[dict[str, Any]]:
    rows = conn.execute("""SELECT s.*, w.status AS wait_status FROM hermes_tingle_steps s
        LEFT JOIN hermes_tingle_items w ON w.id=s.wait_item_id
        WHERE s.goal_id=? ORDER BY s.position""", (goal_id,)).fetchall()
    steps = []
    for row in rows:
        complete = row['wait_status'] == 'done' if row['wait_item_id'] else bool(row['done'])
        skipped = bool(row['skipped']) or bool(row['wait_item_id'] and row['wait_status'] == 'auto_closed')
        # resolved: no longer blocks the goal - complete, explicitly skipped, or
        # auto-closed as stale. A dismissed reply-wait remains unresolved.
        steps.append({**dict(row), 'complete': complete, 'skipped': skipped,
                      'resolved': complete or skipped})
    return steps


def _documents(conn: sqlite3.Connection, item_id: str) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM hermes_tingle_documents WHERE item_id=? ORDER BY position",
                        (item_id,)).fetchall()
    return [dict(row) for row in rows]


def add_document(item_id: str, *, name: str, valid_until: str = '', note: str = '') -> dict[str, Any]:
    """Track a paperwork document an item needs: a form, certificate or proof with an
    optional validity window (e.g. a certificate only accepted when recent)."""
    item_id = resolve_item_id(item_id)
    if not (name or '').strip():
        raise ValueError("document name must be non-empty")
    conn = _connect()
    try:
        now_s = _now()
        position = (conn.execute("SELECT COALESCE(MAX(position),0)+1 FROM hermes_tingle_documents WHERE item_id=?",
                                 (item_id,)).fetchone()[0])
        conn.execute("INSERT INTO hermes_tingle_documents (item_id,position,name,state,valid_until,note,created_at,updated_at) VALUES (?,?,?,'needed',?,?,?,?)",
                     (item_id, position, name.strip(), _stamp(valid_until, field='valid_until') if valid_until else None, note or '', now_s, now_s))
        conn.execute("UPDATE hermes_tingle_items SET reminder_revision=reminder_revision+1,updated_at=? WHERE id=?", (now_s, item_id))
        _touch_retirement_activity(conn, item_id, now_s)
        conn.commit()
        return next(d for d in _documents(conn, item_id) if d['position'] == position)
    finally:
        conn.close()


def update_document(item_id: str, *, position: int, state: str = '', valid_until: str = '', note: str = '') -> dict[str, Any]:
    """Move a document along its lifecycle (needed -> collected -> submitted -> accepted/rejected)
    or set its validity window. The item itself stays open; documents are sub-state."""
    item_id = resolve_item_id(item_id)
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM hermes_tingle_documents WHERE item_id=? AND position=?",
                           (item_id, position)).fetchone()
        if row is None:
            raise ValueError(f"no document {position} on {item_id}")
        if state and state not in ("needed", "collected", "submitted", "accepted", "rejected"):
            raise ValueError("document state must be needed, collected, submitted, accepted or rejected; 'expired' is set only by the validity sweep")
        now_s = _now()
        new_valid_until = _stamp(valid_until, field='valid_until') if valid_until else row["valid_until"]
        progress_changed = ((bool(state) and state != row["state"])
                            or (bool(valid_until) and new_valid_until != row["valid_until"])
                            or (bool(note) and note != row["note"]))
        if state:
            conn.execute("UPDATE hermes_tingle_documents SET state=? WHERE item_id=? AND position=?",
                         (state, item_id, position))
        if valid_until:
            conn.execute("UPDATE hermes_tingle_documents SET valid_until=? WHERE item_id=? AND position=?",
                         (_stamp(valid_until, field='valid_until'), item_id, position))
        if note:
            conn.execute("UPDATE hermes_tingle_documents SET note=? WHERE item_id=? AND position=?",
                         (note, item_id, position))
        conn.execute("UPDATE hermes_tingle_documents SET updated_at=? WHERE item_id=? AND position=?",
                     (now_s, item_id, position))
        conn.execute("UPDATE hermes_tingle_items SET reminder_revision=reminder_revision+1,updated_at=? WHERE id=?", (now_s, item_id))
        if progress_changed:
            _touch_retirement_activity(conn, item_id, now_s)
        conn.commit()
        return next(d for d in _documents(conn, item_id) if d['position'] == position)
    finally:
        conn.close()


def _requirements(conn: sqlite3.Connection, item_id: str) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM hermes_tingle_requirements WHERE item_id=? ORDER BY position",
                        (item_id,)).fetchall()
    return [dict(row) for row in rows]


def _options(conn: sqlite3.Connection, item_id: str) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM hermes_tingle_options WHERE item_id=? ORDER BY position",
                        (item_id,)).fetchall()
    return [dict(row) for row in rows]


def add_requirement(item_id: str, *, text: str, must: bool = False) -> dict[str, Any]:
    """Record a shopping requirement on an item (e.g. 'fits a 15-inch laptop').
    must=True marks a must-have; met tracks whether the current direction satisfies it."""
    item_id = resolve_item_id(item_id)
    if not (text or '').strip():
        raise ValueError("requirement text must be non-empty")
    conn = _connect()
    try:
        now_s = _now()
        position = (conn.execute("SELECT COALESCE(MAX(position),0)+1 FROM hermes_tingle_requirements WHERE item_id=?",
                                 (item_id,)).fetchone()[0])
        conn.execute("INSERT INTO hermes_tingle_requirements (item_id,position,text,must,met,created_at,updated_at) VALUES (?,?,?,?,0,?,?)",
                     (item_id, position, text.strip(), 1 if must else 0, now_s, now_s))
        conn.execute("UPDATE hermes_tingle_items SET reminder_revision=reminder_revision+1,updated_at=? WHERE id=?", (now_s, item_id))
        _touch_retirement_activity(conn, item_id, now_s)
        conn.commit()
        return next(r for r in _requirements(conn, item_id) if r['position'] == position)
    finally:
        conn.close()


def update_requirement(item_id: str, *, position: int, text: str = '',
                       must: bool | None = None, met: bool | None = None) -> dict[str, Any]:
    """Edit a requirement, flip its must-have flag, or mark it met/unmet. The item
    itself stays open; requirements are comparison criteria, not steps."""
    item_id = resolve_item_id(item_id)
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM hermes_tingle_requirements WHERE item_id=? AND position=?",
                           (item_id, position)).fetchone()
        if row is None:
            raise ValueError(f"no requirement {position} on {item_id}")
        now_s = _now()
        progress_changed = ((bool(text.strip()) and text.strip() != row["text"])
                            or (must is not None and int(bool(must)) != int(row["must"]))
                            or (met is not None and int(bool(met)) != int(row["met"])))
        if text.strip():
            conn.execute("UPDATE hermes_tingle_requirements SET text=? WHERE item_id=? AND position=?",
                         (text.strip(), item_id, position))
        if must is not None:
            conn.execute("UPDATE hermes_tingle_requirements SET must=? WHERE item_id=? AND position=?",
                         (1 if must else 0, item_id, position))
        if met is not None:
            conn.execute("UPDATE hermes_tingle_requirements SET met=? WHERE item_id=? AND position=?",
                         (1 if met else 0, item_id, position))
        conn.execute("UPDATE hermes_tingle_requirements SET updated_at=? WHERE item_id=? AND position=?",
                     (now_s, item_id, position))
        conn.execute("UPDATE hermes_tingle_items SET reminder_revision=reminder_revision+1,updated_at=? WHERE id=?", (now_s, item_id))
        if progress_changed:
            _touch_retirement_activity(conn, item_id, now_s)
        conn.commit()
        return next(r for r in _requirements(conn, item_id) if r['position'] == position)
    finally:
        conn.close()


def add_option(item_id: str, *, name: str, detail: str = '', price_text: str = '', note: str = '') -> dict[str, Any]:
    """Track a candidate purchase option for comparison. price_text is display text
    recorded by the assistant ('EUR 189 at example-shop'), never a verified or
    fetched price; live price watches belong to link_watch monitors."""
    item_id = resolve_item_id(item_id)
    if not (name or '').strip():
        raise ValueError("option name must be non-empty")
    conn = _connect()
    try:
        now_s = _now()
        position = (conn.execute("SELECT COALESCE(MAX(position),0)+1 FROM hermes_tingle_options WHERE item_id=?",
                                 (item_id,)).fetchone()[0])
        conn.execute("INSERT INTO hermes_tingle_options (item_id,position,name,detail,price_text,state,note,created_at,updated_at) VALUES (?,?,?,?,?,'candidate',?,?,?)",
                     (item_id, position, name.strip(), detail or '', price_text or '', note or '', now_s, now_s))
        conn.execute("UPDATE hermes_tingle_items SET reminder_revision=reminder_revision+1,updated_at=? WHERE id=?", (now_s, item_id))
        _touch_retirement_activity(conn, item_id, now_s)
        conn.commit()
        return next(o for o in _options(conn, item_id) if o['position'] == position)
    finally:
        conn.close()


def update_option(item_id: str, *, position: int, state: str = '', detail: str = '',
                  price_text: str = '', note: str = '') -> dict[str, Any]:
    """Move an option along candidate -> shortlisted -> rejected, or edit its detail,
    recorded price text or note. 'proposed' is never set by hand: propose_option
    sets it together with an approval request, so a proposed purchase always
    carries an explicit, decidable approval handle."""
    item_id = resolve_item_id(item_id)
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM hermes_tingle_options WHERE item_id=? AND position=?",
                           (item_id, position)).fetchone()
        if row is None:
            raise ValueError(f"no option {position} on {item_id}")
        if state and state not in ("candidate", "shortlisted", "rejected"):
            raise ValueError("option state must be candidate, shortlisted or rejected; 'proposed' is set only by propose_option with its approval request")
        now_s = _now()
        progress_changed = any(
            value and value != row[column]
            for column, value in (("state", state), ("detail", detail), ("price_text", price_text), ("note", note))
        )
        if state:
            conn.execute("UPDATE hermes_tingle_options SET state=? WHERE item_id=? AND position=?",
                         (state, item_id, position))
        if detail:
            conn.execute("UPDATE hermes_tingle_options SET detail=? WHERE item_id=? AND position=?",
                         (detail, item_id, position))
        if price_text:
            conn.execute("UPDATE hermes_tingle_options SET price_text=? WHERE item_id=? AND position=?",
                         (price_text, item_id, position))
        if note:
            conn.execute("UPDATE hermes_tingle_options SET note=? WHERE item_id=? AND position=?",
                         (note, item_id, position))
        conn.execute("UPDATE hermes_tingle_options SET updated_at=? WHERE item_id=? AND position=?",
                     (now_s, item_id, position))
        conn.execute("UPDATE hermes_tingle_items SET reminder_revision=reminder_revision+1,updated_at=? WHERE id=?", (now_s, item_id))
        if progress_changed:
            _touch_retirement_activity(conn, item_id, now_s)
        conn.commit()
        return next(o for o in _options(conn, item_id) if o['position'] == position)
    finally:
        conn.close()


_BOOKING_KINDS = ("flight", "train", "lodging", "rental", "event", "other")
_BOOKING_STATES = ("researched", "booked", "confirmed", "cancelled")


def _bookings(conn: sqlite3.Connection, item_id: str) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM hermes_tingle_bookings WHERE item_id=? ORDER BY position",
                        (item_id,)).fetchall()
    return [dict(row) for row in rows]


def _link_cancel_item(item_id: str, booking_name: str, cancel_by: str, now_s: str) -> str:
    """Create the hard-deadline item guarding a booking's cancellation window. A missed
    window becomes a missed item (FT-03 semantics): the obligation continues on new terms."""
    linked = add_item(
        title=f"Cancel {booking_name} - window closes",
        summary=f"Cancellation window for a booking on {item_id}.",
        due_at=cancel_by,
        hard_deadline=True,
        source_type="manual",
        now=now_s,
    )
    return str(linked["id"])


def add_booking(item_id: str, *, kind: str = 'other', name: str, detail: str = '',
                cancel_by: str = '', confirmation_ref: str = '', note: str = '') -> dict[str, Any]:
    """Track a trip booking (flight, train, lodging, ...) as item sub-state. Recording a
    booking never books anything. confirmation_ref is recorded text, never verified against
    a provider. A cancel_by creates a linked hard-deadline item so the existing alert
    engine guards the window; a missed window is a missed item, not a closed one."""
    item_id = resolve_item_id(item_id)
    if not (name or '').strip():
        raise ValueError("booking name must be non-empty")
    if kind not in _BOOKING_KINDS:
        raise ValueError(f"booking kind must be one of {', '.join(_BOOKING_KINDS)}")
    cancel_dt = _parse_time(cancel_by, field="cancel_by") if (cancel_by or '').strip() else None
    conn = _connect()
    try:
        now_s = _now()
        position = (conn.execute("SELECT COALESCE(MAX(position),0)+1 FROM hermes_tingle_bookings WHERE item_id=?",
                                 (item_id,)).fetchone()[0])
        cancel_item_id = None
        if cancel_dt is not None:
            cancel_item_id = _link_cancel_item(item_id, name.strip(), cancel_dt.isoformat(timespec="seconds"), now_s)
        conn.execute("INSERT INTO hermes_tingle_bookings (item_id,position,kind,name,detail,state,cancel_by,cancel_item_id,confirmation_ref,note,created_at,updated_at) VALUES (?,?,?,?,?,'researched',?,?,?,?,?,?)",
                     (item_id, position, kind, name.strip(), detail or '',
                      cancel_dt.isoformat(timespec="seconds") if cancel_dt else None,
                      cancel_item_id, confirmation_ref or '', note or '', now_s, now_s))
        conn.execute("UPDATE hermes_tingle_items SET reminder_revision=reminder_revision+1,updated_at=? WHERE id=?", (now_s, item_id))
        _touch_retirement_activity(conn, item_id, now_s)
        conn.commit()
        return next(b for b in _bookings(conn, item_id) if b['position'] == position)
    finally:
        conn.close()


def update_booking(item_id: str, *, position: int, state: str = '', detail: str = '',
                   cancel_by: str = '', confirmation_ref: str = '', note: str = '') -> dict[str, Any]:
    """Move a booking along researched -> booked -> confirmed (or cancelled) and edit its
    details. Changing cancel_by moves the linked window item's deadline; cancelling the
    booking completes that item so it stops alerting."""
    item_id = resolve_item_id(item_id)
    cancel_dt = _parse_time(cancel_by, field="cancel_by") if (cancel_by or '').strip() else None
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM hermes_tingle_bookings WHERE item_id=? AND position=?",
                           (item_id, position)).fetchone()
        if row is None:
            raise ValueError(f"no booking {position} on {item_id}")
        if state and state not in _BOOKING_STATES:
            raise ValueError(f"booking state must be one of {', '.join(_BOOKING_STATES)}")
        booking = dict(row)
        now_s = _now()
        # Linked-item operations first, on their own connections, before any local write
        # starts an implicit transaction on this one (sqlite RESERVED-lock conflict).
        cancel_item_id = booking.get("cancel_item_id")
        if cancel_dt is not None and state != 'cancelled' and booking.get('state') != 'cancelled':
            if cancel_item_id:
                linked = get_item(str(cancel_item_id))
                if linked and linked["status"] in ("open", "waiting", "blocked"):
                    update_item(str(cancel_item_id), due_at=cancel_dt.isoformat(timespec="seconds"),
                                next_check_at=_next_check(now=_now(now_s), due_at=cancel_dt,
                                                          owner=linked['owner'], priority=linked['priority'],
                                                          status=linked['status']).isoformat(timespec='seconds'))
            else:
                cancel_item_id = _link_cancel_item(item_id, str(booking["name"]), cancel_dt.isoformat(timespec="seconds"), now_s)
        if state == "cancelled" and cancel_item_id:
            linked = get_item(str(cancel_item_id))
            if linked and linked["status"] in ("open", "waiting", "blocked"):
                complete_item(str(cancel_item_id), note="booking cancelled", now=now_s)
        progress_changed = any(
            value and value != booking[column]
            for column, value in (("state", state), ("detail", detail),
                                  ("confirmation_ref", confirmation_ref), ("note", note))
        ) or (cancel_dt is not None and (
            cancel_dt.isoformat(timespec="seconds") != booking.get("cancel_by")
            or cancel_item_id != booking.get("cancel_item_id")
        ))
        if state:
            conn.execute("UPDATE hermes_tingle_bookings SET state=? WHERE item_id=? AND position=?",
                         (state, item_id, position))
        if detail:
            conn.execute("UPDATE hermes_tingle_bookings SET detail=? WHERE item_id=? AND position=?",
                         (detail, item_id, position))
        if confirmation_ref:
            conn.execute("UPDATE hermes_tingle_bookings SET confirmation_ref=? WHERE item_id=? AND position=?",
                         (confirmation_ref, item_id, position))
        if note:
            conn.execute("UPDATE hermes_tingle_bookings SET note=? WHERE item_id=? AND position=?",
                         (note, item_id, position))
        if cancel_dt is not None:
            conn.execute("UPDATE hermes_tingle_bookings SET cancel_by=?, cancel_item_id=? WHERE item_id=? AND position=?",
                         (cancel_dt.isoformat(timespec="seconds"), cancel_item_id, item_id, position))
        conn.execute("UPDATE hermes_tingle_bookings SET updated_at=? WHERE item_id=? AND position=?",
                     (now_s, item_id, position))
        conn.execute("UPDATE hermes_tingle_items SET reminder_revision=reminder_revision+1,updated_at=? WHERE id=?", (now_s, item_id))
        if progress_changed:
            _touch_retirement_activity(conn, item_id, now_s)
        conn.commit()
        return next(b for b in _bookings(conn, item_id) if b['position'] == position)
    finally:
        conn.close()


def propose_option(item_id: str, *, position: int, approver: Any = None,
                   requested_by: str = '', now: Any = None) -> dict[str, Any]:
    """Propose one option as the purchase to make. This only records the proposal:
    it never buys, books or pays anything. The option becomes 'proposed' and a
    pending 'purchase' approval request is created bound to the exact option and
    recorded price text (parameters_hash), so the proposal is decided through the
    normal /tingle approve flow. Any earlier still-pending purchase approval for
    the same item is superseded (expired) so two proposals never await decision
    at once. The approval decision itself is recorded on the request; the plugin
    takes no further action on approval."""
    item_id = resolve_item_id(item_id)
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM hermes_tingle_options WHERE item_id=? AND position=?",
                           (item_id, position)).fetchone()
        if row is None:
            raise ValueError(f"no option {position} on {item_id}")
        if row['state'] not in ('candidate', 'shortlisted'):
            raise ValueError("only a candidate or shortlisted option can be proposed; move a rejected or already-decided option back to shortlisted first")
        option = dict(row)
        now_s = _stamp(_now(now), field="now")
        conn.execute("UPDATE approval_requests SET status='expired' WHERE action_type='purchase' AND target=? AND status='pending'",
                     (item_id,))
        conn.execute("UPDATE hermes_tingle_options SET state='proposed', updated_at=? WHERE item_id=? AND position=?",
                     (now_s, item_id, position))
        conn.execute("UPDATE hermes_tingle_items SET reminder_revision=reminder_revision+1,updated_at=? WHERE id=?", (now_s, item_id))
        _touch_retirement_activity(conn, item_id, now_s)
        conn.commit()
    finally:
        conn.close()
    approval = request_approval(
        "purchase",
        item_id,
        {"option_position": position, "option_name": option["name"], "price_text": option["price_text"]},
        requested_by=requested_by,
        approver=approver,
        now=now,
    )
    conn = _connect()
    try:
        return {"option": next(o for o in _options(conn, item_id) if o['position'] == position),
                "approval": approval}
    finally:
        conn.close()


def expire_documents(now: str | None = None) -> list[dict[str, Any]]:
    """Mark documents whose validity window has passed as expired. An expired document
    leaves the item open - it must be re-collected, it does not close anything."""
    cutoff = now or _now()
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM hermes_tingle_documents WHERE valid_until IS NOT NULL AND state IN ('needed','collected')").fetchall()
        cutoff_dt = _now(cutoff)
        rows = [row for row in rows if _parse_time(row['valid_until'], field='valid_until') < cutoff_dt]
        now_s = _now()
        for row in rows:
            conn.execute("UPDATE hermes_tingle_documents SET state='expired', updated_at=? WHERE item_id=? AND position=?",
                         (now_s, row['item_id'], row['position']))
            conn.execute("UPDATE hermes_tingle_items SET reminder_revision=reminder_revision+1,updated_at=? WHERE id=?", (now_s, row['item_id']))
        conn.commit()
        return [dict(row) for row in rows]
    finally:
        conn.close()


@serialized("reminders")
def add_step(goal_id: str, *, title: str, why: str, wait_item_id: str = '') -> dict[str, Any]:
    goal_id = resolve_item_id(goal_id)
    wait_item_id = resolve_item_id(wait_item_id)
    goal_id, wait_item_id = str(goal_id).upper(), str(wait_item_id or '').upper()
    title, why = str(title or '').strip(), str(why or '').strip()
    if not title or len(title) > 180 or not why or len(why) > 1200:
        raise ValueError('step title and why are required (max 180 and 1200 characters)')
    conn = _connect()
    try:
        conn.execute('BEGIN IMMEDIATE')
        goal = conn.execute('SELECT * FROM hermes_tingle_items WHERE id=?', (goal_id,)).fetchone()
        if not goal or goal['status'] not in _ACTIVE_STATUSES:
            raise ValueError('goal must be an active item')
        if goal['completion_signal'] == 'source_email_reply_received' or conn.execute('SELECT 1 FROM hermes_tingle_steps WHERE wait_item_id=?', (goal_id,)).fetchone():
            raise ValueError('a reply-wait cannot be a goal')
        if wait_item_id:
            wait = conn.execute('SELECT * FROM hermes_tingle_items WHERE id=?', (wait_item_id,)).fetchone()
            if not wait or wait_item_id == goal_id or wait['completion_signal'] != 'source_email_reply_received' or wait['status'] != 'waiting':
                raise ValueError('wait_item_id must identify a separate active reply-wait')
            if conn.execute('SELECT 1 FROM hermes_tingle_steps WHERE wait_item_id=? OR goal_id=?', (wait_item_id, wait_item_id)).fetchone():
                raise ValueError('wait item is already linked or is a goal')
        position = conn.execute('SELECT COALESCE(MAX(position),0)+1 FROM hermes_tingle_steps WHERE goal_id=?', (goal_id,)).fetchone()[0]
        conn.execute('INSERT INTO hermes_tingle_steps(goal_id,position,title,why,wait_item_id) VALUES(?,?,?,?,?)', (goal_id,position,title,why,wait_item_id or None))
        now_s = _now().isoformat(timespec='seconds')
        conn.execute('UPDATE hermes_tingle_items SET updated_at=? WHERE id=?', (now_s, goal_id))
        _touch_retirement_activity(conn, goal_id, now_s)
        conn.commit()
        return _steps(conn, goal_id)[-1]
    finally:
        conn.close()


@serialized("reminders")
def complete_step(goal_id: str, position: int) -> dict[str, Any]:
    goal_id = resolve_item_id(goal_id)
    goal_id = str(goal_id).upper()
    if isinstance(position, bool) or not isinstance(position, int) or position < 1:
        raise ValueError('step_position must be a positive integer')
    conn = _connect()
    try:
        conn.execute('BEGIN IMMEDIATE')
        goal = conn.execute('SELECT status FROM hermes_tingle_items WHERE id=?', (goal_id,)).fetchone()
        steps = _steps(conn, goal_id)
        step = next((s for s in steps if s['position'] == position), None)
        if not goal or goal['status'] not in _ACTIVE_STATUSES or not step:
            raise ValueError('active goal and step not found')
        if step['wait_item_id']:
            raise ValueError('complete the linked reply-wait item instead')
        if step['resolved'] or any(not s['resolved'] for s in steps if s['position'] < position):
            raise ValueError('complete steps in order')
        conn.execute('UPDATE hermes_tingle_steps SET done=1 WHERE goal_id=? AND position=?', (goal_id,position))
        now_s = _now().isoformat(timespec='seconds')
        conn.execute('UPDATE hermes_tingle_items SET updated_at=? WHERE id=?', (now_s, goal_id))
        _touch_retirement_activity(conn, goal_id, now_s)
        conn.commit()
        return next(s for s in _steps(conn, goal_id) if s['position'] == position)
    finally:
        conn.close()


@serialized("reminders")
def skip_step(goal_id: str, position: int) -> dict[str, Any]:
    """Explicitly skip an unresolved step; never completes the goal by itself.

    Skipping is the user's recovery path when a step is moot - for example after
    dismissing its linked reply-wait, which leaves the step unresolved (the wait
    ended without the reply, so the step is not 'complete'). Skipped steps count
    as resolved for ordering and goal closing, but stay distinguishable from
    completed work in every listing."""
    goal_id = resolve_item_id(goal_id)
    goal_id = str(goal_id).upper()
    if isinstance(position, bool) or not isinstance(position, int) or position < 1:
        raise ValueError('step_position must be a positive integer')
    conn = _connect()
    try:
        conn.execute('BEGIN IMMEDIATE')
        goal = conn.execute('SELECT status FROM hermes_tingle_items WHERE id=?', (goal_id,)).fetchone()
        steps = _steps(conn, goal_id)
        step = next((s for s in steps if s['position'] == position), None)
        if not goal or goal['status'] not in _ACTIVE_STATUSES or not step:
            raise ValueError('active goal and step not found')
        if step['complete']:
            raise ValueError('step is already complete')
        if step['skipped']:
            raise ValueError('step is already skipped')
        conn.execute('UPDATE hermes_tingle_steps SET skipped=1 WHERE goal_id=? AND position=?', (goal_id, position))
        now_s = _now().isoformat(timespec='seconds')
        conn.execute('UPDATE hermes_tingle_items SET updated_at=? WHERE id=?', (now_s, goal_id))
        _touch_retirement_activity(conn, goal_id, now_s)
        conn.commit()
        return next(s for s in _steps(conn, goal_id) if s['position'] == position)
    finally:
        conn.close()


def _guard_close(conn: sqlite3.Connection, item_id: str) -> None:
    if any(not s['resolved'] for s in _steps(conn, item_id)):
        raise ValueError('finish all goal steps before closing the goal')


def _guard_reopen_linked(conn: sqlite3.Connection, item_id: str) -> None:
    link = conn.execute("SELECT goal_id FROM hermes_tingle_steps WHERE wait_item_id=?", (item_id,)).fetchone()
    if not link:
        return
    parent = conn.execute("SELECT status FROM hermes_tingle_items WHERE id=?", (link["goal_id"],)).fetchone()
    if not parent or parent["status"] not in _ACTIVE_STATUSES:
        raise ValueError("cannot reopen a linked step under a closed goal")


def _attach_case(conn, item):
    row=conn.execute('SELECT canonical_id,merge_id FROM case_merge_members WHERE item_id=?',(item['id'],)).fetchone()
    if row:
        item['case_merge']={'canonical_id':row[0],'merge_id':row[1], 'undo_command':'/tingle undo merge '+row[1]}
        if item['id']==row[0]:
            item['case_members']=[dict(r) for r in conn.execute('SELECT i.* FROM hermes_tingle_items i JOIN case_merge_members m ON m.item_id=i.id WHERE m.canonical_id=? ORDER BY i.id',(item['id'],))]


def _guard_merged_member(conn, iid):
    r=conn.execute('SELECT canonical_id FROM case_merge_members WHERE item_id=?',(iid,)).fetchone()
    if r and r[0]!=iid: raise ValueError('This historical member is merged. Review the canonical item or undo the merge before editing it.')


def get_item(item_id: str) -> dict[str, Any] | None:
    item_id = resolve_item_id(item_id)
    conn = _connect()
    try:
        result = _public(conn.execute("SELECT * FROM hermes_tingle_items WHERE id=?", (str(item_id).upper(),)).fetchone())
        if result is not None:
            _attach_case(conn, result)
            steps = _steps(conn, result['id'])
            if steps:
                result['steps'] = steps
            documents = _documents(conn, result['id'])
            if documents:
                result['documents'] = documents
            requirements = _requirements(conn, result['id'])
            if requirements:
                result['requirements'] = requirements
            options = _options(conn, result['id'])
            if options:
                result['options'] = options
            bookings = _bookings(conn, result['id'])
            if bookings:
                result['bookings'] = bookings
        return result
    finally:
        conn.close()


def list_items(*, status: str = "active", limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 100))
    offset = int(offset)
    if offset < 0:
        raise ValueError('offset must be non-negative')
    conn = _connect()
    try:
        if status in {"active", "unresolved"}:
            states = "'open','waiting','blocked','missed'" if status == "unresolved" else "'open','waiting','blocked'"
            rows = conn.execute(
                f"""SELECT * FROM hermes_tingle_items WHERE status IN ({states})
                   ORDER BY CASE WHEN due_at IS NULL THEN 1 ELSE 0 END, due_at, next_check_at, id LIMIT ? OFFSET ?""",
                (limit, offset),
            ).fetchall()
        elif status in _STATUSES:
            rows = conn.execute(
                "SELECT * FROM hermes_tingle_items WHERE status=? ORDER BY updated_at DESC, id LIMIT ? OFFSET ?",
                (status, limit, offset),
            ).fetchall()
        elif status == "all":
            rows = conn.execute("SELECT * FROM hermes_tingle_items ORDER BY updated_at DESC, id LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        else:
            raise ValueError("status must be active, unresolved, all, or a known item status")
        result = [dict(row) for row in rows]
        for item in result:
            _attach_case(conn, item)
            steps = _steps(conn, item['id'])
            if steps:
                item['steps'] = steps
            documents = _documents(conn, item['id'])
            if documents:
                item['documents'] = documents
            requirements = _requirements(conn, item['id'])
            if requirements:
                item['requirements'] = requirements
            options = _options(conn, item['id'])
            if options:
                item['options'] = options
            bookings = _bookings(conn, item['id'])
            if bookings:
                item['bookings'] = bookings
        return result
    finally:
        conn.close()


def link_watch(item_id: Any, watch_ref: Any, *, max_age_hours: Any = None, now: Any = None) -> dict[str, Any]:
    """FT-06: bind an item to an external monitor (a cron job name, a watch id, a
    URL watch - free-form reference, by convention 'cron:<job>' or 'url:<...>').
    The monitor itself lives outside Tingle; this records WHICH monitor serves
    the goal so its reports have somewhere to land."""
    item_id = resolve_item_id(item_id)
    watch_ref = str(watch_ref or "").strip()
    if not watch_ref or len(watch_ref) > 200:
        raise ValueError("watch_ref must be 1-200 characters, e.g. cron:price-check or url:https://...")
    if max_age_hours in (None, ""):
        max_age = None
    else:
        if isinstance(max_age_hours, bool) or not isinstance(max_age_hours, int) or not 1 <= max_age_hours <= 720:
            raise ValueError("watch max_age_hours must be an integer between 1 and 720")
        max_age = max_age_hours
    now_s = _stamp(now or datetime.now(timezone.utc), field="now")
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _guard_merged_member(conn,str(item_id).upper())
        row = conn.execute(
            "SELECT id FROM hermes_tingle_items WHERE id=? AND status IN ('open','waiting','blocked')",
            (str(item_id).upper(),),
        ).fetchone()
        if row is None:
            raise KeyError("active Hermes Tingle item not found")
        conn.execute(
            "UPDATE hermes_tingle_items SET reminder_revision=reminder_revision+1,watch_ref=?,watch_state=NULL,watch_updated_at=NULL,watch_max_age_hours=?,updated_at=? WHERE id=?",
            (watch_ref, max_age, now_s, str(item_id).upper()),
        )
        conn.commit()
        return dict(conn.execute("SELECT * FROM hermes_tingle_items WHERE id=?", (str(item_id).upper(),)).fetchone())  # type: ignore[return-value]
    finally:
        conn.close()


def report_watch(item_id: Any, state: Any, *, now: Any = None) -> dict[str, Any]:
    """FT-06: record a monitor's latest finding against its goal. Overwrites the
    previous state (the snapshot carries the current truth; history lives in the
    monitor's own cron output, not duplicated here)."""
    item_id = resolve_item_id(item_id)
    state = str(state or "").strip()
    if not state or len(state) > 500:
        raise ValueError("watch state must be 1-500 characters; summarize, never paste raw output")
    now_s = _stamp(now or datetime.now(timezone.utc), field="now")
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _guard_merged_member(conn,str(item_id).upper())
        row = conn.execute(
            "SELECT id,watch_ref FROM hermes_tingle_items WHERE id=? AND status IN ('open','waiting','blocked')",
            (str(item_id).upper(),),
        ).fetchone()
        if row is None:
            raise KeyError("active Hermes Tingle item not found")
        if not row["watch_ref"]:
            raise ValueError("item has no linked watch; link one first")
        conn.execute(
            "UPDATE hermes_tingle_items SET reminder_revision=reminder_revision+1,watch_state=?,watch_updated_at=?,updated_at=? WHERE id=?",
            (state, now_s, now_s, str(item_id).upper()),
        )
        conn.commit()
        return dict(conn.execute("SELECT * FROM hermes_tingle_items WHERE id=?", (str(item_id).upper(),)).fetchone())  # type: ignore[return-value]
    finally:
        conn.close()


def watch_staleness(item: dict[str, Any], *, now: Any = None) -> dict[str, Any]:
    """FT-06 staleness: how much a linked watch's current state can be trusted.

    Returns a dict with `state` in {"no_watch", "no_reports", "fresh", "stale",
    "unknown_cadence"}. A monitor that stops reporting must not keep looking
    current: when the operator or model declared an expected cadence
    (watch_max_age_hours), a report older than that is flagged stale. Without a
    declared cadence the age is still surfaced as unknown_cadence so reviews can
    judge rather than assume. Stale means "the monitor has not reported within
    its expected window" - never that the watched thing changed.
    """
    if not item.get("watch_ref"):
        return {"state": "no_watch"}
    updated = _parse_time(item.get("watch_updated_at"), field="watch_updated_at")
    if updated is None:
        return {"state": "no_reports"}
    now_dt = _now(now)
    age_hours = (now_dt - updated).total_seconds() / 3600.0
    result: dict[str, Any] = {"age_hours": round(age_hours, 2)}
    max_age = item.get("watch_max_age_hours")
    if max_age is None:
        result["state"] = "unknown_cadence"
    else:
        result["max_age_hours"] = max_age
        result["state"] = "stale" if age_hours > max_age else "fresh"
    return result


MAX_MISSED_OUTCOMES_PER_SWEEP = 100


def _insert_missed_occurrence(conn, row, due_dt, now_s):
    """One unresolved item per missed window, preserving its parent identity."""
    for _ in range(16):
        candidate = "HT-" + secrets.token_hex(3).upper()
        try:
            conn.execute(
                """INSERT INTO hermes_tingle_items
                   (id,title,summary,status,owner,priority,source_type,source_ref,source_url,
                    due_at,next_check_at,snoozed_until,origin_platform,origin_chat_id,origin_message_id,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (candidate, f"{row['title']} - missed occurrence",
                 f"Missed occurrence (due {due_dt.isoformat(timespec='seconds')}) of recurring item {row['id']}: {row['summary']}",
                 "missed", row["owner"], row["priority"], "recurrence",
                 f"occurrence:{row['id']}:{due_dt.isoformat(timespec='seconds')}", row["source_url"] or "",
                 due_dt.isoformat(timespec="seconds"), now_s, row["snoozed_until"],
                 row["origin_platform"], row["origin_chat_id"], row["origin_message_id"], now_s, now_s),
            )
            return candidate
        except sqlite3.IntegrityError as exc:
            if not _is_primary_key_collision(exc):
                raise
    raise RuntimeError("Unable to allocate a unique Tingle item ID")


def deadline_catchup_state(*, now: Any = None) -> dict[str, Any]:
    """Bounded catch-up can remain incomplete; never silently jump to the future."""
    conn = _connect()
    try:
        ids = [row[0] for row in conn.execute(
            """SELECT id FROM hermes_tingle_items WHERE status IN ('open','waiting','blocked')
               AND hard_deadline=1 AND due_at IS NOT NULL AND due_at<? ORDER BY due_at,id""",
            (_stamp(now or _now()),))]
        return {"complete": not ids, "pending_item_ids": ids}
    finally:
        conn.close()


@serialized("reminders")
def sweep_missed_deadlines(*, now: Any = None) -> list[dict[str, Any]]:
    """Record every elapsed hard window, bounded without discarding backlog.

    Up to 100 outcomes per sweep across all items. Recurring anchors advance only
    to the next unrecorded window, which may still be past. A later poll resumes
    from that persisted point. Missed means unresolved, never completed.
    """
    now_dt = _now(now)
    now_s = now_dt.isoformat(timespec="seconds")
    conn = _connect()
    closed = []
    remaining = MAX_MISSED_OUTCOMES_PER_SWEEP
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """SELECT * FROM hermes_tingle_items
               WHERE status IN ('open','waiting','blocked') AND hard_deadline=1
                 AND due_at IS NOT NULL AND due_at < ? ORDER BY due_at,id""", (now_s,)).fetchall()
        for row in rows:
            if not remaining:
                break
            item_id = row["id"]
            due_dt = _parse_time(row["due_at"], field="due_at")
            if row["recur_interval"]:
                anchor = _parse_time(row["recur_anchor"], field="recur_anchor") if row["recur_anchor"] else due_dt
                outcomes = []
                while due_dt < now_dt and remaining:
                    occurrence_id = _insert_missed_occurrence(conn, row, due_dt, now_s)
                    if not outcomes:
                        # The first missed copy is the SAME occurrence, not a new
                        # budget. Move its cycle identity so old receipts cannot
                        # charge the anchor after it advances to the next window.
                        conn.execute("""UPDATE hermes_tingle_items SET reminder_count=?,reminder_attempt_count=?,
                            reminder_budget_uncertain=?,reminder_revision=?,reminder_cycle=?,last_notified_at=?,
                            next_check_at=? WHERE id=?""",
                            (row["reminder_count"],row["reminder_attempt_count"],row["reminder_budget_uncertain"],
                             row["reminder_revision"],row["reminder_cycle"],row["last_notified_at"],
                             None if row["reminder_attempt_count"] >= 4 else (row["next_check_at"] or now_s), occurrence_id))
                    conn.execute("INSERT INTO recurrence_completions(item_id,occurrence_due_at,completed_at,note) VALUES(?,?,?,?)",
                                 (item_id, due_dt.isoformat(timespec="seconds"), now_s, "missed: cancellation window closed"))
                    outcomes.append({"id": item_id, "outcome": "advanced", "missed_due_at": due_dt.isoformat(timespec="seconds"), "occurrence_id": occurrence_id})
                    remaining -= 1
                    due_dt = _advance_recurrence(due_dt, int(row["recur_interval"]), str(row["recur_unit"]), due_dt, anchor)
                new_check = due_dt - timedelta(days=int(row["recur_lead_days"] or 0))
                if new_check <= now_dt:
                    new_check = due_dt
                conn.execute(
                    """UPDATE hermes_tingle_items SET due_at=?,next_check_at=?,reminder_count=0,reminder_attempt_count=0,reminder_budget_uncertain=0,reminder_cycle=lower(hex(randomblob(16))),
                       reminder_revision=reminder_revision+1,last_notified_at=NULL,updated_at=? WHERE id=?""",
                    (due_dt.isoformat(timespec="seconds"), new_check.isoformat(timespec="seconds"), now_s, item_id))
                for outcome in outcomes:
                    outcome["next_due_at"] = due_dt.isoformat(timespec="seconds")
                    outcome["catchup_pending"] = due_dt < now_dt
                closed.extend(outcomes)
            else:
                conn.execute(
                    """UPDATE hermes_tingle_items SET status='missed',completed_at=NULL,
                       reminder_revision=reminder_revision+1,
                       next_check_at=CASE WHEN reminder_attempt_count>=4 THEN NULL WHEN reminder_attempt_count=0 THEN ? ELSE next_check_at END,
                       updated_at=? WHERE id=?""", (now_s, now_s, item_id))
                conn.execute("INSERT INTO recurrence_completions(item_id,occurrence_due_at,completed_at,note) VALUES(?,?,?,?)",
                             (item_id, due_dt.isoformat(timespec="seconds"), now_s, "missed: cancellation window closed"))
                closed.append({"id": item_id, "outcome": "missed", "missed_due_at": due_dt.isoformat(timespec="seconds")})
                remaining -= 1
        conn.commit()
        return closed
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@serialized("sources")
def purge_source_retained(kind: str) -> int:
    """Clear Tingle source caches, not tracked items or Hermes session history."""
    kind = _validate_source_kind(kind, allow_legacy=True)
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        removed = _purge_source_retained(conn, kind)
        conn.commit()
        return removed
    finally:
        conn.close()


def _purge_source_retained(conn: sqlite3.Connection, kind: str, *, preserve_recent_body_reads: bool = False) -> int:
    removed = 0
    if kind == 'chat':
        removed += conn.execute('DELETE FROM hermes_tingle_chat_discovery_candidates').rowcount
        removed += conn.execute('DELETE FROM discovery_evaluation_ledger').rowcount
        removed += conn.execute('DELETE FROM discovery_evaluation_claims').rowcount
        # Cost reservations deliberately survive source revocation/purge.
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='private_behavior_cases'").fetchone():
        removed += conn.execute('DELETE FROM private_behavior_cases WHERE item_key IN '
                                '(SELECT id FROM hermes_tingle_items WHERE source_type=?)', (kind,)).rowcount
    if kind in ('document','chat'):
        removed += conn.execute('DELETE FROM source_seen WHERE source_type=?',(kind,)).rowcount
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='context_review_receipts'").fetchone():
            removed += conn.execute('DELETE FROM context_review_receipts WHERE kind=?',(kind,)).rowcount
    # Evidence identities are source-derived and can belong to any item type;
    # clear them on every source purge because the marker intentionally stores no type.
    conn.execute("UPDATE hermes_tingle_retirement SET evidence_key='' WHERE evidence_key<>''")
    # A purge alone does not change a grant generation. Bump a dedicated
    # counter so an in-flight collection's evidence fence fails closed too.
    conn.execute(
        """INSERT INTO plugin_meta(key,value) VALUES('retirement_evidence_epoch','1')
           ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1""")
    if kind == "email":
        for table in ("gmail_pending_messages", "gmail_unreadable", "email_candidates"):
            removed += conn.execute(f"DELETE FROM {table}").rowcount
        if preserve_recent_body_reads:
            # Ordinary enable/regrant/narrowing and account migration keep recent
            # legacy ID-only body-read blocks: they can only refuse a duplicate
            # read, never authorize one, and no account is guessed for them.
            # Expired rows drop out; explicit revocation still purges everything.
            body_read_cutoff = (_now() - timedelta(days=BODY_READ_DEDUPE_DAYS)).isoformat(timespec='seconds')
            removed += conn.execute("DELETE FROM email_body_reads WHERE read_at<?", (body_read_cutoff,)).rowcount
        else:
            removed += conn.execute("DELETE FROM email_body_reads").rowcount
        removed += conn.execute("DELETE FROM source_seen WHERE source_type='email'").rowcount
        removed += conn.execute("DELETE FROM plugin_meta WHERE key GLOB 'gmail_*' OR key IN ('trash_review_cursor','due_review_reply_cursor')").rowcount
    elif kind == "calendar":
        removed += conn.execute("DELETE FROM source_seen WHERE source_type='calendar'").rowcount
        removed += conn.execute("DELETE FROM calendar_transition_alerts").rowcount
        removed += conn.execute("DELETE FROM calendar_transition_receipts").rowcount
        removed += conn.execute("DELETE FROM plugin_meta WHERE key GLOB 'calendar_*'").rowcount
    return removed


class SourceOptInRevoked(ValueError):
    """A write was attempted for a source kind whose opt-in is not enabled."""


SOURCE_KINDS = ("email", "calendar", "chat")
# Removed source kinds. They can never be (re)enabled or read, but a recorded
# legacy grant stays revocable/purgeable so old installs keep an explicit cleanup path.
LEGACY_SOURCE_KINDS = ("document",)


def _validate_source_kind(kind: str, *, allow_legacy: bool = False) -> str:
    kind = str(kind or "").strip().casefold()
    if kind not in SOURCE_KINDS:
        if allow_legacy and kind in LEGACY_SOURCE_KINDS:
            return kind
        raise ValueError(f"source kind must be one of {', '.join(SOURCE_KINDS)}")
    return kind


@serialized("sources")
def enable_source(kind: str, *, lookback_days: int | None = None, enabled_by: str, note: str = "", expected_generation: int | None = None, account_key: str | None = None, calendar_context: bool | None = None) -> dict[str, Any]:
    """Record an explicit grant. Every changed grant clears prior source caches."""
    kind = _validate_source_kind(kind)
    if calendar_context is not None and (kind != 'calendar' or type(calendar_context) is not bool):
        raise ValueError('Calendar context must be a boolean for the calendar source')
    if kind == 'calendar' and calendar_context is None:
        calendar_context = True  # New explicit Calendar grants include meeting context by default.
    if lookback_days is not None:
        lookback_days = int(lookback_days)
        if not 1 <= lookback_days <= 30:
            raise ValueError("lookback_days must be between 1 and 30")
    if not (enabled_by or "").strip():
        raise ValueError("enable_source requires the authenticated principal that granted it")
    now_s = _now().isoformat(timespec="seconds")
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        prior = conn.execute("SELECT enabled,lookback_days,generation,calendar_context FROM hermes_tingle_source_optins WHERE kind=?", (kind,)).fetchone()
        if expected_generation is not None and (int(prior["generation"]) if prior else -1) != expected_generation:
            raise ValueError("Source consent changed during connection check; retry explicitly if still intended")
        if account_key is not None and (kind not in ("email", "calendar") or not isinstance(account_key,str) or not re.fullmatch(r"[a-f0-9]{64}", account_key)):
            raise ValueError("Invalid verified source account key")
        # Capture legacy email evidence BEFORE any purge below: a narrowing or
        # regrant purge must not erase the unbound cursor/queue progress the
        # account-migration disclosure depends on, nor the prior account binding.
        legacy_unbound_progress = None
        previous_binding = None
        prior_coverage_gap = None
        prior_coverage_notice = None
        if kind == "email":
            legacy_unbound_progress = conn.execute(
                "SELECT 1 FROM plugin_meta WHERE key IN ('gmail_history_id','gmail_last_full_reconcile_at') "
                "UNION ALL SELECT 1 FROM gmail_pending_messages LIMIT 1").fetchone()
            previous_binding = conn.execute("SELECT value FROM plugin_meta WHERE key='gmail_account_binding'").fetchone()
            # An ordinary enable/regrant/narrowing must not erase an
            # unacknowledged historical-coverage warning either: capture the
            # recorded gap and its notice state before the purge so they can be
            # restored below inside the same transaction. Only the documented
            # owner review (source gap-reviewed) or an explicit disable/revoke
            # clears them.
            row = conn.execute("SELECT value FROM plugin_meta WHERE key='gmail_coverage_gap'").fetchone()
            prior_coverage_gap = row[0] if row else None
            row = conn.execute("SELECT value FROM plugin_meta WHERE key='gmail_coverage_notice'").fetchone()
            prior_coverage_notice = row[0] if row else None
        # Regrants and every narrowing, including unbounded -> bounded, clear
        # prior caches inside the same transaction. Widening preserves progress.
        # Recent legacy body-read blocks survive ordinary enable/migration.
        if (not prior or not prior["enabled"] or
                (kind == 'calendar' and prior['calendar_context'] and calendar_context is False) or
                (lookback_days is not None and
                 (prior["lookback_days"] is None or lookback_days < prior["lookback_days"]))):
            _purge_source_retained(conn, kind, preserve_recent_body_reads=True)
        if kind == 'chat':
            # Every enable increments generation, even a widening or same grant.
            # Discovery labels from the previous generation must not survive.
            conn.execute('DELETE FROM hermes_tingle_chat_discovery_candidates')
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='context_review_receipts'").fetchone():
                conn.execute("DELETE FROM context_review_receipts WHERE kind='chat' AND json_extract(scope,'$.discovery_only')=1")
        conn.execute("""INSERT INTO hermes_tingle_source_optins
            (kind,enabled,lookback_days,enabled_by,note,enabled_at,disabled_at)
            VALUES (?,1,?,?,?,?,NULL) ON CONFLICT(kind) DO UPDATE SET enabled=1,
            lookback_days=excluded.lookback_days, enabled_by=excluded.enabled_by,
            note=excluded.note, enabled_at=excluded.enabled_at, disabled_at=NULL,
            generation=hermes_tingle_source_optins.generation+1""",
            (kind, lookback_days, str(enabled_by).strip(), str(note or ""), now_s))
        if kind == 'calendar' and calendar_context is not None:
            conn.execute("UPDATE hermes_tingle_source_optins SET calendar_context=? WHERE kind='calendar'", (int(calendar_context),))
        if kind == "email" and account_key is not None:
            row = conn.execute("SELECT generation FROM hermes_tingle_source_optins WHERE kind='email'").fetchone()
            try:
                previous = json.loads(previous_binding[0]) if previous_binding and previous_binding[0] else {}
                old_account = previous.get('account') if isinstance(previous,dict) else None
            except (ValueError,TypeError):
                old_account = None
            if old_account != account_key:
                _purge_source_retained(conn, "email", preserve_recent_body_reads=True)
                if legacy_unbound_progress and old_account is None:
                    conn.execute("INSERT OR REPLACE INTO plugin_meta VALUES('gmail_coverage_gap','unprovable_account_migration')")
            conn.execute("INSERT OR REPLACE INTO plugin_meta VALUES(?,?)", ("gmail_account_binding",json.dumps({"generation":row[0],"account":account_key})))
        if kind == 'calendar' and account_key is not None:
            previous = conn.execute("SELECT value FROM plugin_meta WHERE key='calendar_account_binding'").fetchone()
            try:
                old_key = json.loads(previous[0]).get('account') if previous else None
            except (ValueError, TypeError, AttributeError):
                old_key = None
            if old_key != account_key:
                conn.execute("DELETE FROM source_seen WHERE source_type='calendar'")
            generation = conn.execute("SELECT generation FROM hermes_tingle_source_optins WHERE kind='calendar'").fetchone()[0]
            conn.execute('INSERT OR REPLACE INTO plugin_meta VALUES(?,?)', ('calendar_account_binding', json.dumps({'generation':generation,'account':account_key})))
        if kind == "email" and prior_coverage_gap is not None:
            # A narrower, identical or wider ordinary regrant never proves the
            # missing historical interval, so restore the unacknowledged gap
            # marker the cache purge above deleted. The recorded cause is kept
            # verbatim - no coverage is claimed and no provenance invented -
            # and its notice state is restored with it so the notice cadence is
            # unchanged. Legacy cursor/queue progress stays purged.
            conn.execute("INSERT OR REPLACE INTO plugin_meta VALUES('gmail_coverage_gap',?)", (prior_coverage_gap,))
            if prior_coverage_notice is not None:
                conn.execute("INSERT OR REPLACE INTO plugin_meta VALUES('gmail_coverage_notice',?)", (prior_coverage_notice,))
        conn.commit()
        return dict(conn.execute("SELECT * FROM hermes_tingle_source_optins WHERE kind=?", (kind,)).fetchone())
    finally:
        conn.close()


@serialized("sources")
def disable_source(kind: str, *, note: str = "") -> dict[str, Any]:
    """Wait for in-flight operations, revoke/purge primary state, then clear shared audit.

    Both stores remain under the source lock; failure clearing audit is surfaced,
    never claimed as complete. Retry the revoke to finish cleanup.

    A read already started cannot be recalled. Once this returns, no operation
    serialized under the old grant can start another read or restore caches.
    """
    if str(kind).casefold() == 'meeting':
        return disable_calendar_context(note=note)
    kind = _validate_source_kind(kind, allow_legacy=True)
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""INSERT INTO hermes_tingle_source_optins(kind,enabled,note,disabled_at)
            VALUES (?,0,?,?) ON CONFLICT(kind) DO UPDATE SET enabled=0,
            note=excluded.note, disabled_at=excluded.disabled_at,
            generation=hermes_tingle_source_optins.generation+1""",
            (kind, str(note or ""), _now().isoformat(timespec="seconds")))
        _purge_source_retained(conn, kind)
        if kind == 'calendar':
            conn.execute("UPDATE hermes_tingle_source_optins SET calendar_context=0 WHERE kind='calendar'")
            conn.execute("UPDATE hermes_tingle_source_optins SET enabled=0,generation=generation+1,disabled_at=? WHERE kind='meeting'", (_now().isoformat(),))
        conn.commit()
        if kind == "email":
            try:
                from . import hermes_tingle_body_audit as body_audit
            except ImportError:
                import hermes_tingle_body_audit as body_audit
            body_audit.clear()
        return dict(conn.execute("SELECT * FROM hermes_tingle_source_optins WHERE kind=?", (kind,)).fetchone())
    finally:
        conn.close()


def list_source_optins() -> list[dict[str, Any]]:
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM hermes_tingle_source_optins WHERE kind != 'meeting' ORDER BY kind").fetchall()
        known = {row["kind"] for row in rows}
        result = [dict(row) for row in rows]
        for kind in SOURCE_KINDS:
            if kind not in known:
                result.append({"kind": kind, "enabled": 0, "lookback_days": None,
                               "enabled_by": "", "note": "", "enabled_at": None, "disabled_at": None})
        return sorted(result, key=lambda row: row["kind"])
    finally:
        conn.close()


def source_enabled(kind: str) -> bool:
    if str(kind).casefold() == 'meeting':
        return calendar_context_enabled()  # read-only legacy API alias, never a grant
    kind = _validate_source_kind(kind)
    conn = _connect()
    try:
        row = conn.execute("SELECT enabled FROM hermes_tingle_source_optins WHERE kind=?", (kind,)).fetchone()
        return bool(row and row["enabled"])
    finally:
        conn.close()



def calendar_context_enabled() -> bool:
    conn = _connect()
    try:
        row = conn.execute("SELECT enabled,calendar_context FROM hermes_tingle_source_optins WHERE kind='calendar'").fetchone()
        return bool(row and row['enabled'] and row['calendar_context'])
    finally:
        conn.close()


@serialized('sources')
def disable_calendar_context(*, note: str = '') -> dict[str, Any]:
    """Narrow old meeting scope without turning off the existing event grant."""
    conn = _connect()
    try:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute("UPDATE hermes_tingle_source_optins SET calendar_context=0,generation=generation+1 WHERE kind='calendar'")
        conn.execute("UPDATE hermes_tingle_source_optins SET enabled=0,generation=generation+1,note=?,disabled_at=? WHERE kind='meeting'", (note,_now().isoformat()))
        _purge_source_retained(conn, 'calendar')
        conn.commit()
        return dict(conn.execute("SELECT * FROM hermes_tingle_source_optins WHERE kind='calendar'").fetchone())
    finally:
        conn.close()


def source_lookback_cap(kind: str) -> int | None:
    kind = _validate_source_kind(kind)
    conn = _connect()
    try:
        row = conn.execute("SELECT lookback_days FROM hermes_tingle_source_optins WHERE kind=? AND enabled=1", (kind,)).fetchone()
        return int(row["lookback_days"]) if row and row["lookback_days"] is not None else None
    finally:
        conn.close()


def due_items(*, now: Any = None, limit: int = 50, all_due: bool = False) -> list[dict[str, Any]]:
    resume_automatic_reminders(now=now)
    sweep_missed_deadlines(now=now)
    expire_documents(now=_stamp(now or datetime.now(timezone.utc), field="now") if now else None)
    now_s = _stamp(now or datetime.now(timezone.utc), field="now")
    limit = max(1, min(int(limit), 100))
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT * FROM hermes_tingle_items
               WHERE status IN ('open','waiting','blocked','missed')
                 AND reminder_attempt_count < 4 AND reminder_budget_uncertain=0
                 AND NOT (status IN ('open','waiting','blocked') AND hard_deadline=1 AND due_at < ?)
                 AND next_check_at IS NOT NULL AND next_check_at <= ?
                 AND (snoozed_until IS NULL OR snoozed_until <= ?)
               ORDER BY next_check_at, due_at, id""" + ("" if all_due else " LIMIT ?"),
            (now_s, now_s, now_s) if all_due else (now_s, now_s, now_s, limit),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


@serialized("reminders")
def update_item(item_id: str, **updates: Any) -> dict[str, Any]:
    item_id = resolve_item_id(item_id)
    allowed = {"title", "summary", "status", "owner", "priority", "due_at", "next_check_at", "source_url", "completion_signal", "recur_interval", "recur_unit", "recur_lead_days", "hard_deadline", "review_plan"}
    if not updates or set(updates) - allowed:
        raise ValueError("update requires only supported fields")
    normalized: dict[str, Any] = {}
    for key, value in updates.items():
        if key == 'review_plan':
            try:
                from . import hermes_tingle_checkpoint as checkpoint
            except ImportError:
                import hermes_tingle_checkpoint as checkpoint
            normalized['email_review_plan'] = json.dumps(checkpoint.item_plan(value))
            continue
        if key == "title":
            value = str(value).strip()
            if not value or len(value) > 180:
                raise ValueError("title must be 1-180 characters")
        elif key == "summary":
            value = str(value or "").strip()
            if len(value) > 1200:
                raise ValueError("summary must be at most 1200 characters")
        elif key == "status" and (value not in _STATUSES or value in {"merged", "auto_closed"}):
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
        elif key == "recur_interval":
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
                raise ValueError("recur_interval must be a positive integer")
        elif key == "recur_unit":
            if value is not None and value not in _RECUR_UNITS:
                raise ValueError("recur_unit must be day, month, or year")
        elif key == "recur_lead_days":
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ValueError("recur_lead_days must be a non-negative integer")
        elif key == "hard_deadline":
            value = 1 if value in (True, 1, "1", "true", "yes") else 0
        normalized[key] = value
    now_dt = _now()
    now_s = now_dt.isoformat(timespec="seconds")
    conn = _connect()
    try:
        conn.execute('BEGIN IMMEDIATE')
        _guard_merged_member(conn,item_id)
        if conn.execute('SELECT 1 FROM case_merge_members WHERE item_id=?',(item_id,)).fetchone() and set(updates)&{'completion_signal','recur_interval','recur_unit','recur_lead_days','due_at','hard_deadline'}:
            raise ValueError('Undo the merge before changing source-check, deadline or recurrence semantics.')
        if normalized.get('status') in {'done', 'dismissed'}:
            _guard_close(conn, str(item_id).upper())
        current = conn.execute(
            "SELECT * FROM hermes_tingle_items WHERE id=?", (str(item_id).upper(),)
        ).fetchone()
        if current is None:
            raise KeyError("Hermes Tingle item not found")
        routine_fields = {
            "next_check_at", "due_at", "recur_anchor", "reminder_count",
            "reminder_attempt_count", "reminder_budget_uncertain", "reminder_cycle",
            "last_notified_at", "snoozed_until", "completed_at",
        }
        meaningful_update = any(
            key not in routine_fields and key in current.keys() and current[key] != value
            for key, value in normalized.items()
        )
        if current["status"] == "auto_closed" and normalized.get("status") not in {"open", "waiting", "blocked", "done", "dismissed"}:
            raise ValueError("this item was auto-closed; explicitly set an active status to reopen it")
        if current["status"] == "auto_closed" and normalized.get("status") in _ACTIVE_STATUSES:
            created_dt = _parse_time(current["created_at"], field="created_at")
            if created_dt is None or now_dt >= created_dt + timedelta(days=_MAX_ITEM_AGE_DAYS):
                raise ValueError("cannot reopen an item past its one-year tracking cap")
            _guard_reopen_linked(conn, str(item_id).upper())
        if normalized.get("hard_deadline", current["hard_deadline"]) and not normalized.get("due_at", current["due_at"]):
            raise ValueError("a hard deadline (cancellation window) requires a due_at anchor")
        effective_interval = normalized.get("recur_interval", current["recur_interval"])
        if effective_interval is not None:
            if not (normalized.get("recur_unit", current["recur_unit"])):
                raise ValueError("recur_unit must be day, month, or year")
            if not normalized.get("due_at", current["due_at"]):
                raise ValueError("recurring items require a due_at anchor")
            if current["recur_interval"] is None or "due_at" in normalized:
                # Starting a series or moving its deadline starts a new anchor.
                normalized["recur_anchor"] = normalized.get("due_at", current["due_at"])
        elif normalized.get("recur_lead_days") is not None:
            raise ValueError("recur_lead_days requires a recurrence")
        elif current["recur_interval"] is not None:
            normalized["recur_anchor"] = None
        signal = normalized.get("completion_signal", current["completion_signal"])
        state = normalized.get("status", current["status"])
        owner = normalized.get("owner", current["owner"])
        source_type = current["source_type"]
        source_ref = str(current["source_ref"] or "")
        reopening = current["status"] == "auto_closed" and state in _ACTIVE_STATUSES
        if reopening:
            if signal == "source_email_reply_received":
                if state != "waiting" or owner != "other":
                    raise ValueError("reopen a reply-wait as waiting on another person")
            created_dt = _parse_time(current["created_at"], field="created_at")
            if created_dt is None or now_dt >= created_dt + timedelta(days=_MAX_ITEM_AGE_DAYS):
                raise ValueError("cannot reopen an item past its one-year tracking cap")
            if signal == "source_email_checkpoint" and not normalized.get("next_check_at"):
                try:
                    saved_plan = json.loads(str(current["email_review_plan"] or "{}"))
                except (TypeError, ValueError):
                    saved_plan = {}
                if isinstance(saved_plan, dict) and saved_plan.get("review_at"):
                    try:
                        normalized["next_check_at"] = _stamp(saved_plan["review_at"], field="next_check_at")
                    except ValueError:
                        pass  # Preserve the plan but leave an invalid check unscheduled.
            normalized.update(
                completed_at=None, reminder_count=0, reminder_attempt_count=0,
                reminder_budget_uncertain=0, reminder_cycle=secrets.token_hex(16),
                last_notified_at=None, snoozed_until=None,
            )
        if (current['completion_signal'] == 'source_email_checkpoint'
                and signal != 'source_email_checkpoint'):
            raise ValueError('an email checkpoint cannot bypass its source verification gate')
        check_value = normalized.get("next_check_at", current["next_check_at"])
        item_key = str(item_id).upper()
        linked = conn.execute('SELECT goal_id FROM hermes_tingle_steps WHERE wait_item_id=?', (item_key,)).fetchone()
        if linked and signal != 'source_email_reply_received':
            raise ValueError('a linked wait must retain its reply completion signal')
        if linked and state != 'done':
            parent = conn.execute('SELECT status FROM hermes_tingle_items WHERE id=?', (linked['goal_id'],)).fetchone()
            if not parent or parent['status'] not in _ACTIVE_STATUSES:
                raise ValueError('cannot make a linked step incomplete under a closed goal')
        if signal == 'source_email_reply_received' and conn.execute('SELECT 1 FROM hermes_tingle_steps WHERE goal_id=?', (item_key,)).fetchone():
            raise ValueError('a goal cannot become a reply-wait')
        if signal == "source_email_reply_received":
            if source_type != "email" or not source_ref.startswith("gmail:") or "#" not in source_ref[6:] or not source_ref.rsplit("#", 1)[-1]:
                raise ValueError("reply-watch items require a Gmail thread and sent-message anchor")
            if state in _ACTIVE_STATUSES and (state != "waiting" or owner != "other"):
                raise ValueError("active reply-watch items must remain waiting on another person")
            next_dt = _parse_time(check_value, field="next_check_at")
            quieted = (current["status"] in _ACTIVE_STATUSES and current["completion_signal"] == signal
                       and current["reminder_attempt_count"] >= 4 and "next_check_at" not in normalized)
            if state in _ACTIVE_STATUSES and next_dt is None and not quieted:
                raise ValueError("active reply-watch items require a context-specific next_check_at")
            if "next_check_at" in normalized and next_dt is not None:
                interval = int((next_dt - now_dt.replace(microsecond=0)).total_seconds())
                # An overdue check is immediate, not a meaningful repeat interval.
                normalized["reply_interval_seconds"] = interval if interval > 0 else 2 * 24 * 60 * 60
            elif current["completion_signal"] != signal:
                interval = int((next_dt - now_dt).total_seconds()) if next_dt else 0
                normalized["reply_interval_seconds"] = interval if interval > 0 else 2 * 24 * 60 * 60
        if 'email_review_plan' in normalized:
            if signal != 'source_email_checkpoint':
                raise ValueError('review plans belong only to email checkpoints')
            plan = json.loads(normalized['email_review_plan'])
            normalized.setdefault('owner', plan['actor'])
            normalized.setdefault('status', 'waiting' if plan['actor'] == 'other' else 'open')
            if 'next_check_at' not in normalized:
                normalized['next_check_at'] = plan['review_at']
        if "next_check_at" in normalized and normalized["next_check_at"] is not None:
            normalized.update(reminder_count=0, reminder_attempt_count=0, reminder_budget_uncertain=0, reminder_cycle=secrets.token_hex(16), last_notified_at=None, snoozed_until=None)
        if reopening and not normalized.get("next_check_at") and signal not in {"source_email_reply_received", "source_email_checkpoint"}:
            effective_due = _parse_time(normalized.get("due_at", current["due_at"]), field="due_at") if normalized.get("due_at", current["due_at"]) else None
            normalized["next_check_at"] = _next_check(
                now=now_dt, due_at=effective_due, owner=owner,
                priority=normalized.get("priority", current["priority"]), status=state,
            ).isoformat(timespec="seconds")
            normalized.update(reminder_count=0, reminder_attempt_count=0,
                              reminder_budget_uncertain=0, reminder_cycle=secrets.token_hex(16))
        if meaningful_update or "due_at" in updates:
            effective_status = normalized.get("status", current["status"])
            effective_owner = normalized.get("owner", current["owner"])
            effective_due = _parse_time(normalized.get("due_at", current["due_at"]), field="due_at") if normalized.get("due_at", current["due_at"]) else None
            retirement_class = _retirement_class(
                due_at=effective_due, status=effective_status, owner=effective_owner,
                hard_deadline=bool(normalized.get("hard_deadline", current["hard_deadline"])),
                has_open_wait=_has_open_wait(conn, str(item_id).upper()),
            )
            created_dt = _parse_time(current["created_at"], field="created_at")
            if created_dt is None:
                raise ValueError("item creation time is missing; retirement cannot be calculated")
            prior = _retirement_for(conn, str(item_id).upper())
            last_activity_dt = now_dt if meaningful_update else (
                _parse_time(prior["last_activity_at"], field="last_activity_at")
                if prior and prior["last_activity_at"] else None
            ) or created_dt
            retire_fields = (
                retirement_class,
                last_activity_dt.isoformat(timespec="seconds"),
                _auto_close_at(
                    retirement_class=retirement_class, created_at=created_dt,
                    last_activity_at=last_activity_dt, due_at=effective_due,
                ).isoformat(timespec="seconds"),
            )
        else:
            retire_fields = None
        conn.execute("UPDATE hermes_tingle_items SET reminder_revision=reminder_revision+1 WHERE id=?", (item_id,))
        fields = ",".join(f"{key}=?" for key in normalized)
        cur = conn.execute(f"UPDATE hermes_tingle_items SET {fields}, updated_at=? WHERE id=?", (*normalized.values(), now_s, str(item_id).upper()))
        if not cur.rowcount:
            raise KeyError("Hermes Tingle item not found")
        if retire_fields is not None:
            conn.execute(
                """INSERT INTO hermes_tingle_retirement(item_id,retirement_class,last_activity_at,auto_close_at)
                   VALUES(?,?,?,?) ON CONFLICT(item_id) DO UPDATE SET
                   retirement_class=excluded.retirement_class,last_activity_at=excluded.last_activity_at,
                   auto_close_at=excluded.auto_close_at""",
                (str(item_id).upper(), *retire_fields),
            )
        if normalized.get('status') in ('done', 'dismissed'):
            conn.execute("UPDATE hermes_tingle_items SET email_review_plan='' WHERE id=?", (str(item_id).upper(),))
        if normalized.get("status") == "done":
            conn.execute("UPDATE hermes_tingle_items SET completed_at=? WHERE id=?", (now_s, str(item_id).upper()))
        elif normalized.get("status") in _ACTIVE_STATUSES and current["status"] == "auto_closed":
            conn.execute("UPDATE hermes_tingle_items SET completed_at=NULL WHERE id=?", (str(item_id).upper(),))
        conn.commit()
        return _public(conn.execute("SELECT * FROM hermes_tingle_items WHERE id=?", (str(item_id).upper(),)).fetchone())  # type: ignore[return-value]
    finally:
        conn.close()


@serialized("reminders")
def complete_item(item_id: str, *, note: str = "", now: Any = None) -> dict[str, Any]:
    item_id = resolve_item_id(item_id)
    note = str(note or "").strip()[:1200]
    now_dt = _now(now)
    now_s = now_dt.isoformat(timespec="seconds")
    conn = _connect()
    try:
        conn.execute('BEGIN IMMEDIATE')
        _guard_merged_member(conn,item_id)
        _guard_close(conn, str(item_id).upper())
        row = conn.execute(
            "SELECT * FROM hermes_tingle_items WHERE id=? AND status IN ('open','waiting','blocked','missed','auto_closed')",
            (str(item_id).upper(),),
        ).fetchone()
        recurring_eligible = bool(row is not None and row["recur_interval"])
        if recurring_eligible:
            created_dt = _parse_time(row["created_at"], field="created_at")
            if created_dt is None:
                raise ValueError("recurring item creation time is missing")
            recurring_eligible = now_dt < created_dt + timedelta(days=_MAX_ITEM_AGE_DAYS)
        if row is not None and row["status"] == "auto_closed" and recurring_eligible:
            _guard_reopen_linked(conn, str(item_id).upper())
        if row is not None and recurring_eligible:
            # Recurring obligation (FT-03): completing one occurrence records it in
            # recurrence_completions, advances the deadline calendar-aware from the
            # persisted original anchor (never from now, always at least one
            # interval, clamped months do not drift the series), resets the
            # reminder series, and re-applies the stored lead time so every
            # occurrence gets the same advance warning as the first.
            interval = int(row["recur_interval"])
            due_dt = _parse_time(row["due_at"], field="due_at") if row["due_at"] else None
            if due_dt is None:
                raise ValueError("recurring items require a due_at anchor")
            anchor_dt = _parse_time(row["recur_anchor"], field="recur_anchor") if row["recur_anchor"] else None
            new_due = _advance_recurrence(due_dt, interval, str(row["recur_unit"]), now_dt, anchor_dt)
            new_check = new_due - timedelta(days=int(row["recur_lead_days"] or 0))
            if new_check <= now_dt:
                new_check = new_due
            conn.execute(
                "INSERT INTO recurrence_completions(item_id,occurrence_due_at,completed_at,note) VALUES(?,?,?,?)",
                (str(item_id).upper(), due_dt.isoformat(timespec="seconds"), now_s, note),
            )
            created_dt = _parse_time(row["created_at"], field="created_at")
            if created_dt is None:
                raise ValueError("recurring item creation time is missing")
            retirement_class = _retirement_class(due_at=new_due, status=row["status"],
                                                owner=row["owner"], hard_deadline=bool(row["hard_deadline"]))
            retirement_at = _auto_close_at(retirement_class=retirement_class, created_at=created_dt,
                                           last_activity_at=now_dt, due_at=new_due).isoformat(timespec="seconds")
            reopen_status = ("waiting" if (row["completion_signal"] == "source_email_reply_received"
                                           or row["owner"] == "other") else "open")
            conn.execute(
                """UPDATE hermes_tingle_items SET status=CASE WHEN status='auto_closed' THEN ? ELSE status END,
                   due_at=?,next_check_at=?,reminder_count=0,reminder_attempt_count=0,reminder_budget_uncertain=0,
                   reminder_cycle=lower(hex(randomblob(16))),reminder_revision=reminder_revision+1,
                   last_notified_at=NULL,snoozed_until=NULL,updated_at=?,completed_at=NULL,
                   summary=CASE WHEN ?='' THEN summary ELSE ? END WHERE id=?""",
                (reopen_status, new_due.isoformat(timespec="seconds"), new_check.isoformat(timespec="seconds"),
                 now_s, note, note, str(item_id).upper()),
            )
            conn.execute(
                """INSERT INTO hermes_tingle_retirement(item_id,retirement_class,last_activity_at,auto_close_at)
                   VALUES(?,?,?,?) ON CONFLICT(item_id) DO UPDATE SET
                   retirement_class=excluded.retirement_class,last_activity_at=excluded.last_activity_at,
                   auto_close_at=excluded.auto_close_at""",
                (str(item_id).upper(), retirement_class, now_s, retirement_at),
            )
            conn.commit()
            return _public(conn.execute("SELECT * FROM hermes_tingle_items WHERE id=?", (str(item_id).upper(),)).fetchone())  # type: ignore[return-value]
        cur = conn.execute(
            "UPDATE hermes_tingle_items SET status='done',email_review_plan='',reminder_revision=reminder_revision+1,summary=CASE WHEN ?='' THEN summary ELSE ? END,updated_at=?,completed_at=? WHERE id=? AND status IN ('open','waiting','blocked','missed','auto_closed')",
            (note, note, now_s, now_s, str(item_id).upper()),
        )
        if not cur.rowcount:
            raise KeyError("Hermes Tingle item not found or already closed")
        wait_link = conn.execute("SELECT goal_id FROM hermes_tingle_steps WHERE wait_item_id=?", (str(item_id).upper(),)).fetchone()
        if wait_link:
            conn.execute("UPDATE hermes_tingle_items SET updated_at=? WHERE id=?", (now_s, wait_link["goal_id"]))
            _touch_retirement_activity(conn, str(wait_link["goal_id"]), now_s)
        conn.commit()
        return _public(conn.execute("SELECT * FROM hermes_tingle_items WHERE id=?", (str(item_id).upper(),)).fetchone())  # type: ignore[return-value]
    finally:
        conn.close()


def dismiss_item(item_id: str, *, now: Any = None) -> dict[str, Any]:
    item_id = resolve_item_id(item_id)
    return update_item(item_id, status="dismissed")


@serialized("reminders")
def snooze_item(item_id: str, *, until: Any, now: Any = None) -> dict[str, Any]:
    item_id = resolve_item_id(item_id)
    until_dt = _parse_time(until, field="until")
    now_dt = _now(now)
    if until_dt is None or until_dt <= now_dt:
        raise ValueError("snooze time must be in the future")
    now_s = now_dt.isoformat(timespec="seconds")
    until_s = until_dt.isoformat(timespec="seconds")
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM hermes_tingle_items WHERE id=?", (str(item_id).upper(),)).fetchone()
        if row is None or row["status"] not in (*_ACTIVE_STATUSES, "missed", "auto_closed"):
            raise KeyError("Hermes Tingle item not found or already closed")
        if row["status"] == "auto_closed":
            status = ("waiting" if row["completion_signal"] == "source_email_reply_received"
                      or row["owner"] == "other" else "open")
            _guard_reopen_linked(conn, str(item_id).upper())
        else:
            status = str(row["status"])
        due_dt = _parse_time(row["due_at"], field="due_at") if row["due_at"] else None
        created_dt = _parse_time(row["created_at"], field="created_at")
        if created_dt is None:
            raise ValueError("item creation time is missing; retirement cannot be calculated")
        if now_dt >= created_dt + timedelta(days=_MAX_ITEM_AGE_DAYS):
            raise ValueError("cannot reopen an item past its one-year tracking cap")
        retirement_class = _retirement_class(due_at=due_dt, status=status, owner=str(row["owner"]),
                                             hard_deadline=bool(row["hard_deadline"]),
                                             has_open_wait=_has_open_wait(conn, str(item_id).upper()))
        auto_close_at = _auto_close_at(retirement_class=retirement_class, created_at=created_dt,
                                       last_activity_at=now_dt, due_at=due_dt).isoformat(timespec="seconds")
        conn.execute(
            """UPDATE hermes_tingle_items SET status=?,snoozed_until=?,next_check_at=?,
               reminder_count=0,reminder_attempt_count=0,reminder_budget_uncertain=0,
               reminder_cycle=lower(hex(randomblob(16))),reminder_revision=reminder_revision+1,
               last_notified_at=NULL,updated_at=?,completed_at=CASE WHEN status='auto_closed' THEN NULL ELSE completed_at END
               WHERE id=?""",
            (status, until_s, until_s, now_s, str(item_id).upper()),
        )
        conn.execute(
            """INSERT INTO hermes_tingle_retirement(item_id,retirement_class,last_activity_at,auto_close_at)
               VALUES(?,?,?,?) ON CONFLICT(item_id) DO UPDATE SET
               retirement_class=excluded.retirement_class,last_activity_at=excluded.last_activity_at,
               auto_close_at=excluded.auto_close_at""",
            (str(item_id).upper(), retirement_class, now_s, auto_close_at),
        )
        conn.commit()
        return _public(conn.execute("SELECT * FROM hermes_tingle_items WHERE id=?", (str(item_id).upper(),)).fetchone())  # type: ignore[return-value]
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _evidence_epoch_value(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT value FROM plugin_meta WHERE key='retirement_evidence_epoch'").fetchone()
    return int(row[0]) if row else 0


def source_epoch_snapshot() -> dict[str, int | None]:
    """Opaque per-source grant epochs and purge counter for evidence writes."""
    conn = _connect()
    try:
        result = {row["kind"]: (int(row["generation"]) if row["generation"] is not None else None)
                  for row in conn.execute("SELECT kind,generation FROM hermes_tingle_source_optins")}
        result["purge"] = _evidence_epoch_value(conn)
        return result
    finally:
        conn.close()


def _source_epoch_matches(conn: sqlite3.Connection, expected: dict[str, int | None] | None) -> bool:
    if expected is None:
        return True
    current = {row["kind"]: (int(row["generation"]) if row["generation"] is not None else None)
               for row in conn.execute("SELECT kind,generation FROM hermes_tingle_source_optins")}
    current["purge"] = _evidence_epoch_value(conn)
    return current == expected


@serialized("reminders")
def auto_close_stale_items(*, now: Any = None, protected_ids: Iterable[str] = (),
                           protected_evidence: dict[str, str] | None = None,
                           allow_soft_close: bool = True,
                           source_epoch: dict[str, int | None] | None = None) -> list[str]:
    """Backfill retirement policy and atomically retire eligible items.

    The caller runs this only after a daily review. Incomplete source coverage may
    backfill dates but cannot soft-close; the absolute one-year cap always applies.
    All classes and close decisions are computed from one pre-sweep snapshot.
    Linked waits never remain active under a closed parent: a parent reaching its
    hard cap closes its linked waits in the same transaction. Source-derived
    evidence is ignored when the grant epoch changed during collection, so a
    concurrent revoke/purge cannot be undone by a stale snapshot.
    """
    now_dt = _now(now)
    now_s = now_dt.isoformat(timespec="seconds")
    protected = {str(item_id).upper() for item_id in protected_ids if isinstance(item_id, str)}
    evidence = {str(key).upper(): str(value)[:128] for key, value in (protected_evidence or {}).items()}
    active_sql = ",".join("?" for _ in (*_ACTIVE_STATUSES, "missed"))
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        # Check the grant epoch in the same write transaction that persists the
        # evidence marker: a revoke/purge cannot interleave between check and write.
        if not _source_epoch_matches(conn, source_epoch):
            protected, evidence, allow_soft_close = set(), {}, False
        rows = conn.execute(
            f"""SELECT i.*,r.retirement_class AS policy_class,
                      r.last_activity_at AS policy_last_activity_at,
                      r.auto_close_at AS policy_auto_close_at,
                      COALESCE(r.evidence_key,'') AS policy_evidence_key
               FROM hermes_tingle_items i
               LEFT JOIN hermes_tingle_retirement r ON r.item_id=i.id
               WHERE i.status IN ({active_sql}) ORDER BY i.id""",
            (*_ACTIVE_STATUSES, "missed"),
        ).fetchall()
        active_ids = {str(row["id"]).upper() for row in rows}
        link_rows = conn.execute(
            """SELECT s.goal_id,s.wait_item_id FROM hermes_tingle_steps s
               JOIN hermes_tingle_items w ON w.id=s.wait_item_id
               WHERE COALESCE(s.skipped,0)=0 AND COALESCE(s.done,0)=0
                 AND w.completion_signal='source_email_reply_received'
                 AND w.status IN ('open','waiting','blocked','missed')"""
        ).fetchall()
        parent_waits: dict[str, list[str]] = {}
        for link in link_rows:
            parent_id, wait_id = str(link["goal_id"]).upper(), str(link["wait_item_id"]).upper()
            if parent_id in active_ids and wait_id in active_ids:
                parent_waits.setdefault(parent_id, []).append(wait_id)
        decisions: list[dict[str, Any]] = []
        for row in rows:
            try:
                created_at = _parse_time(row["created_at"], field="created_at")
                if created_at is None:
                    continue
                due_dt = _parse_time(row["due_at"], field="due_at") if row["due_at"] else None
                last_activity = ((_parse_time(row["policy_last_activity_at"], field="last_activity_at")
                                  if row["policy_last_activity_at"] else None) or created_at)
            except ValueError:
                continue
            item_id = str(row["id"]).upper()
            retirement_class = _retirement_class(
                due_at=due_dt, status=str(row["status"]), owner=str(row["owner"]),
                hard_deadline=bool(row["hard_deadline"]), has_open_wait=item_id in parent_waits,
            )
            close_at = _auto_close_at(retirement_class=retirement_class, created_at=created_at,
                                      last_activity_at=last_activity, due_at=due_dt)
            key = evidence.get(item_id) if item_id in protected else None
            seen_bits = _decode_retirement_evidence(row["policy_evidence_key"])
            if key:
                seen_before, evidence_key = _remember_retirement_evidence(seen_bits, key)
                new_evidence = not seen_before
            else:
                new_evidence = False
                evidence_key = str(row["policy_evidence_key"] or "")
            at_hard_cap = now_dt >= created_at + timedelta(days=_MAX_ITEM_AGE_DAYS)
            leash_elapsed = now_dt >= close_at
            enough_deadline_reminders = (
                retirement_class != "deadline"
                or int(row["reminder_attempt_count"] or 0) >= _DEADLINE_REMINDERS_BEFORE_CLOSE
            )
            decisions.append({
                "id": item_id,
                "row": row,
                "retirement_class": retirement_class,
                "last_activity_at": last_activity.isoformat(timespec="seconds"),
                "auto_close_at": close_at.isoformat(timespec="seconds"),
                "evidence_key": evidence_key,
                "hard_cap": at_hard_cap,
                "close": at_hard_cap or (allow_soft_close and leash_elapsed
                                          and enough_deadline_reminders and not new_evidence),
            })
        forced_close_ids: set[str] = set()
        by_id = {decision["id"]: decision for decision in decisions}
        for parent_id, wait_ids in parent_waits.items():
            parent = by_id.get(parent_id)
            if parent is None or not parent["close"]:
                continue
            open_waits = [by_id.get(wait_id) for wait_id in wait_ids]
            still_open = [wait for wait in open_waits if wait is None or not wait["close"]]
            if not still_open:
                continue
            if parent["hard_cap"]:
                # The parent's absolute age cap also bounds its dependent waits.
                for wait_id, wait in zip(wait_ids, open_waits):
                    if wait is not None and not wait["close"]:
                        wait["close"] = True
                    elif wait is None:
                        forced_close_ids.add(wait_id)
            else:
                # Never leave an active dependency under a softly retired goal.
                parent["close"] = False
        closed: list[str] = []
        for decision in decisions:
            item_id = decision["id"]
            conn.execute(
                """INSERT INTO hermes_tingle_retirement
                   (item_id,retirement_class,last_activity_at,auto_close_at,evidence_key)
                   VALUES(?,?,?,?,?) ON CONFLICT(item_id) DO UPDATE SET
                   retirement_class=excluded.retirement_class,
                   last_activity_at=excluded.last_activity_at,
                   auto_close_at=excluded.auto_close_at,
                   evidence_key=excluded.evidence_key""",
                (item_id, decision["retirement_class"], decision["last_activity_at"],
                 decision["auto_close_at"], "" if decision["close"] else decision["evidence_key"]),
            )
            if not decision["close"]:
                continue
            result = conn.execute(
                """UPDATE hermes_tingle_items
                   SET status='auto_closed',next_check_at=NULL,snoozed_until=NULL,
                       reminder_count=0,reminder_attempt_count=0,reminder_budget_uncertain=0,
                       reminder_revision=reminder_revision+1,updated_at=?,completed_at=?
                   WHERE id=? AND status IN (""" + active_sql + ")",
                (now_s, now_s, item_id, *_ACTIVE_STATUSES, "missed"),
            )
            if result.rowcount:
                closed.append(item_id)
        for wait_id in sorted(forced_close_ids):
            result = conn.execute(
                """UPDATE hermes_tingle_items
                   SET status='auto_closed',next_check_at=NULL,snoozed_until=NULL,
                       reminder_count=0,reminder_attempt_count=0,reminder_budget_uncertain=0,
                       reminder_revision=reminder_revision+1,updated_at=?,completed_at=?
                   WHERE id=? AND status IN (""" + active_sql + ")",
                (now_s, now_s, wait_id, *_ACTIVE_STATUSES, "missed"),
            )
            if result.rowcount:
                conn.execute(
                    """INSERT OR IGNORE INTO hermes_tingle_retirement
                       (item_id,retirement_class,last_activity_at,auto_close_at,evidence_key)
                       VALUES(?,'wait',NULL,NULL,'')""",
                    (wait_id,),
                )
                closed.append(wait_id)
        conn.commit()
        return closed
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _advance_reminder_attempts(conn, ids, now_dt, checkpoint_next_checks=None):
    now_s = now_dt.isoformat(timespec="seconds")
    for item_id in ids:
        row = conn.execute("SELECT * FROM hermes_tingle_items WHERE id=?", (item_id,)).fetchone()
        if row is None or row["status"] not in (*_ACTIVE_STATUSES, "missed"):
            continue
        count = int(row["reminder_attempt_count"]) + 1
        due_dt = _parse_time(row["due_at"], field="due_at") if row["due_at"] else None
        if count >= 4:
            next_dt = None  # stop repetitive nudges; the item remains open and queryable
        elif row["completion_signal"] == "source_email_checkpoint":
            value = (checkpoint_next_checks or {}).get(item_id)
            next_dt = _parse_time(value, field='checkpoint_next_check') if value else None
            if next_dt is not None and next_dt <= now_dt:
                raise ValueError('next contextual checkpoint must be in the future')
        elif row["completion_signal"] == "source_email_reply_received" and row["reply_interval_seconds"]:
            next_dt = now_dt + timedelta(seconds=int(row["reply_interval_seconds"]))
        elif due_dt and due_dt > now_dt:
            next_dt = due_dt
        else:
            delay_days = 2 if count == 1 else 7 if count == 2 else 14
            next_dt = now_dt + timedelta(days=delay_days)
        next_s = next_dt.isoformat(timespec="seconds") if next_dt else None
        conn.execute(
            "UPDATE hermes_tingle_items SET reminder_attempt_count=?,next_check_at=?,updated_at=? WHERE id=?",
            (count,next_s,now_s,item_id),
        )


@serialized("reminders")
def mark_notified(item_ids: Iterable[str], *, expected_versions: dict[str, int] | None = None, checkpoint_next_checks: dict[str, str | None] | None = None, now: Any = None) -> list[dict[str, Any]]:
    """Prepare an agent-output outbox batch, NOT proof of delivery.

    Returns the current item and optional receipt. Each reservation consumes one
    of four attempts and advances cadence, even if no output ever arrives.
    """
    ids = list(dict.fromkeys(resolve_item_id(x) for x in item_ids))[:100]
    if not ids:
        return []
    selected = [get_item(item_id) for item_id in ids]
    if any((item or {}).get("status") not in (*_ACTIVE_STATUSES, "missed") for item in selected):
        raise ValueError("only unresolved reminder items may be prepared")
    if expected_versions is None:
        expected_versions = {item["id"]: item["reminder_revision"] for item in selected}
    emission_id = record_alert_emission(ids, expected_versions=expected_versions, now=now, channel="agent", checkpoint_next_checks=checkpoint_next_checks)
    batch = get_alert_emission(emission_id)
    return [dict(get_item(item_id) or {"id": item_id},
                 delivery_batch_id=emission_id, delivery_state="prepared_unconfirmed",
                 receipt_command=f"/tingle ack {batch['receipt_token']}") for item_id in ids]


class ReminderRevisionChanged(ValueError):
    pass


@serialized("reminders")
def record_alert_emission(item_ids: Iterable[str], *, expected_versions: dict[str, int] | None = None, now: Any = None, channel: str = "script", checkpoint_next_checks: dict[str, str | None] | None = None) -> int:
    """Log an alert batch BEFORE emission (log-then-emit closes the crash window).
    emitted_at is the logging time, never delivery evidence; emitted_to_gateway_at
    is set by mark_emission_flushed only after the flush really succeeded, and
    acked_at only by an explicit /tingle ack. logged != emitted != delivered."""
    resume_automatic_reminders(now=now)
    ids = list(dict.fromkeys(resolve_item_id(x) for x in item_ids))[:100]
    if not ids:
        raise ValueError("item_ids are required")
    now_dt = _now(now)
    now_s = _stamp(now_dt, field="now")
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _retire_obsolete_batches(conn, now_s)
        versions = {row["id"]: row["reminder_revision"] for row in conn.execute(
            "SELECT id,reminder_revision FROM hermes_tingle_items WHERE id IN (" + ",".join("?" for _ in ids) + ")", ids)}
        if expected_versions is not None and any(expected_versions.get(item_id) != versions.get(item_id) for item_id in ids):
            raise ReminderRevisionChanged("item changed during reminder preparation; recheck before producing output")
        for item_id in ids:
            item = conn.execute("SELECT * FROM hermes_tingle_items WHERE id=?", (item_id,)).fetchone()
            if item is None or item["status"] not in (*_ACTIVE_STATUSES, "missed"):
                raise ValueError("only unresolved reminder items may be prepared")
            if item['completion_signal'] == 'source_email_checkpoint' and channel != 'agent':
                raise ValueError('email checkpoints require verified model-assisted source review')
            if item["reminder_budget_uncertain"]:
                raise ValueError("legacy cycle budget cannot be proved; inspect /tingle delivery and explicitly snooze or set a new schedule to start a new cycle")
            if int(item["reminder_attempt_count"]) >= 4:
                raise ValueError("four reminder attempts already reserved for this cycle; snooze or explicitly update its schedule to re-arm")
            if (not item["next_check_at"] or _parse_time(item["next_check_at"], field="next_check_at") > now_dt
                    or (item["snoozed_until"] and _parse_time(item["snoozed_until"], field="snoozed_until") > now_dt)):
                raise ValueError("reminder is not due; automatic repeats respect the stored cadence and snooze")
        cycles = {row["id"]: row["reminder_cycle"] for row in conn.execute(
            "SELECT id,reminder_cycle FROM hermes_tingle_items WHERE id IN (" + ",".join("?" for _ in ids) + ")", ids)}
        cur = conn.execute(
            "INSERT INTO alert_emissions(item_ids,emitted_at,receipt_token,channel,item_versions,item_cycles,budget_reserved) VALUES(?,?,?,?,?,?,1)",
            (json.dumps(ids), now_s, secrets.token_hex(12), channel, json.dumps(versions), json.dumps(cycles)),
        )
        _advance_reminder_attempts(conn, ids, now_dt, checkpoint_next_checks)
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def mark_emission_flushed(emission_id: int, *, now: Any = None) -> None:
    """Record that the batch's flush to the gateway succeeded. This is emission,
    not delivery: what the gateway does afterward stays unobservable."""
    now_s = _stamp(now or datetime.now(timezone.utc), field="now")
    conn = _connect()
    try:
        conn.execute(
            "UPDATE alert_emissions SET emitted_to_gateway_at=? WHERE id=? AND emitted_to_gateway_at IS NULL",
            (now_s, int(emission_id)),
        )
        conn.commit()
    finally:
        conn.close()


def emission_state(row: dict[str, Any]) -> str:
    """Prepared (emission unknown), emitted-unconfirmed, or explicitly confirmed."""
    if row.get("acked_at"):
        return "confirmed"
    if row.get("emitted_to_gateway_at"):
        return "emitted_unconfirmed"
    return "prepared_unconfirmed"


def _retire_obsolete_batches(conn, now_s):
    # Content invalidation, retry and cycle changes never resolve delivery.
    # Retain even pre-upgrade released rows in recovery until a positive receipt.
    for row in conn.execute("SELECT id,item_ids FROM alert_emissions WHERE budget_reserved=2").fetchall():
        if not any(conn.execute("SELECT 1 FROM hermes_tingle_items WHERE reminder_budget_uncertain=1 AND (id=? OR (source_type='recurrence' AND source_ref LIKE ?))", (item_id, "occurrence:" + item_id + ":%")).fetchone() for item_id in json.loads(row["item_ids"])):
            conn.execute("UPDATE alert_emissions SET budget_reserved=3,released_at=COALESCE(released_at,?) WHERE id=?", (now_s, row["id"]))


def get_alert_emission(emission_id: int) -> dict[str, Any]:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM alert_emissions WHERE id=?", (int(emission_id),)).fetchone()
        if row is None:
            raise KeyError("reminder batch not found")
        return dict(row)
    finally:
        conn.close()


def held_reminder_item_ids() -> set[str]:
    """Automatic repeats are cadence/budget gated, never receipt gated."""
    return set()


def _convert_legacy_reminder_budgets(conn) -> None:
    """Called only in ensure_schema's serialized SQLite write transaction.

    Every known prior confirmation was copied into the attempt budget when the
    column was added. A matching unresolved pending batch adds its own attempt
    once, before any content edit. Older/mismatched provenance cannot prove which
    cycle an attempt belongs to: preserve counters and guard the affected item
    until the owner explicitly starts a new cycle. No history is fabricated.
    budget_reserved: 0=not converted, 1=charged/settled, 2=unprovable legacy cycle.
    """
    for batch in conn.execute("SELECT * FROM alert_emissions WHERE budget_reserved=0 ORDER BY id").fetchall():
        uncertain = False
        versions = json.loads(batch["item_versions"]) if batch["item_versions"] else {}
        for item_id in json.loads(batch["item_ids"]):
            item = conn.execute("SELECT * FROM hermes_tingle_items WHERE id=?", (item_id,)).fetchone()
            if item is None or item["status"] not in (*_ACTIVE_STATUSES, "missed"):
                continue
            # Old retry erased counters and old retirement followed content edits.
            # Neither released_at nor a changed content revision proves a new
            # cycle. Keep historical timestamps and positive receipts intact.
            if batch["released_at"] or versions.get(item_id) != item["reminder_revision"]:
                conn.execute("UPDATE hermes_tingle_items SET reminder_budget_uncertain=1 WHERE id=?", (item_id,))
                uncertain = True
            elif not batch["acked_at"] and item["reminder_attempt_count"] < 4:
                old_check = item["next_check_at"]
                _advance_reminder_attempts(conn, [item_id], _now(batch["emitted_at"]))
                if old_check:
                    conn.execute("UPDATE hermes_tingle_items SET next_check_at=CASE WHEN next_check_at IS NOT NULL AND next_check_at<? THEN ? ELSE next_check_at END WHERE id=?", (old_check, old_check, item_id))
        conn.execute("UPDATE alert_emissions SET budget_reserved=?,surfaced_at=CASE WHEN ? THEN NULL ELSE surfaced_at END WHERE id=?", (2 if uncertain else 1, uncertain, batch["id"]))


@serialized("reminders")
def resume_automatic_reminders(*, now: Any = None) -> None:
    """Open/migrate atomically. No user output or delivery inference occurs."""
    conn = _connect()
    conn.close()


def pending_alert_emissions(*, after_id: int = 0, limit: int | None = None) -> list[dict[str, Any]]:
    """Recovery view deliberately omits receipt tokens: inspecting a log is not receipt."""
    conn = _connect()
    try:
        _retire_obsolete_batches(conn, _stamp(_now()))
        conn.commit()
        return [dict(row) for row in conn.execute("SELECT id,item_ids,emitted_at,emitted_to_gateway_at,channel,budget_reserved,acked_at FROM alert_emissions WHERE ((acked_at IS NULL) OR budget_reserved=2) AND id>? ORDER BY id" + (" LIMIT ?" if limit is not None else ""),
            (int(after_id), max(1, min(int(limit), 101))) if limit is not None else (int(after_id),))]
    finally:
        conn.close()


@serialized("reminders")
def release_alert_emission(emission_id: int, *, now: Any = None) -> bool:
    """Explicit user retry only. Preserve audit and re-arm without claiming receipt."""
    resume_automatic_reminders(now=now)
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        now_s = _stamp(now or _now())
        batch = conn.execute("SELECT * FROM alert_emissions WHERE id=? AND ((acked_at IS NULL AND released_at IS NULL) OR budget_reserved=2)", (int(emission_id),)).fetchone()
        if batch is None:
            conn.commit()
            return False
        if batch["budget_reserved"] == 2:
            raise ValueError("Legacy cycle budget is unknown; retry cannot reset it. Inspect /tingle delivery, then explicitly snooze or set a new schedule to start a new cycle.")
        versions = json.loads(batch["item_versions"]) if batch["item_versions"] else None
        cycles = json.loads(batch["item_cycles"]) if batch["item_cycles"] else {}
        for item_id in json.loads(batch["item_ids"]):
            row = conn.execute("SELECT reminder_revision,reminder_attempt_count,reminder_cycle FROM hermes_tingle_items WHERE id=? AND status IN ('open','waiting','blocked','missed')", (item_id,)).fetchone()
            if row and row[1] < 4 and (versions is not None and versions.get(item_id) == row[0]) and (item_id not in cycles or cycles[item_id] == row[2]):
                conn.execute("UPDATE hermes_tingle_items SET next_check_at=? WHERE id=? AND (snoozed_until IS NULL OR snoozed_until<=?)", (now_s,item_id,now_s))
        changed = conn.execute("UPDATE alert_emissions SET released_at=? WHERE id=?", (now_s,int(emission_id))).rowcount
        conn.commit()
        return bool(changed)
    finally:
        conn.close()


@serialized("reminders")
def acknowledge_alert_emissions(receipt_token: str = "", *, now: Any = None) -> int:
    """Confirm only the batch whose receipt token the user received and supplied.

    Generic ack never changes anything. An authenticated owner's exact unpredictable
    token is positive evidence even if the helper's flush marker failed to persist.
    Optional confirmation changes its matching revision's confirmed count once;
    it never schedules another reminder or consumes another attempt.
    """
    if not re.fullmatch(r"[a-f0-9]{24}", str(receipt_token)):
        return 0
    resume_automatic_reminders(now=now)
    now_dt = _now(now)
    now_s = _stamp(now_dt)
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM alert_emissions WHERE receipt_token=? AND acked_at IS NULL", (receipt_token,)).fetchone()
        if row is None:
            conn.commit()
            return 0
        versions = json.loads(row["item_versions"]) if row["item_versions"] else None
        cycles = json.loads(row["item_cycles"]) if row["item_cycles"] else {}
        ids = []
        for item_id in json.loads(row["item_ids"]):
            if versions is None:
                continue
            # New batches follow an occurrence transferred to a missed copy.
            # Legacy batches lack this provenance: never guess a successor.
            if item_id in cycles:
                targets = conn.execute("SELECT id FROM hermes_tingle_items WHERE reminder_cycle=? AND reminder_revision=?",
                                       (cycles[item_id], versions.get(item_id))).fetchall()
            else:
                targets = conn.execute("SELECT id FROM hermes_tingle_items WHERE id=? AND reminder_revision=?",
                                       (item_id, versions.get(item_id))).fetchall()
            ids.extend(target[0] for target in targets)
        for item_id in set(ids):
            conn.execute("UPDATE hermes_tingle_items SET reminder_count=reminder_count+1,last_notified_at=? WHERE id=?", (now_s,item_id))
        conn.execute("UPDATE alert_emissions SET acked_at=? WHERE id=?", (now_s, row["id"]))
        conn.commit()
        return 1
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def note_command_interaction(*, now: Any = None) -> None:
    """Record that a /tingle command succeeded. Observational only: a working
    user->plugin reply says nothing about plugin->user alert delivery, so this
    timestamp must never be treated as a delivery receipt."""
    now_s = _stamp(now or datetime.now(timezone.utc), field="now")
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO plugin_meta(key,value) VALUES('last_command_interaction_at',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (now_s,),
        )
        conn.commit()
    finally:
        conn.close()


# --- One-time hands-off onboarding state -------------------------------------


def profile_item_count() -> int:
    """Every tracked item, any status: onboarding is due only on a truly empty profile."""
    conn = _connect()
    try:
        return int(conn.execute("SELECT count(*) FROM hermes_tingle_items").fetchone()[0])
    finally:
        conn.close()


def onboarding_state() -> dict[str, Any] | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT value FROM plugin_meta WHERE key='onboarding_state'").fetchone()
    finally:
        conn.close()
    if not row:
        return None
    try:
        value = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _save_onboarding_in_transaction(conn, state):
    """Committed item IDs cannot be lost by a late caller's aggregate marker."""
    state = dict(state)
    if state.get('state') in ('completed', 'partial'):
        journal = [row[0] for row in conn.execute(
            "SELECT item_id FROM onboarding_item_journal WHERE state='imported' ORDER BY rowid")]
        state['items'] = list(dict.fromkeys(journal + list(state.get('items') or [])))
    conn.execute("INSERT INTO plugin_meta(key,value) VALUES('onboarding_state',?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (json.dumps(state, ensure_ascii=False, separators=(',', ':')),))


def set_onboarding_state(state: dict[str, Any]) -> None:
    conn = _connect()
    try:
        conn.execute('BEGIN IMMEDIATE')
        _save_onboarding_in_transaction(conn, state)
        conn.commit()
    finally:
        conn.close()


_ONBOARDING_CHILDREN = (("hermes_tingle_steps", "goal_id"),
                        ("hermes_tingle_documents", "item_id"),
                        ("hermes_tingle_requirements", "item_id"),
                        ("hermes_tingle_options", "item_id"),
                        ("hermes_tingle_bookings", "item_id"))


def _onboarding_fingerprint(conn, item_id):
    """Entire row and dependencies, not note/mtime/revision heuristics.

    Conservatively preserves even automatically modified items. Never infers an
    untouched baseline for pre-journal imports.
    """
    row = conn.execute('SELECT * FROM hermes_tingle_items WHERE id=?', (item_id,)).fetchone()
    if row is None:
        return None
    item = dict(row)
    data = {'item': item}
    for table, column in _ONBOARDING_CHILDREN:
        data[table] = [dict(r) for r in conn.execute(
            f'SELECT * FROM {table} WHERE {column}=? ORDER BY rowid', (item_id,))]
    # Linking another goal to this item is also owner work, not safe to cascade.
    data['linked_from'] = [dict(r) for r in conn.execute(
        'SELECT * FROM hermes_tingle_steps WHERE wait_item_id=? ORDER BY rowid', (item_id,))]
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _delete_untouched_imports(conn, item_ids, expected_origin):
    removed, preserved = [], []
    for item_id in dict.fromkeys(item_ids):
        row = conn.execute('SELECT summary FROM hermes_tingle_items WHERE id=?', (item_id,)).fetchone()
        if row is None:
            continue
        journal = conn.execute('SELECT fingerprint,state FROM onboarding_item_journal WHERE item_id=?', (item_id,)).fetchone()
        if (not journal or journal['state'] != 'imported' or expected_origin not in row['summary']
                or journal['fingerprint'] != _onboarding_fingerprint(conn, item_id)):
            preserved.append(item_id)
            continue
        for table, column in _ONBOARDING_CHILDREN:
            conn.execute(f'DELETE FROM {table} WHERE {column}=?', (item_id,))
        conn.execute('DELETE FROM hermes_tingle_retirement WHERE item_id=?', (item_id,))
        conn.execute('DELETE FROM hermes_tingle_items WHERE id=?', (item_id,))
        conn.execute("UPDATE onboarding_item_journal SET state='removed' WHERE item_id=?", (item_id,))
        removed.append(item_id)
    return removed, preserved


@serialized('sources')
def delete_items(item_ids: list[str], *, expected_origin: str) -> list[str]:
    """Legacy entry point, now conservative without a creation fingerprint."""
    conn = _connect()
    try:
        conn.execute('BEGIN IMMEDIATE')
        removed, _ = _delete_untouched_imports(conn, item_ids, expected_origin)
        conn.commit()
        return removed
    finally:
        conn.close()


@serialized('onboarding')
@serialized('sources')
def undo_onboarding(*, expected_origin: str):
    """Deletion and final journal/marker update commit together or not at all."""
    conn = _connect()
    try:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute("SELECT value FROM plugin_meta WHERE key='onboarding_state'").fetchone()
        state = json.loads(row[0]) if row else {}
        if state.get('state') not in ('completed', 'partial'):
            return None
        ids = list(dict.fromkeys(list(state.get('items') or []) + [r[0] for r in conn.execute(
            "SELECT item_id FROM onboarding_item_journal WHERE state='imported'")]))
        removed, preserved = _delete_untouched_imports(conn, ids, expected_origin)
        result = {'state': 'undone', 'at': _now().isoformat(timespec='seconds'),
                  'scan_id': state.get('scan_id', ''), 'removed': removed, 'preserved': preserved}
        # Retire preserved/legacy baselines: no later undo can delete them.
        conn.execute("UPDATE onboarding_item_journal SET state='preserved' WHERE state='imported'")
        _save_onboarding_in_transaction(conn, result)
        conn.commit()
        return dict(result, original_count=len(ids))
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def unsurfaced_emissions(*, older_than: Any, now: Any = None) -> list[dict[str, Any]]:
    """Unconfirmed batches due for one diagnostic attempt after the grace.

    surfaced_at is a reservation for attempted diagnostic inclusion, not proof
    of output or receipt. Recovery pages remain available after that attempt.
    """
    cutoff_s = _stamp(older_than, field="older_than")
    conn = _connect()
    try:
        _retire_obsolete_batches(conn, _stamp(now or _now()))
        conn.commit()
        rows = conn.execute(
            "SELECT * FROM alert_emissions WHERE ((acked_at IS NULL) OR budget_reserved=2) AND emitted_at <= ? "
            "AND surfaced_at IS NULL ORDER BY emitted_at, id",
            (cutoff_s,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def mark_emissions_surfaced(emission_ids: Iterable[int], *, now: Any = None) -> None:
    """Reserve diagnostic inclusion before output, never claim delivery.

    A crash can lose this note. Recovery pages retain the unresolved batch;
    unlimited automatic diagnostic retries would bypass the reminder budget.
    """
    ids = [int(value) for value in emission_ids]
    if not ids:
        return
    now_s = _stamp(now or datetime.now(timezone.utc), field="now")
    conn = _connect()
    try:
        conn.executemany(
            "UPDATE alert_emissions SET surfaced_at=? WHERE id=?",
            [(now_s, value) for value in ids],
        )
        conn.commit()
    finally:
        conn.close()


@serialized("sources")
def mark_source_reviewed(source_type: str, source_id: str, source_version: str = "", *, source_generation: int | None = None, now: Any = None) -> bool:
    if source_type in ("email", "calendar") and not source_enabled(source_type):
        return False
    if source_type not in {"email", "calendar"}:
        raise ValueError("source_type must be email or calendar")
    source_id = str(source_id or "").strip()[:256]
    source_version = str(source_version or "").strip()[:256]
    if not source_id:
        raise ValueError("source_id is required")
    reviewed_at = _stamp(now or datetime.now(timezone.utc))
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _require_source(conn, source_type)
        grant = conn.execute("SELECT generation FROM hermes_tingle_source_optins WHERE kind=?", (source_type,)).fetchone()
        if type(source_generation) is not int or source_generation != grant[0]:
            raise ValueError("stale or missing source_generation; scan again before marking reviewed")
        cur = conn.execute(
            "INSERT OR IGNORE INTO source_seen(source_type,source_id,source_version,reviewed_at) VALUES(?,?,?,?)",
            (source_type,source_id,source_version,reviewed_at),
        )
        if source_type == "email":
            conn.execute("DELETE FROM gmail_pending_messages WHERE message_id=?", (source_id,))
        conn.commit()
        return bool(cur.rowcount)
    except SourceOptInRevoked:
        conn.rollback()
        return False
    finally:
        conn.close()



def _calendar_transition_pair_key(events: list[dict[str, Any]], pair: dict[str, Any], generation: int) -> str:
    identities = []
    for field in ("from_event", "to_event"):
        index = pair.get(field)
        if type(index) is not int or not 0 <= index < len(events):
            raise ValueError("invalid calendar transition pair")
        event = events[index]
        event_generation = event.get("source_generation")
        if event_generation is not None and event_generation != generation:
            raise ValueError("stale calendar event generation")
        source_id = str(event.get("source_id", "")).strip()
        if not source_id:
            raise ValueError("calendar transition source ID missing")
        identities.append({
            "source_id": source_id,
            "source_version": str(event.get("source_version", "")),
            "start": str(event.get("start", "")),
            "end": str(event.get("end", "")),
            "unconfirmed": bool(event.get("unconfirmed", False)),
        })
    identities.sort(key=lambda value: (value["source_id"], value["source_version"], value["start"], value["end"]))
    payload = json.dumps({"generation": generation, "events": identities}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(b"hermes-tingle-calendar-transition-v1\0" + payload.encode("utf-8")).hexdigest()


@serialized("sources")
def prepare_calendar_transition_pairs(
    events: list[dict[str, Any]], pairs: list[dict[str, Any]], *,
    source_generation: int, expected_policy_key: str | None = None, now: Any = None,
) -> dict[str, Any]:
    """Attach one-run receipts; persist only pair hashes and suppress unchanged alerted pairs."""
    now_dt = _now(now)
    now_s = now_dt.isoformat(timespec="seconds")
    if type(source_generation) is not int:
        raise ValueError("calendar source generation is required")
    policy_key = expected_policy_key or source_policy_key()
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _require_source(conn, "calendar")
        grant = conn.execute("SELECT generation FROM hermes_tingle_source_optins WHERE kind='calendar'").fetchone()
        if not grant or grant[0] != source_generation:
            raise ValueError("stale calendar generation; scan again before preparing transition receipts")
        conn.execute("DELETE FROM calendar_transition_receipts WHERE expires_at<=?", (now_s,))
        conn.execute("DELETE FROM calendar_transition_alerts WHERE alerted_at<?", ((now_dt - timedelta(days=30)).isoformat(timespec="seconds"),))
        if source_policy_key() != policy_key:
            conn.commit()
            return {"events": events, "pairs": [dict(pair, source_generation=source_generation, receipt_unavailable=True) for pair in pairs],
                    "suppressed_pair_count": 0, "untracked_pair_count": len(pairs)}
        kept_pairs: list[dict[str, Any]] = []
        kept_indices: set[int] = set()
        suppressed = 0
        untracked = 0
        for pair in pairs:
            try:
                key = _calendar_transition_pair_key(events, pair, source_generation)
            except (TypeError, ValueError):
                valid_indices = [index for index in (pair.get("from_event"), pair.get("to_event"))
                                 if type(index) is int and 0 <= index < len(events)]
                untracked += 1
                if len(valid_indices) == 2:
                    kept_pairs.append(dict(pair, source_generation=source_generation, receipt_unavailable=True))
                    kept_indices.update(valid_indices)
                continue
            alerted = conn.execute("SELECT 1 FROM calendar_transition_alerts WHERE pair_key=?", (key,)).fetchone()
            if alerted:
                suppressed += 1
                conn.execute("DELETE FROM calendar_transition_receipts WHERE pair_key=?", (key,))
                continue
            receipt = secrets.token_urlsafe(24)
            receipt_hash = hashlib.sha256(receipt.encode("ascii")).hexdigest()
            conn.execute("DELETE FROM calendar_transition_receipts WHERE pair_key=?", (key,))
            conn.execute(
                "INSERT INTO calendar_transition_receipts(receipt_hash,pair_key,source_generation,policy_key,expires_at) VALUES(?,?,?,?,?)",
                (receipt_hash, key, source_generation, policy_key, (now_dt + timedelta(hours=24)).isoformat(timespec="seconds")),
            )
            decorated = dict(pair)
            decorated.update({"source_generation": source_generation, "transition_receipt": receipt, "already_alerted": False})
            kept_pairs.append(decorated)
            kept_indices.update((pair["from_event"], pair["to_event"]))
        ordered = sorted(kept_indices)
        remap = {old: new for new, old in enumerate(ordered)}
        for pair in kept_pairs:
            if "from_event" in pair and "to_event" in pair:
                pair["from_event"] = remap[pair["from_event"]]
                pair["to_event"] = remap[pair["to_event"]]
        if source_policy_key() != policy_key:
            conn.rollback()
            return {"events": events, "pairs": [dict(pair, source_generation=source_generation, receipt_unavailable=True) for pair in pairs],
                    "suppressed_pair_count": 0, "untracked_pair_count": len(pairs)}
        conn.commit()
        return {
            "events": [events[index] for index in ordered],
            "pairs": kept_pairs,
            "suppressed_pair_count": suppressed,
            "untracked_pair_count": untracked,
        }
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


@serialized("sources")
def mark_calendar_transition_alerted(transition_receipt: str, *, source_generation: int, now: Any = None) -> bool:
    """Consume a fresh scan receipt and suppress that exact event-pair/version for 30 days."""
    if not source_enabled("calendar"):
        raise ValueError("calendar source is disabled; transition alert was not marked")
    token = str(transition_receipt or "").strip()
    if len(token) < 20 or len(token) > 128 or any(ch.isspace() for ch in token):
        raise ValueError("invalid calendar transition receipt")
    if type(source_generation) is not int:
        raise ValueError("calendar source_generation is required")
    now_dt = _now(now)
    now_s = now_dt.isoformat(timespec="seconds")
    policy_key = source_policy_key()
    receipt_hash = hashlib.sha256(token.encode("ascii", errors="strict")).hexdigest()
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _require_source(conn, "calendar")
        grant = conn.execute("SELECT generation FROM hermes_tingle_source_optins WHERE kind='calendar'").fetchone()
        row = conn.execute(
            "SELECT pair_key,source_generation,policy_key,expires_at FROM calendar_transition_receipts WHERE receipt_hash=?",
            (receipt_hash,),
        ).fetchone()
        if (not grant or grant[0] != source_generation or not row
                or row["source_generation"] != source_generation
                or row["policy_key"] != policy_key
                or row["expires_at"] <= now_s):
            raise ValueError("calendar transition receipt is stale or unavailable; no marker was written")
        cur = conn.execute(
            "INSERT OR IGNORE INTO calendar_transition_alerts(pair_key,alerted_at) VALUES(?,?)",
            (row["pair_key"], now_s),
        )
        conn.execute("DELETE FROM calendar_transition_receipts WHERE receipt_hash=?", (receipt_hash,))
        if source_policy_key() != policy_key:
            raise ValueError("calendar policy changed during alert mark; no marker was written")
        conn.commit()
        return bool(cur.rowcount)
    except BaseException:
        conn.rollback()
        raise
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


@serialized("sources")
def stage_gmail_sync(
    *, history_id: str | None, message_ids: Iterable[str], full_reconcile_at: Any = None, now: Any = None,
    expected_history_id: str | None = None,
) -> bool:
    """Queue changed message IDs and advance the history cursor in one local transaction."""
    if not source_enabled("email"):
        raise SourceOptInRevoked("email source opt-in is not enabled; refusing to stage Gmail data")
    ids = list(dict.fromkeys(str(value).strip()[:256] for value in message_ids if str(value).strip()))
    if len(ids) > 5000:
        raise ValueError("a single Gmail sync cannot stage more than 5000 pending message IDs")
    history_id = str(history_id or "").strip()[:256]
    now_s = _stamp(now or datetime.now(timezone.utc))
    full_s = _stamp(full_reconcile_at, field="full_reconcile_at") if full_reconcile_at else None
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _require_source(conn, "email")
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


def pending_gmail_message_ids(*, limit: int = 100, now: Any = None) -> list[str]:
    """Due, non-quarantined pending IDs. With now, IDs still in backoff are excluded."""
    limit = max(1, min(int(limit), 5000))
    conn = _connect()
    try:
        if now is not None:
            now_s = _stamp(now, field="now")
            rows = conn.execute(
                "SELECT message_id FROM gmail_pending_messages WHERE quarantined=0 AND (next_retry_at IS NULL OR next_retry_at<=?) ORDER BY queued_at,message_id LIMIT ?",
                (now_s, limit),
            ).fetchall()
            return [str(row["message_id"]) for row in rows]
        rows = conn.execute(
            "SELECT message_id FROM gmail_pending_messages WHERE quarantined=0 ORDER BY queued_at,message_id LIMIT ?",
            (limit,),
        ).fetchall()
        return [str(row["message_id"]) for row in rows]
    finally:
        conn.close()


MAX_GMAIL_METADATA_ATTEMPTS = 5
_GMAIL_BACKOFF_BASE = timedelta(minutes=30)
_GMAIL_BACKOFF_CAP = timedelta(hours=24)


def _gmail_backoff(attempts: int) -> timedelta:
    return min(_GMAIL_BACKOFF_BASE * (2 ** max(0, attempts - 1)), _GMAIL_BACKOFF_CAP)


@serialized("sources")
def defer_gmail_pending(message_ids: Iterable[str], *, error_kind: str = "transient", now: Any = None) -> dict[str, list[str]]:
    """Record failed metadata reads with bounded backoff; never discards coverage silently.

    Repeated failure does not prove permanent unavailability (auth/service failures
    persist too), so transient IDs are never dropped: after MAX_GMAIL_METADATA_ATTEMPTS
    they are QUARANTINED - kept with their error kind, excluded from per-poll retries,
    reported as an incomplete-review state, and recoverable via retry_quarantined_gmail.
    Returns {"retried": [...], "quarantined": [...]}."""
    ids = list(dict.fromkeys(str(value).strip()[:256] for value in message_ids if str(value).strip()))
    result: dict[str, list[str]] = {"retried": [], "quarantined": []}
    if not ids:
        return result
    now_dt = _now(now)
    now_s = now_dt.isoformat(timespec="seconds")
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _require_source(conn, "email")
        row = conn.execute("SELECT MAX(queued_at) FROM gmail_pending_messages").fetchone()
        last = datetime.fromisoformat(row[0]) if row and row[0] else now_dt
        queue_back = (max(now_dt, last) + timedelta(seconds=1)).isoformat(timespec="seconds")
        for value in ids:
            row = conn.execute("SELECT attempts FROM gmail_pending_messages WHERE message_id=?", (value,)).fetchone()
            attempts = int(row[0]) + 1 if row else 1
            if attempts >= MAX_GMAIL_METADATA_ATTEMPTS:
                conn.execute(
                    "UPDATE gmail_pending_messages SET attempts=?, quarantined=1, last_error_kind=?, quarantined_at=? WHERE message_id=?",
                    (attempts, error_kind, now_s, value),
                )
                result["quarantined"].append(value)
            else:
                backoff_until = (now_dt + _gmail_backoff(attempts)).isoformat(timespec="seconds")
                conn.execute(
                    "UPDATE gmail_pending_messages SET queued_at=?, attempts=?, next_retry_at=?, last_error_kind=? WHERE message_id=?",
                    (max(queue_back, now_s), attempts, backoff_until, error_kind, value),
                )
                result["retried"].append(value)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@serialized("sources")
def record_gmail_not_found(message_ids: Iterable[str], *, now: Any = None) -> None:
    """A provider-reported 404 means Gmail cannot find the message through this account
    now; it does not prove deletion (short-lived drafts and other causes look the same).
    The ID is quarantined immediately - no per-poll warnings for a definitive response -
    with its evidence preserved in gmail_unreadable, and stays recoverable via
    retry_quarantined_gmail. Nothing is discarded."""
    ids = list(dict.fromkeys(str(value).strip()[:256] for value in message_ids if str(value).strip()))
    if not ids:
        return
    now_s = _now(now).isoformat(timespec="seconds")
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _require_source(conn, "email")
        for value in ids:
            row = conn.execute("SELECT attempts FROM gmail_pending_messages WHERE message_id=?", (value,)).fetchone()
            attempts = int(row[0]) + 1 if row else 1
            conn.execute(
                """INSERT INTO gmail_unreadable (message_id,kind,attempts,first_failed_at,last_failed_at,note)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(message_id) DO UPDATE SET kind=excluded.kind, attempts=excluded.attempts,
                       last_failed_at=excluded.last_failed_at, note=excluded.note""",
                (value, "not_found", attempts, now_s, now_s,
                 "Provider reported 404 notFound; reason unknown (deleted, short-lived draft, or otherwise unavailable)."),
            )
            conn.execute(
                "UPDATE gmail_pending_messages SET attempts=?, quarantined=1, last_error_kind='not_found', next_retry_at=NULL, quarantined_at=? WHERE message_id=?",
                (attempts, now_s, value),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def quarantined_gmail_messages() -> list[dict[str, Any]]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT message_id,attempts,last_error_kind,next_retry_at,quarantine_cycles,quarantined_at FROM gmail_pending_messages WHERE quarantined=1 ORDER BY queued_at,message_id"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


MAX_GMAIL_QUARANTINE_CYCLES = 2
GMAIL_QUARANTINE_RETRY_AFTER = timedelta(hours=24)


@serialized("sources")
def auto_retry_quarantined_gmail(*, now: Any = None) -> list[str]:
    """Automatic safe recovery for transient quarantines: requeue IDs whose
    quarantine cool-down (24h) has passed, up to MAX_GMAIL_QUARANTINE_CYCLES
    second chances. Auth and not-found quarantines are never auto-requeued -
    retrying cannot help them - and stay recoverable via retry_quarantined_gmail.
    Returns the requeued IDs."""
    now_dt = _now(now)
    cutoff = (now_dt - GMAIL_QUARANTINE_RETRY_AFTER).isoformat(timespec="seconds")
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _require_source(conn, "email")
        rows = conn.execute(
            """SELECT message_id FROM gmail_pending_messages
               WHERE quarantined=1 AND last_error_kind='transient'
                 AND quarantine_cycles<? AND COALESCE(quarantined_at, queued_at)<=?""",
            (MAX_GMAIL_QUARANTINE_CYCLES, cutoff),
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE gmail_pending_messages SET quarantined=0, attempts=0, next_retry_at=NULL, quarantine_cycles=quarantine_cycles+1 WHERE message_id=?",
                (row["message_id"],),
            )
        conn.commit()
        return [row["message_id"] for row in rows]
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_plugin_meta(key: str) -> str | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT value FROM plugin_meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


@serialized("sources")
def set_plugin_meta(key: str, value: str) -> None:
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if key.startswith("gmail_") or key == "coverage_email_aged_queue_gap":
            _require_source(conn, "email")
        elif key.startswith("calendar_"):
            _require_source(conn, "calendar")
        conn.execute("INSERT INTO plugin_meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, value))
        conn.commit()
    finally:
        conn.close()


@serialized("sources")
def retry_quarantined_gmail(message_ids: Iterable[str] | None = None) -> int:
    """Recovery affordance: requeue quarantined IDs (all, or the given subset) for fresh attempts."""
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _require_source(conn, "email")
        if message_ids is None:
            count = conn.execute(
                "UPDATE gmail_pending_messages SET quarantined=0, attempts=0, next_retry_at=NULL WHERE quarantined=1"
            ).rowcount
        else:
            ids = [str(value).strip()[:256] for value in message_ids if str(value).strip()]
            count = sum(
                conn.execute(
                    "UPDATE gmail_pending_messages SET quarantined=0, attempts=0, next_retry_at=NULL WHERE quarantined=1 AND message_id=?",
                    (value,),
                ).rowcount
                for value in ids
            )
        conn.commit()
        return int(count)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def pending_gmail_total() -> int:
    """All staged Gmail IDs awaiting review, including ones in backoff (not yet due)."""
    conn = _connect()
    try:
        return int(conn.execute("SELECT COUNT(*) FROM gmail_pending_messages WHERE quarantined=0").fetchone()[0])
    finally:
        conn.close()


@serialized("sources")
def clear_gmail_pending(message_ids: Iterable[str]) -> None:
    ids = list(dict.fromkeys(str(value).strip()[:256] for value in message_ids if str(value).strip()))
    if not ids:
        return
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _require_source(conn, "email")
        conn.executemany("DELETE FROM gmail_pending_messages WHERE message_id=?", [(value,) for value in ids])
        conn.executemany("DELETE FROM gmail_unreadable WHERE message_id=?", [(value,) for value in ids])
        conn.commit()
    finally:
        conn.close()


def source_policy_key(policy=None):
    import hashlib
    if policy is None:
        try:
            from .hermes_tingle_config import load_settings
        except ImportError:
            from hermes_tingle_config import load_settings
        policy = load_settings()
    return hashlib.sha256(json.dumps(policy,sort_keys=True).encode()).hexdigest()


@serialized("sources")
def register_email_candidates(source_ids: Iterable[str], *, now: Any = None, account_key: str = "", policy_key: str | None = None) -> None:
    if not source_enabled("email"):
        raise SourceOptInRevoked("email source opt-in is not enabled; refusing to register candidates")
    now_dt = _now(now)
    now_s = now_dt.isoformat(timespec="seconds")
    candidate_cutoff = (now_dt - timedelta(hours=24)).isoformat(timespec="seconds")
    body_read_cutoff = (now_dt - timedelta(days=BODY_READ_DEDUPE_DAYS)).isoformat(timespec="seconds")
    policy_key = policy_key if policy_key is not None else source_policy_key()
    ids = list(dict.fromkeys(str(source_id).strip()[:256] for source_id in source_ids if str(source_id).strip()))[:MAX_EMAIL_CANDIDATES]
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _require_source(conn, "email")
        conn.execute("DELETE FROM email_candidates WHERE registered_at < ?", (candidate_cutoff,))
        conn.execute("DELETE FROM email_body_reads WHERE read_at < ?", (body_read_cutoff,))
        conn.executemany(
            "INSERT OR REPLACE INTO email_candidates(source_id,registered_at,account_key,generation,policy_key) VALUES(?,?,?,?,?)",
            [(source_id, now_s, account_key, conn.execute("SELECT generation FROM hermes_tingle_source_optins WHERE kind='email'").fetchone()[0],policy_key) for source_id in ids],
        )
        conn.commit()
    finally:
        conn.close()


@serialized("sources")
def claim_email_body_read(source_id: str, *, now: Any = None, account_key: str = "") -> bool:
    if not source_enabled("email"):
        raise SourceOptInRevoked("email source opt-in is not enabled; refusing to record body reads")
    """Allow one body read per recent scanned message ID during the 14-day source window."""
    policy_key = source_policy_key()
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
        _require_source(conn, "email")
        candidate = conn.execute(
            "SELECT 1 FROM email_candidates WHERE source_id=? AND account_key=? AND account_key<>'' AND generation=? AND policy_key=? AND registered_at >= ?",
            (source_id, account_key, conn.execute("SELECT generation FROM hermes_tingle_source_optins WHERE kind='email'").fetchone()[0],policy_key, candidate_cutoff),
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
        "source_ref", "decided_by", "decision_evidence",
    )}


def capture_feedback_candidate(
    rule_text: str, *, category: str = "general", source_ref: str = "", now: Any = None,
) -> dict[str, Any]:
    """Record a reusable-preference candidate distilled from a direct correction.
    source_ref is its provenance lead: what prompted the rule (e.g. the item or
    conversation reference), never message content."""
    rule_text = " ".join(str(rule_text or "").split())
    if not rule_text:
        raise ValueError("feedback_text is required")
    if len(rule_text) > 500:
        raise ValueError("feedback_text must be at most 500 characters")
    category = str(category or "general").strip().casefold()
    if category not in _FEEDBACK_CATEGORIES:
        raise ValueError("unsupported feedback_category")
    if category in TRIAGE_CATEGORIES and len(rule_text) > TRIAGE_RULE_CHARS:
        raise ValueError(f"a triage rule must be at most {TRIAGE_RULE_CHARS} characters")
    source_ref = str(source_ref or "").strip()[:256]
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
            if source_ref and not existing["source_ref"]:
                conn.execute(
                    "UPDATE feedback_candidates SET source_ref=? WHERE id=?",
                    (source_ref, existing["id"]),
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
               (id,rule_text,category,status,fingerprint,occurrences,created_at,updated_at,approved_at,source_ref)
               VALUES (?,?,?,?,?,1,?,?,NULL,?)""",
            (feedback_id, rule_text, category, "pending", fingerprint, now_s, now_s, source_ref),
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


def resolve_feedback_candidate(feedback_id: str, decision: str, *, decided_by: Any = None,
                               evidence: str = "", now: Any = None) -> dict[str, Any]:
    """Approve or dismiss a candidate. decided_by binds the deciding principal
    ((platform, user_id, chat_id) from the session, never model-supplied); evidence
    records how the decision arrived (e.g. '/tingle command'). An approved rule keeps
    its full provenance: source_ref, who approved it, when, and the evidence."""
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
        if decision == "approved":
            row = conn.execute("SELECT category,status FROM feedback_candidates WHERE id=?", (feedback_id,)).fetchone()
            if row["category"] in TRIAGE_CATEGORIES and row["status"] != "approved":
                count = conn.execute("SELECT COUNT(*) FROM feedback_candidates WHERE status='approved' "
                                     "AND category IN ('triage','general','email')").fetchone()[0]
                if count >= TRIAGE_RULES_MAX:
                    raise ValueError(f"{TRIAGE_RULES_MAX} triage rules are already active; dismiss one first")
        if decided_by is None:
            raise ValueError("resolving a feedback candidate requires the deciding principal "
                             "(an explicit user command path); the model cannot decide")
        decided_by_s = ""
        if decided_by is not None:
            if not isinstance(decided_by, (tuple, list)) or len(decided_by) != 3:
                raise ValueError("decided_by must be a (platform, user_id, chat_id) tuple")
            decided_by_s = ":".join(str(part or "").strip() for part in decided_by)[:240]
        conn.execute(
            "UPDATE feedback_candidates SET status=?, updated_at=?, approved_at=?, decided_by=?, decision_evidence=? WHERE id=?",
            (decision, now_s, now_s if decision == "approved" else None,
             decided_by_s, str(evidence or "").strip()[:240], feedback_id),
        )
        conn.execute("INSERT INTO plugin_meta(key,value) VALUES('feedback_review_epoch','1') "
                     "ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1")
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
            "SELECT id,rule_text,category,approved_at,source_ref,decided_by FROM feedback_candidates "
            "WHERE status='approved' ORDER BY approved_at ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def approved_triage_rules() -> list[str]:
    """Texts of the approved rules that steer scans, oldest first. Capture and approval enforce the
    length and count limits, so nothing here is clipped or dropped."""
    return [r["rule_text"] for r in approved_feedback_rules(limit=100) if r["category"] in TRIAGE_CATEGORIES]


# --- FT-14: per-chat consent records for a future chat collector -----------
# A chat source means OTHER people's messages, so a global opt-in is not enough:
# every chat needs its own explicit owner grant. These records are the consent
# scaffolding only - no chat collector exists, and one must check these records
# before reading anything. Revocation purges retained data (nothing is retained
# yet; the DELETE points are where a collector's cache must be purged).
_CHAT_REF_RE = re.compile(r"^[a-z][a-z0-9_]{0,29}:\S{1,120}$")


@serialized("sources")
def grant_chat_consent(chat_ref: Any, *, lookback_days: Any = 7, granted_by: str = "",
                       now: Any = None) -> dict[str, Any]:
    chat_ref = str(chat_ref or "").strip()
    if not _CHAT_REF_RE.match(chat_ref):
        raise ValueError("chat ref must look like platform:chat-id, e.g. telegram:1001")
    if isinstance(lookback_days, bool) or not isinstance(lookback_days, int) \
            or not 1 <= lookback_days <= 30:
        raise ValueError("lookback days must be an integer from 1 to 30")
    granted_by = str(granted_by or "").strip()[:240]
    if not granted_by:
        raise ValueError("granting chat consent requires the commanding principal")
    now_s = _now(now).isoformat(timespec="seconds")
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """INSERT INTO chat_source_consents (chat_ref,lookback_days,granted_by,created_at,revoked_at)
               VALUES (?,?,?,?,NULL)
               ON CONFLICT(chat_ref) DO UPDATE SET lookback_days=excluded.lookback_days,
                 granted_by=excluded.granted_by, created_at=excluded.created_at, revoked_at=NULL""",
            (chat_ref, lookback_days, granted_by, now_s),
        )
        _purge_source_retained(conn,'chat')
        conn.commit()
        return dict(conn.execute(
            "SELECT * FROM chat_source_consents WHERE chat_ref=?", (chat_ref,)).fetchone())
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_chat_consents(*, include_revoked: bool = False) -> list[dict[str, Any]]:
    conn = _connect()
    try:
        sql = "SELECT * FROM chat_source_consents"
        if not include_revoked:
            sql += " WHERE revoked_at IS NULL"
        return [dict(row) for row in conn.execute(sql + " ORDER BY created_at ASC").fetchall()]
    finally:
        conn.close()


@serialized("sources")
def revoke_chat_consent(chat_ref: Any, *, now: Any = None) -> dict[str, Any]:
    chat_ref = str(chat_ref or "").strip()
    now_s = _now(now).isoformat(timespec="seconds")
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM chat_source_consents WHERE chat_ref=? AND revoked_at IS NULL", (chat_ref,),
        ).fetchone()
        if row is None:
            raise KeyError("active chat consent not found")
        conn.execute("UPDATE chat_source_consents SET revoked_at=? WHERE chat_ref=?", (now_s, chat_ref))
        _purge_source_retained(conn,'chat')
        conn.commit()
        return dict(conn.execute(
            "SELECT * FROM chat_source_consents WHERE chat_ref=?", (chat_ref,)).fetchone())
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --- Default chat ingestion: known chats minus a simple exclusion list -------
# Per-chat opt-in grants were replaced by default ingestion of the chats this
# profile has actually seen. Recorded legacy consents stay on file as history
# and keep their chats known; nothing here reads message content.

DEFAULT_CHAT_LOOKBACK_DAYS = 7


def note_known_chat(chat_ref: Any, *, origin: str = "", now: Any = None) -> None:
    """Best-effort registry of chats this profile has seen. Never raises."""
    chat_ref = str(chat_ref or "").strip()
    if not _CHAT_REF_RE.match(chat_ref):
        return
    now_s = _now(now).isoformat(timespec="seconds")
    try:
        conn = _connect()
        try:
            conn.execute(
                """INSERT INTO known_chats (chat_ref,first_seen,last_seen,origin) VALUES (?,?,?,?)
                   ON CONFLICT(chat_ref) DO UPDATE SET last_seen=excluded.last_seen""",
                (chat_ref, now_s, now_s, str(origin or "")[:120]),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def list_known_chats() -> list[dict[str, Any]]:
    conn = _connect()
    try:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM known_chats ORDER BY last_seen DESC").fetchall()]
    finally:
        conn.close()


def list_chat_exclusions() -> list[dict[str, Any]]:
    conn = _connect()
    try:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM chat_source_exclusions ORDER BY created_at ASC").fetchall()]
    finally:
        conn.close()


@serialized("sources")
def exclude_chat(chat_ref: Any, *, excluded_by: str, note: str = "", now: Any = None) -> dict[str, Any]:
    """Mute one chat from default ingestion; its retained leads/markers are purged."""
    chat_ref = str(chat_ref or "").strip()
    if not _CHAT_REF_RE.match(chat_ref):
        raise ValueError("chat ref must look like platform:chat-id, e.g. telegram:1001")
    excluded_by = str(excluded_by or "").strip()[:240]
    if not excluded_by:
        raise ValueError("excluding a chat requires the commanding principal")
    now_s = _now(now).isoformat(timespec="seconds")
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO chat_source_exclusions (chat_ref,excluded_by,note,created_at) VALUES (?,?,?,?) "
            "ON CONFLICT(chat_ref) DO UPDATE SET excluded_by=excluded.excluded_by, "
            "note=excluded.note, created_at=excluded.created_at",
            (chat_ref, excluded_by, str(note or "")[:240], now_s))
        _purge_source_retained(conn, 'chat')
        conn.commit()
        return dict(conn.execute(
            "SELECT * FROM chat_source_exclusions WHERE chat_ref=?", (chat_ref,)).fetchone())
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@serialized("sources")
def remove_chat_exclusion(chat_ref: Any, *, now: Any = None) -> dict[str, Any]:
    chat_ref = str(chat_ref or "").strip()
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM chat_source_exclusions WHERE chat_ref=?", (chat_ref,)).fetchone()
        if row is None:
            raise KeyError("chat exclusion not found")
        conn.execute("DELETE FROM chat_source_exclusions WHERE chat_ref=?", (chat_ref,))
        _purge_source_retained(conn, 'chat')
        conn.commit()
        return dict(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def chats_in_scope() -> list[str]:
    """Chat refs the collector may read by default: legacy-consented plus seen
    chats, minus exclusions. Content stays inside this profile."""
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT chat_ref FROM chat_source_consents WHERE revoked_at IS NULL
               UNION SELECT chat_ref FROM known_chats
               EXCEPT SELECT chat_ref FROM chat_source_exclusions""").fetchall()
        return [row[0] for row in rows]
    finally:
        conn.close()


# Generic approvals record intent for future external actions.
# The model registers exact intent, only an explicit user command approves it,
# the approval binds action/target/parameters, expires, and is consumed once.
# Future external writes must use the same gate or a stricter one.
_APPROVAL_ACTION_RE = re.compile(r"[a-z][a-z0-9_]{0,59}")
APPROVAL_ID_RE = re.compile(r"AR-[A-F0-9]{6}")
_MAX_APPROVAL_PARAMS_CHARS = 4000
_MIN_APPROVAL_TTL_HOURS = 1
_MAX_APPROVAL_TTL_HOURS = 168


def _canonical_parameters(parameters: Any) -> tuple[str, str]:
    """Canonical JSON + content hash: the exact-intent binding for an approval."""
    if not isinstance(parameters, dict):
        raise ValueError("approval parameters must be an object")
    try:
        canonical = json.dumps(parameters, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("approval parameters must be JSON-serializable") from exc
    if len(canonical) > _MAX_APPROVAL_PARAMS_CHARS:
        raise ValueError("approval parameters are too large; describe the action concisely, never embed content or secrets")
    return canonical, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _audit(conn: sqlite3.Connection, approval_id: str | None, action_type: str, target: str,
           parameters_hash: str, outcome: str, detail: str, now_s: str) -> None:
    conn.execute(
        "INSERT INTO action_audit(approval_id,action_type,target,parameters_hash,outcome,detail,created_at) VALUES(?,?,?,?,?,?,?)",
        (approval_id, action_type[:60], target[:500], parameters_hash, outcome, detail[:300], now_s),
    )


def request_approval(action_type: Any, target: Any, parameters: Any, *, ttl_hours: Any = 24,
                     requested_by: str = "", approver: Any = None, now: Any = None) -> dict[str, Any]:
    """Register a pending approval request for a future external action.

    *approver* binds the request to exactly one deciding principal: a
    ``(platform, user_id, chat_id)`` tuple grounded in the requesting session's
    context. A request without a binding can never be decided - no sender can be
    matched against it - so it simply expires.
    """
    approver_platform = approver_user_id = approver_chat_id = None
    if approver is not None:
        try:
            approver_platform, approver_user_id, approver_chat_id = (
                str(approver[0] or "").strip().casefold() or None,
                str(approver[1] or "").strip() or None,
                str(approver[2] or "").strip() or None,
            )
        except (TypeError, IndexError):
            raise ValueError("approver must be a (platform, user_id, chat_id) tuple")
        if not approver_platform or not approver_user_id:
            raise ValueError("an approver binding needs both a platform and a user id")
    action_type = str(action_type or "").strip()
    if not _APPROVAL_ACTION_RE.fullmatch(action_type):
        raise ValueError("action_type must be a short snake_case identifier")
    target = str(target or "").strip()
    if not target or len(target) > 500:
        raise ValueError("approval target must be 1-500 characters")
    canonical, params_hash = _canonical_parameters(parameters)
    if (isinstance(ttl_hours, bool) or not isinstance(ttl_hours, int)
            or not _MIN_APPROVAL_TTL_HOURS <= ttl_hours <= _MAX_APPROVAL_TTL_HOURS):
        raise ValueError("ttl_hours must be an integer from 1 to 168")
    requested_by = str(requested_by or "").strip()[:120]
    now_dt = _now(now)
    now_s = now_dt.isoformat(timespec="seconds")
    expires_s = (now_dt + timedelta(hours=ttl_hours)).isoformat(timespec="seconds")
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            """SELECT * FROM approval_requests
               WHERE action_type=? AND target=? AND parameters_hash=? AND status='pending' AND expires_at>?
                 AND COALESCE(approver_user_id,'')=COALESCE(?, '')
                 AND COALESCE(approver_platform,'')=COALESCE(?, '')
                 AND COALESCE(approver_chat_id,'')=COALESCE(?, '')""",
            (action_type, target, params_hash, now_s, approver_user_id, approver_platform, approver_chat_id),
        ).fetchone()
        if existing:
            conn.commit()
            result = dict(existing)
            result["deduplicated"] = True
            return result
        request_id = None
        for _ in range(16):
            candidate = "AR-" + secrets.token_hex(3).upper()
            try:
                conn.execute(
                    """INSERT INTO approval_requests
                       (id,action_type,target,parameters_json,parameters_hash,status,requested_by,
                        approver_platform,approver_user_id,approver_chat_id,created_at,expires_at)
                       VALUES (?,?,?,?,?,'pending',?,?,?,?,?,?)""",
                    (candidate, action_type, target, canonical, params_hash, requested_by,
                     approver_platform, approver_user_id, approver_chat_id, now_s, expires_s),
                )
                request_id = candidate
                break
            except sqlite3.IntegrityError as exc:
                if not _is_primary_key_collision(exc):
                    raise
        if request_id is None:
            raise RuntimeError("could not allocate an approval request ID")
        _audit(conn, request_id, action_type, target, params_hash, "requested", requested_by, now_s)
        conn.commit()
        return dict(conn.execute("SELECT * FROM approval_requests WHERE id=?", (request_id,)).fetchone())  # type: ignore[return-value]
    finally:
        conn.close()


def _expire_due_approvals(conn: sqlite3.Connection, now_s: str) -> None:
    conn.execute(
        "UPDATE approval_requests SET status='expired' WHERE status IN ('pending','approved') AND expires_at<=?",
        (now_s,),
    )


def decide_approval(request_id: Any, decision: str, *, evidence: str, decided_by: Any = None,
                    now: Any = None) -> dict[str, Any]:
    """Approve or reject a pending request. Requires evidence of the user's explicit decision
    and a deciding principal matching the request's approver binding.

    *decided_by* is the dispatch-authenticated ``(platform, user_id, chat_id)`` of the sender.
    Decisions fail closed: an unbound request can never be decided, an unidentified decider
    decides nothing, and any other principal - another allowlisted DM user, or the same user id
    on a different platform - is refused.
    """
    if decision not in {"approved", "rejected"}:
        raise ValueError("decision must be approved or rejected")
    evidence = str(evidence or "").strip()
    if not evidence:
        raise ValueError("an approval decision requires recorded evidence of the user's command")
    decider_platform = decider_user_id = decider_chat_id = None
    if decided_by is not None:
        try:
            decider_platform, decider_user_id, decider_chat_id = (
                str(decided_by[0] or "").strip().casefold() or None,
                str(decided_by[1] or "").strip() or None,
                str(decided_by[2] or "").strip() or None,
            )
        except (TypeError, IndexError):
            decider_platform = decider_user_id = decider_chat_id = None
    if not decider_platform or not decider_user_id:
        raise ValueError("the deciding sender is not an identified, authenticated principal")
    request_id = str(request_id or "").upper()
    now_dt = _now(now)
    now_s = now_dt.isoformat(timespec="seconds")
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _expire_due_approvals(conn, now_s)
        row = conn.execute("SELECT * FROM approval_requests WHERE id=?", (request_id,)).fetchone()
        if row is None:
            raise KeyError("approval request not found")
        if row["status"] != "pending":
            raise ValueError(f"approval request is {row['status']}, not pending")
        if not row["approver_user_id"] or not row["approver_platform"]:
            raise ValueError(
                "approval decisions are disabled for this request: it is not bound to an "
                "approving principal; let it expire and re-request it from your own chat")
        if (decider_platform != str(row["approver_platform"]).casefold()
                or decider_user_id != row["approver_user_id"]
                or (row["approver_chat_id"] and decider_chat_id != row["approver_chat_id"])):
            raise ValueError("this approval request is bound to a different account or conversation")
        conn.execute(
            "UPDATE approval_requests SET status=?, decided_at=?, approval_evidence=? WHERE id=?",
            (decision, now_s, evidence[:300], request_id),
        )
        _audit(conn, request_id, row["action_type"], row["target"], row["parameters_hash"], decision, evidence, now_s)
        conn.commit()
        return dict(conn.execute("SELECT * FROM approval_requests WHERE id=?", (request_id,)).fetchone())  # type: ignore[return-value]
    finally:
        conn.close()


def consume_approval(request_id: Any, action_type: Any, target: Any, parameters: Any,
                     *, now: Any = None) -> dict[str, Any]:
    """One-shot gate an external action must pass immediately before acting.

    Fails closed: the request must be approved and unexpired, and the action,
    target and parameters must match the approved request exactly. Consumption
    is atomic; a second consume always fails.
    """
    request_id = str(request_id or "").upper()
    action_type = str(action_type or "").strip()
    target = str(target or "").strip()
    _, params_hash = _canonical_parameters(parameters)
    now_dt = _now(now)
    now_s = now_dt.isoformat(timespec="seconds")
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _expire_due_approvals(conn, now_s)
        row = conn.execute("SELECT * FROM approval_requests WHERE id=?", (request_id,)).fetchone()
        if row is None:
            raise KeyError("approval request not found")
        if row["status"] != "approved":
            _audit(conn, request_id, action_type, target, params_hash,
                   "denied_not_approved", f"status={row['status']}", now_s)
            conn.commit()
            raise ValueError(f"approval request is {row['status']}, not approved")
        if not (row["action_type"] == action_type and row["target"] == target
                and row["parameters_hash"] == params_hash):
            _audit(conn, request_id, action_type, target, params_hash,
                   "denied_mismatch", "action, target or parameters differ from the approved request", now_s)
            conn.commit()
            raise ValueError("the action does not match the approved request exactly")
        conn.execute("UPDATE approval_requests SET status='consumed', consumed_at=? WHERE id=?", (now_s, request_id))
        _audit(conn, request_id, action_type, target, params_hash, "consumed", "", now_s)
        conn.commit()
        return dict(conn.execute("SELECT * FROM approval_requests WHERE id=?", (request_id,)).fetchone())  # type: ignore[return-value]
    finally:
        conn.close()


def audit_action(approval_id: Any, action_type: Any, target: Any, parameters: Any,
                 outcome: Any, *, detail: str = "", now: Any = None) -> None:
    """Record the verified outcome of an external action attempt (executors call this)."""
    outcome = str(outcome or "").strip()
    if not outcome or len(outcome) > 40:
        raise ValueError("audit outcome must be 1-40 characters")
    _, params_hash = _canonical_parameters(parameters)
    now_s = _now(now).isoformat(timespec="seconds")
    conn = _connect()
    try:
        _audit(conn, str(approval_id or "").upper() or None, str(action_type or "").strip(),
               str(target or "").strip(), params_hash, outcome, str(detail or ""), now_s)
        conn.commit()
    finally:
        conn.close()


def list_approvals(status: Any = None, *, limit: Any = 20, now: Any = None,
                   approver: Any = None) -> list[dict[str, Any]]:
    """Approval requests, newest first; optionally scope to one exact approver DM."""
    if status not in (None, "", "pending", "approved", "rejected", "expired", "consumed"):
        raise ValueError("unknown approval status")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer from 1 to 100")
    binding = None
    if approver is not None:
        try:
            binding = tuple(str(approver[i] or "").strip() for i in range(3))
        except (TypeError, IndexError):
            raise ValueError("approver must be a (platform, user_id, chat_id) tuple") from None
        if not all(binding):
            raise ValueError("approval listing requires an exact approver DM binding")
        binding = (binding[0].casefold(), binding[1], binding[2])
    now_s = _now(now).isoformat(timespec="seconds")
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _expire_due_approvals(conn, now_s)
        conditions, values = [], []
        if status:
            conditions.append("status=?")
            values.append(status)
        if binding:
            conditions.extend(("approver_platform=?", "approver_user_id=?", "approver_chat_id=?"))
            values.extend(binding)
        query = "SELECT * FROM approval_requests"
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY created_at DESC LIMIT ?"
        rows = conn.execute(query, (*values, limit)).fetchall()
        conn.commit()
        return [dict(row) for row in rows]
    finally:
        conn.close()


@serialized("sources")
def acknowledge_email_coverage_gap() -> None:
    """Owner attestation, not provider evidence; clear coarse flags atomically."""
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _require_source(conn, "email")
        conn.execute("DELETE FROM plugin_meta WHERE key IN ('gmail_coverage_gap','gmail_coverage_notice','coverage_email_aged_queue_gap','gmail_aged_queue_notice')")
        conn.commit()
    finally:
        conn.close()
