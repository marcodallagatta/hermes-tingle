from delivery_fakes import mock_pending_reader
from delivery_fakes import bind_synthetic_email
from delivery_fakes import confirmed_reminder, review_current_source
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import due_alerts
import hermes_tingle_sources as sources
import hermes_tingle_store as store


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    db_path = tmp_path / "tingle.db"

    def connect(_name: str):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(store, "plugin_db", connect)
    store.enable_source("email", enabled_by="synthetic fixture consent")
    bind_synthetic_email(store)
    store.enable_source("calendar", enabled_by="synthetic fixture consent")
    return store


def test_linked_reply_wait_remains_due_even_when_an_earlier_step_is_open(isolated_store, monkeypatch):
    now = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)
    goal = isolated_store.add_item(title='Goal', source_type='manual', now=now)
    wait = isolated_store.add_item(
        title='Wait for a reply', status='waiting', owner='other', source_type='email',
        source_ref='gmail:a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf/thread#sent', completion_signal='source_email_reply_received',
        next_check_at='2026-11-15T11:00:00Z', now=now - timedelta(days=1))
    isolated_store.add_step(goal['id'], title='First step', why='First task')
    isolated_store.add_step(goal['id'], title='Await reply', why='An email was already sent', wait_item_id=wait['id'])
    due, _ = sources._review_due_batch(sources.all_due_items(now=now))
    monkeypatch.setattr(sources, '_gws_bridge', lambda *args: {
        'id': 'thread', 'messages': [{'id': 'sent', 'labelIds': ['SENT'], 'internalDate': '1000'}]})
    checks, errors = sources._read_email_reply_checks(due)
    assert errors == []
    assert any(check['item_id'] == wait['id'] and check['state'] == 'no_reply' for check in checks)
    assert isolated_store.get_item(goal['id'])['status'] == 'open'
    assert isolated_store.get_item(goal['id'])['steps'][0]['complete'] is False


def test_reconciliation_reads_all_pages_and_queues_history_before_advancing(isolated_store, monkeypatch):
    now = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)
    isolated_store.stage_gmail_sync(history_id='100', message_ids=[], full_reconcile_at=now-timedelta(days=8), now=now)
    monkeypatch.setattr(sources, '_read_gmail_profile_history_id', lambda: '110')
    monkeypatch.setattr(sources, '_read_recent_email', lambda days: [{'source_id': f'm-{i}'} for i in range(100)])
    monkeypatch.setattr(sources, '_read_gmail_history', lambda cursor: (['old-import'], '120'))
    mock_pending_reader(monkeypatch, lambda mid, **kwargs: {'source_id': mid})
    calls = []
    def bridge(*args):
        params = json.loads(args[args.index('--params')+1])
        calls.append(params)
        if 'pageToken' not in params:
            return {'messages': [{'id': f'm-{i}'} for i in range(100)], 'nextPageToken': 'page-two'}
        return {'messages': [{'id': 'm-100'}]}
    monkeypatch.setattr(sources, '_gws_bridge', bridge)
    result = sources._collect_email_candidates(now=now, lookback_days=14)
    assert len(calls) == 2
    assert calls[1]['pageToken'] == 'page-two'
    assert {'m-0', 'm-100', 'old-import'} <= set(isolated_store.pending_gmail_message_ids(limit=5000))
    assert isolated_store.gmail_sync_state()['history_id'] == '120'
    assert result['sync_mode'] == 'reconcile'


def test_stale_sync_cannot_overwrite_a_newer_cursor(isolated_store):
    isolated_store.stage_gmail_sync(history_id='300', message_ids=['newer'])
    committed = isolated_store.stage_gmail_sync(history_id='200', message_ids=['older'], expected_history_id='100')
    assert committed is False
    assert isolated_store.gmail_sync_state()['history_id'] == '300'
    assert set(isolated_store.pending_gmail_message_ids()) == {'newer', 'older'}


def test_failed_reconciliation_page_keeps_partial_ids_and_processes_healthy_history(isolated_store, monkeypatch):
    now = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)
    earlier = now-timedelta(days=8)
    isolated_store.stage_gmail_sync(history_id='100', message_ids=[], full_reconcile_at=earlier, now=now)
    monkeypatch.setattr(sources, '_read_gmail_profile_history_id', lambda: '110')
    monkeypatch.setattr(sources, '_read_gmail_history', lambda cursor: (['new'], '120'))
    mock_pending_reader(monkeypatch, lambda mid, **kwargs: {'source_id': mid})
    def bridge(*args):
        params = json.loads(args[args.index('--params')+1])
        if 'pageToken' in params:
            raise sources.SourceReadError('synthetic timeout')
        return {'messages': [{'id': 'first'}], 'nextPageToken': 'next'}
    monkeypatch.setattr(sources, '_gws_bridge', bridge)
    result = sources._collect_email_candidates(now=now, lookback_days=14)
    assert {'first', 'new'} <= set(isolated_store.pending_gmail_message_ids())
    assert isolated_store.gmail_sync_state()['history_id'] == '120'
    assert isolated_store.gmail_sync_state()['last_full_reconcile_at'] == earlier.isoformat(timespec='seconds')
    assert result['errors']


@pytest.mark.parametrize('payload', [
    {'historyId': None},
    {'historyId': '200', 'history': [{'messagesAdded': 'bad'}]},
    {'historyId': '200', 'history': [{'messagesAdded': [{'message': {}}]}]},
    {'historyId': '200', 'history': [{'messages': [{'id': 'uncovered'}]}]},
    {'historyId': '200', 'history': [{'id': '150'}]},
    {'historyId': '200', 'nextPageToken': None},
])
def test_incomplete_history_response_is_rejected(monkeypatch, payload):
    monkeypatch.setattr(sources, '_gws_bridge', lambda *args: payload)
    with pytest.raises(sources.SourceReadError):
        sources._read_gmail_history('100')


def test_unreadable_message_does_not_block_later_pending_mail(isolated_store, monkeypatch):
    now = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)
    isolated_store.stage_gmail_sync(history_id='100', message_ids=['a-broken', 'z-valid'], full_reconcile_at=now, now=now)
    monkeypatch.setattr(sources, 'MAX_PENDING_EMAILS_PER_RUN', 1)
    monkeypatch.setattr(sources, '_read_gmail_history', lambda cursor: ([], cursor))
    def read(mid, **kwargs):
        if mid == 'a-broken':
            raise sources.SourceReadError('synthetic metadata failure')
        return {'source_id': mid}
    mock_pending_reader(monkeypatch, read)
    first = sources._collect_email_candidates(now=now, lookback_days=14)
    second = sources._collect_email_candidates(now=now+timedelta(minutes=1), lookback_days=14)
    assert first['errors']
    assert [message['source_id'] for message in second['emails']] == ['z-valid']
    assert 'a-broken' in isolated_store.pending_gmail_message_ids()


@pytest.mark.parametrize('payload', [
    {'id': 'different'},
    {'id': 'expected'},
    {'id': 'expected', 'labelIds': None},
    {'id': 'expected', 'payload': {'headers': None}},
])
def test_rejects_mismatched_or_malformed_metadata(monkeypatch, payload):
    monkeypatch.setattr(sources, '_gws_bridge', lambda *args: payload)
    with pytest.raises(sources.SourceReadError):
        sources._read_email_message('expected')


def test_valid_metadata_is_still_processed(monkeypatch):
    monkeypatch.setattr(sources, '_gws_bridge', lambda *args: {
        'id': 'expected', 'threadId': 'thread', 'labelIds': ['INBOX'],
        'payload': {'headers': [{'name': 'Subject', 'value': 'Test'}]},
    })
    assert sources._read_email_message('expected')['subject'] == 'Test'


def test_regressing_history_page_cursor_is_rejected(monkeypatch):
    pages = iter([{'historyId': '200', 'nextPageToken': 'next'}, {'historyId': '150'}])
    monkeypatch.setattr(sources, '_gws_bridge', lambda *args: next(pages))
    with pytest.raises(sources.SourceReadError):
        sources._read_gmail_history('100')


def test_invalid_reconciliation_page_does_not_mark_it_complete(isolated_store, monkeypatch):
    now = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)
    earlier = now-timedelta(days=8)
    isolated_store.stage_gmail_sync(history_id='100', message_ids=[], full_reconcile_at=earlier, now=now)
    monkeypatch.setattr(sources, '_read_gmail_profile_history_id', lambda: '900')
    monkeypatch.setattr(sources, '_read_gmail_history', lambda cursor: ([], cursor))
    monkeypatch.setattr(sources, '_gws_bridge', lambda *args: {'messages': [{'id': f'm{i}'} for i in range(99)] + [{}]})
    result = sources._collect_email_candidates(now=now, lookback_days=14)
    assert isolated_store.gmail_sync_state()['history_id'] == '100'
    assert isolated_store.gmail_sync_state()['last_full_reconcile_at'] == earlier.isoformat(timespec='seconds')
    assert any('invalid message ID' in e for e in result['errors'])


def test_concurrent_normal_scan_cannot_overwrite_newer_reconciliation(isolated_store, monkeypatch):
    now = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)
    isolated_store.stage_gmail_sync(history_id='100', message_ids=[], full_reconcile_at=now-timedelta(days=7), now=now)
    monkeypatch.setattr(sources, '_read_gmail_profile_history_id', lambda: '150')
    monkeypatch.setattr(sources, '_gws_bridge', lambda *args: {'messages': [{'id': 'snapshot'}]})
    mock_pending_reader(monkeypatch, lambda mid, **kwargs: {'source_id': mid})
    calls = []
    def history(cursor):
        calls.append(cursor)
        if len(calls) == 1:
            sources._collect_email_candidates(now=now+timedelta(seconds=1), lookback_days=14)
            return ['old'], '150'
        return ['new'], '200'
    monkeypatch.setattr(sources, '_read_gmail_history', history)
    result = sources._collect_email_candidates(now=now-timedelta(seconds=1), lookback_days=14)
    assert isolated_store.gmail_sync_state()['history_id'] == '200'
    assert {'old', 'new', 'snapshot'} <= set(isolated_store.pending_gmail_message_ids())
    assert any('cursor changed' in e for e in result['errors'])


def test_unexpected_reconciliation_page_is_not_marked_complete(isolated_store, monkeypatch):
    now = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)
    earlier = now-timedelta(days=8)
    isolated_store.stage_gmail_sync(history_id='100', message_ids=[], full_reconcile_at=earlier, now=now)
    monkeypatch.setattr(sources, '_read_gmail_profile_history_id', lambda: '110')
    monkeypatch.setattr(sources, '_read_gmail_history', lambda cursor: ([], '120'))
    mock_pending_reader(monkeypatch, lambda mid, **kwargs: {'source_id': mid})
    pages = iter([{'messages': [{'id': 'first'}], 'nextPageToken': 'next'}, {'unexpected': 'truncated'}])
    monkeypatch.setattr(sources, '_gws_bridge', lambda *args: next(pages))
    result = sources._collect_email_candidates(now=now, lookback_days=14)
    assert 'first' in isolated_store.pending_gmail_message_ids()
    assert isolated_store.gmail_sync_state()['last_full_reconcile_at'] == earlier.isoformat(timespec='seconds')
    assert result['errors']


def test_reply_watch_requires_a_thread_anchor_and_contextual_check_date(isolated_store):
    item = isolated_store.add_item(
        title="Follow up with the contact about the document",
        status="waiting",
        owner="other",
        source_type="email",
        source_ref="gmail:a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf/thread-1#sent-message-1",
        completion_signal="source_email_reply_received",
        next_check_at="2026-12-01T09:00:00+01:00",
        now="2026-11-15T09:00:00+01:00",
    )

    assert item["completion_signal"] == "source_email_reply_received"
    assert item["next_check_at"] == "2026-12-01T08:00:00+00:00"

    with pytest.raises(ValueError, match="next_check_at"):
        isolated_store.add_item(
            title="Follow up with the contact",
            status="waiting",
            owner="other",
            source_type="email",
            source_ref="gmail:a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf/thread-2#sent-message-2",
            completion_signal="source_email_reply_received",
            now="2026-11-15T09:00:00+01:00",
        )


def test_gmail_sync_stages_cursor_and_pending_ids_until_reviewed(isolated_store):
    isolated_store.stage_gmail_sync(
        history_id="100",
        message_ids=["msg-1", "msg-2", "msg-1"],
        full_reconcile_at="2026-11-15T09:00:00+01:00",
        now="2026-11-15T09:00:00+01:00",
    )

    assert isolated_store.gmail_sync_state() == {
        "history_id": "100",
        "last_full_reconcile_at": "2026-11-15T08:00:00+00:00",
    }
    assert isolated_store.pending_gmail_message_ids() == ["msg-1", "msg-2"]

    review_current_source(isolated_store, "email", "msg-1", "msg-1")

    assert isolated_store.pending_gmail_message_ids() == ["msg-2"]


def test_distinct_email_reply_threads_do_not_dedupe_on_same_title(isolated_store):
    first = isolated_store.add_item(
        title="Follow up with the contact", status="waiting", owner="other", source_type="email",
        source_ref="gmail:a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf/thread-one#sent-one", completion_signal="source_email_reply_received",
        next_check_at="2026-11-20T09:00:00+01:00", now="2026-11-15T09:00:00+01:00",
    )
    second = isolated_store.add_item(
        title="Follow up with the contact", status="waiting", owner="other", source_type="email",
        source_ref="gmail:a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf/thread-two#sent-two", completion_signal="source_email_reply_received",
        next_check_at="2026-11-20T09:00:00+01:00", now="2026-11-15T09:00:00+01:00",
    )

    assert second["id"] != first["id"]
    assert second["deduplicated"] is False


def test_reply_watch_repeat_nudges_reuse_its_contextual_interval(isolated_store):
    now = datetime(2026, 11, 15, 9, tzinfo=timezone.utc)
    for offset, days in enumerate((2, 60), start=1):
        first_check = now + timedelta(days=days)
        item = isolated_store.add_item(
            title=f"Wait for answer {offset}", status="waiting", owner="other", source_type="email",
            source_ref=f"gmail:a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf/thread-{offset}#sent-{offset}",
            completion_signal="source_email_reply_received", next_check_at=first_check,
            now=now,
        )
        notified = confirmed_reminder(isolated_store, [item["id"]], now=first_check)[0]
        repeated_check = datetime.fromisoformat(notified["next_check_at"])

        assert repeated_check == first_check + timedelta(days=days)


def test_history_reader_paginates_only_new_message_events(monkeypatch):
    calls = []
    pages = [
        {
            "history": [{"messagesAdded": [{"message": {"id": "msg-1"}}]}],
            "historyId": "101",
            "nextPageToken": "page-2",
        },
        {
            "history": [{"messagesAdded": [{"message": {"id": "msg-2"}}]}],
            "historyId": "102",
        },
    ]

    def fake_bridge(*args):
        params = json.loads(args[args.index("--params") + 1])
        calls.append(params)
        return pages[len(calls) - 1]

    monkeypatch.setattr(sources, "_gws_bridge", fake_bridge)

    message_ids, latest_history_id = sources._read_gmail_history("100")

    assert message_ids == ["msg-1", "msg-2"]
    assert latest_history_id == "102"
    assert calls[0]["startHistoryId"] == "100"
    assert calls[0]["historyTypes"] == ["messageAdded"]
    assert calls[1]["pageToken"] == "page-2"


def test_incremental_sync_does_not_repeat_full_14_day_search(monkeypatch):
    now = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)
    state = {
        "history_id": "100",
        "last_full_reconcile_at": (now - timedelta(days=1)).isoformat(),
    }
    staged = []
    registered = []
    message = {
        "source_type": "email", "source_id": "msg-1", "source_version": "msg-1",
        "thread_id": "thread-1", "direction": "sent", "snippet": "Could you send this?",
    }

    monkeypatch.setattr(sources.store, "gmail_sync_state", lambda: state)
    monkeypatch.setattr(sources, "_read_gmail_history", lambda cursor: (["msg-1"], "101"))
    mock_pending_reader(monkeypatch, lambda message_id, **kwargs: message)
    monkeypatch.setattr(sources.store, "stage_gmail_sync", lambda **kwargs: staged.append(kwargs))
    monkeypatch.setattr(sources.store, "pending_gmail_message_ids", lambda **kwargs: ["msg-1"])
    monkeypatch.setattr(sources.store, "source_reviewed", lambda *args: False)
    monkeypatch.setattr(sources.store, "register_email_candidates", lambda ids, now=None, **kwargs: registered.extend(ids))
    monkeypatch.setattr(sources.store, "clear_gmail_pending", lambda ids: None)
    monkeypatch.setattr(
        sources, "_read_recent_email",
        lambda days: (_ for _ in ()).throw(AssertionError("full-window query must not run")),
    )

    result = sources._collect_email_candidates(now=now, lookback_days=14)

    assert result["sync_mode"] == "history"
    assert [email["source_id"] for email in result["emails"]] == ["msg-1"]
    assert staged[0]["history_id"] == "101"
    assert staged[0]["message_ids"] == ["msg-1"]
    assert registered == ["msg-1"]


def test_failed_history_cursor_falls_back_to_consented_window_reconciliation(monkeypatch):
    now = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)
    state = {"history_id": "stale", "last_full_reconcile_at": (now - timedelta(days=1)).isoformat()}
    scanned = [{
        "source_type": "email", "source_id": "old-new", "source_version": "old-new",
        "thread_id": "thread-2", "direction": "received", "snippet": "Reply arrived",
    }]
    staged = []
    searches = []

    monkeypatch.setattr(sources.store, "gmail_sync_state", lambda: state)
    monkeypatch.setattr(sources, "_read_gmail_history", lambda cursor: (_ for _ in ()).throw(sources.SourceReadError("expired")))
    monkeypatch.setattr(sources, "_read_gmail_profile_history_id", lambda: "200")
    def list_page(*args):
        params = json.loads(args[args.index("--params")+1])
        searches.append(params["q"])
        return {"messages": [{"id": "old-new"}]}
    monkeypatch.setattr(sources, "_gws_bridge", list_page)
    mock_pending_reader(monkeypatch, lambda mid, **kwargs: scanned[0])
    monkeypatch.setattr(sources.store, "stage_gmail_sync", lambda **kwargs: staged.append(kwargs))
    monkeypatch.setattr(sources.store, "pending_gmail_message_ids", lambda **kwargs: ["old-new"])
    monkeypatch.setattr(sources.store, "source_reviewed", lambda *args: False)
    monkeypatch.setattr(sources.store, "register_email_candidates", lambda *args, **kwargs: None)
    monkeypatch.setattr(sources.store, "clear_gmail_pending", lambda ids: None)

    result = sources._collect_email_candidates(now=now, lookback_days=14)

    assert result["sync_mode"] == "reconcile_after_history_error"
    assert "newer_than:14d" in searches[0]
    assert staged[0]["history_id"] is None
    assert staged[0]["message_ids"] == ["old-new"]
    assert any("history" in error.lower() and "reconcil" in error.lower() for error in result["errors"])
    assert any("cursor unchanged" in error.lower() for error in result["errors"])


def test_due_review_rotates_failed_checks_and_manual_items_without_changing_them(isolated_store, monkeypatch):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    watches = [isolated_store.add_item(title=f"Wait {i}", source_type="email",
        source_ref=f"gmail:a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf/thread-{i}#sent-{i}", status="waiting", owner="other",
        completion_signal="source_email_reply_received", now="2020-01-01T00:00:00+00:00",
        next_check_at="2020-01-02T00:00:00+00:00") for i in range(80)]
    manual = [isolated_store.add_item(title=f"Review {i}", source_type="manual",
        now="2020-01-01T00:00:00+00:00", next_check_at="2020-01-03T00:00:00+00:00") for i in range(35)]
    monkeypatch.setattr(sources, "_collect_email_candidates", lambda **kwargs: {"emails": [], "errors": []})
    monkeypatch.setattr(sources, "_read_email_completion_signals", lambda: ([], []))
    monkeypatch.setattr(sources, "_read_calendars", lambda: [])
    attempted = []
    def fail_bridge(*args):
        params = json.loads(args[args.index("--params") + 1])
        attempted.append(params["id"])
        raise sources.SourceReadError("synthetic failure")
    monkeypatch.setattr(sources, "_gws_bridge", fail_bridge)
    shown = set()
    for _ in range(5):
        before = len(attempted)
        snapshot = sources.collect_snapshot(now=now)
        assert len(attempted) - before <= 25
        assert len(snapshot["due_items"]) <= 50
        assert snapshot["email_reply_checks"] == []
        shown.update(item["id"] for item in snapshot["due_items"])
        assert snapshot["errors"]
    assert {f"thread-{i}" for i in range(80)} <= set(attempted)
    assert {item["id"] for item in watches + manual} <= shown
    assert all(isolated_store.get_item(item["id"])["reminder_count"] == 0 for item in watches + manual)
    assert all(isolated_store.get_item(item["id"])["owner"] == "other" for item in watches)


def test_due_reply_check_uses_thread_metadata_and_detects_reply(isolated_store, monkeypatch):
    item = {
        "id": "HT-ABC123", "source_type": "email", "source_ref": "gmail:a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf/thread-1#sent-1",
        "completion_signal": "source_email_reply_received",
    }
    monkeypatch.setattr(sources.store, "list_items", lambda **kwargs: [])
    calls = []

    def fake_bridge(*args):
        params = json.loads(args[args.index("--params") + 1])
        calls.append(params)
        return {"id": "thread-1", "messages": [
            {"id": "sent-1", "labelIds": ["SENT"], "internalDate": "1000"},
            {"id": "reply-1", "labelIds": ["INBOX"], "internalDate": "2000",
             "snippet": "I can send it next week.",
             "payload": {"headers": [{"name": "From", "value": "the contact <s@example.com>"},
                                       {"name": "Date", "value": "Sun, 15 Nov 2026 10:00:00 +0000"}]}},
        ]}

    monkeypatch.setattr(sources, "_gws_bridge", fake_bridge)

    signals, errors = sources._read_email_reply_checks([item])

    assert errors == []
    assert signals == [{
        "item_id": "HT-ABC123", "thread_id": "thread-1", "state": "reply_received",
        "reply": {"source_id": "reply-1", "date": "Sun, 15 Nov 2026 10:00:00 +0000",
                  "from": "the contact <s@example.com>", "snippet": "I can send it next week."},
    }]
    assert "body" not in calls[0]["fields"].lower()
    assert calls[0]["id"] == "thread-1"
    assert isolated_store.claim_email_body_read("reply-1", account_key='a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf') is True


def test_due_reply_check_reports_no_reply_for_old_thread(monkeypatch):
    item = {
        "id": "HT-ABC123", "source_type": "email", "source_ref": "gmail:a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf/thread-1#sent-1",
        "completion_signal": "source_email_reply_received",
    }
    monkeypatch.setattr(sources.store, "list_items", lambda **kwargs: [])
    monkeypatch.setattr(sources, "_gws_bridge", lambda *args: {"id": "thread-1", "messages": [
        {"id": "sent-1", "labelIds": ["SENT"], "internalDate": "1000"},
    ]})

    signals, errors = sources._read_email_reply_checks([item])

    assert errors == []
    assert signals == [{"item_id": "HT-ABC123", "thread_id": "thread-1", "state": "no_reply"}]


def test_equal_timestamp_inbound_reply_is_not_treated_as_no_reply(monkeypatch):
    item = {"id": "HT-ABC123", "source_type": "email", "source_ref": "gmail:a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf/thread-1#sent-1",
            "completion_signal": "source_email_reply_received"}
    monkeypatch.setattr(sources, "_gws_bridge", lambda *args: {"id": "thread-1", "messages": [
        {"id": "sent-1", "labelIds": ["SENT"], "internalDate": "1000"},
        {"id": "reply-1", "labelIds": ["INBOX"], "internalDate": "1000"},
    ]})
    checks, errors = sources._read_email_reply_checks([item])
    assert checks == []
    assert errors


def test_invalid_reply_watch_anchor_does_not_claim_no_reply(monkeypatch):
    item = {
        "id": "HT-ABC123", "source_type": "email", "source_ref": "gmail:a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf/thread-1",
        "completion_signal": "source_email_reply_received",
    }
    monkeypatch.setattr(sources.store, "list_items", lambda **kwargs: [])

    signals, errors = sources._read_email_reply_checks([item])

    assert signals == []
    assert errors and "anchor" in errors[0].lower()


@pytest.mark.parametrize("bad_message", [
    None, {}, {"id": "reply-1", "labelIds": None, "internalDate": "2000"},
    {"id": "reply-1", "labelIds": "SENT", "internalDate": "2000"},
])
def test_malformed_thread_record_never_claims_no_reply(monkeypatch, bad_message):
    item = {"id": "HT-ABC123", "source_type": "email", "source_ref": "gmail:a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf/thread-1#sent-1",
            "completion_signal": "source_email_reply_received"}
    monkeypatch.setattr(sources, "_gws_bridge", lambda *args: {"id": "thread-1", "messages": [
        {"id": "sent-1", "labelIds": ["SENT"], "internalDate": "1000"}, bad_message]})
    checks, errors = sources._read_email_reply_checks([item])
    assert checks == []
    assert errors


@pytest.mark.parametrize("thread_id", [None, "wrong-thread"])
def test_unverified_thread_identity_never_claims_no_reply(monkeypatch, thread_id):
    item = {"id": "HT-ABC123", "source_type": "email", "source_ref": "gmail:a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf/thread-1#sent-1",
            "completion_signal": "source_email_reply_received"}
    payload = {"messages": [{"id": "sent-1", "labelIds": ["SENT"], "internalDate": "1000"}]}
    if thread_id is not None:
        payload["id"] = thread_id
    monkeypatch.setattr(sources, "_gws_bridge", lambda *args: payload)
    checks, errors = sources._read_email_reply_checks([item])
    assert checks == [] and errors


def test_missing_inbound_timestamp_does_not_claim_no_reply(monkeypatch):
    item = {
        "id": "HT-ABC123", "source_type": "email", "source_ref": "gmail:a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf/thread-1#sent-1",
        "completion_signal": "source_email_reply_received",
    }
    monkeypatch.setattr(sources, "_gws_bridge", lambda *args: {"id": "thread-1", "messages": [
        {"id": "sent-1", "labelIds": ["SENT"], "internalDate": "1000"},
        {"id": "reply-1", "labelIds": ["INBOX"], "snippet": "I got this."},
    ]})

    checks, errors = sources._read_email_reply_checks([item])

    assert checks == []
    assert errors and "inbound message date was incomplete" in errors[0].lower()


def test_script_only_due_alert_suppresses_email_waits_until_thread_checked():
    message = due_alerts.render_due_alerts([
        {"id": "HT-ABC123", "title": "Waiting for a reply", "source_type": "email",
         "completion_signal": "source_email_reply_received"},
        {"id": "HT-DEF456", "title": "Send the invoice", "source_type": "manual",
         "completion_signal": ""},
    ])

    assert "Waiting for a reply" not in message
    assert "HT-ABC123" not in message
    assert "Send the invoice" in message
    assert "HT-DEF456" in message


def test_script_only_due_alert_is_silent_when_only_email_waits_are_due():
    message = due_alerts.render_due_alerts([
        {"id": "HT-ABC123", "title": "Waiting for a reply", "source_type": "email",
         "completion_signal": "source_email_reply_received"},
    ])

    assert message == ""


def test_due_review_prioritizes_real_earliest_deadline_with_backlog():
    items = [{'id': f'HT-{n:06X}', 'source_type': 'manual', 'due_at': '2026-01-02T00:00:00+00:00'} for n in range(50)]
    urgent = {'id': 'HT-FFFFFF', 'source_type': 'manual', 'due_at': '2020-01-01T00:00:00+00:00'}
    batch, _ = sources._review_due_batch(items + [urgent])
    assert urgent in batch
    assert len(batch) <= 50


def test_trash_review_rotates_across_more_than_one_hundred_active_items(monkeypatch):
    records = [{'id': f'HT-{n:06X}', 'source_type': 'email', 'source_ref': f'gmail:a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf/thread-{n}', 'completion_signal': 'source_email_trashed'} for n in range(126)]
    monkeypatch.setattr(sources.store, 'list_items', lambda **kw: records[kw.get('offset', 0):kw.get('offset', 0) + kw['limit']])
    calls = []
    def bridge(*args):
        thread = json.loads(args[args.index('--params') + 1])['id']
        calls.append(thread)
        return {'id': thread, 'messages': [{'id': 'm', 'labelIds': ['INBOX']}]}
    monkeypatch.setattr(sources, '_gws_bridge', bridge)
    for _ in range(6):
        before = len(calls)
        signals, errors = sources._read_email_completion_signals()
        assert not signals
        assert len(calls) - before <= 25
    assert {f'thread-{n}' for n in range(126)} <= set(calls)


def test_due_review_rotation_covers_undated_and_malformed_deadlines():
    items = [{'id': f'HT-{n:06X}', 'source_type': 'manual', 'due_at': '2020-01-01T00:00:00Z'} for n in range(50)]
    items.extend([{'id': 'HT-FFFFFE', 'source_type': 'manual', 'due_at': None},
                  {'id': 'HT-FFFFFF', 'source_type': 'manual', 'due_at': 'ambiguous private text'}])
    seen = set()
    for _ in range(3):
        batch, errors = sources._review_due_batch(items)
        assert len(batch) <= 50
        assert all('ambiguous private text' not in error for error in errors)
        seen.update(item['id'] for item in batch)
    assert seen == {item['id'] for item in items}


def test_trash_check_failures_rotate_without_leaking_provider_text(monkeypatch):
    records = [{'id': f'HT-{n:06X}', 'source_type': 'email', 'source_ref': f'gmail:a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf/secret-{n}', 'completion_signal': 'source_email_trashed'} for n in range(30)]
    monkeypatch.setattr(sources.store, 'list_items', lambda **kw: records[kw.get('offset', 0):kw.get('offset', 0) + kw['limit']])
    seen = []
    def fail(*args):
        seen.append(json.loads(args[args.index('--params') + 1])['id'])
        raise sources.SourceReadError('secret provider body')
    monkeypatch.setattr(sources, '_gws_bridge', fail)
    for _ in range(2):
        signals, errors = sources._read_email_completion_signals()
        assert not signals and errors
        assert all('secret' not in error for error in errors)
    assert {f'secret-{n}' for n in range(30)} <= set(seen)


def test_repeated_history_failures_reset_cursor_with_explicit_gap(isolated_store, monkeypatch):
    now = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)
    isolated_store.stage_gmail_sync(history_id='100', message_ids=[], full_reconcile_at=now - timedelta(days=1), now=now)
    sources._set_gmail_history_failures(2)
    monkeypatch.setattr(sources, '_read_gmail_history', lambda cursor: (_ for _ in ()).throw(sources.SourceReadError('expired')))
    monkeypatch.setattr(sources, '_read_gmail_profile_history_id', lambda: '900')
    monkeypatch.setattr(sources, '_gws_bridge', lambda *args: {'messages': [{'id': 'recent'}]})
    mock_pending_reader(monkeypatch, lambda mid, **kwargs: {'source_id': mid, 'source_version': mid})

    result = sources._collect_email_candidates(now=now, lookback_days=14)

    assert isolated_store.gmail_sync_state()['history_id'] == '900'
    assert result['sync_mode'] == 'reconcile_after_history_error'
    assert any('reset' in error.lower() and 'older' in error.lower() for error in result['errors'])
    assert not any('cursor unchanged' in error.lower() for error in result['errors'])
    assert 'recent' in isolated_store.pending_gmail_message_ids()


def test_first_history_failures_keep_the_cursor(isolated_store, monkeypatch):
    now = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)
    isolated_store.stage_gmail_sync(history_id='100', message_ids=[], full_reconcile_at=now - timedelta(days=1), now=now)
    monkeypatch.setattr(sources, '_read_gmail_history', lambda cursor: (_ for _ in ()).throw(sources.SourceReadError('transient')))
    monkeypatch.setattr(sources, '_read_gmail_profile_history_id', lambda: '900')
    monkeypatch.setattr(sources, '_gws_bridge', lambda *args: {'messages': []})

    for expected_failures in (1, 2):
        result = sources._collect_email_candidates(now=now, lookback_days=14)
        assert isolated_store.gmail_sync_state()['history_id'] == '100'
        assert any('cursor unchanged' in error.lower() for error in result['errors'])
        assert sources._gmail_history_failure_count() == expected_failures


def test_history_success_resets_the_failure_count(isolated_store, monkeypatch):
    now = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)
    isolated_store.stage_gmail_sync(history_id='100', message_ids=[], full_reconcile_at=now - timedelta(days=1), now=now)
    sources._set_gmail_history_failures(2)
    monkeypatch.setattr(sources, '_read_gmail_history', lambda cursor: (['new'], '120'))
    mock_pending_reader(monkeypatch, lambda mid, **kwargs: {'source_id': mid, 'source_version': mid})

    result = sources._collect_email_candidates(now=now, lookback_days=14)

    assert result['sync_mode'] == 'history'
    assert isolated_store.gmail_sync_state()['history_id'] == '120'
    assert sources._gmail_history_failure_count() == 0


def test_tracked_email_context_reports_coverage_beyond_100_active_items(isolated_store, monkeypatch):
    now = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)
    for index in range(105):
        isolated_store.add_item(title=f'Item {index}', now=now)
    monkeypatch.setattr(sources, '_collect_email_candidates', lambda **kwargs: {'emails': [], 'errors': [], 'sync_mode': 'history', 'pending_count': 0})
    monkeypatch.setattr(sources, '_read_calendars', lambda: [])

    snapshot = sources.collect_snapshot(now=now)

    assert any('bounded to 100 active items' in error for error in snapshot['errors'])


def test_archived_unlabelled_reply_is_a_valid_thread_message(monkeypatch):
    item = {'id':'HT-ABC123','source_type':'email',
            'source_ref':'gmail:a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf/thread-1#sent-1',
            'completion_signal':'source_email_reply_received'}
    monkeypatch.setattr(sources,'_gws_bridge',lambda *args:{'id':'thread-1','messages':[
        {'id':'sent-1','labelIds':['SENT'],'internalDate':'1000'},
        {'id':'reply-1','labelIds':[],'internalDate':'2000'}]})
    checks,errors=sources._read_email_reply_checks([item])
    assert not errors and checks[0]['state']=='reply_received'
