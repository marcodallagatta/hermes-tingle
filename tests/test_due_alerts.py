from delivery_fakes import seed_outbox_items
from delivery_fakes import capture_reservation
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from due_alerts import render_due_alerts
import due_alerts
import pytest
from runtime_support import hermes_import


@pytest.mark.parametrize("zone, expected", [
    ("America/New_York", "08 Mar 2026, 03:30 EDT"),
    ("Asia/Tokyo", "08 Mar 2026, 16:30 JST"),
])
def test_reminder_uses_profile_timezone_on_dst_transition(tmp_path, synthetic_settings, zone, expected):
    import json
    (tmp_path / "hermes-tingle.json").write_text(json.dumps({**synthetic_settings, "timezone": zone}))
    text = render_due_alerts([{"id": "HT-ABC123", "title": "Task", "due_at": "2026-03-08T07:30:00Z"}])
    assert expected in text


def test_due_script_main_formats_before_marking_notifications(tmp_path, synthetic_settings, monkeypatch, capsys):
    import json
    import hermes_tingle_store as store
    (tmp_path / "hermes-tingle.json").write_text(json.dumps({**synthetic_settings, "timezone": "Asia/Tokyo"}))
    import hermes_tingle_sources as sources
    monkeypatch.setattr(sources, "all_due_items", lambda **kwargs: [{"id": "HT-ABC123", "title": "Task", "due_at": "2026-03-08T07:30:00Z"}])
    marked = []
    capture_reservation(monkeypatch, store, marked)
    assert due_alerts.main() == 0
    assert "08 Mar 2026, 16:30 JST" in capsys.readouterr().out
    assert marked == ["HT-ABC123"]


def test_due_script_missing_config_does_not_mark_reminder(tmp_path, monkeypatch):
    import hermes_tingle_store as store
    (tmp_path / "hermes-tingle.json").unlink()
    monkeypatch.setattr(store, "due_items", lambda limit: [{"id": "HT-ABC123", "due_at": "2026-03-08T07:30:00Z"}])
    marked = []
    capture_reservation(monkeypatch, store, marked)
    with pytest.raises(ValueError, match="config"):
        due_alerts.main()
    assert marked == []


def test_due_script_missing_config_cannot_mark_item_without_deadline(tmp_path, monkeypatch, capsys):
    import hermes_tingle_store as store
    (tmp_path / 'hermes-tingle.json').unlink()
    marked = []
    monkeypatch.setattr(store, 'due_items', lambda limit: [{'id': 'HT-ABC123', 'title': 'Task', 'due_at': None}])
    capture_reservation(monkeypatch, store, marked)
    with pytest.raises(ValueError, match='config'):
        due_alerts.main()
    assert marked == []
    assert capsys.readouterr().out == ''


def test_independent_later_step_deadline_is_not_suppressed():
    import due_alerts
    parent = {"id": "HT-AAAAAA", "title": "Project", "status": "open"}
    first = {"id": "HT-BBBBBB", "title": "First step", "parent_id": parent["id"]}
    later = {"id": "HT-CCCCCC", "title": "Deadline for later step", "parent_id": parent["id"],
             "due_at": "2020-01-01T00:00:00+00:00"}
    selected = due_alerts.alertable_due_items([later], lambda _: first, lambda _: parent)
    assert [item["id"] for item in selected] == [later["id"]]


def test_main_reminds_due_parent_and_child_once_each_in_one_message(monkeypatch, tmp_path, capsys):
    import hermes_constants
    import hermes_tingle_store as store
    import due_alerts

    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    parent = {"id": "HT-AAAAAA", "title": "Project", "status": "open"}
    first = {"id": "HT-BBBBBB", "title": "Get quote", "status": "open", "parent_id": parent["id"], "sequence": 1}
    later = {"id": "HT-CCCCCC", "title": "Approve", "status": "open", "parent_id": parent["id"], "sequence": 2}
    monkeypatch.setattr(store, "due_items", lambda **kwargs: [parent, later, first, first])
    monkeypatch.setattr(store, "get_item", lambda item_id: parent if item_id == parent["id"] else None)
    monkeypatch.setattr(store, "next_step", lambda item_id: first if item_id == parent["id"] else None, raising=False)
    notified = []
    capture_reservation(monkeypatch, store, notified)

    assert due_alerts.main() == 0
    text = capsys.readouterr().out
    assert text.count(f"`{first['id']}`") == 1
    assert text.count(f"`{parent['id']}`") == 1
    assert later["id"] not in text
    assert notified == [parent["id"], first["id"]]


def test_due_parent_is_reminded_when_first_child_is_not_due(monkeypatch, tmp_path, capsys):
    import hermes_constants
    import hermes_tingle_store as store
    import due_alerts

    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    parent = {"id": "HT-AAAAAA", "title": "Project", "status": "open"}
    first = {"id": "HT-BBBBBB", "title": "Get quote", "parent_id": parent["id"], "sequence": 1}
    later = {"id": "HT-CCCCCC", "title": "Approve", "parent_id": parent["id"], "sequence": 2}
    monkeypatch.setattr(store, "due_items", lambda **kwargs: [parent, later])
    monkeypatch.setattr(store, "next_step", lambda item_id: first if item_id == parent["id"] else None, raising=False)
    notified = []
    capture_reservation(monkeypatch, store, notified)
    assert due_alerts.main() == 0
    text = capsys.readouterr().out
    assert parent["id"] in text
    assert later["id"] not in text
    assert notified == [parent["id"]]


def test_nested_child_of_later_step_is_not_reminded(monkeypatch, tmp_path, capsys):
    import hermes_constants
    import hermes_tingle_store as store
    import due_alerts

    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    root = {"id": "HT-AAAAAA", "title": "Goal"}
    first = {"id": "HT-BBBBBB", "parent_id": root["id"], "sequence": 1}
    later = {"id": "HT-CCCCCC", "parent_id": root["id"], "sequence": 2}
    nested = {"id": "HT-DDDDDD", "parent_id": later["id"], "sequence": 1, "title": "Later substep"}
    lookup = {x["id"]: x for x in (root, first, later, nested)}
    monkeypatch.setattr(store, "due_items", lambda **kwargs: [nested])
    monkeypatch.setattr(store, "get_item", lookup.get)
    monkeypatch.setattr(store, "next_step", lambda item_id: {root["id"]: first, later["id"]: nested}.get(item_id), raising=False)
    notified = []
    capture_reservation(monkeypatch, store, notified)
    assert due_alerts.main() == 0
    assert capsys.readouterr().out == ""
    assert notified == []


def test_unverified_email_first_step_does_not_suppress_due_parent(monkeypatch, tmp_path, capsys):
    import hermes_constants
    import hermes_tingle_store as store
    import due_alerts

    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    parent = {"id": "HT-AAAAAA", "title": "Goal"}
    email = {"id": "HT-BBBBBB", "parent_id": parent["id"], "source_type": "email",
             "completion_signal": "source_email_reply_received", "title": "Private reply"}
    later = {"id": "HT-CCCCCC", "parent_id": parent["id"], "title": "Later step"}
    monkeypatch.setattr(store, "due_items", lambda **kwargs: [parent, email, later])
    monkeypatch.setattr(store, "get_item", lambda item_id: parent if item_id == parent["id"] else None)
    monkeypatch.setattr(store, "next_step", lambda item_id: email if item_id == parent["id"] else None, raising=False)
    notified = []
    capture_reservation(monkeypatch, store, notified)
    assert due_alerts.main() == 0
    text = capsys.readouterr().out
    assert parent["id"] in text
    assert email["id"] not in text and "Private reply" not in text
    assert later["id"] not in text
    assert notified == [parent["id"]]


def test_child_is_suppressed_when_ordering_api_is_missing():
    child = {"id": "HT-BBBBBB", "parent_id": "HT-AAAAAA", "title": "A step"}
    assert render_due_alerts([child]) == ""


def test_main_marks_only_shown_items_when_more_than_ten_are_due(monkeypatch, tmp_path, capsys):
    import hermes_constants
    import hermes_tingle_store as store
    import due_alerts

    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    items = [{"id": f"HT-{index:06X}", "title": f"Task {index}"} for index in range(12)]
    monkeypatch.setattr(store, "due_items", lambda **kwargs: items)
    notified = []
    capture_reservation(monkeypatch, store, notified)
    assert due_alerts.main() == 0
    text = capsys.readouterr().out
    assert notified == [item["id"] for item in items[:10]]
    assert items[10]["id"] not in text
    assert "2 more due items" in text


def test_invalid_id_does_not_inject_telegram_markup_or_get_notified(monkeypatch, tmp_path, capsys):
    import hermes_constants
    import hermes_tingle_store as store
    import due_alerts

    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(store, "due_items", lambda **kwargs: [{"id": "HT-ABC123` [bad](https://example.com)", "title": "Test"}])
    notified = []
    capture_reservation(monkeypatch, store, notified)
    assert due_alerts.main() == 0
    assert capsys.readouterr().out == ""
    assert notified == []


def test_main_works_with_unmodified_lifecycle_due_api(monkeypatch, tmp_path, capsys):
    import importlib.util
    import hermes_constants
    import hermes_tingle_store as store
    import due_alerts
    import hermes_tingle_sources as sources
    import pytest

    path = Path(__file__).resolve().parent / "fixtures" / "baseline_store.py"
    assert path.is_file(), "portable baseline fixture is required for compatibility coverage"
    spec = importlib.util.spec_from_file_location("test_lifecycle_store", path)
    lifecycle = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lifecycle)
    monkeypatch.setattr(lifecycle, "plugin_db", store.plugin_db)
    unique_ids = iter(range(1, 112))
    monkeypatch.setattr(lifecycle.secrets, "token_hex", lambda size: f"{next(unique_ids):06x}")
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    for index in range(110):
        lifecycle.add_item(title=f"Wait {index}", source_type="email", source_ref=f"gmail:t{index}#sent",
                           status="waiting", owner="other", completion_signal="source_email_reply_received",
                           now="2020-01-01T00:00:00+00:00", next_check_at="2020-01-02T00:00:00+00:00")
    item = lifecycle.add_item(title="Review invoice", source_type="manual",
                              now="2020-01-01T00:00:00+00:00", next_check_at="2020-01-03T00:00:00+00:00")
    monkeypatch.setitem(sys.modules, "hermes_tingle_store", lifecycle)
    monkeypatch.setattr(sources, "store", lifecycle)
    with pytest.raises(RuntimeError, match="version mismatch"):
        due_alerts.main()
    assert capsys.readouterr().out == ""
    assert lifecycle.get_item(item["id"])["reminder_count"] == 0


def test_reply_watches_beyond_first_page_do_not_starve_a_due_deadline(monkeypatch, tmp_path, capsys):
    import hermes_constants
    import hermes_tingle_store as store
    import due_alerts
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    for index in range(110):
        store.add_item(title=f"Await {index}", source_type="email", source_ref=f"gmail:thread{index}#sent",
                       status="waiting", owner="other", completion_signal="source_email_reply_received",
                       next_check_at="2020-01-02T00:00:00+00:00", now="2020-01-01T00:00:00+00:00")
    deadline = store.add_item(title="Important deadline", source_type="manual",
                              due_at="2020-01-04T00:00:00+00:00",
                              next_check_at="2020-01-03T00:00:00+00:00", now="2020-01-01T00:00:00+00:00")
    assert due_alerts.main() == 0
    assert deadline["id"] in capsys.readouterr().out
    assert store.get_item(deadline["id"])["reminder_count"] == 0
    assert store.pending_alert_emissions()
    assert all(item["reminder_count"] == 0 for item in store.due_items(limit=100))


def test_suggested_source_text_is_literal_in_telegram_conversion():
    to_markdown_v2 = hermes_import(
        "plugins.platforms.telegram.telegram_markdown",
        reason="requires the Hermes runtime Telegram converter",
    ).to_markdown_v2
    text = render_due_alerts([{
        "id": "HT-ABC123", "title": "Invoice", "status": "open", "owner": "me",
        "summary": "Next action: Review [link](https://example.com) *urgent* `injected`",
    }])
    converted = to_markdown_v2(text)
    assert "`Review [link](https://example.com) *urgent* 'injected'`" in converted
    assert "`HT-ABC123`" in converted


def test_due_parent_and_child_at_different_times_keep_independent_notices():
    import due_alerts
    parent = {"id": "HT-AAAAAA", "title": "Project", "due_at": "2026-11-15T10:00:00+00:00"}
    child = {"id": "HT-BBBBBB", "title": "Review quote", "parent_id": parent["id"],
             "due_at": "2026-11-16T10:00:00+00:00"}
    lookup = {parent["id"]: parent, child["id"]: child}
    next_step = lambda item_id: child if item_id == parent["id"] else None
    assert [i["id"] for i in due_alerts.alertable_due_items([parent], next_step, lookup.get)] == [parent["id"]]
    assert [i["id"] for i in due_alerts.alertable_due_items([child], next_step, lookup.get)] == [child["id"]]
    assert [i["id"] for i in due_alerts.alertable_due_items([parent, child], next_step, lookup.get)] == [parent["id"], child["id"]]


def test_unverified_or_email_child_is_not_suggested():
    parent = {"id": "HT-AAAAAA", "title": "Project", "status": "open", "owner": "me"}
    child = {"id": "HT-BBBBBB", "title": "Review reply", "status": "open", "owner": "me",
             "parent_id": parent["id"], "summary": "", "source_type": "email",
             "completion_signal": "source_email_reply_received"}
    for lookup in (lambda _: None, lambda _: {**child, "id": "HT-CCCCCC"}, lambda _: child):
        text = render_due_alerts([parent], next_step=lambda _: child, get_item=lookup)
        assert "Review reply" not in text
        assert "Specific next action not determined" in text


def test_telegram_markup_and_budget_for_escape_heavy_suggestions():
    to_markdown_v2 = hermes_import(
        "plugins.platforms.telegram.telegram_markdown",
        reason="requires the Hermes runtime Telegram converter",
    ).to_markdown_v2
    items = [{"id": f"HT-{i:06X}", "title": "T" * 100, "status": "open", "owner": "me",
              "summary": "Next action: " + "\\" * 30 + "` [x](https://bad)"[:10],
              "due_at": "2026-11-15T08:10:00+00:00"} for i in range(10)]
    text = render_due_alerts(items)
    converted = to_markdown_v2(text)
    assert text.count("Stored next step (verify): `") == 10
    assert len(converted) <= 4096
    assert "`HT-000000`" in converted


def test_telegram_budget_with_escape_heavy_titles_and_suggestions():
    to_markdown_v2 = hermes_import(
        "plugins.platforms.telegram.telegram_markdown",
        reason="requires the Hermes runtime Telegram converter",
    ).to_markdown_v2
    items = [{"id": f"HT-{i:06X}", "title": "\\" * 100, "status": "open", "owner": "me",
              "summary": "Next action: " + "\\" * 100,
              "due_at": "2026-11-15T08:10:00+00:00"} for i in range(10)]
    text = render_due_alerts(items)
    assert len(to_markdown_v2(text)) <= 4096
    assert text.count("Specific next action not determined") == 10
    for item in items:
        item["summary"] = "Next action: " + "\\" * 40
    text = render_due_alerts(items)
    assert text.count("Stored next step (verify)") == 10
    assert len(to_markdown_v2(text)) <= 4096


def test_four_notice_limit_and_no_automatic_external_actions(monkeypatch, tmp_path, capsys):
    import subprocess
    from datetime import timedelta
    import hermes_constants
    import hermes_tingle_store as store
    import due_alerts

    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    def forbidden(*args, **kwargs):
        raise AssertionError("external action attempted")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    clock = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)
    monkeypatch.setattr(due_alerts, "datetime", Clock)
    item = store.add_item(title="Review invoice", source_type="manual", now=clock[0], next_check_at=clock[0])
    monkeypatch.setattr(store, "_now", lambda value=None: store._parse_time(value) if value else clock[0])
    for count in range(1, 5):
        assert due_alerts.main() == 0
        assert item["id"] in capsys.readouterr().out
        batch_id = store.pending_alert_emissions()[-1]["id"]
        assert store.acknowledge_alert_emissions(store.get_alert_emission(batch_id)["receipt_token"], now=clock[0]) == 1
        current = store.get_item(item["id"])
        assert current["reminder_count"] == count
        if current["next_check_at"]:
            clock[0] = datetime.fromisoformat(current["next_check_at"]) + timedelta(seconds=1)
    assert current["next_check_at"] is None
    clock[0] += timedelta(days=60)
    assert due_alerts.main() == 0
    assert capsys.readouterr().out == ""
    assert store.get_item(item["id"])["status"] == "open"


def test_overflow_items_are_not_marked_until_a_later_run(monkeypatch, tmp_path, capsys):
    import hermes_constants
    import hermes_tingle_store as store
    import due_alerts
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    items = [store.add_item(title=f"Task {i}", source_type="manual", next_check_at="2020-01-02T00:00:00+00:00",
                            now="2020-01-01T00:00:00+00:00") for i in range(23)]
    shown = set()
    for expected in (10, 10, 3):
        assert due_alerts.main() == 0
        text = capsys.readouterr().out
        batch = {i["id"] for i in items if i["id"] in text}
        assert len(batch) == expected and not (batch & shown)
        shown |= batch
        assert all(store.get_item(i["id"])["reminder_count"] == 0 for i in items)
        assert not (shown & {i["id"] for i in store.due_items(all_due=True)})
    assert len(shown) == 23


def test_due_alert_script_is_silent_when_no_items():
    assert render_due_alerts([]) == ""


def test_due_alert_script_formats_one_compact_message_for_multiple_items():
    text = render_due_alerts([
        {"id": "HT-ABC123", "title": "Send the invoice", "status": "open", "due_at": "2026-11-15T08:10:00+00:00"},
        {"id": "HT-DEF456", "title": "Check with the venue", "status": "waiting", "due_at": None},
    ])

    assert "Hermes Tingle reminders" in text
    assert "`HT-ABC123`" in text
    assert "Send the invoice" in text
    assert "`HT-DEF456`" in text
    assert "/tingle done" in text
    assert "15 Nov 2026, 08:10 UTC" in text
    assert "2026-11-15T08:10:00+00:00" not in text


def test_explicit_stored_next_action_is_a_suggestion_not_an_external_write():
    text = render_due_alerts([{
        "id": "HT-ABC123", "title": "Invoice", "status": "open", "owner": "me",
        "summary": "Next action: Review the invoice before sending.",
    }])
    assert "Stored next step (verify): `Review the invoice before sending.`" in text
    assert "Specific next action not determined" not in text


def test_due_parent_never_suggests_snoozed_child():
    parent = {"id": "HT-AAAAAA", "title": "Project", "status": "open", "owner": "me"}
    child = {"id": "HT-BBBBBB", "title": "Review quote", "status": "open", "owner": "me",
             "parent_id": parent["id"], "snoozed_until": "9999-01-01T00:00:00+00:00"}
    text = render_due_alerts([parent], next_step=lambda _: child, get_item=lambda _: child)
    assert "Review quote" not in text
    assert "Specific next action not determined" in text


def test_snooze_gate_accepts_expired_and_rejects_invalid_values():
    import due_alerts
    assert due_alerts._not_snoozed({"snoozed_until": "2020-01-01T00:00:00Z"})
    assert not due_alerts._not_snoozed({"snoozed_until": "not-a-date"})
    assert not due_alerts._not_snoozed({"snoozed_until": "2020-01-01T00:00:00"})


def test_due_parent_uses_verified_first_child_as_a_stored_step():
    parent = {"id": "HT-AAAAAA", "title": "Project", "status": "open", "owner": "me"}
    child = {"id": "HT-BBBBBB", "title": "Review the quote", "status": "open", "owner": "me",
             "parent_id": parent["id"], "summary": ""}
    text = render_due_alerts([parent], next_step=lambda _: child, get_item=lambda item_id: child if item_id == child["id"] else None)
    assert parent["id"] in text
    assert "Stored next step (verify): `Review the quote`" in text


def test_simple_actionable_title_without_conflicting_context_is_useful():
    text = render_due_alerts([{
        "id": "HT-ABC123", "title": "Review the invoice", "status": "open", "owner": "me",
        "summary": "", "source_type": "manual",
    }])
    assert "Stored next step (verify): `Review the invoice`" in text


def test_conflicting_title_is_an_item_not_a_claimed_next_action_or_raw_context():
    text = render_due_alerts([{
        "id": "HT-ABC123", "title": "Send the invoice", "status": "open", "owner": "me",
        "summary": "Invoice is waiting for private approval, not ready to send.",
    }])

    assert "Item: `Send the invoice`" in text
    assert "Next: Send the invoice" not in text
    assert "private approval" not in text
    assert "Specific next action not determined" in text


def test_untrusted_title_is_literal_in_actual_telegram_markdown_conversion():
    to_markdown_v2 = hermes_import(
        "plugins.platforms.telegram.telegram_markdown",
        reason="requires the Hermes runtime Telegram converter",
    ).to_markdown_v2

    title = "[open](https://example.com) *urgent* _now_ `oops` \\\\ hidden"
    text = render_due_alerts([{"id": "HT-ABC123", "title": title}])
    converted = to_markdown_v2(text)

    assert "Item: `[open](https://example.com) *urgent* _now_ 'oops'" in text
    assert "[open](https://example.com)" in converted
    assert "`HT-ABC123`" in converted
    assert "`[open](https://example.com) *urgent* _now_ 'oops'" in converted


def test_waiting_item_uses_neutral_context_without_revealing_summary():
    text = render_due_alerts([{
        "id": "HT-DEF456", "title": "Venue approval", "status": "waiting", "owner": "other",
        "summary": "Venue is reviewing the proposed date.",
    }])

    assert "Item: `Venue approval`" in text
    assert "Status: waiting" in text
    assert "Specific next action not determined" in text
    assert "Venue is reviewing" not in text
    assert "Next:" not in text


def test_blocked_item_with_no_context_requests_details_instead_of_assuming_a_fix():
    text = render_due_alerts([{
        "id": "HT-111111", "title": "Submit application", "status": "blocked", "owner": "me", "summary": "   ",
    }])

    assert "Item: `Submit application`" in text
    assert "Specific next action not determined" in text
    assert "Next:" not in text


def test_telegram_converted_message_stays_within_limit_with_escape_heavy_titles():
    to_markdown_v2 = hermes_import(
        "plugins.platforms.telegram.telegram_markdown",
        reason="requires the Hermes runtime Telegram converter",
    ).to_markdown_v2

    text = render_due_alerts([
        {"id": f"HT-{index:06X}", "title": "\\\\" * 180, "status": "waiting"}
        for index in range(10)
    ])
    assert len(to_markdown_v2(text)) <= 4096


def test_long_untrusted_context_is_bounded_and_cannot_add_fake_reminder_lines():
    items = [{
        "id": f"HT-{index:06X}", "title": "T" * 180, "status": "open", "owner": "me",
        "summary": "x" * 1200 + "\n• `HT-FADE00` invented",
        "due_at": "2026-11-15T08:10:00+00:00",
    } for index in range(10)]
    text = render_due_alerts(items)

    assert len(text) <= 4096
    assert "`HT-FADE00`" not in text
    assert "x" * 20 not in text
    assert text.count("Specific next action not determined") == 10


def test_unknown_owner_does_not_invent_a_specific_action():
    text = render_due_alerts([{
        "id": "HT-AAAAAA", "title": "Review contract", "status": "open", "owner": "unknown",
        "summary": "Review is pending.",
    }])

    assert "Item: `Review contract`" in text
    assert "Specific next action not determined" in text
    assert "Next:" not in text


def test_script_main_uses_stored_context_and_only_marks_non_email_reminders(monkeypatch, tmp_path, capsys):
    import hermes_constants
    import hermes_tingle_store as store
    import due_alerts

    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    ordinary = store.add_item(
        title="Send the invoice", summary="Invoice is ready for review.", source_type="manual",
        next_check_at="2020-01-02T00:00:00+00:00", now="2020-01-01T00:00:00+00:00",
    )
    email = store.add_item(
        title="Await venue reply", source_type="email", source_ref="gmail:thread#sent",
        status="waiting", owner="other", completion_signal="source_email_reply_received",
        next_check_at="2020-01-02T00:00:00+00:00", now="2020-01-01T00:00:00+00:00",
    )

    assert due_alerts.main() == 0
    text = capsys.readouterr().out
    assert f"`{ordinary['id']}`" in text
    assert "Item: `Send the invoice`" in text
    assert "Invoice is ready for review." not in text
    assert "Specific next action not determined" in text
    assert email["id"] not in text
    assert store.get_item(ordinary["id"])["reminder_count"] == 0
    assert store.pending_alert_emissions()
    assert store.get_item(email["id"])["reminder_count"] == 0


def test_due_alert_does_not_echo_unparseable_dates():
    text = render_due_alerts([
        {"id": "HT-ABC123", "title": "Call the contact", "status": "open", "due_at": "not-a-date"},
    ])

    assert "date unavailable" in text
    assert "not-a-date" not in text


def test_v2_goal_displays_first_incomplete_step():
    import hermes_tingle_store as store
    import hermes_tingle_sources as sources
    goal = store.add_item(title='Project', source_type='manual', next_check_at='2020-01-02T00:00:00+00:00', now='2020-01-01T00:00:00+00:00')
    store.add_step(goal['id'], title='Gather records', why='Need context')
    store.add_step(goal['id'], title='Review outcome', why='Decide')
    store.complete_step(goal['id'], 1)
    output = render_due_alerts(sources.all_due_items(now=datetime(2026, 1, 1, tzinfo=timezone.utc)), get_item=store.get_item)
    assert 'Review outcome' in output
    assert 'Gather records' not in output


def test_v2_goal_shows_linked_wait_as_wait_without_send_instruction():
    import hermes_tingle_store as store
    import hermes_tingle_sources as sources
    goal = store.add_item(title='Project', source_type='manual', next_check_at='2020-01-02T00:00:00+00:00', now='2020-01-01T00:00:00+00:00')
    wait = store.add_item(title='Await response', source_type='email', source_ref='gmail:synthetic#sent', status='waiting', owner='other', completion_signal='source_email_reply_received', next_check_at='2020-01-02T00:00:00+00:00', now='2020-01-01T00:00:00+00:00')
    store.add_step(goal['id'], title='Await response', why='Need response', wait_item_id=wait['id'])
    store.add_step(goal['id'], title='Review outcome', why='Decide')
    output = render_due_alerts(sources.all_due_items(now=datetime(2026, 1, 1, tzinfo=timezone.utc)), get_item=store.get_item)
    assert 'Waiting: `Await response`' in output
    assert 'Review outcome' not in output
    assert 'send' not in output.lower()


def test_v2_linked_snoozed_wait_is_not_exposed():
    import hermes_tingle_store as store
    import hermes_tingle_sources as sources
    goal = store.add_item(title='Project', source_type='manual', next_check_at='2020-01-02T00:00:00+00:00', now='2020-01-01T00:00:00+00:00')
    wait = store.add_item(title='Private wait', source_type='email', source_ref='gmail:synthetic#sent', status='waiting', owner='other', completion_signal='source_email_reply_received', next_check_at='2020-01-02T00:00:00+00:00', now='2020-01-01T00:00:00+00:00')
    store.add_step(goal['id'], title='Private wait', why='Need response', wait_item_id=wait['id'])
    store.snooze_item(wait['id'], until='2099-01-01T00:00:00+00:00', now='2026-01-01T00:00:00+00:00')
    output = render_due_alerts(sources.all_due_items(now=datetime(2026, 1, 1, tzinfo=timezone.utc)), get_item=store.get_item)
    assert 'Private wait' not in output
    assert wait['id'] not in output
    assert goal['id'] in output


class _RecordingStdout:
    def __init__(self, events=None, fail=False):
        self.events = events if events is not None else []
        self.fail = fail

    def write(self, text):
        if self.fail:
            raise OSError("synthetic stdout failure")
        self.events.append(("write", text))
        return len(text)

    def flush(self):
        self.events.append(("flush", None))


def test_main_flush_does_not_count_as_delivery(tmp_path, synthetic_settings, monkeypatch):
    import hermes_tingle_store as store
    item = store.add_item(title="Synthetic task", next_check_at="2020-01-01T00:00:00+00:00")
    events = []
    monkeypatch.setattr(due_alerts.sys, "stdout", _RecordingStdout(events))
    assert due_alerts.main() == 0
    batch = store.get_alert_emission(store.pending_alert_emissions()[0]["id"])
    assert batch["emitted_to_gateway_at"] and not batch["acked_at"]
    assert store.get_item(item["id"])["reminder_count"] == 0
    assert any(kind == "flush" for kind, _ in events)


def test_main_does_not_count_unemitted_alerts_when_stdout_fails(tmp_path, synthetic_settings, monkeypatch):
    import hermes_tingle_store as store
    import hermes_tingle_sources as sources
    monkeypatch.setattr(sources, "all_due_items", lambda **kwargs: [{"id": "HT-ABC123", "title": "Task", "due_at": "2026-03-08T07:30:00Z"}])
    monkeypatch.setattr(due_alerts.sys, "stdout", _RecordingStdout(fail=True))
    marked = []
    capture_reservation(monkeypatch, store, marked)

    assert due_alerts.main() == 1
    assert marked == ["HT-ABC123"]  # reserved, never counted as delivered


def test_main_emitted_alert_survives_a_flush_marker_failure(tmp_path, synthetic_settings, monkeypatch):
    import hermes_tingle_store as store
    item = store.add_item(title="Synthetic task", next_check_at="2020-01-01T00:00:00+00:00")
    events = []
    monkeypatch.setattr(due_alerts.sys, "stdout", _RecordingStdout(events))
    def broken_marker(*args):
        raise RuntimeError("synthetic database failure")
    monkeypatch.setattr(store, "mark_emission_flushed", broken_marker)
    with pytest.raises(RuntimeError, match="synthetic database failure"):
        due_alerts.main()
    assert item["id"] in next(text for kind, text in events if kind == "write")
    assert store.get_item(item["id"])["reminder_count"] == 0
    assert store.pending_alert_emissions()
    events.clear()
    assert due_alerts.main() == 0
    assert events == []  # crash/flush uncertainty is held, never auto-repeated


def _one_due_item(monkeypatch):
    import hermes_tingle_store as store
    seed_outbox_items(store, ['HT-ABC123'])


def test_main_records_an_unconfirmed_emission_after_success(tmp_path, synthetic_settings, monkeypatch, capsys):
    import hermes_tingle_store as store
    _one_due_item(monkeypatch)
    assert due_alerts.main() == 0
    capsys.readouterr()
    rows = store.unsurfaced_emissions(older_than="2999-01-01T00:00:00+00:00")
    assert len(rows) == 1
    assert rows[0]["acked_at"] is None
    assert "HT-ABC123" in rows[0]["item_ids"]


def test_main_logs_before_emitting_so_a_failed_flush_stays_visible(tmp_path, synthetic_settings, monkeypatch):
    # The outbox record remains available for owner inspection even though the
    # scheduled helper no longer repeats an uncertainty notice.
    import hermes_tingle_store as store
    _one_due_item(monkeypatch)
    monkeypatch.setattr(due_alerts.sys, "stdout", _RecordingStdout(fail=True))
    assert due_alerts.main() == 1
    logged = store.unsurfaced_emissions(older_than="2999-01-01T00:00:00+00:00")
    assert len(logged) == 1
    assert logged[0]["acked_at"] is None
    assert "HT-ABC123" in logged[0]["item_ids"]
    # The batch is not counted and is held: no automatic ambiguous repeat.
    notified_calls = []
    monkeypatch.setattr(store, "mark_notified", lambda ids: notified_calls.append(ids))
    monkeypatch.setattr(due_alerts.sys, "stdout", _RecordingStdout(fail=True))
    assert due_alerts.main() == 0
    assert notified_calls == []


def test_old_unconfirmed_emissions_stay_silent_and_available_for_inspection(tmp_path, synthetic_settings, monkeypatch, capsys):
    import hermes_tingle_store as store
    store.record_alert_emission(seed_outbox_items(store, ["HT-F00D11"], now="2020-01-01T00:00:00+00:00"), now="2020-01-01T00:00:00+00:00")
    import hermes_tingle_sources as sources
    monkeypatch.setattr(sources, "all_due_items", lambda **kwargs: [])

    assert due_alerts.main() == 0
    assert capsys.readouterr().out == ""
    rows = store.unsurfaced_emissions(older_than="2999-01-01T00:00:00+00:00")
    assert len(rows) == 1
    assert rows[0]["acked_at"] is None
    assert "HT-F00D11" in rows[0]["item_ids"]

    assert due_alerts.main() == 0
    assert capsys.readouterr().out == ""
    assert len(store.unsurfaced_emissions(older_than="2999-01-01T00:00:00+00:00")) == 1


def test_standalone_helper_copy_keeps_old_batch_silent(tmp_path, synthetic_settings, monkeypatch, capsys):
    import runpy
    import hermes_constants
    import hermes_tingle_store as store
    import hermes_tingle_sources as sources

    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    store.record_alert_emission(seed_outbox_items(store, ["HT-F00D11"], now="2020-01-01T00:00:00+00:00"), now="2020-01-01T00:00:00+00:00")
    monkeypatch.setattr(sources, "all_due_items", lambda **kwargs: [])
    helper = tmp_path / "scripts" / "hermes_tingle_due.py"
    helper.parent.mkdir()
    source = due_alerts.__file__
    assert source is not None
    helper.write_bytes(Path(source).read_bytes())

    copied = runpy.run_path(str(helper), run_name="tingle_profile_helper_test")
    assert copied["main"]() == 0
    assert capsys.readouterr().out == ""
    rows = store.unsurfaced_emissions(older_than="2999-01-01T00:00:00+00:00")
    assert len(rows) == 1 and rows[0]["acked_at"] is None



def test_acked_emissions_do_not_surface(tmp_path, synthetic_settings, monkeypatch, capsys):
    import hermes_tingle_store as store
    batch_id = store.record_alert_emission(seed_outbox_items(store, ["HT-F00D11"], now="2020-01-01T00:00:00+00:00"), now="2020-01-01T00:00:00+00:00")
    store.mark_emission_flushed(batch_id)
    store.acknowledge_alert_emissions(store.get_alert_emission(batch_id)["receipt_token"], now="2020-01-02T00:00:00+00:00")
    _one_due_item(monkeypatch)
    assert due_alerts.main() == 0
    assert "unconfirmed" not in capsys.readouterr().out


def test_emission_states_never_equate_prepared_with_delivered(tmp_path, synthetic_settings, monkeypatch):
    import hermes_tingle_store as store
    _one_due_item(monkeypatch)
    monkeypatch.setattr(due_alerts.sys, "stdout", _RecordingStdout(fail=True))
    assert due_alerts.main() == 1
    batch = store.get_alert_emission(store.pending_alert_emissions()[0]["id"])
    assert store.emission_state(batch) == "prepared_unconfirmed"
    assert store.get_alert_emission(batch["id"])["acked_at"] is None  # no inferred receipt
    monkeypatch.setattr(due_alerts.sys, "stdout", _RecordingStdout())
    assert due_alerts.main() == 0
    assert len(store.pending_alert_emissions()) == 1
    assert store.release_alert_emission(batch["id"])
    assert due_alerts.main() == 0
    # Retry does not erase the first unknown batch. The new emitted
    # batch is a separate attempt, not a replacement for its history.
    pending = store.pending_alert_emissions()
    assert pending[0]["id"] == batch["id"]
    assert store.emission_state(store.get_alert_emission(batch["id"])) == "prepared_unconfirmed"
    batch2 = store.get_alert_emission(pending[-1]["id"])
    assert store.emission_state(batch2) == "emitted_unconfirmed"
    assert store.acknowledge_alert_emissions(batch2["receipt_token"]) == 1
    assert [r["id"] for r in store.pending_alert_emissions()] == [batch["id"]]
    assert store.acknowledge_alert_emissions(batch["receipt_token"]) == 1
    assert not store.pending_alert_emissions()


def test_mark_emission_flushed_is_one_way(tmp_path, synthetic_settings):
    import hermes_tingle_store as store
    emission_id = store.record_alert_emission(seed_outbox_items(store, ["HT-ABC123"], now="2026-11-16T10:00:00+00:00"), now="2026-11-16T10:00:00+00:00")
    store.mark_emission_flushed(emission_id, now="2026-11-16T10:00:05+00:00")
    store.mark_emission_flushed(emission_id, now="2026-11-16T10:00:09+00:00")
    rows = store.unsurfaced_emissions(older_than="2999-01-01T00:00:00+00:00")
    assert rows[0]["emitted_to_gateway_at"] == "2026-11-16T10:00:05+00:00"
