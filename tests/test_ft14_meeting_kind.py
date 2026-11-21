"""FT-14: meeting kind = attendee-scoped context over the calendar collector.

Reconciliation: meeting EVENTS (time, title, own RSVP) are the calendar
kind and were always collected. The meeting kind adds WHO ELSE is in the
meeting - gated behind its own explicit opt-in, scoped to an attendee count
plus organizer identity, never a full list of other people's details. Without
the opt-in the fetch itself asks the provider for self-attendee fields only.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

import hermes_tingle_sources as sources
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
    store.enable_source("calendar", enabled_by="synthetic fixture consent", calendar_context=False)
    return store


def _event(attendees=None, organizer=None):
    event = {
        "id": "evt1",
        "summary": "Weekly sync",
        "status": "confirmed",
        "updated": "2026-11-16T09:00:00Z",
        "start": {"dateTime": "2026-11-17T10:00:00+01:00"},
        "end": {"dateTime": "2026-11-17T10:30:00+01:00"},
    }
    if attendees is not None:
        event["attendees"] = attendees
    if organizer is not None:
        event["organizer"] = organizer
    return event


_ATTENDEES = [
    {"email": "me@example", "self": True, "responseStatus": "accepted"},
    {"email": "colleague1@example", "responseStatus": "accepted"},
    {"email": "colleague2@example", "responseStatus": "tentative"},
]


class TestNormalizeMeetingGate:
    def test_without_opt_in_no_attendee_data(self):
        item = sources.normalize_calendar_event(_event(attendees=_ATTENDEES), "Work", "cal1")
        assert item is not None
        assert "meeting" not in item
        blob = str(item)
        assert "colleague1" not in blob and "colleague2" not in blob

    def test_with_opt_in_count_and_organizer_only(self):
        item = sources.normalize_calendar_event(
            _event(attendees=_ATTENDEES, organizer={"displayName": "Organizer Person"}),
            "Work", "cal1", meeting_opt_in=True)
        assert item["meeting"] == {"attendee_count": 2, "organizer": "Organizer Person"}
        assert "colleague1" not in str(item) and "colleague2" not in str(item)

    def test_with_opt_in_solo_event_has_no_meeting_block(self):
        item = sources.normalize_calendar_event(
            _event(attendees=[{"email": "me@example", "self": True}]),
            "Work", "cal1", meeting_opt_in=True)
        assert "meeting" not in item

    def test_default_is_off(self):
        item = sources.normalize_calendar_event(_event(attendees=_ATTENDEES), "Work", "cal1")
        assert "meeting" not in item  # default parameter is the fail-closed path


class TestFetchFieldsFollowConsent:
    def _captured_fields(self, monkeypatch, meeting_enabled):
        captured = {}

        def fake_bridge(service, *args, **kwargs):
            params = __import__("json").loads(args[-1])
            captured["fields"] = params["fields"]
            return {"items": []}

        monkeypatch.setattr(sources, "_gws_bridge", fake_bridge)
        if meeting_enabled is None:
            monkeypatch.setattr(store, "calendar_context_enabled", lambda: False)
            sources._read_calendar_events(
                {"id": "cal1", "summary": "Work"}, datetime.now(timezone.utc), 7)
        else:
            sources._read_calendar_events(
                {"id": "cal1", "summary": "Work"}, datetime.now(timezone.utc), 7,
                meeting_opt_in=meeting_enabled)
        return captured["fields"]

    def test_default_fetch_requests_self_attendee_only(self, monkeypatch):
        fields = self._captured_fields(monkeypatch, None)
        assert "attendees(self,responseStatus)" in fields
        assert "organizer" not in fields

    def test_opted_in_fetch_requests_attendees_and_organizer(self, monkeypatch):
        fields = self._captured_fields(monkeypatch, True)
        assert "attendees," in fields or fields.endswith("attendees),nextPageToken") or "attendees,organizer" in fields
        assert "organizer" in fields

    def test_explicit_off_keeps_self_only(self, monkeypatch):
        fields = self._captured_fields(monkeypatch, False)
        assert "attendees(self,responseStatus)" in fields
        assert "organizer" not in fields

    def test_live_consent_read_when_not_overridden(self, isolated_store, monkeypatch):
        captured = {}

        def fake_bridge(service, *args, **kwargs):
            params = __import__("json").loads(args[-1])
            captured["fields"] = params["fields"]
            return {"items": []}

        monkeypatch.setattr(sources, "_gws_bridge", fake_bridge)
        store.enable_source("calendar", enabled_by="test", calendar_context=True)
        sources._read_calendar_events(
            {"id": "cal1", "summary": "Work"}, datetime.now(timezone.utc), 7)
        assert "organizer" in captured["fields"]
