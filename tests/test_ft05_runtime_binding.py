"""FT-05 synthetic runtime harness: the approval context binding exercised
through the REAL Hermes dispatch and session-context machinery (pinned Hermes).

Skipped under the stub suite; the pinned-Hermes CI job runs it for real.

Verifies, without any live account:
1. The real plugin-command dispatcher (hermes_cli.plugin_command_dispatch)
   delivers the session source into the plugin handler, and a decision binds
   to the dispatch-authenticated principal.
2. A model-facing tool call inside a session-context-bound turn registers the
   request bound to THAT turn's principal (tool worker threads inherit the
   bindings via copy_context, as Hermes's DaemonThreadPoolExecutor does).
3. Concurrent turns cannot overwrite each other's binding.
4. The model cannot supply or override the principal through tool arguments.
5. Process-global os.environ identity never leaks into a binding.
"""
import sqlite3
import threading
from contextvars import copy_context

import pytest

gateway_sc = pytest.importorskip("gateway.session_context")
gateway_session = pytest.importorskip("gateway.session")
gateway_config = pytest.importorskip("gateway.config")
dispatch = pytest.importorskip("hermes_cli.plugin_command_dispatch")

import hermes_tingle_store as store
import hermes_tingle


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    import json
    settings_path = tmp_path / "hermes-tingle.json"
    settings = json.loads(settings_path.read_text())
    settings["owner_principals"] = ["telegram:424242"]
    settings_path.write_text(json.dumps(settings))
    db_path = tmp_path / "hermes-tingle.db"

    def connect(_name: str):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(store, "plugin_db", connect)
    store.enable_source("email", enabled_by="synthetic fixture consent")
    store.enable_source("calendar", enabled_by="synthetic fixture consent")
    return store


def _source(user_id="424242", chat_id="1001", chat_type="dm", platform=None):
    return gateway_session.SessionSource(
        platform=platform or gateway_config.Platform.TELEGRAM,
        chat_id=chat_id, chat_type=chat_type, user_id=user_id, user_name="Synthetic User")


def _plugin_handler(raw_args, **kwargs):
    # Registered exactly like the plugin registers it (see __init__.py).
    return hermes_tingle.handle_command(None, raw_args, **kwargs)


def _dispatch_command(source, raw_args):
    return dispatch.invoke_plugin_command(
        _plugin_handler, raw_args, source=source, session_id="sess-synthetic",
        session_key="key-synthetic", platform=source.platform.value)


@pytest.fixture
def bound_turn():
    """Bind a synthetic Telegram DM turn's session context; cleared afterwards."""
    tokens = gateway_sc.set_session_vars(
        platform="telegram", source="telegram", chat_id="1001", chat_type="dm",
        user_id="424242", user_name="Synthetic User",
        session_key="key-synthetic", session_id="sess-synthetic")
    try:
        yield
    finally:
        gateway_sc.clear_session_vars(tokens)


def test_real_dispatch_delivers_source_and_binds_decision(isolated_db):
    req = store.request_approval("send_email", "vendor@example.invalid", {"reference": "Q-1"},
                                 requested_by="test", approver=("telegram", "424242", "1001"))
    reply = _dispatch_command(_source(), f"approve {req['id']}")
    assert reply.startswith("Approved")
    row = store.list_approvals("approved", limit=1)[0]
    assert "telegram user 424242 in dm" in row["approval_evidence"]


def test_real_dispatch_refuses_a_different_principal(isolated_db):
    req = store.request_approval("send_email", "vendor@example.invalid", {"reference": "Q-1"},
                                 requested_by="test", approver=("telegram", "424242", "1001"))
    reply = _dispatch_command(_source(user_id="999999"), f"approve {req['id']}")
    assert "not a configured owner" in reply
    assert store.list_approvals("pending", limit=5)[0]["id"] == req["id"]


def test_tool_call_inside_turn_binds_that_turns_principal(isolated_db, bound_turn):
    import json
    result = json.loads(hermes_tingle.handle_tool(None, {
        "action": "request_approval", "action_type": "send_email",
        "target": "vendor@example.invalid", "parameters": {"reference": "Q-2"}}))
    assert result["success"] is True
    approval = result["approval"]
    assert (approval["approver_platform"], approval["approver_user_id"],
            approval["approver_chat_id"]) == ("telegram", "424242", "1001")
    # The bound principal can decide through the real dispatch path.
    reply = _dispatch_command(_source(), f"approve {approval['id']}")
    assert reply.startswith("Approved")


def test_tool_arguments_cannot_override_the_principal(isolated_db, bound_turn):
    import json
    result = json.loads(hermes_tingle.handle_tool(None, {
        "action": "request_approval", "action_type": "send_email",
        "target": "vendor@example.invalid", "parameters": {"reference": "Q-3"},
        "approver": ("telegram", "999999", "2002"), "user_id": "999999"}))
    assert result["success"] is True
    approval = result["approval"]
    assert approval["approver_user_id"] == "424242"  # the turn's, never the argument's


def test_concurrent_turns_keep_their_own_binding(isolated_db):
    # Two turns bound at the SAME TIME must each read their own principal.
    # copy_context per submission: exactly how Hermes tool worker threads run.
    barrier = threading.Barrier(2)

    def read_principal_as(user_id, chat_id):
        def work():
            gateway_sc.set_session_vars(
                platform="telegram", source="telegram", chat_id=chat_id, chat_type="dm",
                user_id=user_id, user_name="Synthetic", session_key=f"key-{user_id}",
                session_id=f"sess-{user_id}")
            barrier.wait(timeout=10)  # both contexts live before either reads
            return hermes_tingle._current_principal()
        return copy_context().run(work)

    results = []
    threads = [threading.Thread(target=lambda u=u, c=c: results.append(read_principal_as(u, c)))
               for u, c in (("424242", "1001"), ("999999", "2002"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [("telegram", "424242", "1001"), ("telegram", "999999", "2002")]

    # And a request bound to one principal cannot be decided by the other.
    req = store.request_approval("send_email", "vendor@example.invalid", {"reference": "Q-9"},
                                 requested_by="test", approver=("telegram", "424242", "1001"))
    reply = _dispatch_command(_source(user_id="999999", chat_id="2002"), f"approve {req['id']}")
    assert "not a configured owner" in reply


def test_process_environ_identity_never_leaks(isolated_db, monkeypatch):
    import json
    monkeypatch.setenv("HERMES_SESSION_USER_ID", "999999")
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "2002")
    gateway_sc.reset_session_vars()  # this context never bound anything
    try:
        result = json.loads(hermes_tingle.handle_tool(None, {
            "action": "request_approval", "action_type": "send_email",
            "target": "vendor@example.invalid", "parameters": {"reference": "Q-4"}}))
        approval = result["approval"]
        assert approval["approver_user_id"] is None  # unbound, not the environ identity
        with pytest.raises(ValueError, match="not bound to an approving principal"):
            store.decide_approval(approval["id"], "approved", evidence="x",
                                  decided_by=("telegram", "999999", "2002"))
    finally:
        gateway_sc.reset_session_vars()


@pytest.mark.parametrize("actor", ["synthetic-other", ""])
def test_real_turn_actor_not_session_route(isolated_db, actor):
    import json
    tokens = gateway_sc.set_session_vars(platform="telegram",chat_id="synthetic-route",
        user_id="synthetic-route-owner",actor_id=actor)
    try:
        result=json.loads(hermes_tingle.handle_tool(None, {"action":"request_approval",
            "action_type":"send_email","target":"test@example.invalid","parameters":{"purpose":"synthetic"}}))
        assert result["approval"]["approver_user_id"] == (actor or None)
    finally:
        gateway_sc.clear_session_vars(tokens)


def test_real_absorbed_sender_dynamically_clears_approval_actor(isolated_db):
    import json
    from types import SimpleNamespace
    event=SimpleNamespace(user_id="synthetic-owner")
    tokens=gateway_sc.set_session_vars(platform="telegram",chat_id="synthetic-route",
        user_id="synthetic-owner",actor_id="synthetic-owner",turn_event=event)
    try:
        assert hermes_tingle._current_principal()==("telegram","synthetic-owner","synthetic-route")
        event.user_id=""
        assert gateway_sc._bound_session_value("HERMES_SESSION_ACTOR_ID")==""
        assert hermes_tingle._current_principal() is None
        result=json.loads(hermes_tingle.handle_tool(None,{"action":"request_approval",
            "action_type":"send_email","target":"test@example.invalid","parameters":{"purpose":"synthetic"}}))
        assert result["approval"]["approver_user_id"] is None
    finally:
        gateway_sc.clear_session_vars(tokens)
