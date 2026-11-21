"""FT-06 gap closure: watch staleness.

A monitor that stops reporting must not keep looking current. link_watch
accepts an expected reporting cadence (watch_max_age_hours); reports older
than that are flagged stale, a watch with no reports is flagged, and without a
declared cadence the age is surfaced without a verdict. Stale means "no report
within the expected window", never that the watched thing changed.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

import hermes_tingle
import hermes_tingle_store as store


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


def _item_with_watch(isolated_store, *, max_age_hours=None):
    item = isolated_store.add_item(title="Watch the price")
    return isolated_store.link_watch(item["id"], "cron:price-check", max_age_hours=max_age_hours)


class TestStalenessStates:
    def test_no_watch(self, isolated_store):
        item = isolated_store.add_item(title="Plain item")
        assert store.watch_staleness(item)["state"] == "no_watch"

    def test_no_reports_yet(self, isolated_store):
        item = _item_with_watch(isolated_store, max_age_hours=24)
        assert store.watch_staleness(item)["state"] == "no_reports"

    def test_fresh_within_cadence(self, isolated_store):
        item = _item_with_watch(isolated_store, max_age_hours=24)
        item = isolated_store.report_watch(item["id"], "EUR 42", now="2026-11-16T08:00:00Z")
        result = store.watch_staleness(item, now="2026-11-16T20:00:00Z")
        assert result["state"] == "fresh"
        assert result["age_hours"] == 12.0

    def test_stale_beyond_cadence(self, isolated_store):
        item = _item_with_watch(isolated_store, max_age_hours=6)
        item = isolated_store.report_watch(item["id"], "EUR 42", now="2026-11-16T08:00:00Z")
        result = store.watch_staleness(item, now="2026-11-16T20:00:00Z")
        assert result["state"] == "stale"
        assert result["max_age_hours"] == 6

    def test_unknown_cadence_surfaces_age_without_verdict(self, isolated_store):
        item = _item_with_watch(isolated_store)
        item = isolated_store.report_watch(item["id"], "EUR 42", now="2026-11-16T08:00:00Z")
        result = store.watch_staleness(item, now="2026-11-16T20:00:00Z")
        assert result["state"] == "unknown_cadence"
        assert result["age_hours"] == 12.0
        assert "max_age_hours" not in result

    def test_new_report_clears_staleness(self, isolated_store):
        item = _item_with_watch(isolated_store, max_age_hours=6)
        item = isolated_store.report_watch(item["id"], "EUR 42", now="2026-11-16T08:00:00Z")
        assert store.watch_staleness(item, now="2026-11-16T20:00:00Z")["state"] == "stale"
        item = isolated_store.report_watch(item["id"], "EUR 39", now="2026-11-16T19:00:00Z")
        assert store.watch_staleness(item, now="2026-11-16T20:00:00Z")["state"] == "fresh"

    def test_relinking_resets_reports_and_cadence(self, isolated_store):
        item = _item_with_watch(isolated_store, max_age_hours=6)
        isolated_store.report_watch(item["id"], "EUR 42", now="2026-11-16T08:00:00Z")
        relinked = isolated_store.link_watch(item["id"], "cron:new-check", max_age_hours=48)
        assert store.watch_staleness(relinked)["state"] == "no_reports"


class TestCadenceValidation:
    def test_invalid_cadence_refused(self, isolated_store):
        item = isolated_store.add_item(title="Watch the price")
        for bad in (0, -1, 721, 1.5, "6", True):
            with pytest.raises(ValueError):
                isolated_store.link_watch(item["id"], "cron:x", max_age_hours=bad)


class TestToolAndListSurface:
    def test_link_watch_tool_passes_cadence(self, isolated_store):
        item = isolated_store.add_item(title="Watch the price")
        result = json.loads(hermes_tingle.handle_tool(None, {
            "action": "link_watch", "item_id": item["id"],
            "watch_ref": "cron:price-check", "watch_max_age_hours": 12,
        }))
        assert result["success"] is True
        assert result["item"]["watch_max_age_hours"] == 12

    def test_list_flags_a_stale_watch(self, isolated_store, monkeypatch):
        item = _item_with_watch(isolated_store, max_age_hours=1)
        isolated_store.report_watch(item["id"], "EUR 42", now="2026-11-10T08:00:00Z")
        listing = hermes_tingle.handle_command(None, "list")
        assert "stale" in listing
        assert "price-check" in listing

    def test_list_flags_a_watch_without_reports(self, isolated_store):
        _item_with_watch(isolated_store, max_age_hours=24)
        listing = hermes_tingle.handle_command(None, "list")
        assert "no reports yet" in listing
