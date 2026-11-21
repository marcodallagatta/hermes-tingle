"""FT-12: /tingle list opens with a one-glance overview of what needs attention.

Counts appear only when nonzero, the soonest upcoming deadline is named, and
the overview stays on page 1 so pagination output is unchanged.
"""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

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


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def test_overview_counts_nonzero_states(isolated_db):
    now = datetime.now(timezone.utc)
    isolated_db.add_item(title="Overdue synthetic", status="open", due_at=_iso(now - timedelta(hours=2)))
    isolated_db.add_item(title="Soon synthetic", status="open", due_at=_iso(now + timedelta(hours=24)))
    isolated_db.add_item(title="Waiting synthetic", status="waiting")
    isolated_db.add_item(title="Blocked synthetic", status="blocked")

    listed = hermes_tingle.handle_command(None, "list")

    assert "⚠️ 1 overdue" in listed
    assert "📅 1 due within 48h" in listed
    assert "⏳ 1 waiting on others" in listed
    assert "🚧 1 blocked" in listed


def test_overview_hides_zero_states(isolated_db):
    isolated_db.add_item(title="Waiting synthetic", status="waiting")

    listed = hermes_tingle.handle_command(None, "list")

    assert "⏳ 1 waiting on others" in listed
    assert "overdue" not in listed
    assert "due within 48h" not in listed
    assert "🚧" not in listed


def test_overview_names_the_next_deadline(isolated_db):
    now = datetime.now(timezone.utc)
    isolated_db.add_item(title="Later synthetic", status="open", due_at=_iso(now + timedelta(days=5)))
    isolated_db.add_item(title="Sooner synthetic", status="open", due_at=_iso(now + timedelta(days=2)))

    listed = hermes_tingle.handle_command(None, "list")

    assert "Next deadline: Sooner synthetic" in listed


def test_overview_absent_when_all_clear(isolated_db):
    listed = hermes_tingle.handle_command(None, "list")
    assert "All clear" in listed
    assert "Next deadline" not in listed


def test_overview_only_on_first_page(isolated_db):
    for i in range(21):
        isolated_db.add_item(title=f"Synthetic task {i:02d}", status="open")

    page_two = hermes_tingle.handle_command(None, "list 2")

    assert "Next deadline" not in page_two
    assert "overdue" not in page_two
    assert "waiting on others" not in page_two
