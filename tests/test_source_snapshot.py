import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import source_snapshot
from source_snapshot import build_agent_input


def test_source_snapshot_uses_hermes_tingle_plugin_directory(monkeypatch, tmp_path):
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)

    assert source_snapshot._plugin_root() == tmp_path / "plugins" / "hermes-tingle"


def test_source_snapshot_silences_when_nothing_needs_review():
    result = build_agent_input({"due_items": [], "emails": [], "calendar_events": [], "errors": []})

    assert result == '{"wakeAgent": false}'


def test_review_payload_exposes_profile_timezone_outside_untrusted_sources():
    result = json.loads(build_agent_input({"emails": [{"source_id": "m-1"}]}, profile_timezone="Asia/Tokyo"))
    assert result["profile_timezone"] == "Asia/Tokyo"
    assert "profile_timezone" not in result["source_snapshot"]


def test_snapshot_main_loads_current_profile_before_source_reads(tmp_path, synthetic_settings, monkeypatch, capsys):
    import hermes_tingle_sources as sources
    import hermes_tingle_wiring as wiring
    (tmp_path / "hermes-tingle.json").write_text(json.dumps({**synthetic_settings, "timezone": "Asia/Tokyo"}))
    monkeypatch.setattr(wiring, 'wire_from_runtime', lambda: {'chat_history': False})
    calls = []
    def collect(**kwargs):
        calls.append(kwargs)
        return {"emails": [{"source_id": "m-1"}]}
    monkeypatch.setattr(sources, "collect_snapshot", collect)
    assert source_snapshot.main() == 0
    assert calls == [{"discovery_enabled": True}]
    assert json.loads(capsys.readouterr().out)["profile_timezone"] == "Asia/Tokyo"


def test_snapshot_main_wires_chat_history_before_local_discovery(monkeypatch, capsys):
    import hermes_tingle_sources as sources
    import hermes_tingle_wiring as wiring
    import hermes_tingle_discovery as discovery
    calls = []
    monkeypatch.setattr(wiring, 'wire_from_runtime', lambda: calls.append('wire') or {'chat_history': True})
    monkeypatch.setattr(sources, 'collect_snapshot', lambda **kwargs:
        calls.append(('collect', kwargs)) or {'chat_review': {'matches': [], 'discovery': {'matches': []}},
            'due_items': [], 'emails': [], 'tracked_email_context': [],
            'email_completion_signals': [], 'calendar_events': [], 'errors': []})
    monkeypatch.setattr(discovery, 'stage_local_candidates', lambda snapshot: calls.append('stage') or 0)
    assert source_snapshot.main() == 0
    assert calls[0] == 'wire'
    assert calls[1] == ('collect', {'discovery_enabled': True})
    assert calls[2] == 'stage'
    assert '"wakeAgent": false' in capsys.readouterr().out


def test_snapshot_main_missing_config_does_not_read_sources(tmp_path, monkeypatch):
    import pytest
    import hermes_tingle_sources as sources
    (tmp_path / "hermes-tingle.json").unlink()
    calls = []
    monkeypatch.setattr(sources, "collect_snapshot", lambda **kwargs: calls.append(kwargs))
    with pytest.raises(ValueError, match="config"):
        source_snapshot.main()
    assert calls == []


def test_source_snapshot_wakes_for_candidates_and_fences_them_as_data():
    snapshot = {
        "due_items": [],
        "emails": [{"source_type": "email", "source_id": "m-1", "snippet": "reply by Friday"}],
        "calendar_events": [],
        "errors": [],
    }

    result = json.loads(build_agent_input(snapshot))

    assert result["source_snapshot"]["emails"][0]["source_id"] == "m-1"
    assert result["source_snapshot"]["untrusted_source_data"] is True


def test_source_snapshot_wakes_for_email_deletion_completion_signal():
    result = json.loads(build_agent_input({
        "due_items": [], "emails": [], "calendar_events": [], "errors": [],
        "email_completion_signals": [{"item_id": "HT-ABC123", "state": "all_messages_trashed"}],
    }))

    assert result["source_snapshot"]["email_completion_signals"][0]["item_id"] == "HT-ABC123"


def test_source_snapshot_wakes_for_due_email_thread_context():
    result = json.loads(build_agent_input({
        "due_items": [], "emails": [], "calendar_events": [], "errors": [],
        "tracked_email_context": [{"item_id": "HT-ABC123", "messages": [{"source_id": "m-1"}]}],
    }))

    assert result["source_snapshot"]["tracked_email_context"][0]["item_id"] == "HT-ABC123"


def test_approved_feedback_rules_are_separate_from_untrusted_source_data():
    rules = [{"id": "FB-ABC123", "category": "links", "rule_text": "Read an eligible body before saying no link exists."}]
    result = json.loads(build_agent_input({
        "due_items": [], "emails": [{"source_id": "m-1"}], "calendar_events": [], "errors": [],
        "approved_feedback_rules": rules,
    }))

    assert result["source_snapshot"]["untrusted_source_data"] is True
    assert "approved_feedback_rules" not in result["source_snapshot"]
    assert result["user_approved_feedback_rules"] == rules


def test_approved_feedback_rules_alone_do_not_wake_the_agent():
    result = build_agent_input({
        "due_items": [], "emails": [], "calendar_events": [], "errors": [],
        "approved_feedback_rules": [{"id": "FB-ABC123", "rule_text": "rule"}],
    })

    assert result == '{"wakeAgent": false}'
