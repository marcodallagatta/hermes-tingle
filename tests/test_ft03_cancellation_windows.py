"""FT-03 completion: cancellation windows (hard deadlines that close).

A hard deadline that passes unmet does not nag forever: a non-recurring item
becomes terminal `missed`; a recurring one records the missed occurrence and
advances to the next window from its persisted anchor. The sweep runs at the
top of every due_items poll, so the regular deadline checks enforce it.
"""
import sqlite3

import pytest

import hermes_tingle_store as store


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


def test_missed_window_becomes_terminal(isolated_db):
    item = isolated_db.add_item(title="Cancel synthetic subscription", due_at="2026-11-10T12:00:00+00:00",
                                hard_deadline=True)
    closed = isolated_db.sweep_missed_deadlines(now="2026-11-16T09:00:00+00:00")
    assert closed == [{"id": item["id"], "outcome": "missed", "missed_due_at": "2026-11-10T12:00:00+00:00"}]
    after = isolated_db.get_item(item["id"])
    assert after["status"] == "missed"
    assert after["completed_at"] is None
    # Missed is no longer active but remains due for outcome notification.
    assert [row["id"] for row in isolated_db.due_items(now="2026-11-16T09:00:00+00:00")] == [item["id"]]
    assert isolated_db.list_items(status="active") == []
    assert isolated_db.sweep_missed_deadlines(now="2026-11-16T10:00:00+00:00") == []


def test_soft_deadline_still_nags(isolated_db):
    item = isolated_db.add_item(title="Ordinary synthetic task", due_at="2026-11-10T12:00:00+00:00",
                                next_check_at="2026-11-10T12:00:00+00:00")
    closed = isolated_db.sweep_missed_deadlines(now="2026-11-16T09:00:00+00:00")
    assert closed == []
    assert isolated_db.get_item(item["id"])["status"] == "open"
    assert [row["id"] for row in isolated_db.due_items(now="2026-11-16T09:00:00+00:00")] == [item["id"]]


def test_recurring_window_records_miss_and_advances(isolated_db):
    item = isolated_db.add_item(title="Synthetic annual renewal opt-out",
                                due_at="2026-11-10T12:00:00+00:00",
                                recur_interval=1, recur_unit="year", recur_lead_days=14,
                                hard_deadline=True)
    closed = isolated_db.sweep_missed_deadlines(now="2026-11-16T09:00:00+00:00")
    assert closed[0]["outcome"] == "advanced"
    assert closed[0]["next_due_at"] == "2027-11-10T12:00:00+00:00"
    after = isolated_db.get_item(item["id"])
    assert after["status"] == "open"
    assert after["due_at"] == "2027-11-10T12:00:00+00:00"
    assert after["next_check_at"] == "2027-10-27T12:00:00+00:00"  # stored lead re-applied
    assert after["reminder_count"] == 0
    # The miss is auditable alongside completions.
    conn = isolated_db.plugin_db("x")
    try:
        rows = conn.execute("SELECT * FROM recurrence_completions WHERE item_id=?", (item["id"],)).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1 and "missed" in rows[0]["note"]


def test_hard_deadline_requires_anchor(isolated_db):
    with pytest.raises(ValueError, match="requires a due_at"):
        isolated_db.add_item(title="Synthetic", hard_deadline=True)


def test_update_can_set_and_clear_hard_deadline(isolated_db):
    item = isolated_db.add_item(title="Synthetic", due_at="2026-11-10T12:00:00+00:00")
    updated = isolated_db.update_item(item["id"], hard_deadline=True)
    assert updated["hard_deadline"] == 1
    cleared = isolated_db.update_item(item["id"], hard_deadline=False)
    assert cleared["hard_deadline"] == 0
    # Cleared in time: the sweep leaves it alone.
    assert isolated_db.sweep_missed_deadlines(now="2026-11-16T09:00:00+00:00") == []
    assert isolated_db.get_item(item["id"])["status"] == "open"


def test_update_hard_deadline_without_anchor_rejected(isolated_db):
    item = isolated_db.add_item(title="Synthetic")
    with pytest.raises(ValueError, match="requires a due_at"):
        isolated_db.update_item(item["id"], hard_deadline=True)


def test_sweep_via_due_items_poll(isolated_db):
    # The regular deadline-check path enforces windows without a separate job.
    item = isolated_db.add_item(title="Cancel synthetic trial", due_at="2026-11-12T12:00:00+00:00",
                                next_check_at="2026-11-12T12:00:00+00:00", hard_deadline=True)
    isolated_db.due_items(now="2026-11-16T09:00:00+00:00")
    assert isolated_db.get_item(item["id"])["status"] == "missed"


def test_tool_surface_add_and_update(isolated_db):
    import json
    import hermes_tingle
    added = json.loads(hermes_tingle.handle_tool(None, {
        "action": "add", "title": "Cancel synthetic booking",
        "due_at": "2026-11-10T12:00:00+00:00", "hard_deadline": True}))
    assert added["success"] and added["item"]["hard_deadline"] == 1
    updated = json.loads(hermes_tingle.handle_tool(None, {
        "action": "update", "item_id": added["item"]["id"], "hard_deadline": False}))
    assert updated["success"] and updated["item"]["hard_deadline"] == 0


def test_missed_item_stays_discoverable_and_actionable(isolated_db):
    item = isolated_db.add_item(title="Cancel synthetic subscription", due_at="2026-11-10T12:00:00+00:00",
                                hard_deadline=True)
    isolated_db.sweep_missed_deadlines(now="2026-11-16T09:00:00+00:00")
    # Discoverable: explicit status listing, and the obligation is NOT implied done.
    assert [row["id"] for row in isolated_db.list_items(status="missed")] == [item["id"]]
    assert isolated_db.list_items(status="done") == []
    # Actionable: handled on new terms (complete), or dismissed, or reopened.
    done = isolated_db.complete_item(item["id"], note="charged; cancelled on new terms")
    assert done["status"] == "done"


def test_missed_item_can_be_dismissed_or_reopened(isolated_db):
    item = isolated_db.add_item(title="Synthetic", due_at="2026-11-10T12:00:00+00:00", hard_deadline=True)
    isolated_db.sweep_missed_deadlines(now="2026-11-16T09:00:00+00:00")
    reopened = isolated_db.update_item(item["id"], status="open", due_at="2026-12-01T12:00:00+00:00")
    assert reopened["status"] == "open"
    dismissed = isolated_db.dismiss_item(item["id"])
    assert dismissed["status"] == "dismissed"


def test_miss_never_completes_steps(isolated_db):
    goal = isolated_db.add_item(title="Synthetic goal with steps", due_at="2026-11-10T12:00:00+00:00",
                                hard_deadline=True)
    isolated_db.add_step(goal["id"], title="Gather synthetic document", why="required")
    isolated_db.sweep_missed_deadlines(now="2026-11-16T09:00:00+00:00")
    after = isolated_db.get_item(goal["id"])
    assert after["status"] == "missed"
    steps = after.get("steps", [])
    assert steps and not any(step["complete"] or step.get("skipped") for step in steps)


def test_missed_windows_visible_with_no_active_items(isolated_db):
    import hermes_tingle
    isolated_db.add_item(title="Cancel synthetic trial", due_at="2026-11-10T12:00:00+00:00",
                         hard_deadline=True)
    isolated_db.sweep_missed_deadlines(now="2026-11-16T09:00:00+00:00")
    listed = hermes_tingle.handle_command(None, "list")
    assert "Missed window" in listed
    assert "All clear" not in listed


def test_missed_windows_counted_in_overview(isolated_db):
    import hermes_tingle
    isolated_db.add_item(title="Active synthetic", status="open")
    isolated_db.add_item(title="Cancel synthetic trial", due_at="2026-11-10T12:00:00+00:00",
                         hard_deadline=True)
    isolated_db.sweep_missed_deadlines(now="2026-11-16T09:00:00+00:00")
    listed = hermes_tingle.handle_command(None, "list")
    assert "Missed window" in listed


def test_recurring_miss_creates_discoverable_occurrence(isolated_db):
    item = isolated_db.add_item(title="Pay synthetic bill", due_at="2026-11-10T12:00:00+00:00",
                                hard_deadline=True, recur_interval=1, recur_unit="month")
    closed = isolated_db.sweep_missed_deadlines(now="2026-11-16T09:00:00+00:00")
    assert closed and closed[0]["outcome"] == "advanced"
    occurrence_id = closed[0].get("occurrence_id")
    assert occurrence_id
    occurrence = isolated_db.get_item(occurrence_id)
    assert occurrence["status"] == "missed"
    assert occurrence["source_ref"] == f"occurrence:{item['id']}:2026-11-10T12:00:00+00:00"
    assert occurrence["due_at"] == "2026-11-10T12:00:00+00:00"
    missed = isolated_db.list_items(status="missed")
    assert any(row["id"] == occurrence_id for row in missed)
    parent = isolated_db.get_item(item["id"])
    assert parent["status"] == "open"
    assert parent["due_at"] > "2026-11-16"


def test_recurring_miss_occurrence_is_actionable(isolated_db):
    item = isolated_db.add_item(title="Pay synthetic bill", due_at="2026-11-10T12:00:00+00:00",
                                hard_deadline=True, recur_interval=1, recur_unit="month")
    closed = isolated_db.sweep_missed_deadlines(now="2026-11-16T09:00:00+00:00")
    occurrence_id = closed[0]["occurrence_id"]
    completed = isolated_db.complete_item(occurrence_id, note="paid late with fee")
    assert completed["status"] == "done"
    assert isolated_db.get_item(item["id"])["status"] == "open"


def test_recurring_miss_sweep_is_idempotent_for_occurrences(isolated_db):
    isolated_db.add_item(title="Pay synthetic bill", due_at="2026-11-10T12:00:00+00:00",
                         hard_deadline=True, recur_interval=1, recur_unit="month")
    isolated_db.sweep_missed_deadlines(now="2026-11-16T09:00:00+00:00")
    again = isolated_db.sweep_missed_deadlines(now="2026-11-16T10:00:00+00:00")
    assert again == []
    assert len(isolated_db.list_items(status="missed")) == 1


def test_each_recurring_miss_leaves_its_own_occurrence(isolated_db):
    item = isolated_db.add_item(title="Pay synthetic bill", due_at="2026-11-10T12:00:00+00:00",
                                hard_deadline=True, recur_interval=1, recur_unit="day")
    isolated_db.sweep_missed_deadlines(now="2026-11-16T09:00:00+00:00")
    isolated_db.update_item(item["id"], due_at="2026-11-17T12:00:00+00:00", next_check_at="2026-11-17T12:00:00+00:00")
    isolated_db.sweep_missed_deadlines(now="2026-11-18T09:00:00+00:00")
    missed = isolated_db.list_items(status="missed")
    occurrences = [row for row in missed if row["source_ref"].startswith(f"occurrence:{item['id']}:")]
    assert len(occurrences) == 7
    assert {row["due_at"] for row in occurrences} == {f"2026-11-{day:02d}T12:00:00+00:00" for day in [10,11,12,13,14,15,17]}


def test_occurrence_miss_does_not_touch_parent_steps(isolated_db):
    goal = isolated_db.add_item(title="Recurring synthetic goal", due_at="2026-11-10T12:00:00+00:00",
                                hard_deadline=True, recur_interval=1, recur_unit="month")
    isolated_db.add_step(goal["id"], title="Synthetic step", why="required")
    isolated_db.sweep_missed_deadlines(now="2026-11-16T09:00:00+00:00")
    parent = isolated_db.get_item(goal["id"])
    assert parent["status"] == "open"
    assert not any(step["complete"] or step.get("skipped") for step in parent.get("steps", []))
