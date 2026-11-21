"""FT-08: memory with provenance.

The plugin's behavioral memory is its approved feedback rules, surfaced to the
agent separately from untrusted source data; it does not duplicate the gateway's
own memory. Every rule carries its provenance: what prompted it (source_ref),
who approved or dismissed it (decided_by, bound from the session, never
model-supplied), and how the decision arrived (evidence).
"""

from __future__ import annotations

import pytest

import hermes_tingle
import hermes_tingle_store as store


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    import sqlite3
    db_path = tmp_path / "data.db"

    def connect(_name: str):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(store, "plugin_db", connect)
    store.enable_source("email", enabled_by="synthetic fixture consent")
    store.enable_source("calendar", enabled_by="synthetic fixture consent")
    yield


class TestCaptureProvenance:
    def test_source_ref_recorded(self):
        candidate = store.capture_feedback_candidate(
            "Prefer concise reminders.", source_ref="correction on item IT-0001"
        )
        assert candidate["source_ref"] == "correction on item IT-0001"

    def test_source_ref_never_required_and_bounded(self):
        candidate = store.capture_feedback_candidate("Prefer concise reminders.")
        assert candidate["source_ref"] == ""
        long_ref = store.capture_feedback_candidate("Rule two.", source_ref="x" * 500)
        assert len(long_ref["source_ref"]) <= 256

    def test_dedup_keeps_first_source_ref_and_fills_empty_one(self):
        first = store.capture_feedback_candidate("Prefer concise reminders.")
        second = store.capture_feedback_candidate(
            "Prefer concise reminders.", source_ref="IT-0002 correction"
        )
        assert second["deduplicated"] is True
        assert second["id"] == first["id"]
        assert second["source_ref"] == "IT-0002 correction"
        third = store.capture_feedback_candidate(
            "Prefer concise reminders.", source_ref="another ref"
        )
        assert third["source_ref"] == "IT-0002 correction"

    def test_capture_tool_passes_source_ref(self):
        import json
        result = json.loads(hermes_tingle.handle_tool(None, {
            "action": "capture_feedback",
            "feedback_text": "Prefer concise reminders.",
            "feedback_source_ref": "IT-0003",
        }))
        assert result["feedback"]["source_ref"] == "IT-0003"


class TestDecisionProvenance:
    def test_approval_binds_the_deciding_principal_and_evidence(self):
        candidate = store.capture_feedback_candidate("Prefer concise reminders.")
        resolved = store.resolve_feedback_candidate(
            candidate["id"], "approved",
            decided_by=("telegram", "u-1", "chat-1"), evidence="/tingle approve FB",
        )
        assert resolved["decided_by"] == "telegram:u-1:chat-1"
        assert resolved["decision_evidence"] == "/tingle approve FB"
        assert resolved["approved_at"]

    def test_the_model_can_never_resolve_through_the_tool(self):
        candidate = store.capture_feedback_candidate("Prefer concise reminders.")
        import json
        result = json.loads(hermes_tingle.handle_tool(None, {
            "action": "resolve_feedback",
            "feedback_id": candidate["id"],
            "feedback_decision": "approved",
            "feedback_evidence": "user replied yes",
            "decided_by": "telegram:attacker:chat",
        }))
        # even during an authenticated user turn the model call is refused:
        # a session principal is not the user's decision
        assert result["success"] is False
        assert "explicit /tingle rule" in result["error"]
        assert store.approved_feedback_rules() == []
        pending = store.list_feedback_candidates(status="pending")
        assert pending[0]["id"] == candidate["id"]
        assert pending[0]["decided_by"] == ""

    def test_resolution_requires_a_deciding_principal_at_the_store_boundary(self):
        candidate = store.capture_feedback_candidate("Prefer concise reminders.")
        for decision in ("approved", "dismissed"):
            with pytest.raises(ValueError):
                store.resolve_feedback_candidate(candidate["id"], decision)
        assert store.list_feedback_candidates(status="pending")[0]["id"] == candidate["id"]

    def test_invalid_decided_by_rejected(self):
        candidate = store.capture_feedback_candidate("Prefer concise reminders.")
        with pytest.raises(ValueError):
            store.resolve_feedback_candidate(
                candidate["id"], "approved", decided_by="not-a-tuple")

    def test_approved_rule_surfaces_full_provenance(self):
        candidate = store.capture_feedback_candidate(
            "Prefer concise reminders.", source_ref="IT-0001 correction")
        store.resolve_feedback_candidate(
            candidate["id"], "approved",
            decided_by=("telegram", "u-1", "chat-1"), evidence="chat reply")
        rule = store.approved_feedback_rules()[0]
        assert rule["source_ref"] == "IT-0001 correction"
        assert rule["decided_by"] == "telegram:u-1:chat-1"
        assert rule["approved_at"]

    def test_dismissed_rule_disappears_from_the_memory_surface(self):
        candidate = store.capture_feedback_candidate("Prefer concise reminders.")
        store.resolve_feedback_candidate(
            candidate["id"], "dismissed",
            decided_by=("telegram", "u-1", "chat-1"), evidence="user said no")
        assert store.approved_feedback_rules() == []
        # provenance retained on the record itself
        record = store.list_feedback_candidates(status="dismissed")[0]
        assert record["decided_by"] == "telegram:u-1:chat-1"

    def test_revoking_an_approval_updates_provenance(self):
        candidate = store.capture_feedback_candidate("Prefer concise reminders.")
        store.resolve_feedback_candidate(
            candidate["id"], "approved", decided_by=("telegram", "u-1", "chat-1"))
        revoked = store.resolve_feedback_candidate(
            candidate["id"], "dismissed", decided_by=("telegram", "u-1", "chat-1"),
            evidence="user changed their mind")
        assert revoked["status"] == "dismissed"
        assert revoked["approved_at"] is None
        assert revoked["decision_evidence"] == "user changed their mind"
        assert store.approved_feedback_rules() == []


class _FakePlatform:
    def __init__(self, value="telegram"):
        self.value = value


class _FakeSource:
    def __init__(self, *, user_id="424242", chat_type="dm", is_bot=False,
                 platform="telegram", chat_id="1001"):
        self.user_id = user_id
        self.chat_type = chat_type
        self.is_bot = is_bot
        self.platform = _FakePlatform(platform)
        self.chat_id = chat_id


@pytest.fixture
def owners(tmp_path, monkeypatch):
    import json
    import hermes_constants
    settings = json.loads((tmp_path / "hermes-tingle.json").read_text())
    settings["owner_principals"] = ["telegram:424242"]
    (tmp_path / "hermes-tingle.json").write_text(json.dumps(settings))
    return settings


@pytest.mark.usefixtures("owners")
class TestCommandAuthorizationBoundary:
    def test_explicit_dm_command_approves_with_dispatch_principal(self):
        candidate = store.capture_feedback_candidate("Prefer concise reminders.")
        reply = hermes_tingle.handle_command(
            None, f"rule approve {candidate['id']}", source=_FakeSource())
        assert "Approved" in reply
        rule = store.approved_feedback_rules()[0]
        assert rule["decided_by"] == "telegram:424242:1001"
        assert "/tingle rule approve" in store.list_feedback_candidates(
            status="approved")[0]["decision_evidence"]

    def test_missing_source_decides_nothing(self):
        candidate = store.capture_feedback_candidate("Prefer concise reminders.")
        reply = hermes_tingle.handle_command(None, f"rule approve {candidate['id']}")
        assert "Couldn't verify" in reply
        assert store.approved_feedback_rules() == []

    def test_bot_sender_decides_nothing(self):
        candidate = store.capture_feedback_candidate("Prefer concise reminders.")
        reply = hermes_tingle.handle_command(
            None, f"rule approve {candidate['id']}",
            source=_FakeSource(is_bot=True))
        assert "not a bot" in reply or "person" in reply
        assert store.approved_feedback_rules() == []

    def test_group_sender_decides_nothing(self):
        candidate = store.capture_feedback_candidate("Prefer concise reminders.")
        reply = hermes_tingle.handle_command(
            None, f"rule approve {candidate['id']}",
            source=_FakeSource(chat_type="group"))
        assert "direct message" in reply
        assert store.approved_feedback_rules() == []

    def test_anonymous_sender_decides_nothing(self):
        candidate = store.capture_feedback_candidate("Prefer concise reminders.")
        reply = hermes_tingle.handle_command(
            None, f"rule approve {candidate['id']}", source=_FakeSource(user_id=""))
        assert store.approved_feedback_rules() == []
        assert reply

    def test_revoke_via_explicit_command(self):
        candidate = store.capture_feedback_candidate("Prefer concise reminders.")
        hermes_tingle.handle_command(
            None, f"rule approve {candidate['id']}", source=_FakeSource())
        assert len(store.approved_feedback_rules()) == 1
        reply = hermes_tingle.handle_command(
            None, f"rule dismiss {candidate['id']}", source=_FakeSource())
        assert "Dismissed" in reply
        assert store.approved_feedback_rules() == []

    def test_identified_dm_user_who_is_not_the_owner_decides_nothing(self):
        candidate = store.capture_feedback_candidate("Prefer concise reminders.")
        reply = hermes_tingle.handle_command(
            None, f"rule approve {candidate['id']}",
            source=_FakeSource(user_id="999999"))
        assert "not a configured owner" in reply
        assert store.approved_feedback_rules() == []

    def test_no_owner_configured_refuses_everyone(self, tmp_path, monkeypatch):
        import json
        settings = json.loads((tmp_path / "hermes-tingle.json").read_text())
        del settings["owner_principals"]
        (tmp_path / "hermes-tingle.json").write_text(json.dumps(settings))
        candidate = store.capture_feedback_candidate("Prefer concise reminders.")
        reply = hermes_tingle.handle_command(
            None, f"rule approve {candidate['id']}", source=_FakeSource())
        assert "no owner principal is configured" in reply
        assert store.approved_feedback_rules() == []

    def test_unknown_or_malformed_id(self):
        reply = hermes_tingle.handle_command(
            None, "rule approve FB-FFFFFF", source=_FakeSource())
        assert "No feedback candidate" in reply
        reply = hermes_tingle.handle_command(
            None, "rule approve nope", source=_FakeSource())
        assert "Usage" in reply


class TestNoParallelMemory:
    def test_candidates_hold_rules_never_content(self):
        with pytest.raises(ValueError):
            store.capture_feedback_candidate("x" * 501)

    def test_tool_description_forbids_raw_content(self):
        from schemas import HERMES_TINGLE
        assert "never store raw report/source content" in HERMES_TINGLE["description"].lower()
