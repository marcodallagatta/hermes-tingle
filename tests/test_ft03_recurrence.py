from delivery_fakes import confirmed_reminder, review_current_source
"""FT-03: recurring obligations (bills, renewals, cancellation windows).

Completing a recurring item records the occurrence in recurrence_completions,
advances its deadline calendar-aware from the previous due_at (never from now,
always at least one interval), resets the reminder series, and re-applies the
stored lead time so every occurrence gets the same advance warning.
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import hermes_tingle_store as store


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


def _bill(isolated_store, **overrides):
    params = dict(title="Pay synthetic hosting bill", source_type="manual",
                  due_at="2026-01-31T09:00:00+00:00", next_check_at="2026-01-24T09:00:00+00:00",
                  recur_interval=1, recur_unit="month", now="2026-01-01T00:00:00+00:00")
    params.update(overrides)
    return isolated_store.add_item(**params)


def _audit_rows(isolated_store):
    conn = isolated_store.plugin_db("hermes-tingle")
    rows = conn.execute("SELECT * FROM recurrence_completions ORDER BY id").fetchall()
    conn.close()
    return [dict(row) for row in rows]


def test_completing_recurring_item_advances_instead_of_closing(isolated_store):
    bill = _bill(isolated_store)
    done = isolated_store.complete_item(bill["id"], now="2026-01-31T10:00:00+00:00")
    assert done["status"] == "open"
    assert done["completed_at"] is None
    # Calendar month: Jan 31 + 1 month clamps to Feb 28, from the old due, not now.
    assert done["due_at"] == "2026-02-28T09:00:00+00:00"
    # The initial 7-day lead is stored and re-applied to every occurrence.
    assert done["recur_lead_days"] == 7
    assert done["next_check_at"] == "2026-02-21T09:00:00+00:00"
    assert done["reminder_count"] == 0 and done["last_notified_at"] is None


def test_fixed_day_recurrence_still_supported(isolated_store):
    bill = _bill(isolated_store, recur_interval=31, recur_unit="day")
    done = isolated_store.complete_item(bill["id"], now="2026-01-31T10:00:00+00:00")
    assert done["due_at"] == "2026-03-03T09:00:00+00:00"


def test_yearly_recurrence_clamps_leap_day_and_stops_at_item_cap(isolated_store):
    bill = _bill(isolated_store, due_at="2028-02-29T09:00:00+00:00", recur_interval=1,
                 recur_unit="year", now="2027-10-01T00:00:00+00:00")
    first = isolated_store.complete_item(bill["id"], now="2028-02-29T10:00:00+00:00")
    assert first["status"] == "open"
    assert first["due_at"] == "2029-02-28T09:00:00+00:00"

    final = isolated_store.complete_item(bill["id"], now="2029-02-28T10:00:00+00:00")
    assert final["status"] == "done"
    assert final["due_at"] == "2029-02-28T09:00:00+00:00"


def test_yearly_date_math_retains_leap_anchor_without_long_lived_item(isolated_store):
    anchor = datetime.fromisoformat("2028-02-29T09:00:00+00:00")
    current = anchor
    expected = ["2029-02-28", "2030-02-28", "2031-02-28", "2032-02-29", "2033-02-28"]
    for day in expected:
        current = isolated_store._advance_recurrence(
            current, 1, "year", current + timedelta(hours=1), anchor)
        assert current.isoformat(timespec="seconds") == f"{day}T09:00:00+00:00"


def test_overdue_recurrence_skips_past_occurrences(isolated_store):
    bill = _bill(isolated_store)
    done = isolated_store.complete_item(bill["id"], now="2026-05-15T10:00:00+00:00")
    # Whole calendar months from Jan 31: Feb 28, Mar 31, Apr 30 are all past.
    assert done["due_at"] == "2026-05-31T09:00:00+00:00"


def test_early_completion_still_advances_a_full_interval(isolated_store):
    bill = _bill(isolated_store)
    done = isolated_store.complete_item(bill["id"], now="2026-01-20T10:00:00+00:00")
    assert done["due_at"] == "2026-02-28T09:00:00+00:00"


def test_repeated_monthly_completions_keep_the_anchor_day(isolated_store):
    bill = _bill(isolated_store)
    expected = ["2026-02-28", "2026-03-31", "2026-04-30", "2026-05-31", "2026-06-30"]
    current_now = "2026-01-31T10:00:00+00:00"
    for day in expected:
        done = isolated_store.complete_item(bill["id"], now=current_now)
        assert done["due_at"] == f"{day}T09:00:00+00:00"
        # The persisted original anchor (Jan 31) prevents clamp drift.
        assert done["recur_anchor"] == "2026-01-31T09:00:00+00:00"
        current_now = done["due_at"].replace("T09:00:00", "T10:00:00")


def test_moving_the_deadline_starts_a_new_anchor(isolated_store):
    bill = _bill(isolated_store)
    isolated_store.complete_item(bill["id"], now="2026-01-31T10:00:00+00:00")
    moved = isolated_store.update_item(bill["id"], due_at="2026-03-15T09:00:00+00:00")
    assert moved["recur_anchor"] == "2026-03-15T09:00:00+00:00"
    done = isolated_store.complete_item(bill["id"], now="2026-03-15T10:00:00+00:00")
    assert done["due_at"] == "2026-04-15T09:00:00+00:00"
    assert done["recur_anchor"] == "2026-03-15T09:00:00+00:00"


def test_reminder_series_reset_keeps_lead_for_next_occurrence(isolated_store):
    bill = _bill(isolated_store)
    for _ in range(1):
        confirmed_reminder(isolated_store, [bill["id"]], now=isolated_store.get_item(bill["id"])["next_check_at"])
    # The series converged next_check_at onto the deadline (existing behavior)...
    assert isolated_store.get_item(bill["id"])["next_check_at"] == "2026-01-31T09:00:00+00:00"
    done = isolated_store.complete_item(bill["id"], now="2026-01-31T10:00:00+00:00")
    # ...but the stored lead still gives the next occurrence its full warning.
    assert done["next_check_at"] == "2026-02-21T09:00:00+00:00"
    due = [row["id"] for row in isolated_store.due_items(now="2026-02-21T09:00:00+00:00")]
    assert due == [bill["id"]]


def test_day_recurrence_preserves_wall_clock_across_dst(tmp_path, synthetic_settings, isolated_store):
    (tmp_path / "hermes-tingle.json").write_text(json.dumps({**synthetic_settings, "timezone": "America/New_York"}))
    bill = _bill(isolated_store, due_at="2026-03-07T09:00:00-05:00",
                 next_check_at="2026-03-06T09:00:00-05:00", recur_interval=1, recur_unit="day")
    done = isolated_store.complete_item(bill["id"], now="2026-03-07T10:00:00-05:00")
    # 2026-03-08 is the DST switch in America/New_York: the 09:00 local wall clock holds.
    assert done["due_at"] == "2026-03-08T09:00:00-04:00"


def test_every_completion_is_audited(isolated_store):
    bill = _bill(isolated_store)
    isolated_store.complete_item(bill["id"], note="Paid by card", now="2026-01-31T10:00:00+00:00")
    isolated_store.complete_item(bill["id"], now="2026-02-28T10:00:00+00:00")
    rows = _audit_rows(isolated_store)
    assert [row["occurrence_due_at"] for row in rows] == ["2026-01-31T09:00:00+00:00", "2026-02-28T09:00:00+00:00"]
    assert rows[0]["note"] == "Paid by card" and rows[1]["note"] == ""
    assert all(row["item_id"] == bill["id"] for row in rows)


@pytest.mark.parametrize("value", [0, -5, True, "monthly"])
def test_invalid_recurrence_interval_is_rejected(isolated_store, value):
    with pytest.raises(ValueError, match="positive integer"):
        _bill(isolated_store, recur_interval=value)


def test_invalid_recurrence_unit_is_rejected(isolated_store):
    with pytest.raises(ValueError, match="day, month, or year"):
        _bill(isolated_store, recur_unit="weekly")


def test_recurrence_requires_a_deadline(isolated_store):
    with pytest.raises(ValueError, match="due_at anchor"):
        _bill(isolated_store, due_at=None, next_check_at=None)


def test_lead_without_recurrence_is_rejected(isolated_store):
    with pytest.raises(ValueError, match="requires a recurrence"):
        isolated_store.add_item(title="No recurrence", source_type="manual",
                                due_at="2026-02-01T09:00:00+00:00", recur_lead_days=3,
                                now="2026-01-01T00:00:00+00:00")


def test_explicit_lead_overrides_the_initial_gap(isolated_store):
    bill = _bill(isolated_store, recur_lead_days=14)
    done = isolated_store.complete_item(bill["id"], now="2026-01-31T10:00:00+00:00")
    assert done["recur_lead_days"] == 14
    assert done["next_check_at"] == "2026-02-14T09:00:00+00:00"


def test_update_can_start_and_stop_recurrence(isolated_store):
    item = isolated_store.add_item(title="Synthetic domain renewal", source_type="manual",
                                   due_at="2026-02-01T09:00:00+00:00", now="2026-01-01T00:00:00+00:00")
    updated = isolated_store.update_item(item["id"], recur_interval=1, recur_unit="year", recur_lead_days=30)
    assert updated["recur_interval"] == 1 and updated["recur_unit"] == "year"
    done = isolated_store.complete_item(item["id"], now="2026-02-01T10:00:00+00:00")
    assert done["due_at"] == "2027-02-01T09:00:00+00:00"
    assert done["next_check_at"] == "2027-01-02T09:00:00+00:00"
    stopped = isolated_store.update_item(item["id"], recur_interval=None, recur_unit=None)
    assert stopped["recur_interval"] is None
    assert stopped["recur_anchor"] is None
    closed = isolated_store.complete_item(item["id"], now="2027-02-01T10:00:00+00:00")
    assert closed["status"] == "done"


def test_update_rejects_recurrence_without_deadline_or_lead_alone(isolated_store):
    item = isolated_store.add_item(title="No date", source_type="manual", now="2026-01-01T00:00:00+00:00")
    with pytest.raises(ValueError, match="due_at anchor"):
        isolated_store.update_item(item["id"], recur_interval=30, recur_unit="day")
    with pytest.raises(ValueError, match="requires a recurrence"):
        isolated_store.update_item(item["id"], recur_lead_days=5)


def test_legacy_database_gains_the_recurrence_columns_and_audit_table(tmp_path):
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
    columns = {row[1] for row in conn.execute("PRAGMA table_info(hermes_tingle_items)")}
    assert {"recur_interval", "recur_unit", "recur_lead_days", "recur_anchor"} <= columns
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "recurrence_completions" in tables
    conn.close()
