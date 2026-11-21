"""FT-03 gap closure: natural-language recurrence parsing.

Users phrase recurrence in their own words ("every Monday", "every 2 weeks",
"quarterly"); the model passes the phrase through unchanged and the plugin
parses it deterministically. Ambiguous or contradictory phrases are refused,
never guessed.
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


@pytest.fixture
def isolated_db(isolated_store):
    return isolated_store


class TestParseRecurrence:
    @pytest.mark.parametrize("phrase,expected", [
        ("every Monday", (7, "day")),
        ("every friday", (7, "day")),
        ("daily", (1, "day")),
        ("every morning", (1, "day")),
        ("weekly", (7, "day")),
        ("every week", (7, "day")),
        ("every 2 weeks", (14, "day")),
        ("every two weeks", (14, "day")),
        ("fortnightly", (14, "day")),
        ("every other week", (14, "day")),
        ("monthly", (1, "month")),
        ("every month", (1, "month")),
        ("every other month", (2, "month")),
        ("every three months", (3, "month")),
        ("quarterly", (3, "month")),
        ("yearly", (1, "year")),
        ("annually", (1, "year")),
        ("every year", (1, "year")),
        ("every 6 months", (6, "month")),
    ])
    def test_common_phrases(self, phrase, expected):
        assert store.parse_recurrence(phrase) == expected

    def test_no_recurrence_refused(self):
        with pytest.raises(ValueError):
            store.parse_recurrence("soon")
        with pytest.raises(ValueError):
            store.parse_recurrence("")

    def test_contradictory_phrases_refused(self):
        with pytest.raises(ValueError):
            store.parse_recurrence("every monday monthly")

    def test_phrase_inside_a_sentence(self):
        assert store.parse_recurrence("the rent is due monthly") == (1, "month")


class TestResolveRecurrenceArgs:
    def test_text_alone_fills_parameters(self):
        assert store.resolve_recurrence_args("weekly", None, None) == (7, "day")

    def test_no_text_passes_parameters_through(self):
        assert store.resolve_recurrence_args(None, 1, "month") == (1, "month")

    def test_agreeing_sources_pass(self):
        assert store.resolve_recurrence_args("weekly", 7, "day") == (7, "day")

    def test_conflicting_sources_refused(self):
        with pytest.raises(ValueError):
            store.resolve_recurrence_args("weekly", 1, "month")


class TestToolPath:
    def test_add_with_recur_text(self, isolated_db):
        result = json.loads(hermes_tingle.handle_tool(None, {
            "action": "add",
            "title": "Pay the rent",
            "due_at": "2026-12-01T09:00:00",
            "recur_text": "monthly",
        }))
        assert result["success"] is True
        item = result["item"]
        assert item["recur_interval"] == 1
        assert item["recur_unit"] == "month"

    def test_add_with_conflicting_sources_refused(self, isolated_db):
        result = json.loads(hermes_tingle.handle_tool(None, {
            "action": "add",
            "title": "Pay the rent",
            "due_at": "2026-12-01T09:00:00",
            "recur_text": "monthly",
            "recur_interval": 7,
            "recur_unit": "day",
        }))
        assert result["success"] is False

    def test_add_with_unparseable_text_refused(self, isolated_db):
        result = json.loads(hermes_tingle.handle_tool(None, {
            "action": "add",
            "title": "Pay the rent",
            "due_at": "2026-12-01T09:00:00",
            "recur_text": "whenever",
        }))
        assert result["success"] is False
        assert "recurrence" in result["error"]

    def test_update_with_recur_text(self, isolated_db):
        created = json.loads(hermes_tingle.handle_tool(None, {
            "action": "add", "title": "Water plants", "due_at": "2026-11-20T09:00:00",
        }))
        updated = json.loads(hermes_tingle.handle_tool(None, {
            "action": "update",
            "item_id": created["item"]["id"],
            "recur_text": "every 3 days",
        }))
        assert updated["item"]["recur_interval"] == 3
        assert updated["item"]["recur_unit"] == "day"

    def test_weekly_series_keeps_the_weekday(self, isolated_store):
        item = isolated_store.add_item(
            title="Bin day", due_at="2026-11-16T07:00:00",  # a Monday
            recur_interval=7, recur_unit="day")
        completed = isolated_store.complete_item(item["id"], now="2026-11-16T08:00:00")
        assert completed["due_at"].startswith("2026-11-23")
