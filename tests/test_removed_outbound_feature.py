"""Public beta has no third-party outbound messaging surface."""

import ast
import json
from pathlib import Path
import sqlite3

import schemas
import hermes_tingle as plugin
import hermes_tingle_store as store
import hermes_tingle_wiring as wiring
import hermes_tingle_discovery as discovery


def test_outbound_actions_are_not_exposed_but_approvals_remain():
    properties = schemas.HERMES_TINGLE["parameters"]["properties"]
    actions = properties["action"]["enum"]
    assert "prepare_coordination_send" not in actions
    assert "coordination_replies" not in actions
    assert {"request_approval", "list_approvals"} <= set(actions)
    assert not {"contact_id", "coordination_purpose", "coordination_words", "unreviewed_only"} & set(properties)


def test_removed_tool_actions_and_commands_do_not_execute():
    for action in ("prepare_coordination_send", "coordination_replies"):
        result = json.loads(plugin.handle_tool(None, {"action": action}))
        assert result["success"] is False
        assert "unsupported action" in result["error"]
    for command in ("contacts", "contact add Alex telegram:123", "dispatch"):
        assert "didn't catch that command" in plugin.handle_command(None, command)


def test_new_store_keeps_approvals_and_has_no_messaging_tables():
    with store._connect() as conn:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "approval_requests" in names
    assert not {"coordination_contacts", "coordination_grants", "inbound_messages", "outbound_messages"} & names
    assert not hasattr(store, "execute_coordination_sends")


def test_legacy_records_are_not_destroyed_or_reactivated_on_open(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy.db"

    def connect(_name):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    monkeypatch.setattr(store, "plugin_db", connect)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript("""
            CREATE TABLE coordination_contacts (
                id TEXT PRIMARY KEY, label TEXT NOT NULL, platform TEXT NOT NULL,
                handle TEXT NOT NULL, created_at TEXT NOT NULL,
                created_by TEXT NOT NULL DEFAULT '', revoked_at TEXT);
            CREATE TABLE coordination_grants (
                id TEXT PRIMARY KEY, contact_id TEXT NOT NULL REFERENCES coordination_contacts(id),
                purpose TEXT NOT NULL, bounds TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','revoked')),
                created_at TEXT NOT NULL, created_by TEXT NOT NULL DEFAULT '', revoked_at TEXT);
            CREATE TABLE inbound_messages (
                id TEXT PRIMARY KEY, contact_id TEXT NOT NULL, platform TEXT NOT NULL,
                handle TEXT NOT NULL, body TEXT NOT NULL, received_at TEXT NOT NULL, reviewed_at TEXT);
            CREATE TABLE outbound_messages (
                id TEXT PRIMARY KEY, contact_id TEXT NOT NULL REFERENCES coordination_contacts(id),
                approval_id TEXT NOT NULL, purpose TEXT NOT NULL, words TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'prepared' CHECK(status IN ('prepared','sent','failed','cancelled','outcome_unknown')),
                created_at TEXT NOT NULL, sent_at TEXT, error TEXT NOT NULL DEFAULT '', reply_consumed_by TEXT);
            INSERT INTO coordination_contacts VALUES ('CT-OLD','Synthetic','telegram','4242','2026-01-01','legacy',NULL);
            INSERT INTO coordination_grants VALUES ('GR-OLD','CT-OLD','synthetic purpose','', 'active','2026-01-01','legacy',NULL);
            INSERT INTO inbound_messages VALUES ('IN-OLD','CT-OLD','telegram','4242','synthetic reply','2026-11-21T23:00:00+00:00',NULL);
            INSERT INTO outbound_messages VALUES ('OUT-PREPARED','CT-OLD','AR-PREP','synthetic purpose','synthetic prepared words','prepared','2026-11-21T22:00:00+00:00',NULL,'',NULL);
            INSERT INTO outbound_messages VALUES ('OUT-SENT','CT-OLD','AR-SENT','synthetic purpose','synthetic sent words','sent','2026-11-21T22:00:00+00:00','2026-11-21T23:00:00+00:00','',NULL);
        """)

    tables = ("coordination_contacts", "coordination_grants", "inbound_messages", "outbound_messages")
    def snapshot():
        with sqlite3.connect(db_path) as conn:
            return {name: [tuple(row) for row in conn.execute(f"SELECT * FROM {name} ORDER BY id")]
                    for name in tables}
    before = snapshot()
    monkeypatch.setattr(plugin, "load_settings", lambda: {
        "owner_principals": ["telegram:4242"],
        "coordination_enabled": True,  # stale legacy opt-in must not revive capture
    })

    # Opening storage performs ordinary Tingle migrations, but does not drop or
    # reactivate legacy messaging rows. Admitted inbound still reaches source
    # discovery; it cannot be captured as a third-party reply or dispatched.
    with store._connect():
        pass
    monkeypatch.setattr(store, "note_known_chat", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(discovery, "observe_source", lambda *_args, **_kwargs: None)
    wiring.observe_admitted_inbound(
        platform="telegram", source={"chat_id": "4242", "user_id": "4242", "chat_type": "dm"},
        message_id="MSG-NEW", text="synthetic reply after upgrade")

    assert snapshot() == before
    assert {row[0] for row in before["outbound_messages"]} == {"OUT-PREPARED", "OUT-SENT"}
    assert not hasattr(store, "execute_coordination_sends")
    assert not hasattr(store, "ingest_coordination_reply")


def test_eligible_legacy_state_never_fires_send_or_reply_hooks(tmp_path, monkeypatch):
    """The negative path is eligible: owner DM, approved legacy request, fresh unconsumed row.

    Spies sit on the exact historical seams. Restoring either legacy call makes
    this test fail; the liveness probe at the end proves the spies are armed.
    """
    db_path = tmp_path / "eligible-legacy.db"

    def connect(_name):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    monkeypatch.setattr(store, "plugin_db", connect)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript("""
            CREATE TABLE coordination_contacts (
                id TEXT PRIMARY KEY, label TEXT NOT NULL, platform TEXT NOT NULL,
                handle TEXT NOT NULL, created_at TEXT NOT NULL,
                created_by TEXT NOT NULL DEFAULT '', revoked_at TEXT);
            CREATE TABLE coordination_grants (
                id TEXT PRIMARY KEY, contact_id TEXT NOT NULL REFERENCES coordination_contacts(id),
                purpose TEXT NOT NULL, bounds TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','revoked')),
                created_at TEXT NOT NULL, created_by TEXT NOT NULL DEFAULT '', revoked_at TEXT);
            CREATE TABLE inbound_messages (
                id TEXT PRIMARY KEY, contact_id TEXT NOT NULL, platform TEXT NOT NULL,
                handle TEXT NOT NULL, body TEXT NOT NULL, received_at TEXT NOT NULL, reviewed_at TEXT);
            CREATE TABLE outbound_messages (
                id TEXT PRIMARY KEY, contact_id TEXT NOT NULL REFERENCES coordination_contacts(id),
                approval_id TEXT NOT NULL, purpose TEXT NOT NULL, words TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'prepared' CHECK(status IN ('prepared','sent','failed','cancelled','outcome_unknown')),
                created_at TEXT NOT NULL, sent_at TEXT, error TEXT NOT NULL DEFAULT '', reply_consumed_by TEXT);
            CREATE TABLE approval_requests (
                id TEXT PRIMARY KEY, action_type TEXT NOT NULL, target TEXT NOT NULL,
                parameters_json TEXT NOT NULL, parameters_hash TEXT NOT NULL,
                status TEXT NOT NULL, requested_by TEXT NOT NULL DEFAULT '',
                approver_platform TEXT, approver_user_id TEXT, approver_chat_id TEXT,
                created_at TEXT NOT NULL, expires_at TEXT NOT NULL, decided_at TEXT,
                approval_evidence TEXT NOT NULL DEFAULT '', consumed_at TEXT);
            INSERT INTO coordination_contacts VALUES ('CT-OLD','Synthetic','telegram','4242','2026-01-01','legacy',NULL);
            INSERT INTO coordination_grants VALUES ('GR-OLD','CT-OLD','synthetic purpose','','active','2026-01-01','legacy',NULL);
            INSERT INTO inbound_messages VALUES ('IN-OLD','CT-OLD','telegram','4242','synthetic reply','2026-11-21T23:00:00+00:00',NULL);
            INSERT INTO approval_requests VALUES ('AR-1A2B3C','send_message','telegram:4242','{}','synthetic-hash','pending','legacy','telegram','4242','4242','2026-11-21T22:00:00+00:00','2099-01-01T00:00:00+00:00',NULL,'',NULL);
            INSERT INTO outbound_messages VALUES ('OUT-PREPARED','CT-OLD','AR-1A2B3C','synthetic purpose','synthetic prepared words','prepared','2026-11-21T22:00:00+00:00',NULL,'',NULL);
            INSERT INTO outbound_messages VALUES ('OUT-SENT','CT-OLD','AR-SENT','synthetic purpose','synthetic sent words','sent','2026-11-21T22:00:00+00:00','2026-11-21T23:00:00+00:00','',NULL);
        """)

    monkeypatch.setattr(plugin, "load_settings", lambda: {
        "owner_principals": ["telegram:4242"],
        "coordination_enabled": True,  # stale legacy policy must not revive removed behavior
    })
    called = []
    monkeypatch.setattr(store, "execute_coordination_sends",
                        lambda *a, **k: called.append("send"), raising=False)
    monkeypatch.setattr(store, "ingest_coordination_reply",
                        lambda *a, **k: called.append("reply"), raising=False)
    monkeypatch.setattr(store, "note_known_chat", lambda *a, **k: None)
    monkeypatch.setattr(discovery, "observe_source", lambda *a, **k: None)

    class Owner:
        platform = "telegram"
        user_id = "4242"
        chat_id = "4242"
        chat_type = "dm"
        is_bot = False

    with sqlite3.connect(db_path) as conn:
        outbound_before = [tuple(r) for r in conn.execute("SELECT * FROM outbound_messages ORDER BY id")]
        inbound_before = [tuple(r) for r in conn.execute("SELECT * FROM inbound_messages ORDER BY id")]
    reply = plugin.handle_command(None, "approve AR-1A2B3C", source=Owner())
    assert "Recorded only" in reply and "no external action is executed or sent" in reply
    wiring.observe_admitted_inbound(
        platform="telegram",
        source={"chat_id": "4242", "user_id": "4242", "chat_type": "dm"},
        message_id="MSG-ELIGIBLE", text="synthetic reply after upgrade")

    assert called == []
    with sqlite3.connect(db_path) as conn:
        assert [tuple(r) for r in conn.execute("SELECT * FROM outbound_messages ORDER BY id")] == outbound_before
        assert [tuple(r) for r in conn.execute("SELECT * FROM inbound_messages ORDER BY id")] == inbound_before
        assert [tuple(r) for r in conn.execute(
            "SELECT status,consumed_at FROM approval_requests WHERE id='AR-1A2B3C'")] == [("approved", None)]
        assert [tuple(r) for r in conn.execute(
            "SELECT outcome FROM action_audit WHERE approval_id='AR-1A2B3C'")] == [("approved",)]

    # Spy liveness: a restored historical call at either seam must be detected.
    getattr(store, "execute_coordination_sends")(None)
    getattr(store, "ingest_coordination_reply")(platform="telegram", handle="4242", body="synthetic",
                                                 message_id="MSG-ELIGIBLE")
    assert called == ["send", "reply"]


def test_production_code_has_no_removed_messaging_symbols():
    root = Path(__file__).resolve().parents[1]
    banned = {
        "execute_coordination_sends", "ingest_coordination_reply",
        "_ingest_coordination_reply_once", "prepare_coordination_send",
        "coordination_replies", "coordination_preview_safe", "coordination_enabled",
        "coordination_contacts", "coordination_grants",
        "inbound_messages", "outbound_messages",
    }
    hits = []
    paths = sorted(root.glob("*.py")) + sorted((root / "scripts").glob("*.py"))
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                name = node.id
            elif isinstance(node, ast.Attribute):
                name = node.attr
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = node.name
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                name = node.value
            else:
                continue
            if name in banned:
                hits.append(f"{path.name}:{getattr(node, 'lineno', 0)}:{name}")
    assert hits == [], hits


def test_public_copy_describes_record_only_approvals_and_no_external_messaging():
    root = Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text(encoding="utf-8")
    tracker = (root / "docs" / "feature-tracker.md").read_text(encoding="utf-8")
    description = schemas.HERMES_TINGLE["description"]
    bug_template = (root / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml").read_text(encoding="utf-8")
    assert "Tingle does not send messages to external counterparties" in readme
    assert "this beta only records approve/reject decisions" in readme
    assert "does not send messages to external users or bots" in tracker
    assert "no executor that carries them out" in tracker
    assert "does not initiate messages to external contacts or agents" in description
    assert "owner reminders and reports use the configured destination" in description
    assert "coordination attempt" not in bug_template
