from delivery_fakes import mock_pending_reader
from delivery_fakes import confirmed_reminder, review_current_source
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import hermes_tingle_sources as sources


@pytest.fixture(autouse=True)
def isolated_plugin_database(tmp_path, monkeypatch):
    db_path = tmp_path / "tingle-sources.db"

    def connect(_name: str):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(sources.store, "plugin_db", connect)
    sources.store.enable_source("email", enabled_by="synthetic fixture consent")
    sources.store.enable_source("calendar", enabled_by="synthetic fixture consent")
    import json
    sources.store.set_plugin_meta('calendar_account_binding',json.dumps({'generation':sources.store.source_generation('calendar'),'account':'c'*64}))


@pytest.mark.parametrize("requested, expected", [(-9, 1), (1, 1), (30, 30), (999, 30)])
def test_snapshot_clamps_provider_windows_before_reading(monkeypatch, requested, expected):
    from datetime import datetime, timezone

    seen = {}
    monkeypatch.setattr(sources, "_collect_email_candidates", lambda **kw: seen.update(email=kw["lookback_days"]) or {"emails": [], "errors": []})
    monkeypatch.setattr(sources, "_read_email_completion_signals", lambda: ([], []))
    monkeypatch.setattr(sources, "_read_email_reply_checks", lambda loops: ([], []))
    monkeypatch.setattr(sources, "_read_calendars", lambda: [{"id": "one", "summary": "Personal"}])
    monkeypatch.setattr(sources, "_read_calendar_events", lambda calendar, now, days: seen.update(calendar=days) or [])
    snapshot = sources.collect_snapshot(now=datetime(2026, 11, 15, tzinfo=timezone.utc), lookback_days=requested, upcoming_days=requested)
    assert seen == {"email": expected, "calendar": expected}
    assert snapshot["lookback_days"] == snapshot["upcoming_days"] == expected


def test_snapshot_finds_unreviewed_calendar_event_after_reviewed_pages(monkeypatch):
    import json
    from datetime import datetime, timezone

    monkeypatch.setattr(sources, "_collect_email_candidates", lambda **kw: {"emails": [], "errors": []})
    monkeypatch.setattr(sources, "_read_email_completion_signals", lambda: ([], []))
    monkeypatch.setattr(sources, "_read_email_reply_checks", lambda loops: ([], []))
    monkeypatch.setattr(sources, "_read_calendars", lambda: [{"id": "one", "summary": "Personal"}])
    for i in range(100):
        review_current_source(sources.store, "calendar", f"one:old-{i}", "v1")
    calls = []
    def bridge(*args):
        params = json.loads(args[-1])
        calls.append(params.get("pageToken"))
        if len(calls) == 1:
            return {"items": [{"id": f"old-{i}", "updated": "v1"} for i in range(100)], "nextPageToken": "page2"}
        return {"items": [{"id": "new", "updated": "v1", "summary": "Action"}]}
    monkeypatch.setattr(sources, "_gws_bridge", bridge)
    snapshot = sources.collect_snapshot(now=datetime(2026, 11, 15, tzinfo=timezone.utc))
    assert calls == [None, "page2"]
    assert [event["source_id"] for event in snapshot["calendar_events"]] == ["one:new"]
    assert not snapshot["errors"]


def test_snapshot_reports_calendar_page_cap_incomplete(monkeypatch):
    import json
    from datetime import datetime, timezone

    monkeypatch.setattr(sources, "MAX_CALENDAR_EVENT_PAGES", 2)
    monkeypatch.setattr(sources, "_collect_email_candidates", lambda **kw: {"emails": [], "errors": []})
    monkeypatch.setattr(sources, "_read_email_completion_signals", lambda: ([], []))
    monkeypatch.setattr(sources, "_read_email_reply_checks", lambda loops: ([], []))
    monkeypatch.setattr(sources, "_read_calendars", lambda: [{"id": "one", "summary": "Personal"}])
    calls = []
    def bridge(*args):
        calls.append(json.loads(args[-1]).get("pageToken"))
        return {"items": [], "nextPageToken": f"token-{len(calls)}"}
    monkeypatch.setattr(sources, "_gws_bridge", bridge)
    snapshot = sources.collect_snapshot(now=datetime(2026, 11, 15, tzinfo=timezone.utc))
    assert calls == [None, "token-1"]
    # Quiet by default: technical detail in diagnostics, structural contract flags it.
    assert any("incomplete" in d.lower() for d in snapshot["diagnostics"])
    assert snapshot["calendar_review"]["review_complete"] is False
    assert "incomplete" in snapshot["calendar_review"]["incomplete_reasons"]
    assert "token-" not in str(snapshot)


@pytest.mark.parametrize("token", [None, "", "bad token", 123])
def test_calendar_event_rejects_malformed_page_token(monkeypatch, token):
    from datetime import datetime, timezone
    monkeypatch.setattr(sources, "_gws_bridge", lambda *args: {"items": [], "nextPageToken": token})
    with pytest.raises(sources.SourceReadError, match="invalid page token"):
        sources._read_calendar_events({"id": "one", "summary": "Personal"}, datetime(2026, 11, 15, tzinfo=timezone.utc), 7)


def test_calendar_event_rejects_repeating_page_token(monkeypatch):
    from datetime import datetime, timezone
    calls = []
    def bridge(*args):
        calls.append(1)
        return {"items": [], "nextPageToken": "repeated"}
    monkeypatch.setattr(sources, "_gws_bridge", bridge)
    with pytest.raises(sources.SourceReadError, match="invalid page token"):
        sources._read_calendar_events({"id": "one", "summary": "Personal"}, datetime(2026, 11, 15, tzinfo=timezone.utc), 7)
    assert len(calls) == 2


def test_calendar_page_bound_warns_on_each_run_without_claiming_tail_coverage(monkeypatch):
    import json
    from datetime import datetime, timezone
    monkeypatch.setattr(sources, "_collect_email_candidates", lambda **kw: {"emails": [], "errors": []})
    monkeypatch.setattr(sources, "_read_email_completion_signals", lambda: ([], []))
    monkeypatch.setattr(sources, "_read_email_reply_checks", lambda loops: ([], []))
    monkeypatch.setattr(sources, "_read_calendars", lambda: [{"id": "one", "summary": "Personal"}])
    calls = []
    def bridge(*args):
        token = json.loads(args[-1]).get("pageToken")
        page = int(token) if token else 0
        calls.append(page)
        return {"items": [], "nextPageToken": str(page + 1)} if page < 10 else {"items": [{"id": "tail"}]}
    monkeypatch.setattr(sources, "_gws_bridge", bridge)
    for _ in range(2):
        snapshot = sources.collect_snapshot(now=datetime(2026, 11, 15, tzinfo=timezone.utc))
        assert snapshot["calendar_events"] == []
        assert any("incomplete" in d.lower() for d in snapshot["diagnostics"])
        assert snapshot["calendar_review"]["review_complete"] is False
    assert calls == list(range(10)) * 2


def test_rearmed_reply_watch_still_requires_thread_verification(monkeypatch):
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 11, 15, tzinfo=timezone.utc)
    item = sources.store.add_item(title="Wait", status="waiting", owner="other", source_type="email",
                                  source_ref="gmail:a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf/thread#sent", completion_signal="source_email_reply_received",
                                  now=now, next_check_at=now + timedelta(days=3))
    for _ in range(4):
        now = datetime.fromisoformat(sources.store.get_item(item["id"])["next_check_at"])
        confirmed_reminder(sources.store, [item["id"]], now=now)
    original_now = sources.store._now
    monkeypatch.setattr(sources.store, "_now", lambda value=None: original_now(value) if value is not None else now)
    target = now + timedelta(days=3)
    sources.store.update_item(item["id"], next_check_at=target)
    due = sources.all_due_items(now=target)
    monkeypatch.setattr(sources, "_gws_bridge", lambda *args: {"id": "wrong", "messages": [{"id": "sent", "labelIds": ["SENT"], "internalDate": "1000"}]})
    checks, errors = sources._read_email_reply_checks(due)
    assert checks == [] and errors
    assert sources.store.get_item(item["id"])["reminder_count"] == 0
    monkeypatch.setattr(sources, "_gws_bridge", lambda *args: {"id": "thread", "messages": [{"id": "sent", "labelIds": ["SENT"], "internalDate": "1000"}]})
    checks, errors = sources._read_email_reply_checks(due)
    assert not errors and checks[0]["state"] == "no_reply"


def test_direct_email_read_requires_profile_config(monkeypatch):
    def missing():
        raise ValueError('Tingle config missing or invalid')

    def forbidden(*args, **kwargs):
        raise AssertionError('email fetched without profile configuration')

    monkeypatch.setattr(sources, 'load_settings', missing)
    monkeypatch.setattr(sources, '_google_api', forbidden)
    with pytest.raises(ValueError, match='config missing'):
        sources.read_email('message-1')


def test_snapshot_requires_profile_config_before_any_source_reads(monkeypatch):
    def missing():
        raise ValueError('Tingle config missing or invalid')

    def forbidden(*args, **kwargs):
        raise AssertionError('source read occurred without profile configuration')

    monkeypatch.setattr(sources, 'load_settings', missing)
    monkeypatch.setattr(sources, '_collect_email_candidates', forbidden)
    monkeypatch.setattr(sources, '_read_email_completion_signals', forbidden)
    monkeypatch.setattr(sources, '_read_calendars', forbidden)
    snapshot = sources.collect_snapshot()
    assert snapshot['emails'] == []
    assert snapshot['calendar_events'] == []
    assert any('config' in error.lower() for error in snapshot['errors'])


@pytest.mark.parametrize("zone, start, end", [
    ("America/New_York", "08:00", "12:00"),
    ("Asia/Tokyo", "13:30", "17:15"),
])
def test_availability_windows_follow_profile_not_explicit_candidates(tmp_path, synthetic_settings, monkeypatch, zone, start, end):
    import json
    from datetime import datetime, timezone
    (tmp_path / "hermes-tingle.json").write_text(json.dumps({**synthetic_settings, "timezone": zone, "availability_start": start, "availability_end": end}))
    monkeypatch.setattr(sources, "_read_calendars", lambda: [{"id": "one", "summary": "Personal"}])
    monkeypatch.setattr(sources, "_gws_bridge", lambda *args: {"items": []})
    result = sources.check_calendar_availability("2026-03-08", ["19:00"], now=datetime(2026, 3, 7, tzinfo=timezone.utc))
    assert result["free_windows"] == [{"start": start, "end": end}]
    assert result["candidate_results"][0]["available"] is True


@pytest.mark.parametrize("day, start", [("2026-03-08", "02:30"), ("2026-11-01", "01:30")])
def test_availability_rejects_dst_ambiguous_window_before_calendar_read(tmp_path, synthetic_settings, monkeypatch, day, start):
    import json
    from datetime import datetime, timedelta, timezone
    (tmp_path / "hermes-tingle.json").write_text(json.dumps({**synthetic_settings, "timezone": "America/New_York", "availability_start": start, "availability_end": "10:00"}))
    monkeypatch.setattr(sources, "_read_calendars", lambda: pytest.fail("must not read calendar"))
    now = datetime.fromisoformat(day).replace(tzinfo=timezone.utc) - timedelta(days=1)
    with pytest.raises(ValueError, match="ambiguous|nonexistent"):
        sources.check_calendar_availability(day, ["19:00"], now=now)


@pytest.mark.parametrize("zone, start, day_start, day_end", [
    ("America/New_York", "10:00", "2026-03-08T00:00:00-05:00", "2026-03-09T00:00:00-04:00"),
    ("Asia/Tokyo", "23:00", "2026-03-08T00:00:00+09:00", "2026-03-09T00:00:00+09:00"),
])
def test_availability_uses_profile_zone_and_event_instant(tmp_path, synthetic_settings, monkeypatch, zone, start, day_start, day_end):
    import json
    from datetime import datetime, timezone
    (tmp_path / "hermes-tingle.json").write_text(json.dumps({**synthetic_settings, "timezone": zone}))
    monkeypatch.setattr(sources, "_read_calendars", lambda: [{"id": "one", "summary": "Personal"}])
    calls = []
    def bridge(*args):
        calls.append(json.loads(args[args.index("--params") + 1]))
        return {"items": [{"start": {"dateTime": "2026-03-08T14:00:00Z"}, "end": {"dateTime": "2026-03-08T14:30:00Z"}}]}
    monkeypatch.setattr(sources, "_gws_bridge", bridge)
    result = sources.check_calendar_availability("2026-03-08", [start], now=datetime(2026, 3, 7, 12, tzinfo=timezone.utc))
    assert result["timezone"] == zone
    assert result["candidate_results"][0]["available"] is False
    assert calls[0]["timeMin"] == day_start
    assert calls[0]["timeMax"] == day_end


def test_missing_config_never_reads_calendar_for_availability(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    (tmp_path / "hermes-tingle.json").unlink()
    calls = []
    monkeypatch.setattr(sources, "_gws_bridge", lambda *args: calls.append(args))
    with pytest.raises((ValueError,sources.SourceReadError), match="config"):
        sources.check_calendar_availability("2026-03-08", ["10:00"], now=datetime(2026, 3, 7, tzinfo=timezone.utc))
    assert calls == []


def test_availability_counts_elapsed_duration_over_dst_gap(tmp_path, synthetic_settings, monkeypatch):
    import json
    from datetime import datetime, timezone
    (tmp_path / "hermes-tingle.json").write_text(json.dumps({**synthetic_settings, "timezone": "America/New_York"}))
    monkeypatch.setattr(sources, "_read_calendars", lambda: [{"id": "one", "summary": "Personal"}])
    monkeypatch.setattr(sources, "_gws_bridge", lambda *args: {"items": [
        {"start": {"dateTime": "2026-03-08T07:10:00Z"}, "end": {"dateTime": "2026-03-08T07:20:00Z"}}
    ]})
    result = sources.check_calendar_availability("2026-03-08", ["01:30"], 60, now=datetime(2026, 3, 7, tzinfo=timezone.utc))
    assert result["candidate_results"][0]["end"] == "03:30"
    assert result["candidate_results"][0]["available"] is False


@pytest.mark.parametrize("day, start", [("2026-03-08", "02:30"), ("2026-11-01", "01:30")])
def test_availability_rejects_nonexistent_or_ambiguous_local_start(tmp_path, synthetic_settings, day, start):
    import json
    from datetime import datetime, timezone
    (tmp_path / "hermes-tingle.json").write_text(json.dumps({**synthetic_settings, "timezone": "America/New_York"}))
    now = datetime.fromisoformat(day).replace(tzinfo=timezone.utc) - __import__("datetime").timedelta(days=1)
    with pytest.raises(ValueError, match="ambiguous|nonexistent"):
        sources.check_calendar_availability(day, [start], now=now)


def test_ambiguous_offsetless_event_keeps_availability_incomplete(tmp_path, synthetic_settings, monkeypatch):
    import json
    from datetime import datetime, timezone
    (tmp_path / "hermes-tingle.json").write_text(json.dumps({**synthetic_settings, "timezone": "America/New_York"}))
    monkeypatch.setattr(sources, "_read_calendars", lambda: [{"id": "one", "summary": "Personal"}])
    monkeypatch.setattr(sources, "_gws_bridge", lambda *args: {"items": [
        {"start": {"dateTime": "2026-11-01T01:15:00"}, "end": {"dateTime": "2026-11-01T02:15:00"}}
    ]})
    result = sources.check_calendar_availability("2026-11-01", ["03:00"], now=datetime(2026, 10, 31, tzinfo=timezone.utc))
    assert result["coverage_complete"] is False
    assert result["candidate_results"][0]["available"] is None


def test_check_calendar_availability_checks_candidates_and_returns_private_free_windows(monkeypatch):
    from datetime import datetime, timezone
    import json

    monkeypatch.setattr(sources, "_read_calendars", lambda: [
        {"id": "primary", "summary": "Personal", "workstream": "other"},
        {"id": "work", "summary": "Work Alpha", "workstream": "Work Alpha"},
    ])
    events = {
        "primary": [{
            "id": "private-1", "summary": "Private appointment", "status": "confirmed",
            "start": {"dateTime": "2026-11-17T17:00:00+01:00"},
            "end": {"dateTime": "2026-11-17T18:30:00+01:00"},
        }],
        "work": [
            {"id": "work-1", "summary": "Confidential work event", "status": "confirmed",
             "start": {"dateTime": "2026-11-17T16:30:00+01:00"},
             "end": {"dateTime": "2026-11-17T16:40:00+01:00"}},
            {"id": "work-2", "summary": "Confidential work event", "status": "confirmed",
             "start": {"dateTime": "2026-11-17T17:00:00+01:00"},
             "end": {"dateTime": "2026-11-17T18:30:00+01:00"}},
        ],
    }

    def fake_bridge(*args):
        params = json.loads(args[args.index("--params") + 1])
        return {"items": events[params["calendarId"]]}

    monkeypatch.setattr(sources, "_gws_bridge", fake_bridge)
    result = sources.check_calendar_availability(
        "2026-11-17", ["16:00", "18:00"], 30,
        now=datetime(2026, 11, 15, 1, 0, tzinfo=timezone.utc),
    )

    assert result["coverage_complete"] is True
    assert [row["available"] for row in result["candidate_results"]] == [False, True]
    assert result["candidate_results"][0]["conflicts"] == [{"start": "16:00", "end": "17:30", "state": "busy"}]
    assert {"start": "17:30", "end": "20:00"} in result["free_windows"]
    assert "Private appointment" not in json.dumps(result)
    assert "Confidential work event" not in json.dumps(result)


def test_availability_keeps_tentative_as_possible_but_ignores_declined_and_birthday_calendars(monkeypatch):
    from datetime import datetime, timezone
    import json

    monkeypatch.setattr(sources, "_read_calendars", lambda: [
        {"id": "personal", "summary": "Personal", "workstream": "other"},
        {"id": "birthdays", "summary": "Birthdays (Manual)", "workstream": "other"},
    ])
    events = {
        "personal": [
            {"id": "tentative", "summary": "Tentative", "status": "tentative",
             "start": {"dateTime": "2026-11-17T15:00:00+01:00"},
             "end": {"dateTime": "2026-11-17T16:00:00+01:00"}},
            {"id": "cancelled", "status": "cancelled",
             "start": {"dateTime": "2026-11-17T17:00:00+01:00"},
             "end": {"dateTime": "2026-11-17T18:00:00+01:00"}},
            {"id": "free", "status": "confirmed", "transparency": "transparent",
             "start": {"dateTime": "2026-11-17T17:00:00+01:00"},
             "end": {"dateTime": "2026-11-17T18:00:00+01:00"}},
            {"id": "declined", "status": "confirmed", "attendees": [{"self": True, "responseStatus": "declined"}],
             "start": {"dateTime": "2026-11-17T18:00:00+01:00"},
             "end": {"dateTime": "2026-11-17T19:00:00+01:00"}},
        ],
        "birthdays": [{"id": "birthday", "summary": "Birthday", "status": "confirmed",
                       "start": {"date": "2026-11-17"}, "end": {"date": "2026-11-18"}}],
    }

    def fake_bridge(*args):
        params = json.loads(args[args.index("--params") + 1])
        return {"items": events[params["calendarId"]]}

    monkeypatch.setattr(sources, "_gws_bridge", fake_bridge)
    result = sources.check_calendar_availability(
        "2026-11-17", ["14:00", "17:00"], 30,
        now=datetime(2026, 11, 15, 1, 0, tzinfo=timezone.utc),
    )

    assert result["candidate_results"][0]["available"] is False
    assert result["candidate_results"][0]["conflicts"] == [{"start": "14:00", "end": "15:00", "state": "possible"}]
    assert result["candidate_results"][1]["available"] is True
    assert result["all_day_uncertain"] is False


def test_calendar_list_paginates(monkeypatch):
    import json

    calls = []
    pages = [
        {"items": [{"id": "personal", "summary": "Personal"}], "nextPageToken": "page-2"},
        {"items": [{"id": "work", "summary": "Work Alpha"}]},
    ]

    def fake_bridge(*args):
        params = json.loads(args[args.index("--params") + 1])
        calls.append(params)
        return pages[len(calls) - 1]

    monkeypatch.setattr(sources, "_gws_bridge", fake_bridge)

    calendars = sources._read_calendars()

    assert [calendar["id"] for calendar in calendars] == ["personal", "work"]
    assert calls[1]["pageToken"] == "page-2"


def test_calendar_scope_uses_explicit_exclusions_and_workstream():
    calendars = [
        {"id": "work-id", "summary": "Work Alpha"},
        {"id": "home-id", "summary": "Excluded Calendar"},
        {"id": "personal-id", "summary": "Personal"},
    ]

    selected = sources.select_calendars(calendars)

    assert [item["id"] for item in selected] == ["work-id", "personal-id"]
    assert selected[0]["workstream"] == "Work Alpha"
    assert selected[1]["workstream"] == "other"


@pytest.mark.parametrize("summary,expected", [
    ("Work Beta lab", "Work Beta"),
    ("lab planning", "Work Alpha"),
])
def test_calendar_alias_precedence_preserves_config_order(summary, expected):
    settings = {
        "calendar_exclude_substrings": [],
        "workstream_exclude_values": ["Work Alpha", "Work Beta"],
        "calendar_workstream_aliases": {" LAB ": "Work Alpha", "lab": "Work Beta"},
    }
    selected = sources.select_calendars([{"id": "work", "summary": summary}], settings)
    assert selected[0]["workstream"] == expected


@pytest.mark.parametrize("summary", ["ZENITH planning", "lab scheduling"])
def test_calendar_alias_maps_to_excluded_workstream_before_snapshot_read(tmp_path, synthetic_settings, monkeypatch, summary):
    import json
    from datetime import datetime, timezone

    settings = {**synthetic_settings, "workstream_exclude_values": ["Dept Zenith"],
                "calendar_workstream_aliases": {"zenith": "Dept Zenith", "lab": "Dept Zenith"}}
    (tmp_path / "hermes-tingle.json").write_text(json.dumps(settings))
    selected = sources.select_calendars([{"id": "work", "summary": summary}], settings)
    assert selected[0]["workstream"] == "Dept Zenith"
    monkeypatch.setattr(sources, "_read_calendars", lambda: selected)
    monkeypatch.setattr(sources, "_read_calendar_events", lambda *args: pytest.fail("excluded calendar read"))
    monkeypatch.setattr(sources, "_collect_email_candidates", lambda **kwargs: {"emails": [], "errors": []})
    monkeypatch.setattr(sources, "_read_email_completion_signals", lambda: ([], []))
    monkeypatch.setattr(sources, "_read_email_reply_checks", lambda due_items: ([], []))
    snapshot = sources.collect_snapshot(now=datetime(2026, 11, 15, tzinfo=timezone.utc))
    assert snapshot["calendar_events"] == []


@pytest.mark.parametrize("summary", ["Work Alpha", "TEAM work alpha / planning", "work alpha plus"])
def test_calendar_workstream_matches_configured_substring(summary):
    settings = {"calendar_exclude_substrings": [], "workstream_exclude_values": ["  Work Alpha  "]}
    selected = sources.select_calendars([{"id": "synthetic-work", "summary": summary}], settings)
    assert selected[0]["workstream"] == "Work Alpha"


def test_calendar_event_filter_drops_cancelled_and_declined_but_keeps_pending():
    base = {
        "id": "event-1",
        "summary": "Review draft",
        "updated": "2026-11-14T10:00:00Z",
        "status": "confirmed",
        "start": {"dateTime": "2026-11-15T10:00:00+01:00"},
        "end": {"dateTime": "2026-11-15T11:00:00+01:00"},
        "attendees": [{"self": True, "responseStatus": "needsAction"}],
    }

    pending = sources.normalize_calendar_event(base, "Personal")
    declined = sources.normalize_calendar_event({**base, "attendees": [{"self": True, "responseStatus": "declined"}]}, "Personal")
    cancelled = sources.normalize_calendar_event({**base, "status": "cancelled"}, "Personal")

    assert pending["unconfirmed"] is True
    assert pending["source_type"] == "calendar"
    assert declined is None
    assert cancelled is None


def test_work_calendar_event_preserves_configured_workstream(monkeypatch):
    from datetime import datetime, timezone

    event = {
        "id": "event-work", "summary": "Prepare slides", "status": "confirmed",
        "updated": "2026-11-14T10:00:00Z",
    }
    monkeypatch.setattr(sources, "_gws_bridge", lambda *args: {"items": [event]})
    calendar = sources.select_calendars([{"id": "work-id", "summary": "wOrK AlPhA"}])[0]
    candidates = sources._read_calendar_events(calendar, datetime(2026, 11, 15, tzinfo=timezone.utc), 7)

    assert candidates[0]["workstream"] == "Work Alpha"
    assert candidates[0]["source_id"] == "work-id:event-work"
    assert candidates[0]["untrusted_source"] is True


def test_snapshot_never_exposes_configured_work_events_as_candidates(monkeypatch):
    import json
    from datetime import datetime, timezone

    monkeypatch.setattr(sources, "_collect_email_candidates", lambda **kwargs: {"emails": [], "errors": []})
    monkeypatch.setattr(sources, "_read_email_completion_signals", lambda: ([], []))
    monkeypatch.setattr(sources, "_read_email_reply_checks", lambda due_items: ([], []))
    monkeypatch.setattr(sources, "_read_calendars", lambda: sources.select_calendars([
        {"id": "private-work", "summary": "Team WORK ALPHA planning"},
        {"id": "personal", "summary": "Personal"},
    ]))
    calls = []

    def bridge(*args):
        calendar_id = json.loads(args[args.index("--params") + 1])["calendarId"]
        calls.append(calendar_id)
        return {"items": [{"id": "event", "summary": "Prepare slides", "updated": "v1"}]}

    monkeypatch.setattr(sources, "_gws_bridge", bridge)
    snapshot = sources.collect_snapshot(now=datetime(2026, 11, 15, tzinfo=timezone.utc))
    assert calls == ["personal"]
    assert [event["source_id"] for event in snapshot["calendar_events"]] == ["personal:event"]


def test_alias_matched_work_calendar_still_blocks_availability(tmp_path, synthetic_settings, monkeypatch):
    import json
    from datetime import datetime, timezone

    settings = {**synthetic_settings, "calendar_workstream_aliases": {"lab": "Work Alpha"}}
    (tmp_path / "hermes-tingle.json").write_text(json.dumps(settings))
    calendars = sources.select_calendars([{"id": "synthetic-work", "summary": "Lab planning"}])
    assert calendars[0]["workstream"] == "Work Alpha"
    monkeypatch.setattr(sources, "_read_calendars", lambda: calendars)
    monkeypatch.setattr(sources, "_gws_bridge", lambda *args: {"items": [{
        "id": "synthetic-event", "status": "confirmed",
        "start": {"dateTime": "2026-11-17T10:00:00Z"},
        "end": {"dateTime": "2026-11-17T11:00:00Z"},
    }]})
    result = sources.check_calendar_availability(
        "2026-11-17", ["10:00"], 30, now=datetime(2026, 11, 15, tzinfo=timezone.utc),
    )
    assert result["candidate_results"][0]["available"] is False


def test_substring_matched_work_calendar_still_blocks_availability(monkeypatch):
    from datetime import datetime, timezone

    monkeypatch.setattr(sources, "_read_calendars", lambda: sources.select_calendars([
        {"id": "synthetic-work", "summary": "Team WORK ALPHA planning"},
    ]))
    monkeypatch.setattr(sources, "_gws_bridge", lambda *args: {"items": [{
        "id": "synthetic-event", "status": "confirmed",
        "start": {"dateTime": "2026-11-17T10:00:00Z"},
        "end": {"dateTime": "2026-11-17T11:00:00Z"},
    }]})
    result = sources.check_calendar_availability(
        "2026-11-17", ["10:00"], 30, now=datetime(2026, 11, 15, tzinfo=timezone.utc),
    )
    assert result["candidate_results"][0]["available"] is False


def test_snapshot_filters_reviewed_sources_and_includes_due_loops(monkeypatch):
    emails = [
        {"source_type": "email", "source_id": "new", "source_version": "new", "thread_id": "thread-1", "snippet": "Please reply"},
    ]
    calendars = [{"id": "club-id", "summary": "Club", "workstream": "other"}]
    events = [{"source_type": "calendar", "source_id": "club-id:event-1", "source_version": "v1", "workstream": "other"}]
    email_sync = {"emails": emails, "errors": [], "sync_mode": "history", "pending_count": 0}
    monkeypatch.setattr(sources, "_collect_email_candidates", lambda **kwargs: email_sync)
    monkeypatch.setattr(sources, "_read_calendars", lambda: calendars)
    monkeypatch.setattr(sources, "_read_calendar_events", lambda calendar, now, days: events)
    monkeypatch.setattr(sources, "_read_email_completion_signals", lambda: ([], []))
    monkeypatch.setattr(sources, "_read_email_reply_checks", lambda due_items: ([], []))
    monkeypatch.setattr(sources.store, "list_items", lambda **kwargs: [])
    monkeypatch.setattr(sources, "all_due_items", lambda now: [{"id": "HT-ABC123"}])
    approved_rules = [{"id": "FB-ABC123", "category": "links", "rule_text": "Read eligible email bodies for relevant links."}]
    monkeypatch.setattr(sources.store, "approved_feedback_rules", lambda: approved_rules)

    snapshot = sources.collect_snapshot()

    assert [item["source_id"] for item in snapshot["emails"]] == ["new"]
    assert [item["source_id"] for item in snapshot["calendar_events"]] == ["club-id:event-1"]
    assert snapshot["due_items"] == [{"id": "HT-ABC123"}]
    assert snapshot["email_sync"] == {"sync_mode": "history", "pending_count": 0, "emails": emails, "errors": []}
    assert snapshot["approved_feedback_rules"] == approved_rules


def test_recent_email_scan_covers_all_mail_and_exposes_thread_direction(monkeypatch):
    captured = {}
    message = {
        "id": "message-1",
        "threadId": "thread-1",
        "from": "Sender <sender@example.invalid>",
        "to": "Recipient <recipient@example.invalid>",
        "subject": "Question",
        "date": "2026-11-14",
        "snippet": "Can you clarify?",
        "labels": ["SENT"],
    }

    def fake_api(*args):
        captured["args"] = args
        return [message]

    monkeypatch.setattr(sources, "_google_api", fake_api)
    items = sources._read_recent_email(14)
    query = captured["args"][2]

    assert "in:anywhere" in query
    assert "-in:trash" in query and "-in:spam" in query and "-in:drafts" in query
    assert "in:inbox" not in query
    assert captured["args"][-1] == "100"
    assert items[0]["thread_id"] == "thread-1"
    assert items[0]["direction"] == "sent"
    assert items[0]["source_ref"] == "gmail:thread-1"


def test_email_completion_signal_requires_every_message_in_thread_in_trash(monkeypatch):
    items = [
        {"id": "HT-AAA111", "source_type": "email", "source_ref": "gmail:a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf/thread-1", "completion_signal": "source_email_trashed"},
        {"id": "HT-BBB222", "source_type": "email", "source_ref": "gmail:a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf/thread-2", "completion_signal": ""},
    ]
    monkeypatch.setattr(sources.store, "list_items", lambda **kwargs: items)
    calls = []

    def fake_bridge(*args):
        params = args[args.index("--params") + 1]
        calls.append(params)
        return {"id": "thread-1", "messages": [
            {"id": "m-1", "labelIds": ["TRASH"]},
            {"id": "m-2", "labelIds": ["TRASH"]},
        ]}

    monkeypatch.setattr(sources, "_gws_bridge", fake_bridge)
    signals, errors = sources._read_email_completion_signals()

    assert errors == []
    assert signals == [{"item_id": "HT-AAA111", "thread_id": "thread-1", "state": "all_messages_trashed"}]
    assert len(calls) == 1
    assert "payload" not in calls[0]


@pytest.mark.parametrize('returned_id', ['wrong-thread', None])
def test_trash_completion_requires_correct_thread_id(monkeypatch, returned_id):
    monkeypatch.setattr(sources.store, 'list_items', lambda **kwargs: [
        {'id': 'HT-AAA111', 'source_type': 'email', 'source_ref': 'gmail:a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf/thread-1',
         'completion_signal': 'source_email_trashed'}])
    monkeypatch.setattr(sources, '_gws_bridge', lambda *args: {
        'id': returned_id, 'messages': [{'id': 'message-1', 'labelIds': ['TRASH']}]})
    signals, errors = sources._read_email_completion_signals()
    assert signals == []
    assert errors


def test_partial_thread_trash_does_not_emit_completion_signal(monkeypatch):
    monkeypatch.setattr(sources.store, "list_items", lambda **kwargs: [
        {"id": "HT-AAA111", "source_type": "email", "source_ref": "gmail:a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf/thread-1", "completion_signal": "source_email_trashed"},
    ])
    monkeypatch.setattr(sources, "_gws_bridge", lambda *args: {"id": "thread-1", "messages": [
        {"id": "m-1", "labelIds": ["TRASH"]},
        {"id": "m-2", "labelIds": ["INBOX"]},
    ]})

    signals, errors = sources._read_email_completion_signals()

    assert signals == []
    assert errors == []


def test_due_email_loop_gets_recent_reviewed_thread_context(monkeypatch):
    message = {
        "source_type": "email", "source_id": "m-1", "source_version": "m-1",
        "thread_id": "thread-1", "direction": "sent", "snippet": "Can you clarify?",
        "source_ref": "gmail:a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf/thread-1",
    }
    email_sync = {"emails": [message], "errors": [], "sync_mode": "history", "pending_count": 0}
    monkeypatch.setattr(sources, "_collect_email_candidates", lambda **kwargs: email_sync)
    monkeypatch.setattr(sources, "_read_calendars", lambda: [{"id": "personal", "summary": "Personal"}])
    monkeypatch.setattr(sources, "_read_calendar_events", lambda calendar, now, days: [])
    monkeypatch.setattr(sources, "_read_email_completion_signals", lambda: ([], []))
    monkeypatch.setattr(sources, "_read_email_reply_checks", lambda due_items: ([], []))
    tracked = {
        "id": "HT-ABC123", "source_type": "email", "source_ref": "gmail:a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf/thread-1",
        "title": "Waiting on a colleague", "summary": "Document request", "status": "waiting",
        "completion_signal": "",
    }
    monkeypatch.setattr(sources.store, "list_items", lambda **kwargs: [tracked])
    monkeypatch.setattr(sources.store, "due_items", lambda **kwargs: [tracked])

    snapshot = sources.collect_snapshot()

    assert snapshot["emails"] == [message]
    assert snapshot["tracked_email_context"] == [{
        "item_id": "HT-ABC123", "title": "Waiting on a colleague", "summary": "Document request",
        "status": "waiting", "completion_signal": "", "thread_id": "thread-1", "messages": [message],
    }]


def test_snapshot_reports_when_recent_all_mail_bound_is_reached(monkeypatch):
    from datetime import datetime, timezone

    message = {"source_type": "email", "source_id": "m-1", "source_version": "m-1"}
    monkeypatch.setattr(sources, "MAX_GMAIL_HISTORY_PAGES", 1)
    monkeypatch.setattr(sources, "_read_gmail_profile_history_id", lambda: "101")
    monkeypatch.setattr(sources, "_gws_bridge", lambda *args: {"messages": [{"id": "m-1"}], "nextPageToken": "more"})
    mock_pending_reader(monkeypatch, lambda mid, **kwargs: message)

    result = sources._collect_email_candidates(
        now=datetime(2026, 11, 15, 12, tzinfo=timezone.utc), lookback_days=14,
    )

    assert any("page limit" in error and "incomplete" in error for error in result["errors"])
    assert result["sync_mode"] == "bootstrap"
    assert [item["source_id"] for item in result["emails"]] == ["m-1"]


def test_availability_paginates_event_pages(monkeypatch):
    from datetime import datetime, timezone
    import json

    monkeypatch.setattr(sources, "_read_calendars", lambda: [{"id": "personal", "summary": "Personal"}])
    calls = []
    pages = [
        {"items": [{"id": "first", "status": "confirmed", "start": {"dateTime": "2026-11-17T17:00:00+01:00"}, "end": {"dateTime": "2026-11-17T18:30:00+01:00"}}], "nextPageToken": "events-2"},
        {"items": [{"id": "second", "status": "confirmed", "start": {"dateTime": "2026-11-17T18:30:00+01:00"}, "end": {"dateTime": "2026-11-17T19:00:00+01:00"}}]},
    ]

    def fake_bridge(*args):
        params = json.loads(args[args.index("--params") + 1])
        calls.append(params)
        return pages[len(calls) - 1]

    monkeypatch.setattr(sources, "_gws_bridge", fake_bridge)
    result = sources.check_calendar_availability(
        "2026-11-17", ["17:30"], 30,
        now=datetime(2026, 11, 15, 1, 0, tzinfo=timezone.utc),
    )

    assert result["coverage_complete"] is True
    assert result["candidate_results"][0]["available"] is False
    assert result["candidate_results"][0]["conflicts"] == [{"start": "16:00", "end": "18:00", "state": "busy"}]
    assert calls[1]["pageToken"] == "events-2"


def test_availability_rejects_out_of_window_dates_and_too_many_candidates():
    from datetime import datetime, timezone

    now = datetime(2026, 11, 15, 1, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="7 days"):
        sources.check_calendar_availability("2026-12-16", [], 30, now=now)
    with pytest.raises(ValueError, match="at most"):
        sources.check_calendar_availability("2026-11-17", ["09:00"] * 11, 30, now=now)


def test_availability_reports_incomplete_when_calendar_bound_is_exceeded(monkeypatch):
    from datetime import datetime, timezone

    monkeypatch.setattr(sources, "MAX_CALENDARS", 1)
    monkeypatch.setattr(sources, "_read_calendars", lambda: [
        {"id": "primary", "summary": "Personal"},
        {"id": "work", "summary": "Work Alpha"},
    ])
    monkeypatch.setattr(sources, "_gws_bridge", lambda *args: {"items": []})

    result = sources.check_calendar_availability(
        "2026-11-17", ["19:00"], 30,
        now=datetime(2026, 11, 15, 1, 0, tzinfo=timezone.utc),
    )

    assert result["coverage_complete"] is False
    assert result["candidate_results"][0]["available"] is None
    assert result["free_windows"] == []


def test_availability_reports_incomplete_when_event_bound_is_exceeded(monkeypatch):
    from datetime import datetime, timezone

    monkeypatch.setattr(sources, "MAX_EVENTS", 1)
    monkeypatch.setattr(sources, "_read_calendars", lambda: [{"id": "personal", "summary": "Personal"}])
    monkeypatch.setattr(sources, "_gws_bridge", lambda *args: {"items": [
        {"id": "event-1", "status": "confirmed", "start": {"dateTime": "2026-11-17T10:00:00+01:00"}, "end": {"dateTime": "2026-11-17T11:00:00+01:00"}},
        {"id": "event-2", "status": "confirmed", "start": {"dateTime": "2026-11-17T12:00:00+01:00"}, "end": {"dateTime": "2026-11-17T13:00:00+01:00"}},
    ]})

    result = sources.check_calendar_availability(
        "2026-11-17", ["19:00"], 30,
        now=datetime(2026, 11, 15, 1, 0, tzinfo=timezone.utc),
    )

    assert result["coverage_complete"] is False
    assert result["candidate_results"][0]["available"] is None
    assert result["free_windows"] == []


def test_ambiguous_all_day_commitment_does_not_claim_candidate_is_free(monkeypatch):
    from datetime import datetime, timezone
    import json

    monkeypatch.setattr(sources, "_read_calendars", lambda: [{"id": "primary", "summary": "Personal"}])
    monkeypatch.setattr(sources, "_gws_bridge", lambda *args: {"items": [
        {"id": "all-day", "summary": "Workshop", "status": "confirmed",
         "start": {"date": "2026-11-17"}, "end": {"date": "2026-11-18"}},
    ]})

    result = sources.check_calendar_availability(
        "2026-11-17", ["19:00"], 30,
        now=datetime(2026, 11, 15, 1, 0, tzinfo=timezone.utc),
    )

    assert result["all_day_uncertain"] is True
    assert result["candidate_results"][0]["available"] is None
    assert "Workshop" not in json.dumps(result)


def test_calendar_policy_missing_fails_before_calendar_provider_access(tmp_path, monkeypatch):
    (tmp_path / "hermes-tingle.json").unlink()
    monkeypatch.setattr(sources, "_gws_bridge", lambda *args: pytest.fail("provider must not be called"))
    with pytest.raises(sources.SourceReadError, match="config"):
        sources._read_calendars()


def test_snapshot_missing_policy_reports_incomplete_without_calendar_reads(tmp_path, monkeypatch):
    (tmp_path / "hermes-tingle.json").unlink()
    monkeypatch.setattr(sources, "_collect_email_candidates", lambda **kwargs: {"emails": [], "errors": []})
    monkeypatch.setattr(sources, "_read_email_completion_signals", lambda: ([], []))
    monkeypatch.setattr(sources, "_read_email_reply_checks", lambda due_items: ([], []))
    monkeypatch.setattr(sources, "_read_calendars", lambda: pytest.fail("calendar must not be read"))
    snapshot = sources.collect_snapshot()
    assert snapshot["calendar_events"] == []
    assert any("Calendar" in error for error in snapshot["errors"])


def test_unconfigured_calendar_name_does_not_imply_a_workstream():
    settings = {"calendar_exclude_substrings": [], "workstream_exclude_values": []}
    selected = sources.select_calendars([{"id": "work", "summary": "Work Alpha"}], settings)
    assert selected[0]["workstream"] == "other"
