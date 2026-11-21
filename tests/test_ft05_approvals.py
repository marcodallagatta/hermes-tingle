"""FT-05: approvals and action audit for future external actions.

The model registers exact intent; only an explicit user command approves it;
the approval binds the exact action/target/parameters, expires, and is
consumed exactly once. Every transition lands in the audit trail.
"""
import json
import sqlite3

import pytest

import hermes_tingle_store as store
import hermes_tingle


from datetime import datetime, timedelta, timezone

# Fixture clock is relative to the real clock: the 09:00 anchor sits four hours in the past,
# so approvals made through the command handler (which uses the real clock) never look expired.
_BASE = (datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(hours=4))


def _t(hour, minute=0):
    return (_BASE + timedelta(hours=hour - 9, minutes=minute)).isoformat()


@pytest.fixture(autouse=True)
def configured_owner(monkeypatch):
    monkeypatch.setattr(hermes_tingle, "load_settings", lambda: {"owner_principals": ["telegram:424242"]})


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    db_path = tmp_path / "tingle.db"

    def connect(_name):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(store, "plugin_db", connect)
    store.enable_source("email", enabled_by="synthetic fixture consent")
    store.enable_source("calendar", enabled_by="synthetic fixture consent")
    return store


def _request(isolated_store, **overrides):
    params = dict(action_type="send_email", target="vendor@example.invalid",
                  parameters={"subject": "Renewal quote", "reference": "Q-123"},
                  ttl_hours=24, requested_by="test", approver=("telegram", "424242", "1001"),
                  now=_t(9, 0))
    params.update(overrides)
    return isolated_store.request_approval(**params)


def _approve(isolated_store, request_id, **kwargs):
    return isolated_store.decide_approval(request_id, "approved", evidence="/tingle approve command",
                                          decided_by=("telegram", "424242", "1001"),
                                          now=kwargs.pop("now", _t(10, 0)))


def _consume(isolated_store, req, **overrides):
    params = dict(action_type="send_email", target="vendor@example.invalid",
                  parameters={"subject": "Renewal quote", "reference": "Q-123"},
                  now=_t(11, 0))
    params.update(overrides)
    return isolated_store.consume_approval(req["id"], **params)


def _audit_rows(isolated_store):
    conn = isolated_store.plugin_db("hermes-tingle")
    rows = conn.execute("SELECT * FROM action_audit ORDER BY id").fetchall()
    conn.close()
    return [dict(row) for row in rows]


def test_request_registers_pending_with_expiry(isolated_store):
    req = _request(isolated_store)
    assert req["status"] == "pending"
    assert req["expires_at"] == _t(33, 0)
    assert req["requested_by"] == "test"
    assert store.APPROVAL_ID_RE.fullmatch(req["id"])
    assert [row["outcome"] for row in _audit_rows(isolated_store)] == ["requested"]


def test_identical_pending_request_deduplicates(isolated_store):
    first = _request(isolated_store)
    second = _request(isolated_store, now=_t(9, 30))
    assert second["id"] == first["id"] and second["deduplicated"] is True


def test_request_validation(isolated_store):
    with pytest.raises(ValueError, match="snake_case"):
        _request(isolated_store, action_type="Send Email!")
    with pytest.raises(ValueError, match="1-500"):
        _request(isolated_store, target="")
    with pytest.raises(ValueError, match="object"):
        _request(isolated_store, parameters=["not", "a", "dict"])
    with pytest.raises(ValueError, match="too large"):
        _request(isolated_store, parameters={"blob": "x" * 5000})
    with pytest.raises(ValueError, match="ttl_hours"):
        _request(isolated_store, ttl_hours=0)
    with pytest.raises(ValueError, match="ttl_hours"):
        _request(isolated_store, ttl_hours=200)


def test_approve_requires_evidence(isolated_store):
    req = _request(isolated_store)
    with pytest.raises(ValueError, match="evidence"):
        isolated_store.decide_approval(req["id"], "approved", evidence="", now=_t(10, 0))


def test_approve_then_consume_happy_path(isolated_store):
    req = _request(isolated_store)
    approved = _approve(isolated_store, req["id"])
    assert approved["status"] == "approved"
    assert approved["approval_evidence"] == "/tingle approve command"
    consumed = _consume(isolated_store, req)
    assert consumed["status"] == "consumed" and consumed["consumed_at"]
    outcomes = [row["outcome"] for row in _audit_rows(isolated_store)]
    assert outcomes == ["requested", "approved", "consumed"]


def test_consumption_is_one_shot(isolated_store):
    req = _request(isolated_store)
    _approve(isolated_store, req["id"])
    _consume(isolated_store, req)
    with pytest.raises(ValueError, match="consumed"):
        _consume(isolated_store, req, now=_t(11, 5))
    outcomes = [row["outcome"] for row in _audit_rows(isolated_store)]
    assert outcomes[-1] == "denied_not_approved"


def test_consume_binds_exact_parameters(isolated_store):
    req = _request(isolated_store)
    _approve(isolated_store, req["id"])
    with pytest.raises(ValueError, match="exactly"):
        _consume(isolated_store, req, parameters={"subject": "Changed!", "reference": "Q-123"})
    with pytest.raises(ValueError, match="exactly"):
        _consume(isolated_store, req, target="other@example.invalid")
    with pytest.raises(ValueError, match="exactly"):
        _consume(isolated_store, req, action_type="delete_email")
    outcomes = [row["outcome"] for row in _audit_rows(isolated_store)]
    assert outcomes.count("denied_mismatch") == 3
    assert req["id"] and isolated_store.list_approvals("approved")[0]["status"] == "approved"


def test_unapproved_request_cannot_be_consumed(isolated_store):
    req = _request(isolated_store)
    with pytest.raises(ValueError, match="pending"):
        _consume(isolated_store, req)
    rejected = isolated_store.decide_approval(req["id"], "rejected", evidence="/tingle reject command",
                                          decided_by=("telegram", "424242", "1001"),
                                              now=_t(10, 0))
    assert rejected["status"] == "rejected"
    with pytest.raises(ValueError, match="rejected"):
        _consume(isolated_store, req)


def test_decision_only_once(isolated_store):
    req = _request(isolated_store)
    _approve(isolated_store, req["id"])
    with pytest.raises(ValueError, match="not pending"):
        isolated_store.decide_approval(req["id"], "rejected", evidence="/tingle reject command",
                                       decided_by=("telegram", "424242", "1001"),
                                       now=_t(10, 30))


def test_expired_requests_cannot_be_approved_or_consumed(isolated_store):
    req = _request(isolated_store, ttl_hours=2)
    with pytest.raises(ValueError, match="expired"):
        isolated_store.decide_approval(req["id"], "approved", evidence="/tingle approve command",
                                       decided_by=("telegram", "424242", "1001"), now=_t(12, 0))
    req2 = _request(isolated_store, parameters={"subject": "Other", "reference": "Q-9"}, ttl_hours=2)
    _approve(isolated_store, req2["id"])
    with pytest.raises(ValueError, match="expired"):
        _consume(isolated_store, req2, parameters={"subject": "Other", "reference": "Q-9"},
                 now=_t(12, 0))


def test_unknown_request_ids(isolated_store):
    with pytest.raises(KeyError):
        isolated_store.decide_approval("AR-FFFFFF", "approved", evidence="x",
                                       decided_by=("telegram", "424242", "1001"), now=_t(10, 0))
    with pytest.raises(KeyError):
        isolated_store.consume_approval("AR-FFFFFF", "send_email", "t", {}, now=_t(10, 0))


def test_list_approvals_filters_and_orders(isolated_store):
    first = _request(isolated_store, now=_t(9, 0))
    second = _request(isolated_store, parameters={"subject": "Other", "reference": "Q-9"},
                      now=_t(9, 30))
    _approve(isolated_store, first["id"])
    pending = isolated_store.list_approvals("pending", now=_t(10, 0))
    assert [row["id"] for row in pending] == [second["id"]]
    all_rows = isolated_store.list_approvals(now=_t(10, 0))
    assert [row["id"] for row in all_rows] == [second["id"], first["id"]]
    with pytest.raises(ValueError, match="status"):
        isolated_store.list_approvals("bogus")


def test_list_approvals_filters_exact_approver_conversation(isolated_store):
    first = isolated_store.request_approval(
        "send_message", "CT-AAAAAA", {"words": "owner draft"},
        requested_by="owner", approver=("telegram", "424242", "1001"), now=_t(9, 0))
    isolated_store.request_approval(
        "send_message", "CT-BBBBBB", {"words": "other conversation draft"},
        requested_by="same owner", approver=("telegram", "424242", "2002"), now=_t(9, 1))
    rows = isolated_store.list_approvals("pending", approver=("telegram", "424242", "1001"), now=_t(9, 2))
    assert [row["id"] for row in rows] == [first["id"]]
    assert "other conversation draft" not in str(rows)


def test_audit_action_records_outcomes(isolated_store):
    req = _request(isolated_store)
    _approve(isolated_store, req["id"])
    _consume(isolated_store, req)
    isolated_store.audit_action(req["id"], "send_email", "vendor@example.invalid",
                                {"subject": "Renewal quote", "reference": "Q-123"}, "failed",
                                detail="SMTP unreachable", now=_t(11, 1))
    last = _audit_rows(isolated_store)[-1]
    assert last["outcome"] == "failed" and last["detail"] == "SMTP unreachable"
    assert last["parameters_hash"] == req["parameters_hash"]


def test_tool_can_request_and_list_but_not_approve(isolated_store, monkeypatch):
    owner = _FakeSource(user_id="424242", chat_type="dm")
    monkeypatch.setattr(hermes_tingle, "_tool_turn_source", lambda: owner)
    monkeypatch.setattr(hermes_tingle, "_current_principal", lambda: ("telegram", "424242", "1001"))
    raw = hermes_tingle.handle_tool(None, {"action": "request_approval", "action_type": "send_email",
                                           "target": "vendor@example.invalid",
                                           "parameters": {"subject": "Hi"}, "ttl_hours": 24})
    payload = json.loads(raw)
    assert payload["success"] and payload["approval"]["status"] == "pending"
    listed = json.loads(hermes_tingle.handle_tool(None, {"action": "list_approvals", "approval_status": "pending"}))
    assert listed["count"] == 1
    rejected = json.loads(hermes_tingle.handle_tool(None, {"action": "approve_approval"}))
    assert rejected["success"] is False


def test_command_surface_approve_and_reject(isolated_store, monkeypatch):
    req = _request(isolated_store)
    owner = _FakeSource(user_id="424242", chat_type="dm")
    listing = hermes_tingle.handle_command(None, f"approvals", source=owner)
    assert req["id"] in listing and "send_email" in listing and "vendor@example.invalid" in listing
    bad = hermes_tingle.handle_command(None, "approve not-an-id", source=owner)
    assert "AR-1A2B3C" in bad and "Nothing changed" in bad
    missing = hermes_tingle.handle_command(None, "approve AR-FFFFFF", source=owner)
    assert "No approval request" in missing
    ok = hermes_tingle.handle_command(None, f"approve {req['id'].lower()}", source=owner)
    assert "Approved" in ok and "one-shot" in ok
    assert "Recorded only" in ok and "no external action is executed or sent" in ok
    assert isolated_store.list_approvals("approved")[0]["approval_evidence"] == "/tingle approve command from telegram user 424242 in dm"
    again = hermes_tingle.handle_command(None, f"reject {req['id']}", source=owner)
    assert "Couldn't reject" in again


def test_approval_parameters_with_backticks_stay_inside_safe_code_display(isolated_store):
    params = {"body": "Please use `literal` text, not ```markdown```."}
    req = _request(isolated_store, parameters=params)
    owner = _FakeSource(user_id="424242", chat_type="dm")
    listing = hermes_tingle.handle_command(None, "approvals", source=owner)
    display = listing.split("Parameters: `", 1)[1].split("`", 1)[0]
    assert json.loads(display) == params
    assert r"\u0060" in display


def test_legacy_database_gains_the_approval_tables(tmp_path):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.execute("""CREATE TABLE hermes_tingle_items (
        id TEXT PRIMARY KEY, title TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'open', owner TEXT NOT NULL DEFAULT 'me',
        priority TEXT NOT NULL DEFAULT 'normal', source_type TEXT NOT NULL DEFAULT 'conversation',
        source_ref TEXT NOT NULL DEFAULT '', source_url TEXT NOT NULL DEFAULT '',
        completion_signal TEXT NOT NULL DEFAULT '', due_at TEXT, next_check_at TEXT,
        snoozed_until TEXT, reminder_count INTEGER NOT NULL DEFAULT 0,
        last_notified_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, completed_at TEXT
    )""")
    store.ensure_schema(conn)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"approval_requests", "action_audit"} <= tables
    conn.close()


class _FakePlatform:
    def __init__(self, value):
        self.value = value


class _FakeSource:
    """Minimal SessionSource stand-in for command-dispatch sender checks."""

    def __init__(self, *, user_id="424242", chat_type="dm", is_bot=False, platform="telegram",
                 chat_id="1001"):
        self.user_id = user_id
        self.chat_type = chat_type
        self.is_bot = is_bot
        self.platform = _FakePlatform(platform)
        self.chat_id = chat_id


def test_approval_binds_sender_identity_in_evidence(isolated_store):
    req = _request(isolated_store)
    source = _FakeSource(user_id="424242")
    reply = hermes_tingle.handle_command(None, f"approve {req['id']}", source=source)
    assert reply.startswith("Approved")
    row = isolated_store.list_approvals("approved", limit=1)[0]
    assert "telegram user 424242 in dm" in row["approval_evidence"]


def test_approval_from_group_chat_is_refused(isolated_store):
    req = _request(isolated_store)
    source = _FakeSource(chat_type="group")
    reply = hermes_tingle.handle_command(None, f"approve {req['id']}", source=source)
    assert "direct message" in reply
    assert isolated_store.list_approvals("pending", limit=5)[0]["id"] == req["id"]


def test_approval_from_bot_sender_is_refused(isolated_store):
    req = _request(isolated_store)
    source = _FakeSource(is_bot=True)
    reply = hermes_tingle.handle_command(None, f"reject {req['id']}", source=source)
    assert "must come from a person" in reply
    assert isolated_store.list_approvals("pending", limit=5)[0]["id"] == req["id"]


def test_approval_without_identified_sender_is_refused(isolated_store):
    req = _request(isolated_store)
    source = _FakeSource(user_id=None)
    reply = hermes_tingle.handle_command(None, f"approve {req['id']}", source=source)
    assert "identified sender" in reply
    assert isolated_store.list_approvals("pending", limit=5)[0]["id"] == req["id"]


def test_approval_without_source_is_refused(isolated_store):
    # Local/legacy dispatch carries no authenticated sender: fail closed, never infer locality.
    req = _request(isolated_store)
    reply = hermes_tingle.handle_command(None, f"approve {req['id']}")
    assert "Couldn't verify who sent this command" in reply
    assert isolated_store.list_approvals("pending", limit=5)[0]["id"] == req["id"]


def test_approval_by_different_dm_principal_is_refused(isolated_store):
    # Another allowlisted DM user knowing the AR id must not decide.
    req = _request(isolated_store)
    other = _FakeSource(user_id="999999", chat_type="dm")
    reply = hermes_tingle.handle_command(None, f"approve {req['id']}", source=other)
    assert "not a configured owner" in reply
    assert isolated_store.list_approvals("pending", limit=5)[0]["id"] == req["id"]


def test_approval_cross_platform_identity_collision_is_refused(isolated_store):
    # The same numeric user id on another platform is a different principal.
    req = _request(isolated_store)
    colliding = _FakeSource(user_id="424242", chat_type="dm", platform="whatsapp")
    reply = hermes_tingle.handle_command(None, f"approve {req['id']}", source=colliding)
    assert "not a configured owner" in reply
    assert isolated_store.list_approvals("pending", limit=5)[0]["id"] == req["id"]


def test_unbound_request_is_undecidable(isolated_store):
    # No approver binding (no session context at request time): nobody can decide; it expires.
    req = _request(isolated_store, approver=None)
    with pytest.raises(ValueError, match="not bound to an approving principal"):
        isolated_store.decide_approval(req["id"], "approved", evidence="/tingle approve command",
                                       decided_by=("telegram", "424242", "1001"))
    with pytest.raises(ValueError, match="not an identified"):
        isolated_store.decide_approval(req["id"], "rejected", evidence="/tingle reject command",
                                       decided_by=None)
