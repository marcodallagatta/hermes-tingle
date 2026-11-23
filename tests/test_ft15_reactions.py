"""FT-15: task-relevant emoji reactions (capability-gated, best-effort).

Capture reacts eyes on the triggering message; done reacts checkmark on the
command message and the origin message; snooze reacts sleeping on the command
message. Reactions are decoration: every failure mode is a silent no-op and
the underlying state change always succeeds.
"""
import json
import sqlite3

import pytest

from runtime_support import hermes_import

import hermes_tingle_store as store
import hermes_tingle


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "hermes-tingle.db"

    def connect(_name: str):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(store, "plugin_db", connect)
    store.enable_source("email", enabled_by="synthetic fixture consent")
    store.enable_source("calendar", enabled_by="synthetic fixture consent")
    return store


class _Recorder:
    def __init__(self, calls, fail=False):
        self._calls = calls
        self._fail = fail

    async def add_reaction(self, platform, chat_id, message_id, emoji):
        if self._fail:
            raise RuntimeError("synthetic gateway failure")
        self._calls.append((platform, chat_id, message_id, emoji))
        return {"ok": True}


@pytest.fixture
def reactions(monkeypatch):
    calls = []

    def factory():
        return _Recorder(calls)

    monkeypatch.setattr(hermes_tingle, "_platform_actions", factory)
    return calls


class _FakePlatform:
    def __init__(self, value):
        self.value = value


class _FakeSource:
    def __init__(self, message_id="cmd-1", chat_id="1001", platform="telegram"):
        self.platform = _FakePlatform(platform)
        self.chat_id = chat_id
        self.message_id = message_id
        self.user_id = "424242"
        self.chat_type = "dm"
        self.is_bot = False


def _add(isolated_db, title="Synthetic task"):
    return json.loads(hermes_tingle.handle_tool(None, {"action": "add", "title": title}))["item"]


def test_capture_binds_origin_and_reacts(isolated_db, reactions, monkeypatch):
    monkeypatch.setattr(hermes_tingle, "_reaction_context", lambda: ("telegram", "1001", "msg-1"))
    item = _add(isolated_db)
    assert (item["origin_platform"], item["origin_chat_id"], item["origin_message_id"]) == (
        "telegram", "1001", "msg-1")
    assert ("telegram", "1001", "msg-1", "👀") in reactions


def test_capture_without_messaging_context_stays_quiet(isolated_db, reactions, monkeypatch):
    monkeypatch.setattr(hermes_tingle, "_reaction_context", lambda: None)
    item = _add(isolated_db)
    assert item["origin_message_id"] is None
    assert reactions == []


def test_done_reacts_command_and_origin(isolated_db, reactions, monkeypatch):
    monkeypatch.setattr(hermes_tingle, "_reaction_context", lambda: ("telegram", "1001", "msg-1"))
    item = _add(isolated_db)
    reactions.clear()
    reply = hermes_tingle.handle_command(None, f"done {item['id']}", source=_FakeSource("cmd-9"))
    assert reply.startswith("Marked")
    assert ("telegram", "1001", "cmd-9", "✅") in reactions
    assert ("telegram", "1001", "msg-1", "✅") in reactions


def test_done_reacts_command_only_without_origin(isolated_db, reactions, monkeypatch):
    monkeypatch.setattr(hermes_tingle, "_reaction_context", lambda: None)
    item = _add(isolated_db)
    reactions.clear()
    hermes_tingle.handle_command(None, f"done {item['id']}", source=_FakeSource("cmd-9"))
    assert reactions == [("telegram", "1001", "cmd-9", "✅")]


def test_done_skips_duplicate_when_command_is_origin(isolated_db, reactions, monkeypatch):
    monkeypatch.setattr(hermes_tingle, "_reaction_context", lambda: ("telegram", "1001", "cmd-9"))
    item = _add(isolated_db)
    reactions.clear()
    hermes_tingle.handle_command(None, f"done {item['id']}", source=_FakeSource("cmd-9"))
    assert reactions == [("telegram", "1001", "cmd-9", "✅")]


def test_snooze_reacts_on_command_message(isolated_db, reactions, monkeypatch):
    monkeypatch.setattr(hermes_tingle, "_reaction_context", lambda: None)
    item = _add(isolated_db)
    reply = hermes_tingle.handle_command(None, f"snooze {item['id']} 2d", source=_FakeSource("cmd-5"))
    assert reply.startswith("Snoozed")
    assert reactions == [("telegram", "1001", "cmd-5", "😴")]


def test_reaction_failure_never_breaks_the_flow(isolated_db, monkeypatch):
    def factory():
        return _Recorder([], fail=True)

    monkeypatch.setattr(hermes_tingle, "_platform_actions", factory)
    monkeypatch.setattr(hermes_tingle, "_reaction_context", lambda: ("telegram", "1001", "msg-1"))
    item = _add(isolated_db)
    assert item["id"]
    reply = hermes_tingle.handle_command(None, f"done {item['id']}", source=_FakeSource("cmd-9"))
    assert reply.startswith("Marked")


def test_no_gateway_or_capability_is_a_noop(isolated_db):
    # Real Hermes runtime present but no live gateway: the facade answers
    # ok=False and nothing raises. (Skipped under the stub suite.)
    hermes_import("hermes_cli.platform_actions")
    item = json.loads(hermes_tingle.handle_tool(None, {"action": "add", "title": "Synthetic"}))["item"]
    reply = hermes_tingle.handle_command(None, f"done {item['id']}", source=_FakeSource("cmd-1"))
    assert reply.startswith("Marked")
