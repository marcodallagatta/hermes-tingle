"""FT-10 trip: booking sub-state with confirmation refs and guarded cancellation windows.

Recording a booking never books or cancels anything with a provider; a cancel_by
creates a linked hard-deadline item so the existing alert engine guards the window."""
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


def test_booking_lifecycle(isolated_db):
    item = isolated_db.add_item(title="Synthetic spring trip")
    booking = isolated_db.add_booking(item["id"], kind="flight", name="Synthetic outbound")
    assert booking["state"] == "researched" and booking["position"] == 1
    booking = isolated_db.update_booking(item["id"], position=1, state="booked",
                                         confirmation_ref="SYNTH-REF-123")
    assert booking["state"] == "booked"
    assert booking["confirmation_ref"] == "SYNTH-REF-123"
    booking = isolated_db.update_booking(item["id"], position=1, state="confirmed")
    assert booking["state"] == "confirmed"


def test_bookings_attached_to_item(isolated_db):
    item = isolated_db.add_item(title="Synthetic trip")
    isolated_db.add_booking(item["id"], kind="train", name="Synthetic train")
    isolated_db.add_booking(item["id"], kind="lodging", name="Synthetic stay")
    fetched = isolated_db.get_item(item["id"])
    assert [b["name"] for b in fetched["bookings"]] == ["Synthetic train", "Synthetic stay"]


def test_booking_validation(isolated_db):
    item = isolated_db.add_item(title="Synthetic")
    try:
        isolated_db.add_booking(item["id"], name="  ")
        assert False, "expected ValueError"
    except ValueError:
        pass
    try:
        isolated_db.add_booking(item["id"], kind="spacecraft", name="Synthetic")
        assert False, "expected ValueError"
    except ValueError:
        pass
    try:
        isolated_db.update_booking(item["id"], position=9, state="booked")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_cancel_by_creates_guarded_window_item(isolated_db):
    item = isolated_db.add_item(title="Synthetic trip")
    booking = isolated_db.add_booking(item["id"], kind="lodging", name="Synthetic hotel",
                                      cancel_by="2026-12-01T18:00:00+00:00")
    linked_id = booking["cancel_item_id"]
    assert linked_id
    linked = isolated_db.get_item(linked_id)
    assert linked["due_at"] == "2026-12-01T18:00:00+00:00"
    assert linked["hard_deadline"] == 1
    assert "Synthetic hotel" in linked["title"]


def test_missed_window_is_a_missed_item_not_a_closed_booking(isolated_db):
    item = isolated_db.add_item(title="Synthetic trip")
    booking = isolated_db.add_booking(item["id"], kind="lodging", name="Synthetic hotel",
                                      cancel_by="2026-12-01T18:00:00+00:00")
    missed = isolated_db.sweep_missed_deadlines(now="2026-12-02T09:00:00+00:00")
    assert any(m["id"] == booking["cancel_item_id"] for m in missed)
    # The booking record keeps its state: a missed window never closes anything.
    assert isolated_db.get_item(item["id"])["bookings"][0]["state"] == "researched"
    assert isolated_db.get_item(booking["cancel_item_id"])["status"] == "missed"


def test_moving_cancel_by_moves_the_linked_window(isolated_db):
    item = isolated_db.add_item(title="Synthetic trip")
    booking = isolated_db.add_booking(item["id"], name="Synthetic stay",
                                      cancel_by="2026-12-01T18:00:00+00:00")
    booking = isolated_db.update_booking(item["id"], position=1, cancel_by="2026-12-05T18:00:00+00:00")
    assert booking["cancel_by"] == "2026-12-05T18:00:00+00:00"
    assert isolated_db.get_item(booking["cancel_item_id"])["due_at"] == "2026-12-05T18:00:00+00:00"


def test_late_cancel_by_creates_the_window_item(isolated_db):
    item = isolated_db.add_item(title="Synthetic trip")
    booking = isolated_db.add_booking(item["id"], name="Synthetic stay")
    assert booking["cancel_item_id"] is None
    booking = isolated_db.update_booking(item["id"], position=1, cancel_by="2026-12-01T18:00:00+00:00")
    assert booking["cancel_item_id"]
    assert isolated_db.get_item(booking["cancel_item_id"])["hard_deadline"] == 1


def test_cancelling_booking_completes_the_window_item(isolated_db):
    item = isolated_db.add_item(title="Synthetic trip")
    booking = isolated_db.add_booking(item["id"], name="Synthetic stay",
                                      cancel_by="2026-12-01T18:00:00+00:00")
    isolated_db.update_booking(item["id"], position=1, state="cancelled")
    assert isolated_db.get_item(booking["cancel_item_id"])["status"] == "done"
    # A completed window item does not re-alert or become missed.
    assert isolated_db.sweep_missed_deadlines(now="2026-12-02T09:00:00+00:00") == []


def test_cancelling_after_a_missed_window_keeps_the_consequence_visible(isolated_db):
    item = isolated_db.add_item(title="Synthetic trip")
    booking = isolated_db.add_booking(item["id"], name="Synthetic stay",
                                      cancel_by="2026-12-01T18:00:00+00:00")
    isolated_db.sweep_missed_deadlines(now="2026-12-02T09:00:00+00:00")
    assert isolated_db.get_item(booking["cancel_item_id"])["status"] == "missed"
    # Cancelling the booking afterwards (a user/model-supplied state, not provider proof)
    # must not hide the missed window: the missed item stays on the record.
    isolated_db.update_booking(item["id"], position=1, state="cancelled")
    assert isolated_db.get_item(booking["cancel_item_id"])["status"] == "missed"
    # Moving the window after it was missed does not rewrite the miss either.
    isolated_db.update_booking(item["id"], position=1, cancel_by="2026-12-10T18:00:00+00:00")
    assert isolated_db.get_item(booking["cancel_item_id"])["status"] == "missed"


def test_tool_actions_and_list_line(isolated_db):
    import json
    import hermes_tingle
    added = hermes_tingle.handle_tool(None, {"action": "add", "title": "Synthetic winter trip"})
    item_id = json.loads(added)["item"]["id"]
    hermes_tingle.handle_tool(None, {"action": "add_booking", "item_id": item_id,
                                     "booking_kind": "flight", "booking_name": "Synthetic outbound",
                                     "cancel_by": "2026-12-01T18:00:00+00:00"})
    hermes_tingle.handle_tool(None, {"action": "add_booking", "item_id": item_id,
                                     "booking_kind": "lodging", "booking_name": "Synthetic hotel"})
    result = json.loads(hermes_tingle.handle_tool(None, {"action": "update_booking", "item_id": item_id,
                                                         "booking_position": 1, "booking_state": "booked",
                                                         "confirmation_ref": "SYNTH-9"}))
    assert result["booking"]["state"] == "booked"
    listed = hermes_tingle.handle_command(None, "list")
    assert "🧳 Bookings: 1/2 booked or confirmed - next cancel-by" in listed
