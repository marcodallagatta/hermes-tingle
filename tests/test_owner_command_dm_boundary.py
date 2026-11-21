"""Consequential commands require positive human-DM evidence, not a group denylist."""
import copy
import json
from types import SimpleNamespace

import pytest

import hermes_tingle as plugin
import hermes_tingle_discovery as discovery
import hermes_tingle_sources as sources
import hermes_tingle_store as store
import setup_jobs


SOURCE_CASES = [
    pytest.param({}, False, id="missing-chat-type"),
    *[pytest.param({"chat_type": value}, False, id=f"chat-type-{value!r}")
      for value in (None, "", "unknown", "supergroup", "group", "channel", "thread")],
    pytest.param({"chat_type": "dm", "is_bot": True}, False, id="bot"),
    pytest.param({"chat_type": "dm", "chat_id": None}, False, id="null-chat-id"),
    pytest.param({"chat_type": "dm", "chat_id": ""}, False, id="empty-chat-id"),
    pytest.param({"chat_type": "dm", "chat_id": "  "}, False, id="blank-chat-id"),
    pytest.param({"chat_type": "dm"}, True, id="dm"),
    pytest.param({"chat_type": "private"}, True, id="private"),
    pytest.param({"chat_type": "DM"}, True, id="uppercase-dm"),
    pytest.param({"chat_type": "PRIVATE"}, True, id="uppercase-private"),
]


@pytest.fixture
def owner_boundary(tmp_path, synthetic_settings):
    settings = {**synthetic_settings, "owner_principals": ["telegram:424242"]}
    (tmp_path / "hermes-tingle.json").write_text(json.dumps(settings))
    # An existing owner-DM binding also must survive refused commands unchanged.
    store.note_known_chat("telegram:1001", origin="command")
    discovery.observe_source(SimpleNamespace(platform="telegram", user_id="424242",
                                             chat_id="1001", chat_type="dm", is_bot=False))

    def source(overrides):
        values = dict(platform="telegram", user_id="424242", chat_id="1001", is_bot=False)
        values.update(overrides)
        return SimpleNamespace(**values)

    return source


def _database_snapshot():
    conn = store._connect()
    try:
        return list(conn.iterdump())
    finally:
        conn.close()


def _assert_refused(reply, source):
    if source.is_bot:
        assert "person" in reply and "bot" in reply
    else:
        assert "direct message" in reply.casefold()


@pytest.mark.parametrize("metadata,allowed", SOURCE_CASES + [
    pytest.param({"chat_type": "dm", "omit_chat_id": True}, False, id="missing-chat-id"),
])
@pytest.mark.parametrize("command,mutation", [
    ("source disable email", "disable_source"),
    ("source enable email 7", "enable_source"),
    ("source gap-reviewed email", "acknowledge_email_coverage_gap"),
    ("approve", "decide_approval"),
    ("reject", "decide_approval"),
])
def test_consequential_command_requires_verified_owner_dm(
        owner_boundary, monkeypatch, metadata, allowed, command, mutation):
    source = owner_boundary(metadata)
    if getattr(source, "omit_chat_id", False):
        del source.chat_id
    if command in ("approve", "reject"):
        request = store.request_approval("send_email", "vendor@example.invalid", {"subject": "Synthetic"},
                                         requested_by="synthetic", approver=("telegram", "424242", "1001"))
        command += " " + request["id"]
    if command == "source gap-reviewed email":
        store.set_plugin_meta("gmail_coverage_gap", "1")
    monkeypatch.setattr(sources, "probe_source", lambda kind: "a" * 64)
    calls = []
    original = getattr(store, mutation)

    def record(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(store, mutation, record)
    before = _database_snapshot()
    reply = plugin.handle_command(None, command, source=source)

    assert len(calls) == int(allowed), reply
    if allowed:
        assert _database_snapshot() != before
        assert any(text in reply for text in ("opted out", "opted in", "Recorded your confirmation", "Approved", "Rejected"))
    else:
        _assert_refused(reply, source)
        assert _database_snapshot() == before


def test_nonowner_cannot_approve_own_profile_external_action(owner_boundary):
    request = store.request_approval(
        "send_message", "CT-AAAAAA", {"purpose": "synthetic", "words": "private draft"},
        requested_by="synthetic non-owner", approver=("telegram", "999999", "2002"))
    outsider = owner_boundary({"user_id": "999999", "chat_id": "2002", "chat_type": "dm"})
    before = _database_snapshot()

    reply = plugin.handle_command(None, f"approve {request['id']}", source=outsider)

    assert "not a configured owner" in reply
    assert _database_snapshot() == before
    assert store.list_approvals("pending")[0]["id"] == request["id"]


def test_approval_listing_requires_owner_dm_and_shows_only_bound_owner(owner_boundary):
    request = store.request_approval(
        "send_message", "CT-AAAAAA", {"purpose": "synthetic", "words": "private draft"},
        requested_by="synthetic owner", approver=("telegram", "424242", "1001"))
    outsider = owner_boundary({"user_id": "999999", "chat_id": "2002", "chat_type": "dm"})

    refused = plugin.handle_command(None, "approvals", source=outsider)
    assert "not a configured owner" in refused
    assert request["id"] not in refused and "private draft" not in refused

    owner = owner_boundary({"chat_type": "dm"})
    visible = plugin.handle_command(None, "approvals", source=owner)
    assert request["id"] in visible and "private draft" in visible


class _SyntheticCron:
    def __init__(self):
        self.jobs = {}
        self.calls = []

    def ask_choice(self, question, choices):
        return "create"

    def dispatch_tool(self, name, payload):
        self.calls.append((name, copy.deepcopy(payload)))
        assert name == "cronjob_manage"
        if payload["action"] == "list":
            return {"success": True, "count": len(self.jobs), "jobs": [
                {"job_id": key, "name": value["name"]} for key, value in self.jobs.items()]}
        assert payload["action"] == "create"
        job_id = f"synthetic-{len(self.jobs)}"
        record = {key: copy.deepcopy(value) for key, value in payload.items() if key != "action"}
        record.update(id=job_id, schedule=_schedule(payload["schedule"]), enabled=True,
                      no_agent=payload.get("no_agent", False))
        self.jobs[job_id] = record
        return {"success": True, "job_id": job_id}


def _schedule(value):
    if value.startswith("every "):
        return {"kind": "interval", "minutes": int(value[6:-1]), "display": value}
    return {"kind": "cron", "expr": value, "display": value}


@pytest.mark.parametrize("metadata,allowed", SOURCE_CASES + [
    pytest.param({"chat_type": "dm", "omit_chat_id": True}, False, id="missing-chat-id"),
])
def test_setup_command_requires_verified_owner_dm(owner_boundary, tmp_path, monkeypatch, metadata, allowed):
    source = owner_boundary(metadata)
    if getattr(source, "omit_chat_id", False):
        del source.chat_id
    ctx = _SyntheticCron()
    monkeypatch.setattr(setup_jobs, "_cron_api", lambda: (_schedule, lambda key: copy.deepcopy(ctx.jobs.get(key))))
    helpers = tmp_path / "plugins" / "hermes-tingle" / "scripts"
    helpers.mkdir(parents=True)
    for name in ("source_snapshot.py", "due_alerts.py"):
        (helpers / name).write_text("# Synthetic helper; never executed.\n")
    before = _database_snapshot()

    reply = setup_jobs.setup_from_command(ctx, source=source)

    if allowed:
        assert "is scheduled" in reply
        assert len(ctx.jobs) == 2
        assert [payload["action"] for _, payload in ctx.calls].count("create") == 2
        assert {path.name for path in (tmp_path / "scripts").iterdir()} == {
            "hermes_tingle_snapshot.py", "hermes_tingle_due.py"}
    else:
        assert ctx.calls == [], reply
        assert not (tmp_path / "scripts").exists()
        _assert_refused(reply, source)
    assert _database_snapshot() == before
