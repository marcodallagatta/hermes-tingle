"""Production wiring glue: the plugin entry point connects what the runtime
offers through its verified public surfaces and stays inert otherwise;
the chat reader bounds history at the source query and the review discloses its exact
partial coverage."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import types
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

import __init__ as plugin
import hermes_tingle
import hermes_tingle_sources as sources
import hermes_tingle_store as store
import hermes_tingle_wiring as wiring


@pytest.fixture(autouse=True)
def unwire():
    yield
    sources.set_chat_history_interfaces(None, None)


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    db_path = tmp_path / "data.db"

    def connect(_name: str):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(store, "plugin_db", connect)
    store.enable_source("email", enabled_by="synthetic fixture consent")
    store.enable_source("calendar", enabled_by="synthetic fixture consent")
    return store


class _FakeStateDB:
    def __init__(self):
        self.list_calls = []
        self.get_calls = []

    def list_gateway_sessions(self, *, platform=None, active_only=True):
        self.list_calls.append((platform, active_only))
        return [
            {"id": "sess-1", "source": "telegram", "chat_id": "1001", "thread_id": ""},
            {"id": "sess-2", "source": "Telegram", "chat_id": "1001", "thread_id": "topic-2"},
            {"id": "other-chat", "source": "telegram", "chat_id": "2002", "thread_id": ""},
        ]

    def get_messages(self, session_id, limit=None, latest=False, **kwargs):
        self.get_calls.append({"session_id": session_id, "limit": limit,
                               "latest": latest, **kwargs})
        return [{"role": "user", "content": "passport hello", "created_at": 1763300000.0}]








class _RuntimeCtx:
    """PluginContext-shaped test double over the verified pinned surfaces."""

    def __init__(self):
        self.tools = []
        self.commands = []
        self.skills = []
        self.platform_handlers = {}
        self.hooks = {}

    def register_tool(self, **kwargs):
        self.tools.append(kwargs)

    def register_command(self, name, **kwargs):
        self.commands.append((name, kwargs))

    def register_skill(self, name, path):
        self.skills.append((name, path))

    def register_platform_handler(self, platform, factory):
        self.platform_handlers[platform] = factory

    def register_hook(self, hook, callback):
        self.hooks[hook] = callback


def _fake_hermes_state(db):
    """Stand-in for the public hermes_state runtime module."""
    module = types.ModuleType("hermes_state")
    module.DEFAULT_DB_PATH = "/nonexistent/hermes/state.db"
    module.SessionDB = lambda path, read_only=False: db
    return module


def _dm_source(user_id="4242", user_name="alex", chat_type="dm"):
    """SessionSource.to_dict()-shaped payload of the admitted hook."""
    return {"platform": "telegram", "chat_id": "1001", "chat_name": "",
            "chat_type": chat_type, "user_id": user_id, "user_name": user_name,
            "thread_id": None, "chat_topic": None}


class TestRegisterGlue:


    def test_unwired_integrate_wires_nothing(self):
        class BareCtx:  # a ctx without the integration surfaces
            pass

        with patch.dict("sys.modules", {"hermes_state": None}):
            report = hermes_tingle.integrate(BareCtx())
        assert report == {"chat_history": False, "inbound_hook": False}
        assert sources._chat_session_resolver is None

    def test_integrate_wires_offered_interfaces(self):
        db = _FakeStateDB()
        ctx = _RuntimeCtx()
        with patch.dict("sys.modules", {"hermes_state": _fake_hermes_state(db)}):
            report = hermes_tingle.integrate(ctx)
        assert report["chat_history"] is True
        assert report["inbound_hook"] is True
        assert ctx.platform_handlers == {}
        assert ctx.hooks["post_gateway_admission"] is wiring.observe_admitted_inbound
        assert sources._chat_session_resolver is not None
        assert "chat" in sources.source_capabilities()

    def test_reader_bounds_history_at_the_query(self):
        db = _FakeStateDB()
        wiring.wire_chat_history(db)
        resolver = sources._chat_session_resolver
        reader = sources._chat_message_reader
        sessions = resolver("telegram", "1001")
        assert sessions == ["sess-1", "sess-2"]
        rows = list(reader("sess-1"))
        # A query cannot materialize a sentinel row outside the disclosed cap.
        assert db.get_calls[0]["limit"] == sources.MAX_CHAT_READS_PER_CHAT
        assert db.get_calls[0]["latest"] is True
        # include_ancestors stays False: the pinned store materializes every
        # lineage row before applying the limit when it is True (unbounded).
        assert db.get_calls[0]["include_ancestors"] is False
        assert rows[0]["timestamp"] == 1763300000.0  # created_at mapped
        assert db.list_calls == [("telegram", True)]

    def test_chat_capability_refusal_names_runtime_wiring(self, monkeypatch):
        sources.set_chat_history_interfaces(None, None)
        monkeypatch.setattr(sources, "source_capabilities", lambda: {"email", "calendar"})

        with pytest.raises(sources.SourceReadError) as exc:
            sources.probe_source("chat")

        assert "Chat history isn't wired into this Hermes runtime" in str(exc.value)
        assert "retry `/tingle source enable chat`" in str(exc.value)
        assert "sign-in issue" in str(exc.value)

    def test_chat_review_carries_exact_partial_coverage_note(self):
        store.enable_source("chat", enabled_by="synthetic fixture consent")
        review = sources._collect_chat_matches(now=datetime.now(timezone.utc))
        assert review["coverage"] == wiring.CHAT_COVERAGE_NOTE
        assert "up to 5 active session routes per chat" in review["coverage"]
        assert "including separate threads/topics" in review["coverage"]
        assert "200 message reads per chat" in review["coverage"]
        assert "active session records (including stored prompt/config fields) are listed platform-wide" in review["coverage"]
        assert "only session IDs/platform/chat IDs are used" in review["coverage"]
        assert "no messages from other chats are fetched" in review["coverage"]
        assert "ended sessions" in review["coverage"]
        assert "not covered" in review["coverage"]

    def test_collector_reads_each_active_thread_route_within_disclosed_bounds(self, isolated_store):
        store.enable_source("chat", enabled_by="synthetic fixture consent")
        store.note_known_chat("telegram:1001", origin="inbound")
        store.add_item(title="Passport follow-up")
        db = _FakeStateDB()
        wiring.wire_chat_history(db)

        review = sources._collect_chat_matches(now=datetime.now(timezone.utc))

        assert [call["session_id"] for call in db.get_calls] == ["sess-1", "sess-2"]
        assert [call["limit"] for call in db.get_calls] == [
            sources.MAX_CHAT_READS_PER_CHAT,
            sources.MAX_CHAT_READS_PER_CHAT - 1,
        ]
        assert review["coverage"] == wiring.CHAT_COVERAGE_NOTE
        assert "not covered" in review["coverage"]

    def test_collector_materializes_at_most_200_rows_across_routes(self, isolated_store):
        store.enable_source("chat", enabled_by="synthetic fixture consent")
        store.note_known_chat("telegram:1001", origin="inbound")
        store.add_item(title="Passport follow-up")
        now = datetime.now(timezone.utc)

        class CountingStateDB(_FakeStateDB):
            def __init__(self):
                super().__init__()
                self.materialized = 0

            def get_messages(self, session_id, limit=None, latest=False, **kwargs):
                self.get_calls.append({"session_id": session_id, "limit": limit,
                                       "latest": latest, **kwargs})
                count = 150 if session_id == "sess-1" else int(limit or 201)
                rows = [{"role": "user", "content": "ordinary hello",
                         "timestamp": now.timestamp() - 60} for _ in range(count)]
                self.materialized += len(rows)
                return rows

        db = CountingStateDB()
        wiring.wire_chat_history(db)

        review = sources._collect_chat_matches(now=now)

        assert db.materialized == sources.MAX_CHAT_READS_PER_CHAT
        assert [call["limit"] for call in db.get_calls] == [
            sources.MAX_CHAT_READS_PER_CHAT,
            sources.MAX_CHAT_READS_PER_CHAT - 150,
        ]
        assert review["review_complete"] is False
        assert "bounded" in review["incomplete_reasons"]

    def test_collector_caps_active_chat_routes_at_five(self, isolated_store):
        store.enable_source("chat", enabled_by="synthetic fixture consent")
        store.note_known_chat("telegram:1001", origin="inbound")
        store.add_item(title="Passport follow-up")

        class SevenRouteDB(_FakeStateDB):
            def list_gateway_sessions(self, *, platform=None, active_only=True):
                self.list_calls.append((platform, active_only))
                return [{"id": f"session-{i}", "source": "telegram", "chat_id": "1001",
                         "thread_id": f"topic-{i}"} for i in range(7)]

        db = SevenRouteDB()
        wiring.wire_chat_history(db)
        review = sources._collect_chat_matches(now=datetime.now(timezone.utc))

        assert [call["session_id"] for call in db.get_calls] == [f"session-{i}" for i in range(5)]
        assert review["review_complete"] is False
        assert "bounded" in review["incomplete_reasons"]

    def test_unbounded_reader_is_refused_before_invocation(self):
        called = []

        def eager_reader(session_id, **kwargs):
            called.append(session_id)
            return [{"role": "user", "content": "synthetic private text"}] * 500

        with pytest.raises(ValueError, match="per-call limit"):
            sources.set_chat_history_interfaces(lambda platform, chat_id: ["s"], eager_reader)
        assert called == []

    def test_one_argument_eager_reader_is_refused_before_invocation(self):
        called = []

        def eager_reader(session_id):
            called.append(session_id)
            return [{"role": "user", "content": "synthetic private text"}] * 500

        with pytest.raises(ValueError, match="per-call limit"):
            sources.set_chat_history_interfaces(lambda platform, chat_id: ["s"], eager_reader)
        assert called == []

    def test_wire_chat_history_validates_shape(self):
        with pytest.raises(ValueError):
            wiring.wire_chat_history(object())




class TestSnapshotWakeOnSourceFindings:
    def test_chat_matches_are_actionable(self):
        from scripts.source_snapshot import build_agent_input
        base = {"due_items": [], "emails": [], "tracked_email_context": [],
                "email_completion_signals": [], "calendar_events": [], "errors": []}
        assert build_agent_input(dict(base)) == '{"wakeAgent": false}'
        for key in ("chat_review",):
            snapshot = dict(base)
            snapshot[key] = {"enabled": True, "review_complete": True,
                             "incomplete_reasons": [], "matches": [{"excerpt": "x"}], "scanned": 1}
            assert build_agent_input(snapshot) != '{"wakeAgent": false}', key
        # routine disabled/empty reviews stay quiet
        snapshot = dict(base)
        snapshot["chat_review"] = {"enabled": False, "review_complete": False,
                                   "incomplete_reasons": ["disabled"], "matches": [], "scanned": 0}
        assert build_agent_input(snapshot) == '{"wakeAgent": false}'




_REAL_POPEN = subprocess.Popen  # captured before conftest forbids subprocesses

_LOADER_SCRIPT = r"""
import importlib.util
import sys
import types
import asyncio
from pathlib import Path

pkg_dir = Path(sys.argv[1])
# Host-runtime stubs only; the plugin root is NEVER added to sys.path, so any
# absolute sibling import in the plugin fails here exactly as it would when
# Hermes loads the plugin as a package.
hermes_constants = types.ModuleType("hermes_constants")
hermes_constants.get_hermes_home = lambda: pkg_dir
sys.modules["hermes_constants"] = hermes_constants
plugins_pkg = types.ModuleType("plugins")
storage = types.ModuleType("plugins.plugin_storage")
storage.plugin_db = lambda name: None
plugins_pkg.plugin_storage = storage
sys.modules["plugins"] = plugins_pkg
sys.modules["plugins.plugin_storage"] = storage


class Ctx:
    def __init__(self):
        self.platform_handlers = {}
        self.hooks = {}

    def register_tool(self, **kwargs):
        pass

    def register_command(self, name, **kwargs):
        pass

    def register_skill(self, name, path):
        pass

    def register_platform_handler(self, platform, factory):
        self.platform_handlers[platform] = factory

    def register_hook(self, hook, callback):
        self.hooks[hook] = callback


spec = importlib.util.spec_from_file_location(
    "hermes_tingle_plugin", pkg_dir / "__init__.py",
    submodule_search_locations=[str(pkg_dir)])
module = importlib.util.module_from_spec(spec)
sys.modules["hermes_tingle_plugin"] = module
spec.loader.exec_module(module)
ctx = Ctx()
module.register(ctx)
assert not ctx.platform_handlers
assert "post_gateway_admission" in ctx.hooks
print("PACKAGE-MODE-OK")
"""


class TestPackageModeLoad:
    def test_package_loads_and_registers_without_plugin_root_on_sys_path(self, monkeypatch):
        monkeypatch.setattr(subprocess, "Popen", _REAL_POPEN)
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [sys.executable, "-c", _LOADER_SCRIPT, str(root)],
            capture_output=True, text=True, timeout=60)
        assert "PACKAGE-MODE-OK" in result.stdout, result.stderr


def test_plugin_registers_history_and_admission_without_a_platform_factory(isolated_store):
    db = _FakeStateDB()
    ctx = _RuntimeCtx()
    with patch.dict("sys.modules", {"hermes_state": _fake_hermes_state(db)}):
        plugin.register(ctx)
    assert any(t["name"] == "hermes_tingle" for t in ctx.tools)
    assert any(name == "tingle" for name, _ in ctx.commands)
    assert sources._chat_session_resolver("telegram", "1001") == ["sess-1", "sess-2"]
    assert not ctx.platform_handlers
    hook = ctx.hooks["post_gateway_admission"]
    assert hook(session_key="s1", platform="telegram", source=_dm_source(),
                message_id="m1", text="synthetic") is None
    assert [r["chat_ref"] for r in isolated_store.list_known_chats()] == ["telegram:1001"]


def test_plugin_registers_without_runtime():
    ctx = _RuntimeCtx()
    with patch.dict("sys.modules", {"hermes_state": None}):
        plugin.register(ctx)
    assert sources._chat_session_resolver is None
    assert not ctx.platform_handlers
    assert "post_gateway_admission" in ctx.hooks


class TestAdmissionChatDiscovery:
    """Chat-scope enumeration: an ADMITTED inbound message is Hermes' own
    authorization that this profile sees the chat, so DMs AND groups enter
    the default-ingestion registry by default. A stranger's command still
    never registers a chat, registration retains no content, and the owner's
    exclusion list keeps winning after a chat registers."""

    def _hook(self):
        ctx = _RuntimeCtx()
        with patch.dict("sys.modules", {"hermes_state": None}):
            plugin.register(ctx)
        return ctx.hooks["post_gateway_admission"]

    def test_admitted_dm_and_group_register_their_chats(self, isolated_store):
        hook = self._hook()
        assert hook(session_key="s1", platform="telegram", source=_dm_source(),
                    message_id="m1", text="hi") is None
        group = _dm_source(chat_type="group")
        group["chat_id"] = "-100224466"
        assert hook(session_key="s2", platform="telegram", source=group,
                    message_id="m2", text="group noise") is None
        rows = isolated_store.list_known_chats()
        assert sorted(r["chat_ref"] for r in rows) == ["telegram:-100224466", "telegram:1001"]
        assert all(r["origin"] == "inbound" for r in rows)
        # registration retains no content and alters no dispatch
        assert sorted(isolated_store.chats_in_scope()) == ["telegram:-100224466", "telegram:1001"]

    def test_registration_needs_no_profile_setting(self, isolated_store):
        # No owners configured: admission still registers.
        hook = self._hook()
        assert hook(session_key="s1", platform="telegram", source=_dm_source(),
                    message_id="m1", text="hi") is None
        assert [r["chat_ref"] for r in isolated_store.list_known_chats()] == ["telegram:1001"]

    def test_malformed_events_register_nothing(self, isolated_store):
        hook = self._hook()
        no_chat_id = {k: v for k, v in _dm_source().items() if k != "chat_id"}
        for source, platform in ((None, "telegram"), ("not-a-dict", "telegram"),
                                 (_dm_source(), ""), (no_chat_id, "telegram")):
            assert hook(session_key="s", platform=platform, source=source,
                        message_id="m", text="hi") is None
        assert isolated_store.list_known_chats() == []

    def test_exclusion_still_wins_after_admission(self, isolated_store):
        isolated_store.exclude_chat("telegram:-100224466", excluded_by="telegram:4242")
        hook = self._hook()
        group = _dm_source(chat_type="group")
        group["chat_id"] = "-100224466"
        assert hook(session_key="s1", platform="telegram", source=group,
                    message_id="m1", text="noise") is None
        assert "telegram:-100224466" in [r["chat_ref"] for r in isolated_store.list_known_chats()]
        assert isolated_store.chats_in_scope() == []
