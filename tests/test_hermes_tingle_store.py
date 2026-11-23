from delivery_fakes import bind_synthetic_email
from delivery_fakes import seed_outbox_items
from delivery_fakes import confirmed_reminder, review_current_source
from datetime import datetime, timedelta
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import hermes_tingle_store as store


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    db_path = tmp_path / "data.db"

    def connect(_name: str):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(store, "plugin_db", connect)
    store.enable_source("email", enabled_by="synthetic fixture consent")
    bind_synthetic_email(store)
    store.enable_source("calendar", enabled_by="synthetic fixture consent")
    return store


def test_add_item_retries_random_id_collision(isolated_store, monkeypatch):
    ids = iter(['aaaaaa', 'aaaaaa', 'bbbbbb'])
    monkeypatch.setattr(store.secrets, 'token_hex', lambda length: next(ids))
    first = isolated_store.add_item(title='First item')
    second = isolated_store.add_item(title='Second item')
    assert first['id'] == 'HT-AAAAAA'
    assert second['id'] == 'HT-BBBBBB'


@pytest.mark.parametrize("zone, expected", [
    ("America/New_York", "2026-03-08T13:10:00+00:00"),
    ("Asia/Tokyo", "2026-03-08T00:10:00+00:00"),
])
def test_date_only_deadline_uses_profile_timezone_across_dst(tmp_path, synthetic_settings, isolated_store, zone, expected):
    import json
    (tmp_path / "hermes-tingle.json").write_text(json.dumps({**synthetic_settings, "timezone": zone}))
    item = isolated_store.add_item(title="Send form", due_at="2026-03-08", now="2026-03-01T00:00:00Z")
    assert item["due_at"] == expected


@pytest.mark.parametrize("zone, clock, expected", [
    ("America/New_York", "08:45", "2026-03-08T12:45:00+00:00"),
    ("Asia/Tokyo", "16:20", "2026-03-08T07:20:00+00:00"),
])
def test_date_only_clock_is_profile_specific_and_offsets_are_preserved(tmp_path, synthetic_settings, isolated_store, zone, clock, expected):
    import json
    (tmp_path / "hermes-tingle.json").write_text(json.dumps({**synthetic_settings, "timezone": zone, "date_only_time": clock}))
    item = isolated_store.add_item(title="Submit", due_at="2026-03-08", next_check_at="2026-03-08", now="2026-03-01T00:00:00Z")
    assert item["due_at"] == expected
    assert item["next_check_at"] == expected
    assert isolated_store._stamp("2026-03-08T09:10:00+02:00") == "2026-03-08T07:10:00+00:00"


@pytest.mark.parametrize("day, clock", [("2026-03-08", "02:30"), ("2026-11-01", "01:30")])
def test_date_only_configured_clock_rejects_dst_gap_and_fold(tmp_path, synthetic_settings, day, clock):
    import json
    (tmp_path / "hermes-tingle.json").write_text(json.dumps({**synthetic_settings, "timezone": "America/New_York", "date_only_time": clock}))
    with pytest.raises(ValueError, match="ambiguous|nonexistent"):
        store._stamp(day)


@pytest.mark.parametrize("zone, expected", [
    ("America/New_York", "2026-03-08T13:10:00+00:00"),
    ("Asia/Tokyo", "2026-03-08T00:10:00+00:00"),
])
def test_naive_timestamps_use_profile_but_offsets_preserve_instants(tmp_path, synthetic_settings, zone, expected):
    import json
    (tmp_path / "hermes-tingle.json").write_text(json.dumps({**synthetic_settings, "timezone": zone}))
    assert store._stamp("2026-03-08T09:10:00") == expected
    assert store._stamp("2026-03-08T09:10:00+02:00") == "2026-03-08T07:10:00+00:00"


@pytest.mark.parametrize("value", ["2026-03-08T02:30:00", "2026-11-01T01:30:00"])
def test_ambiguous_or_nonexistent_naive_deadlines_require_offset(tmp_path, synthetic_settings, value):
    import json
    (tmp_path / "hermes-tingle.json").write_text(json.dumps({**synthetic_settings, "timezone": "America/New_York"}))
    with pytest.raises(ValueError, match="ambiguous|nonexistent"):
        store._stamp(value)


def test_goal_steps_preserve_profile_date_only_deadline(tmp_path, synthetic_settings, isolated_store):
    import json
    (tmp_path / "hermes-tingle.json").write_text(json.dumps({**synthetic_settings, "timezone": "Asia/Tokyo", "date_only_time": "16:20"}))
    goal = isolated_store.add_item(title="Prepare packet", due_at="2026-03-08", now="2026-03-01T00:00:00Z")
    isolated_store.add_step(goal["id"], title="Collect documents", why="Needed for the packet")
    assert isolated_store.get_item(goal["id"])["due_at"] == "2026-03-08T07:20:00+00:00"
    with pytest.raises(ValueError, match="finish all goal steps"):
        isolated_store.complete_item(goal["id"])


def test_add_item_persists_across_connections_and_uses_stable_id(isolated_store):
    item = isolated_store.add_item(
        title="Send the invoice",
        source_type="conversation",
        source_ref="session:abc:message:12",
        now="2026-11-14T12:00:00+01:00",
    )

    loaded = isolated_store.get_item(item["id"])

    assert item["id"].startswith("HT-")
    assert isolated_store.PLUGIN_ID == "hermes-tingle"
    assert loaded["title"] == "Send the invoice"
    assert loaded["status"] == "open"
    assert loaded["source_ref"] == "session:abc:message:12"
    assert loaded["next_check_at"]


def test_existing_store_migrates_and_persists_source_trash_completion_signal(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.execute("""CREATE TABLE open_loops (
        id TEXT PRIMARY KEY, title TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'open', owner TEXT NOT NULL DEFAULT 'me',
        priority TEXT NOT NULL DEFAULT 'normal', source_type TEXT NOT NULL DEFAULT 'conversation',
        source_ref TEXT NOT NULL DEFAULT '', source_url TEXT NOT NULL DEFAULT '', due_at TEXT,
        next_check_at TEXT, snoozed_until TEXT, reminder_count INTEGER NOT NULL DEFAULT 0,
        last_notified_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, completed_at TEXT
    )""")
    conn.commit()
    conn.close()

    def connect(_name: str):
        result = sqlite3.connect(db_path)
        result.row_factory = sqlite3.Row
        return result

    monkeypatch.setattr(store, "plugin_db", connect)
    item = store.add_item(
        title="Download the Takeout export",
        source_type="email",
        source_ref="gmail:thread-1",
        completion_signal="source_email_trashed",
    )
    updated = store.update_item(item["id"], completion_signal="")

    assert item["completion_signal"] == "source_email_trashed"
    assert updated["completion_signal"] == ""


def test_lifecycle_failed_legacy_migration_rolls_back_table_rename(tmp_path):
    conn = sqlite3.connect(tmp_path / 'legacy.db')
    conn.execute("CREATE TABLE open_loops (id TEXT PRIMARY KEY, source_type TEXT, source_ref TEXT, status TEXT, next_check_at TEXT)")
    conn.executemany("INSERT INTO open_loops VALUES(?, 'email', 'same', 'open', NULL)", [('OL-AAAAAA',), ('OL-BBBBBB',)])
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        store.ensure_schema(conn)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert 'open_loops' in tables and 'hermes_tingle_items' not in tables
    assert conn.execute('SELECT count(*) FROM open_loops').fetchone()[0] == 2
    conn.close()


def test_duplicate_source_reference_returns_existing_item(isolated_store):
    first = isolated_store.add_item(
        title="Reply to the venue",
        source_type="email",
        source_ref="gmail:thread-1",
        now="2026-11-14T12:00:00+01:00",
    )
    duplicate = isolated_store.add_item(
        title="Reply to the venue",
        source_type="email",
        source_ref="gmail:thread-1",
        now="2026-11-15T12:00:00+01:00",
    )

    assert duplicate["id"] == first["id"]
    assert duplicate["deduplicated"] is True
    assert len(isolated_store.list_items(status="open")) == 1


def test_same_title_with_different_sources_stays_distinct(isolated_store):
    due = "2026-11-20T09:10:00+01:00"
    first = isolated_store.add_item(
        title="Send the signed form",
        source_type="conversation",
        source_ref="session:one",
        due_at=due,
        now="2026-11-14T12:00:00+01:00",
    )
    duplicate = isolated_store.add_item(
        title="Send the signed form",
        source_type="email",
        source_ref="gmail:thread-two",
        due_at=due,
        now="2026-11-15T12:00:00+01:00",
    )

    assert duplicate["id"] != first["id"]
    assert duplicate["deduplicated"] is False
    assert len(isolated_store.list_items(status="open")) == 2


def test_unsourced_same_title_different_summary_remains_distinct_and_retry_is_idempotent(isolated_store):
    first = isolated_store.add_item(title="Prepare", summary="Prepare the contract", due_at="2026-11-20T10:00:00Z")
    second = isolated_store.add_item(title="Prepare", summary="Prepare the invoice", due_at="2026-11-20T10:00:00Z")
    retry = isolated_store.add_item(title="Prepare", summary="Prepare the invoice", due_at="2026-11-20T10:00:00Z")
    assert first["id"] != second["id"]
    assert retry["id"] == second["id"]
    assert retry["deduplicated"] is True
    assert len(isolated_store.list_items(status="open")) == 2


def test_completed_or_snoozed_items_are_not_due(isolated_store):
    due = "2026-11-14T12:00:00+01:00"
    done = isolated_store.add_item(title="Already done", next_check_at=due, now=due)
    snoozed = isolated_store.add_item(title="Snoozed", next_check_at=due, now=due)
    isolated_store.complete_item(done["id"], now=due)
    isolated_store.snooze_item(snoozed["id"], until="2026-11-17T12:00:00+01:00", now=due)

    assert isolated_store.due_items(now=due) == []


def test_due_reminder_reschedules_instead_of_firing_every_poll(isolated_store):
    now = "2026-11-14T12:00:00+01:00"
    item = isolated_store.add_item(
        title="Follow up with the contractor",
        status="waiting",
        next_check_at=now,
        now=now,
    )

    notified = confirmed_reminder(isolated_store, [item["id"]], now=now)

    assert notified[0]["reminder_count"] == 1
    assert datetime.fromisoformat(notified[0]["next_check_at"]) > datetime.fromisoformat(now)
    assert isolated_store.due_items(now=now) == []


def test_source_review_marker_is_idempotent(isolated_store):
    assert review_current_source(isolated_store, "email", "message-1", "v1") is True
    assert isolated_store.source_reviewed("email", "message-1", "v1") is True
    assert review_current_source(isolated_store, "email", "message-1", "v1") is False
    assert isolated_store.source_reviewed("email", "message-1", "v2") is False


def test_reminder_series_stops_after_four_alerts(isolated_store):
    now = datetime.fromisoformat("2026-11-14T12:00:00+01:00")
    item = isolated_store.add_item(
        title="Follow up", next_check_at=now.isoformat(), now=now.isoformat()
    )
    for _ in range(4):
        due = isolated_store.due_items(now=now.isoformat())
        assert [row["id"] for row in due] == [item["id"]]
        confirmed_reminder(isolated_store, [item["id"]], now=now.isoformat())
        current = isolated_store.get_item(item["id"])
        if current["next_check_at"] is None:
            break
        now = datetime.fromisoformat(current["next_check_at"]) + timedelta(minutes=1)

    assert isolated_store.get_item(item["id"])["next_check_at"] is None
    assert isolated_store.due_items(now=(now + timedelta(days=30)).isoformat()) == []


def test_snooze_rearms_a_quieted_item(isolated_store):
    now = datetime.fromisoformat("2026-11-14T12:00:00+01:00")
    item = isolated_store.add_item(title="Follow up", next_check_at=now.isoformat(), now=now.isoformat())
    for _ in range(4):
        confirmed_reminder(isolated_store, [item["id"]], now=now.isoformat())
        current = isolated_store.get_item(item["id"])
        if current["next_check_at"] is None:
            break
        now = datetime.fromisoformat(current["next_check_at"]) + timedelta(minutes=1)

    until = (now + timedelta(days=2)).isoformat()
    snoozed = isolated_store.snooze_item(item["id"], until=until, now=now.isoformat())

    assert snoozed["reminder_count"] == 0
    assert snoozed["next_check_at"] == snoozed["snoozed_until"]


@pytest.mark.parametrize("reply_watch", [False, True])
def test_explicit_check_update_rearms_only_that_reminder_series(isolated_store, monkeypatch, reply_watch):
    from datetime import timezone
    now = datetime(2026, 11, 15, tzinfo=timezone.utc)
    details = ({"status": "waiting", "owner": "other", "source_type": "email",
                "source_ref": "gmail:thread#sent", "completion_signal": "source_email_reply_received"}
               if reply_watch else {})
    item = isolated_store.add_item(title="Follow up", now=now, next_check_at=now + timedelta(days=5), **details)
    for _ in range(4):
        now = datetime.fromisoformat(isolated_store.get_item(item["id"])["next_check_at"])
        current = confirmed_reminder(isolated_store, [item["id"]], now=now)[0]
    assert current["reminder_count"] == 4 and current["next_check_at"] is None
    original_now = isolated_store._now
    monkeypatch.setattr(isolated_store, "_now", lambda value=None: original_now(value) if value is not None else now)
    unchanged = isolated_store.update_item(item["id"], summary="More detail")
    assert unchanged["reminder_count"] == 4 and unchanged["next_check_at"] is None
    target = now + timedelta(days=5)
    rearmed = isolated_store.update_item(item["id"], next_check_at=target)
    assert rearmed["reminder_count"] == 0 and rearmed["last_notified_at"] is None
    assert [row["id"] for row in isolated_store.due_items(now=target)] == [item["id"]]
    notified = confirmed_reminder(isolated_store, [item["id"]], now=target)[0]
    assert notified["reminder_count"] == 1 and notified["next_check_at"] is not None
    if reply_watch:
        assert notified["completion_signal"] == "source_email_reply_received"
        assert notified["reply_interval_seconds"] == 5 * 24 * 60 * 60
        assert datetime.fromisoformat(notified["next_check_at"]) == target + timedelta(days=5)


def test_explicit_reply_check_keeps_short_per_thread_interval(isolated_store, monkeypatch):
    from datetime import timezone
    now = datetime(2026, 11, 15, tzinfo=timezone.utc)
    item = isolated_store.add_item(title="Wait", status="waiting", owner="other", source_type="email",
                                   source_ref="gmail:thread#sent", completion_signal="source_email_reply_received",
                                   now=now, next_check_at=now + timedelta(days=3))
    original_now = isolated_store._now
    monkeypatch.setattr(isolated_store, "_now", lambda value=None: original_now(value) if value is not None else now)
    target = now + timedelta(hours=12)
    updated = isolated_store.update_item(item["id"], next_check_at=target)
    assert updated["reply_interval_seconds"] == 12 * 60 * 60
    notified = confirmed_reminder(isolated_store, [item["id"]], now=target)[0]
    assert datetime.fromisoformat(notified["next_check_at"]) == target + timedelta(hours=12)


def test_email_body_reads_are_recent_candidate_only_and_deduped_per_message(isolated_store):
    now = datetime.fromisoformat("2026-11-14T12:00:00+01:00")
    ids = [f"message-{number}" for number in range(1, 13)]
    isolated_store.register_email_candidates(ids, now=now.isoformat(), account_key='a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf')

    assert isolated_store.claim_email_body_read("not-scanned", now=now.isoformat(), account_key='a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf') is False
    assert all(isolated_store.claim_email_body_read(item_id, now=now.isoformat(), account_key='a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf') for item_id in ids)
    assert isolated_store.claim_email_body_read(ids[0], now=now.isoformat(), account_key='a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf') is False

    later = now + timedelta(hours=25)
    isolated_store.register_email_candidates(ids, now=later.isoformat(), account_key='a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf')
    assert isolated_store.claim_email_body_read(ids[0], now=later.isoformat(), account_key='a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf') is False

    expired = now + timedelta(days=15)
    isolated_store.register_email_candidates(ids, now=expired.isoformat(), account_key='a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf')
    assert isolated_store.claim_email_body_read(ids[0], now=expired.isoformat(), account_key='a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf') is True


def test_body_read_candidate_registration_covers_all_bounded_scan_ids(isolated_store):
    now = datetime.fromisoformat("2026-11-14T12:00:00+01:00")
    ids = [f"message-{number}" for number in range(1, 101)]
    isolated_store.register_email_candidates(ids, now=now.isoformat(), account_key='a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf')

    assert isolated_store.claim_email_body_read(ids[-1], now=now.isoformat(), account_key='a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf') is True


def test_feedback_candidates_require_approval_and_dedupe(isolated_store):
    now = datetime.fromisoformat("2026-11-15T10:00:00+01:00")
    rule = "Never claim a relevant email has no URL from snippets alone."
    first = isolated_store.capture_feedback_candidate(rule, category="links", now=now.isoformat())

    assert first["id"].startswith("FB-")
    assert first["status"] == "pending"
    assert first["occurrences"] == 1
    assert isolated_store.approved_feedback_rules() == []
    assert isolated_store.list_feedback_candidates(status="pending")[0]["id"] == first["id"]

    duplicate = isolated_store.capture_feedback_candidate(
        "  NEVER claim a relevant email has no URL from snippets alone.  ",
        category="links",
        now=(now + timedelta(hours=1)).isoformat(),
    )
    assert duplicate["id"] == first["id"]
    assert duplicate["occurrences"] == 2
    assert duplicate["status"] == "pending"
    assert isolated_store.approved_feedback_rules() == []

    approved = isolated_store.resolve_feedback_candidate(first["id"], "approved", decided_by=("telegram", "u-1", "chat-1"), evidence="test", now=(now + timedelta(hours=2)).isoformat())
    assert approved["status"] == "approved"
    assert approved["approved_at"] is not None
    rules = isolated_store.approved_feedback_rules()
    assert len(rules) == 1
    assert rules[0]["rule_text"] == rule


def test_feedback_remains_separate_across_synthetic_profile_databases(tmp_path, monkeypatch):
    profile = ['a']
    def connect(_name):
        conn = sqlite3.connect(tmp_path / (profile[0] + '.db'))
        conn.row_factory = sqlite3.Row
        return conn
    monkeypatch.setattr(store, 'plugin_db', connect)
    candidate = store.capture_feedback_candidate('Prefer concise reminders.')
    store.resolve_feedback_candidate(candidate['id'], 'approved', decided_by=("telegram", "u-1", "chat-1"), evidence='test')
    store.add_item(title='Goal')
    profile[0] = 'b'
    assert store.approved_feedback_rules() == []
    second = store.capture_feedback_candidate('Ask before a reminder.')
    assert second['status'] == 'pending'
    profile[0] = 'a'
    assert [r['rule_text'] for r in store.approved_feedback_rules()] == ['Prefer concise reminders.']
    assert store.list_feedback_candidates(status='pending') == []


def test_dismissed_feedback_candidate_is_not_applied(isolated_store):
    candidate = isolated_store.capture_feedback_candidate("A one-off correction.", now="2026-11-15T10:00:00+01:00")
    dismissed = isolated_store.resolve_feedback_candidate(candidate["id"], "dismissed", decided_by=("telegram", "u-1", "chat-1"), evidence="test", now="2026-11-15T10:05:00+01:00")

    assert dismissed["status"] == "dismissed"
    assert isolated_store.approved_feedback_rules() == []
    assert isolated_store.list_feedback_candidates(status="pending") == []


def test_legacy_table_migrates_to_hermes_tingle_and_renumbers_legacy_id(tmp_path):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.execute("""CREATE TABLE open_loops (
        id TEXT PRIMARY KEY, title TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'open', owner TEXT NOT NULL DEFAULT 'me',
        priority TEXT NOT NULL DEFAULT 'normal', source_type TEXT NOT NULL DEFAULT 'conversation',
        source_ref TEXT NOT NULL DEFAULT '', source_url TEXT NOT NULL DEFAULT '',
        completion_signal TEXT NOT NULL DEFAULT '', due_at TEXT, next_check_at TEXT,
        snoozed_until TEXT, reminder_count INTEGER NOT NULL DEFAULT 0,
        last_notified_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, completed_at TEXT
    )""")
    conn.execute(
        "INSERT INTO open_loops (id,title,created_at,updated_at) VALUES (?,?,?,?)",
        ("OL-ABC123", "Keep the old identifier", "2026-11-14T12:00:00+01:00", "2026-11-14T12:00:00+01:00"),
    )

    store.ensure_schema(conn)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    migrated = conn.execute("SELECT id,title FROM hermes_tingle_items").fetchone()

    assert "open_loops" not in tables
    assert migrated[1] == "Keep the old identifier"  # row and title preserved
    assert migrated[0].startswith("HT-")  # legacy OL- ID renumbered, not kept
    columns = {row[1] for row in conn.execute("PRAGMA table_info(hermes_tingle_items)")}
    assert not ({"retirement_class", "retirement_last_activity_at", "auto_close_at", "retirement_evidence_key"} & columns)
    assert "hermes_tingle_retirement" in tables
    policy = conn.execute("SELECT 1 FROM hermes_tingle_retirement WHERE item_id=?", (migrated[0],)).fetchone()
    assert policy is None  # next daily review owns legacy backfill
    store.ensure_schema(conn)  # additive migration is idempotent
    conn.execute(
        "INSERT INTO hermes_tingle_items (id,title,created_at,updated_at) VALUES (?,?,?,?)",
        ("HT-DEF456", "Old writer shape", "2026-11-14T12:00:00+01:00", "2026-11-14T12:00:00+01:00"),
    )
    assert conn.execute("SELECT * FROM hermes_tingle_retirement WHERE item_id='HT-DEF456'").fetchone() is None
    conn.close()


def test_legacy_retirement_columns_migrate_into_side_table_and_are_removed():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE hermes_tingle_items (
        id TEXT PRIMARY KEY,title TEXT NOT NULL,summary TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'open',owner TEXT NOT NULL DEFAULT 'me',
        priority TEXT NOT NULL DEFAULT 'normal',source_type TEXT NOT NULL DEFAULT 'conversation',
        source_ref TEXT NOT NULL DEFAULT '',source_url TEXT NOT NULL DEFAULT '',
        completion_signal TEXT NOT NULL DEFAULT '',due_at TEXT,next_check_at TEXT,
        snoozed_until TEXT,reminder_count INTEGER NOT NULL DEFAULT 0,last_notified_at TEXT,
        created_at TEXT NOT NULL,updated_at TEXT NOT NULL,completed_at TEXT,
        hard_deadline INTEGER NOT NULL DEFAULT 0,retirement_class TEXT NOT NULL DEFAULT '',
        retirement_last_activity_at TEXT,auto_close_at TEXT,retirement_evidence_key TEXT
    )""")
    conn.execute(
        """INSERT INTO hermes_tingle_items
           (id,title,status,owner,created_at,updated_at,retirement_class,
            retirement_last_activity_at,auto_close_at,retirement_evidence_key)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        ("HT-ABC123", "Legacy lifecycle row", "open", "me", "2026-08-01T00:00:00+00:00",
         "2026-11-20T00:00:00+00:00", "default", "2026-08-01T00:00:00+00:00",
         "2026-10-30T00:00:00+00:00", "RTE1:" + "ab" * 512),
    )

    store.ensure_schema(conn)

    columns = {row[1] for row in conn.execute("PRAGMA table_info(hermes_tingle_items)")}
    assert not ({"retirement_class", "retirement_last_activity_at", "auto_close_at", "retirement_evidence_key"} & columns)
    policy = conn.execute("SELECT * FROM hermes_tingle_retirement WHERE item_id='HT-ABC123'").fetchone()
    assert policy is not None
    assert policy["retirement_class"] == "default"
    assert policy["last_activity_at"] == "2026-08-01T00:00:00+00:00"
    assert policy["auto_close_at"] == "2026-10-30T00:00:00+00:00"
    assert policy["evidence_key"] == "RTE1:" + "ab" * 512
    conn.close()


def test_legacy_null_evidence_marker_preserves_migrated_policy():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE hermes_tingle_items (
        id TEXT PRIMARY KEY,title TEXT NOT NULL,summary TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'open',owner TEXT NOT NULL DEFAULT 'me',
        priority TEXT NOT NULL DEFAULT 'normal',source_type TEXT NOT NULL DEFAULT 'conversation',
        source_ref TEXT NOT NULL DEFAULT '',source_url TEXT NOT NULL DEFAULT '',
        completion_signal TEXT NOT NULL DEFAULT '',due_at TEXT,next_check_at TEXT,
        snoozed_until TEXT,reminder_count INTEGER NOT NULL DEFAULT 0,last_notified_at TEXT,
        created_at TEXT NOT NULL,updated_at TEXT NOT NULL,completed_at TEXT,
        hard_deadline INTEGER NOT NULL DEFAULT 0,retirement_class TEXT NOT NULL DEFAULT '',
        retirement_last_activity_at TEXT,auto_close_at TEXT,retirement_evidence_key TEXT
    )""")
    conn.execute(
        """INSERT INTO hermes_tingle_items
           (id,title,status,owner,created_at,updated_at,retirement_class,
            retirement_last_activity_at,auto_close_at,retirement_evidence_key)
           VALUES(?,?,?,?,?,?,?,?,?,NULL)""",
        ("HT-NULLKEY", "Legacy null marker", "open", "me", "2026-08-04T00:00:00+00:00",
         "2026-11-23T00:00:00+00:00", "default", "2026-11-23T00:00:00+00:00",
         "2027-02-21T00:00:00+00:00"),
    )

    store.ensure_schema(conn)

    policy = conn.execute("SELECT * FROM hermes_tingle_retirement WHERE item_id='HT-NULLKEY'").fetchone()
    assert policy is not None
    assert policy["last_activity_at"] == "2026-11-23T00:00:00+00:00"
    assert policy["auto_close_at"] == "2027-02-21T00:00:00+00:00"
    assert policy["evidence_key"] == ""
    conn.close()


def test_alert_emission_lifecycle(isolated_store):
    emitted = "2026-01-01T00:00:00+00:00"
    emission_id = isolated_store.record_alert_emission(seed_outbox_items(isolated_store, ["ht-aaaaaa", "HT-BBBBBB"], now=emitted), now=emitted)
    assert isinstance(emission_id, int)
    recent = isolated_store.unsurfaced_emissions(older_than="2026-01-01T01:00:00+00:00")
    assert [row["id"] for row in recent] == [emission_id]
    assert recent[0]["acked_at"] is None and recent[0]["surfaced_at"] is None

    isolated_store.mark_emissions_surfaced([emission_id], now="2026-01-01T02:00:00+00:00")
    # Recently surfaced: the disclosure note went out, so it stays quiet for now.
    assert isolated_store.unsurfaced_emissions(older_than="2026-01-01T01:30:00+00:00") == []

    isolated_store.mark_emission_flushed(emission_id)
    receipt = isolated_store.get_alert_emission(emission_id)["receipt_token"]
    acked = isolated_store.acknowledge_alert_emissions(receipt, now="2026-01-01T04:00:00+00:00")
    assert acked == 1
    assert isolated_store.acknowledge_alert_emissions(now="2026-01-01T05:00:00+00:00") == 0


def test_unsurfaced_emissions_respect_the_cutoff(isolated_store):
    isolated_store.record_alert_emission(seed_outbox_items(isolated_store, ["HT-AAAAAA"], now="2026-01-01T00:00:00+00:00"), now="2026-01-01T00:00:00+00:00")
    isolated_store.record_alert_emission(seed_outbox_items(isolated_store, ["HT-BBBBBB"], now="2026-01-01T10:00:00+00:00"), now="2026-01-01T10:00:00+00:00")
    rows = isolated_store.unsurfaced_emissions(older_than="2026-01-01T06:00:00+00:00")
    assert len(rows) == 1


def test_record_alert_emission_requires_item_ids(isolated_store):
    import pytest
    with pytest.raises(ValueError, match="item_ids"):
        isolated_store.record_alert_emission([], now="2026-01-01T00:00:00+00:00")


def test_disclosure_is_bounded_for_still_unconfirmed_batches(isolated_store):
    # surfaced_at records inclusion in a note, not assured visibility, so a batch
    # surfaced longer than the grace cutoff qualifies for disclosure again.
    emission_id = isolated_store.record_alert_emission(seed_outbox_items(isolated_store, ["HT-AAAAAA"], now="2026-01-01T00:00:00+00:00"), now="2026-01-01T00:00:00+00:00")
    isolated_store.mark_emissions_surfaced([emission_id], now="2026-01-01T01:00:00+00:00")
    assert isolated_store.unsurfaced_emissions(older_than="2026-01-01T00:30:00+00:00") == []
    rows = isolated_store.unsurfaced_emissions(older_than="2026-01-01T07:00:00+00:00")
    assert rows == []  # one inclusion per batch; recovery remains queryable


def test_skip_step_validation(isolated_store):
    import pytest
    goal = isolated_store.add_item(title='Goal', source_type='manual')
    isolated_store.add_step(goal['id'], title='Only step', why='Because')
    with pytest.raises(ValueError, match='positive integer'):
        isolated_store.skip_step(goal['id'], 0)
    with pytest.raises(ValueError, match='active goal and step not found'):
        isolated_store.skip_step(goal['id'], 9)
    with pytest.raises(ValueError, match='active goal and step not found'):
        isolated_store.skip_step('HT-FFFFFF', 1)
    isolated_store.skip_step(goal['id'], 1)
    with pytest.raises(ValueError, match='already skipped'):
        isolated_store.skip_step(goal['id'], 1)


def test_completed_step_cannot_be_skipped(isolated_store):
    import pytest
    goal = isolated_store.add_item(title='Goal', source_type='manual')
    isolated_store.add_step(goal['id'], title='Only step', why='Because')
    isolated_store.complete_step(goal['id'], 1)
    with pytest.raises(ValueError, match='already complete'):
        isolated_store.skip_step(goal['id'], 1)


def test_skipping_an_earlier_step_unblocks_later_completion_order(isolated_store):
    goal = isolated_store.add_item(title='Goal', source_type='manual')
    isolated_store.add_step(goal['id'], title='First', why='One')
    isolated_store.add_step(goal['id'], title='Second', why='Two')
    isolated_store.skip_step(goal['id'], 1)
    step = isolated_store.complete_step(goal['id'], 2)
    assert step['complete'] is True


def test_legacy_ol_ids_renumber_preserving_references(tmp_path, monkeypatch):
    import json as _json
    db_path = tmp_path / "data.db"

    def connect(_name):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(store, "plugin_db", connect)
    conn = store._connect()
    stamp = "2026-11-14T12:00:00+01:00"
    conn.execute("INSERT INTO hermes_tingle_items (id,title,created_at,updated_at) VALUES ('OL-ABC123','Legacy task',?,?)", (stamp, stamp))
    conn.execute("INSERT INTO hermes_tingle_items (id,title,created_at,updated_at) VALUES ('HT-111111','Current task',?,?)", (stamp, stamp))
    conn.execute("INSERT INTO hermes_tingle_steps (goal_id,position,title,why) VALUES ('OL-ABC123',1,'First step','why it matters')")
    conn.execute("INSERT INTO hermes_tingle_documents (item_id,position,name,created_at,updated_at) VALUES ('OL-ABC123',1,'Passport',?,?)", (stamp, stamp))
    conn.execute("INSERT INTO hermes_tingle_requirements (item_id,position,text,created_at,updated_at) VALUES ('OL-ABC123',1,'Budget approved',?,?)", (stamp, stamp))
    conn.execute("INSERT INTO hermes_tingle_options (item_id,position,name,created_at,updated_at) VALUES ('OL-ABC123',1,'Option A',?,?)", (stamp, stamp))
    conn.execute("INSERT INTO hermes_tingle_bookings (item_id,position,kind,name,cancel_item_id,created_at,updated_at) VALUES ('OL-ABC123',1,'flight','Flight','OL-ABC123',?,?)", (stamp, stamp))
    conn.execute("INSERT INTO recurrence_completions (item_id,occurrence_due_at,completed_at) VALUES ('OL-ABC123','2026-11-15T09:00:00+01:00','2026-11-15T09:05:00+01:00')")
    conn.execute("INSERT INTO alert_emissions (item_ids,emitted_at) VALUES ('[\"OL-ABC123\", \"HT-111111\"]','2026-11-15T09:00:00+01:00')")
    conn.execute("INSERT INTO approval_requests (id,action_type,target,parameters_json,parameters_hash,status,created_at,expires_at) VALUES ('AR-LEG11','propose_option','OL-ABC123','{\"item_id\": \"OL-ABC123\"}','hash-legacy','pending',?,'2027-01-01T00:00:00+00:00')", (stamp,))
    conn.commit()
    conn.close()

    conn = store._connect()  # second connect runs the renumber migration
    by_title = {r["title"]: r["id"] for r in conn.execute("SELECT id,title FROM hermes_tingle_items")}
    new_id = by_title["Legacy task"]
    assert new_id.startswith("HT-") and len(new_id) == 9
    assert by_title["Current task"] == "HT-111111"
    assert conn.execute("SELECT goal_id FROM hermes_tingle_steps").fetchone()[0] == new_id
    assert conn.execute("SELECT item_id FROM hermes_tingle_documents").fetchone()[0] == new_id
    assert conn.execute("SELECT item_id FROM hermes_tingle_requirements").fetchone()[0] == new_id
    assert conn.execute("SELECT item_id FROM hermes_tingle_options").fetchone()[0] == new_id
    booking = conn.execute("SELECT item_id,cancel_item_id FROM hermes_tingle_bookings").fetchone()
    assert booking[0] == new_id and booking[1] == new_id
    assert conn.execute("SELECT item_id FROM recurrence_completions").fetchone()[0] == new_id
    assert _json.loads(conn.execute("SELECT item_ids FROM alert_emissions").fetchone()[0]) == [new_id, "HT-111111"]
    assert conn.execute("SELECT status FROM approval_requests WHERE id='AR-LEG11'").fetchone()[0] == "expired"
    audit = conn.execute("SELECT detail FROM action_audit WHERE approval_id='AR-LEG11' AND outcome='expired'").fetchone()
    assert audit is not None and "OL-ABC123" in audit[0] and new_id in audit[0]
    meta = _json.loads(conn.execute("SELECT value FROM plugin_meta WHERE key='legacy_id_migration'").fetchone()[0])
    assert meta["mapping"] == {"OL-ABC123": new_id}
    conn.close()

    conn = store._connect()  # idempotent: nothing left to migrate, no double expiry
    assert conn.execute("SELECT count(*) FROM hermes_tingle_items WHERE id LIKE 'OL-%'").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM action_audit WHERE approval_id='AR-LEG11'").fetchone()[0] == 1
    conn.close()


def test_ol_ids_refused_by_item_controls(isolated_store):
    added = isolated_store.add_item(title="Track the renewal")
    with pytest.raises(KeyError):
        isolated_store.complete_item("OL-ABC123")
    assert isolated_store.get_item(added["id"])["status"] == "open"


def test_legacy_renumber_succeeds_under_foreign_keys_on(tmp_path, monkeypatch):
    db_path = tmp_path / "fk.db"

    def connect(_name):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    monkeypatch.setattr(store, "plugin_db", connect)
    conn = store._connect()
    stamp = "2026-11-14T12:00:00+01:00"
    conn.execute("INSERT INTO hermes_tingle_items (id,title,source_type,source_ref,created_at,updated_at) VALUES ('OL-ABC123','Legacy task','email','gmail:thread-1',?,?)", (stamp, stamp))
    conn.execute("INSERT INTO hermes_tingle_items (id,title,created_at,updated_at) VALUES ('OL-DEF456','Second legacy task',?,?)", (stamp, stamp))
    conn.execute("INSERT INTO hermes_tingle_steps (goal_id,position,title,why) VALUES ('OL-ABC123',1,'First step','why it matters')")
    conn.execute("INSERT INTO hermes_tingle_steps (goal_id,position,title,why,wait_item_id) VALUES ('OL-DEF456',1,'Wait step','why it matters','OL-ABC123')")
    conn.execute("INSERT INTO approval_requests (id,action_type,target,parameters_json,parameters_hash,status,created_at,expires_at) VALUES ('AR-APP11','propose_option','OL-ABC123','{}','hash-approved','approved',?,'2027-01-01T00:00:00+00:00')", (stamp,))
    conn.execute("INSERT INTO approval_requests (id,action_type,target,parameters_json,parameters_hash,status,created_at,expires_at,consumed_at) VALUES ('AR-CON11','propose_option','OL-ABC123','{}','hash-consumed','consumed',?,'2027-01-01T00:00:00+00:00',?)", (stamp, stamp))
    conn.commit()
    conn.close()

    conn = store._connect()  # FK enforced, exactly as pinned Hermes plugin_storage
    by_title = {r["title"]: r["id"] for r in conn.execute("SELECT id,title FROM hermes_tingle_items")}
    first, second = by_title["Legacy task"], by_title["Second legacy task"]
    assert first.startswith("HT-") and second.startswith("HT-") and first != second
    # the unique (source_type, source_ref) slot moved to the new row exactly once
    sourced = conn.execute("SELECT id FROM hermes_tingle_items WHERE source_ref='gmail:thread-1'").fetchall()
    assert [r[0] for r in sourced] == [first]
    steps = {r["goal_id"]: r["wait_item_id"] for r in conn.execute("SELECT goal_id,wait_item_id FROM hermes_tingle_steps")}
    assert steps[first] is None
    assert steps[second] == first  # cross-item reference between two renumbered items
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    statuses = {r[0]: r[1] for r in conn.execute("SELECT id,status FROM approval_requests")}
    assert statuses == {"AR-APP11": "expired", "AR-CON11": "consumed"}  # approved invalidated, consumed immutable
    conn.close()

    conn = store._connect()  # idempotent under FK ON
    assert conn.execute("SELECT count(*) FROM hermes_tingle_items WHERE id LIKE 'OL-%'").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM action_audit WHERE outcome='expired'").fetchone()[0] == 1
    conn.close()


def test_legacy_renumber_allocation_failure_rolls_back(tmp_path, monkeypatch):
    db_path = tmp_path / "collision.db"

    def connect(_name):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    monkeypatch.setattr(store, "plugin_db", connect)
    conn = store._connect()
    stamp = "2026-11-14T12:00:00+01:00"
    conn.execute("INSERT INTO hermes_tingle_items (id,title,created_at,updated_at) VALUES ('OL-ABC123','Legacy task',?,?)", (stamp, stamp))
    conn.execute("INSERT INTO hermes_tingle_items (id,title,created_at,updated_at) VALUES ('HT-FFFFFF','Current task',?,?)", (stamp, stamp))
    conn.commit()
    conn.close()

    monkeypatch.setattr(store.secrets, "token_hex", lambda n: "ffffff")  # every candidate collides
    with pytest.raises(RuntimeError, match="could not allocate"):
        store._connect()

    monkeypatch.undo()
    monkeypatch.setattr(store, "plugin_db", connect)
    conn = store._connect()  # rollback left the database migratable and intact
    ids = {r[0] for r in conn.execute("SELECT id FROM hermes_tingle_items")}
    assert len(ids) == 2 and "HT-FFFFFF" in ids and any(i.startswith("HT-") for i in ids)
    assert conn.execute("SELECT count(*) FROM action_audit").fetchone()[0] == 0
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()
