from delivery_fakes import mock_pending_reader
from delivery_fakes import bind_synthetic_email
from delivery_fakes import confirmed_reminder, review_current_source
"""FT-04: unreadable Gmail metadata -> classify, bounded backoff, quarantine, recovery.

No blind discard: transient IDs are never dropped; verified provider-not-found
gets distinct treatment with evidence preserved."""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import hermes_tingle_store as store
import hermes_tingle_sources as sources


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "hermes-tingle.db"

    def connect(_name: str):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(store, "plugin_db", connect)
    store.enable_source("email", enabled_by="synthetic fixture consent")
    bind_synthetic_email(store)
    store.enable_source("calendar", enabled_by="synthetic fixture consent")
    return store


NOW = datetime(2026, 11, 16, 9, tzinfo=timezone.utc)


def test_transient_failure_backs_off_and_keeps_id(isolated_db):
    isolated_db.stage_gmail_sync(history_id="H1", message_ids=["m1"])
    outcome = isolated_db.defer_gmail_pending(["m1"], error_kind="transient", now=NOW)
    assert outcome == {"retried": ["m1"], "quarantined": []}
    # In backoff: not due now, due after the backoff window, never dropped.
    assert isolated_db.pending_gmail_message_ids(now=NOW) == []
    later = NOW + timedelta(minutes=31)
    assert isolated_db.pending_gmail_message_ids(now=later) == ["m1"]


def test_backoff_grows_and_caps(isolated_db):
    isolated_db.stage_gmail_sync(history_id="H1", message_ids=["m1"])
    for attempt in range(1, store.MAX_GMAIL_METADATA_ATTEMPTS):
        isolated_db.defer_gmail_pending(["m1"], error_kind="transient", now=NOW)
    conn = isolated_db._connect()
    row = conn.execute("SELECT attempts,next_retry_at FROM gmail_pending_messages WHERE message_id='m1'").fetchone()
    conn.close()
    assert row[0] == store.MAX_GMAIL_METADATA_ATTEMPTS - 1
    expected = NOW + min(timedelta(minutes=30) * 8, timedelta(hours=24))
    assert row[1] == expected.isoformat(timespec="seconds")


def test_repeated_transient_failure_quarantines_without_discarding(isolated_db):
    isolated_db.stage_gmail_sync(history_id="H1", message_ids=["m1"])
    for _ in range(store.MAX_GMAIL_METADATA_ATTEMPTS - 1):
        isolated_db.defer_gmail_pending(["m1"], error_kind="transient", now=NOW)
    outcome = isolated_db.defer_gmail_pending(["m1"], error_kind="transient", now=NOW)
    assert outcome["quarantined"] == ["m1"]
    quarantined = isolated_db.quarantined_gmail_messages()
    assert [q["message_id"] for q in quarantined] == ["m1"]
    assert quarantined[0]["last_error_kind"] == "transient"
    # Quarantined IDs leave the due queue but keep their state.
    assert isolated_db.pending_gmail_message_ids(now=NOW + timedelta(days=30)) == []
    # Recovery affordance requeues them for fresh attempts.
    assert isolated_db.retry_quarantined_gmail() == 1
    assert isolated_db.pending_gmail_message_ids(now=NOW) == ["m1"]


def test_verified_not_found_gets_distinct_treatment_with_evidence(isolated_db):
    isolated_db.stage_gmail_sync(history_id="H1", message_ids=["m1"])
    isolated_db.record_gmail_not_found(["m1"], now=NOW)
    # Not discarded: quarantined with evidence, recoverable.
    assert isolated_db.pending_gmail_message_ids(now=NOW + timedelta(days=30)) == []
    conn = isolated_db._connect()
    row = conn.execute("SELECT kind,note FROM gmail_unreadable WHERE message_id='m1'").fetchone()
    pending = conn.execute("SELECT quarantined,last_error_kind FROM gmail_pending_messages WHERE message_id='m1'").fetchone()
    conn.close()
    assert row[0] == "not_found"
    assert "reason unknown" in row[1]
    assert pending[0] == 1 and pending[1] == "not_found"
    assert isolated_db.retry_quarantined_gmail() == 1
    assert isolated_db.pending_gmail_message_ids(now=NOW) == ["m1"]


def test_not_found_classification():
    assert sources._is_not_found_error(sources.SourceReadError("gws failed: 404 Not Found"))
    assert sources._is_not_found_error(sources.SourceReadError("Requested entity was not found."))
    assert not sources._is_not_found_error(sources.SourceReadError("gws failed: 503 backend error"))
    assert not sources._is_not_found_error(sources.SourceReadError("timed out"))


def test_error_kind_classification():
    assert sources._classify_read_error(sources.SourceReadError("401 Unauthorized")) == "auth"
    assert sources._classify_read_error(sources.SourceReadError("invalid_grant: token expired")) == "auth"
    assert sources._classify_read_error(sources.SourceReadError("503 backend error")) == "transient"
    assert sources._classify_read_error(ValueError("bad id")) == "transient"


def _snapshot_with_failures(isolated_db, monkeypatch, synthetic_settings, exc):
    isolated_db.stage_gmail_sync(history_id="H0", message_ids=[], full_reconcile_at=NOW, now=NOW)
    monkeypatch.setattr(sources, "_read_gmail_profile_history_id", lambda: "H1")
    monkeypatch.setattr(sources, "_read_calendars", lambda: [])
    monkeypatch.setattr(sources, "_read_email_completion_signals", lambda: ([], []))
    monkeypatch.setattr(sources, "_read_email_reply_checks", lambda loops: ([], []))
    monkeypatch.setattr(sources, "_read_gmail_history", lambda hid: (["m1"], "H2"))
    def fail_read(_id, **kwargs):
        raise exc
    mock_pending_reader(monkeypatch, fail_read)


def test_snapshot_quiet_by_default_keeps_diagnostics_out_of_user_errors(isolated_db, monkeypatch, synthetic_settings):
    _snapshot_with_failures(isolated_db, monkeypatch, synthetic_settings,
                            sources.SourceReadError("synthetic 503"))
    for round_ in range(store.MAX_GMAIL_METADATA_ATTEMPTS):
        snapshot = sources.collect_snapshot(now=NOW + timedelta(days=round_ + 1), lookback_days=1)
    # Quarantined, but within its automatic second chances: the user sees NOTHING
    # technical - no error kinds, no retry plumbing - and no notice at all yet.
    assert not any("quarantined" in e or "retry" in e or "Gmail review is incomplete" in e
                   for e in snapshot["errors"])
    # Coverage state stays truthful and the technical detail stays available in diagnostics.
    assert snapshot["email_sync"]["quarantined_count"] == 1
    assert any("quarantined" in d for d in snapshot["diagnostics"])
    # Quiet holds across later cycles; never a per-cycle nag.
    for cycle in range(3):
        snapshot = sources.collect_snapshot(now=NOW + timedelta(days=4, hours=cycle), lookback_days=1)
        assert not any("Gmail" in e for e in snapshot["errors"])
        assert snapshot["email_sync"]["quarantined_count"] == 1
    # Nothing was discarded: explicit recovery requeues and the next poll retries.
    import json, hermes_tingle
    result = json.loads(hermes_tingle.handle_tool(None, {"action": "retry_unreadable"}))
    assert result["requeued"] == 1


def test_snapshot_verbose_mode_surfaces_technical_detail(isolated_db, monkeypatch, synthetic_settings, tmp_path):
    import json
    synthetic_settings["verbose_diagnostics"] = True
    (tmp_path / "hermes-tingle.json").write_text(json.dumps(synthetic_settings))
    _snapshot_with_failures(isolated_db, monkeypatch, synthetic_settings,
                            sources.SourceReadError("synthetic 503"))
    for round_ in range(store.MAX_GMAIL_METADATA_ATTEMPTS):
        snapshot = sources.collect_snapshot(now=NOW + timedelta(days=round_ + 1), lookback_days=1)
    assert any("quarantined" in e and "retry_unreadable" in e for e in snapshot["errors"])
    for cycle in range(2):
        snapshot = sources.collect_snapshot(now=NOW + timedelta(days=4, hours=cycle), lookback_days=1)
        assert any("Gmail review is incomplete" in e and "quarantined" in e for e in snapshot["errors"])
        assert snapshot["email_sync"]["quarantined_count"] == 1


def test_automatic_recovery_retries_transient_quarantine_after_cooldown(isolated_db):
    isolated_db.stage_gmail_sync(history_id="H1", message_ids=["m1"], now=NOW)
    for _ in range(store.MAX_GMAIL_METADATA_ATTEMPTS):
        isolated_db.defer_gmail_pending(["m1"], error_kind="transient", now=NOW)
    assert [q["message_id"] for q in isolated_db.quarantined_gmail_messages()] == ["m1"]
    # Within the cool-down: no automatic requeue.
    assert isolated_db.auto_retry_quarantined_gmail(now=NOW + timedelta(hours=2)) == []
    # After 24h the transient quarantine gets an automatic second chance.
    assert isolated_db.auto_retry_quarantined_gmail(now=NOW + timedelta(hours=25)) == ["m1"]
    assert isolated_db.pending_gmail_message_ids(now=NOW + timedelta(hours=25)) == ["m1"]
    row = isolated_db.quarantined_gmail_messages()
    assert row == []
    # Exhausting every second chance parks the ID (still recoverable explicitly).
    when = NOW + timedelta(hours=25)
    for _ in range(store.MAX_GMAIL_METADATA_ATTEMPTS):
        isolated_db.defer_gmail_pending(["m1"], error_kind="transient", now=when)
    assert isolated_db.auto_retry_quarantined_gmail(now=when + timedelta(hours=25)) == ["m1"]
    when = when + timedelta(hours=25)
    for _ in range(store.MAX_GMAIL_METADATA_ATTEMPTS):
        isolated_db.defer_gmail_pending(["m1"], error_kind="transient", now=when)
    assert isolated_db.auto_retry_quarantined_gmail(now=when + timedelta(days=7)) == []
    parked = isolated_db.quarantined_gmail_messages()
    assert parked[0]["quarantine_cycles"] == store.MAX_GMAIL_QUARANTINE_CYCLES
    assert isolated_db.retry_quarantined_gmail() == 1


def test_auto_recovery_never_touches_auth_or_not_found(isolated_db):
    isolated_db.stage_gmail_sync(history_id="H1", message_ids=["m1", "m2"], now=NOW)
    for _ in range(store.MAX_GMAIL_METADATA_ATTEMPTS):
        isolated_db.defer_gmail_pending(["m1"], error_kind="auth", now=NOW)
    isolated_db.record_gmail_not_found(["m2"], now=NOW)
    assert isolated_db.auto_retry_quarantined_gmail(now=NOW + timedelta(days=30)) == []
    assert {q["message_id"] for q in isolated_db.quarantined_gmail_messages()} == {"m1", "m2"}


def test_persistent_degradation_yields_one_plain_language_notice(isolated_db, monkeypatch, synthetic_settings):
    _snapshot_with_failures(isolated_db, monkeypatch, synthetic_settings,
                            sources.SourceReadError("synthetic 503"))
    # Drive the ID through quarantine and both automatic second chances: each poll
    # lands after the backoff, each cycle after the 24h quarantine cool-down.
    when = NOW
    notices: list[str] = []
    for cycle in range(1 + store.MAX_GMAIL_QUARANTINE_CYCLES):
        for _ in range(store.MAX_GMAIL_METADATA_ATTEMPTS):
            # Keep the sync in history-delta mode; a weekly full reconcile is not under test.
            isolated_db.stage_gmail_sync(history_id="H2", message_ids=[], full_reconcile_at=when, now=when)
            snapshot = sources.collect_snapshot(now=when, lookback_days=1)
            notices.extend(e for e in snapshot["errors"] if "kept aside" in e)
            when = when + timedelta(days=1)
        when = when + timedelta(hours=25)
    # Parked after exhausting automatic recovery: exactly one plain-language notice
    # across the whole degradation, with no error kinds or retry plumbing.
    assert len(notices) == 1
    assert "retry_unreadable" not in notices[0] and "quarantined" not in notices[0]
    # Still parked more than 24h later: the notice re-fires once (emission is not receipt)...
    isolated_db.stage_gmail_sync(history_id="H2", message_ids=[], full_reconcile_at=when, now=when)
    snapshot = sources.collect_snapshot(now=when, lookback_days=1)
    assert any("kept aside" in e for e in snapshot["errors"])
    # ...then deduplicates again within the next 24h, with coverage still truthful.
    for cycle in range(2):
        isolated_db.stage_gmail_sync(history_id="H2", message_ids=[], full_reconcile_at=when, now=when)
        snapshot = sources.collect_snapshot(now=when + timedelta(hours=cycle + 1), lookback_days=1)
        assert not any("kept aside" in e for e in snapshot["errors"]), snapshot["errors"]
        assert snapshot["email_sync"]["quarantined_count"] == 1


def test_auth_degradation_notice_names_the_connection(isolated_db, monkeypatch, synthetic_settings):
    _snapshot_with_failures(isolated_db, monkeypatch, synthetic_settings,
                            sources.SourceReadError("synthetic 401 Unauthorized"))
    for round_ in range(store.MAX_GMAIL_METADATA_ATTEMPTS):
        snapshot = sources.collect_snapshot(now=NOW + timedelta(days=round_ + 1), lookback_days=1)
    notices = [e for e in snapshot["errors"] if "connection may need a refresh" in e]
    assert len(notices) == 1
    # Within 24h: deduplicated. After 24h still degraded: re-notified - emission is never
    # treated as receipt, so a lost delivery is corrected rather than suppressing forever.
    snapshot = sources.collect_snapshot(now=NOW + timedelta(days=5, hours=1), lookback_days=1)
    assert not any("connection may need a refresh" in e for e in snapshot["errors"])
    snapshot = sources.collect_snapshot(now=NOW + timedelta(days=6), lookback_days=1)
    assert any("connection may need a refresh" in e for e in snapshot["errors"])
    # New IDs failing the same way do not change the kind signature: no extra notices.
    isolated_db.stage_gmail_sync(history_id="H3", message_ids=["m2"], now=NOW + timedelta(days=6))
    for _ in range(store.MAX_GMAIL_METADATA_ATTEMPTS):
        isolated_db.defer_gmail_pending(["m2"], error_kind="auth", now=NOW + timedelta(days=6))
    snapshot = sources.collect_snapshot(now=NOW + timedelta(days=6, hours=2), lookback_days=1)
    assert not any("connection may need a refresh" in e for e in snapshot["errors"])


def test_review_complete_is_structural_not_inferred_from_quiet_output(isolated_db, monkeypatch, synthetic_settings):
    _snapshot_with_failures(isolated_db, monkeypatch, synthetic_settings,
                            sources.SourceReadError("synthetic 503"))
    snapshot = sources.collect_snapshot(now=NOW, lookback_days=1)
    # Pending retry: review is structurally incomplete even though output is quiet.
    assert snapshot["email_sync"]["review_complete"] is False
    assert snapshot["email_sync"]["incomplete_reasons"] == ["pending_retry"]
    for round_ in range(store.MAX_GMAIL_METADATA_ATTEMPTS - 1):
        snapshot = sources.collect_snapshot(now=NOW + timedelta(days=round_ + 1), lookback_days=1)
    assert snapshot["email_sync"]["review_complete"] is False
    assert "quarantined_unreadable" in snapshot["email_sync"]["incomplete_reasons"]
    # Recovery + a successful, reviewed read restores completeness structurally.
    isolated_db.retry_quarantined_gmail()
    mock_pending_reader(monkeypatch, lambda _id, **kwargs: {"source_id": "m1", "thread_id": "t1"})
    # Mirror production: the agent marks the lead's account-qualified version.
    review_current_source(isolated_db, "email", "m1", "a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf:m1")
    snapshot = sources.collect_snapshot(now=NOW + timedelta(days=6), lookback_days=1)
    assert snapshot["email_sync"]["review_complete"] is True
    assert snapshot["email_sync"]["incomplete_reasons"] == []


def test_calendar_failures_are_quiet_with_persistent_notice(isolated_db, monkeypatch, synthetic_settings):
    isolated_db.stage_gmail_sync(history_id="H0", message_ids=[], full_reconcile_at=NOW, now=NOW)
    monkeypatch.setattr(sources, "_read_gmail_history", lambda hid: ([], "H1"))
    monkeypatch.setattr(sources, "_read_email_completion_signals", lambda: ([], []))
    monkeypatch.setattr(sources, "_read_email_reply_checks", lambda loops: ([], []))
    monkeypatch.setattr(sources, "_read_calendars", lambda: [{"id": "cal-1", "summary": "Synthetic Cal"}])
    def fail_events(_calendar, _now, _days):
        raise sources.SourceReadError("synthetic calendar outage")
    monkeypatch.setattr(sources, "_read_calendar_events", fail_events)
    # First failure: a blip stays silent for the user, recorded in diagnostics,
    # and the structural contract marks calendar review incomplete.
    snapshot = sources.collect_snapshot(now=NOW, lookback_days=1)
    assert not any("Calendar" in e and "could not be checked" in e for e in snapshot["errors"])
    assert any("Calendar read failed" in d for d in snapshot["diagnostics"])
    assert snapshot["calendar_review"]["review_complete"] is False
    assert snapshot["calendar_review"]["incomplete_reasons"] == ["read_failed"]
    # Persistent same failure: one plain-language notice, no technical detail.
    snapshot = sources.collect_snapshot(now=NOW + timedelta(hours=2), lookback_days=1)
    notices = [e for e in snapshot["errors"] if "some calendars could not be checked" in e]
    assert len(notices) == 1
    assert "read_failed" not in notices[0] and "Synthetic Cal" not in notices[0]
    # Deduplicated within 24h, re-notified after 24h while still failing (emission != receipt).
    snapshot = sources.collect_snapshot(now=NOW + timedelta(hours=3), lookback_days=1)
    assert not any("some calendars could not be checked" in e for e in snapshot["errors"])
    snapshot = sources.collect_snapshot(now=NOW + timedelta(hours=27), lookback_days=1)
    assert any("some calendars could not be checked" in e for e in snapshot["errors"])


def test_snapshot_not_found_is_quiet_with_evidence(isolated_db, monkeypatch, synthetic_settings):
    _snapshot_with_failures(isolated_db, monkeypatch, synthetic_settings,
                            sources.GmailMessageNotFoundError("404 Not Found"))
    snapshot = sources.collect_snapshot(now=NOW, lookback_days=1)
    # A 404-only quarantine is routine: no user-facing error, no notice.
    assert not any("not found" in e or "Gmail" in e for e in snapshot["errors"])
    assert any("not found (404)" in d and "sender or subject" in d for d in snapshot["diagnostics"])
    assert snapshot["email_sync"]["quarantined_count"] == 1
    conn = isolated_db._connect()
    row = conn.execute("SELECT kind FROM gmail_unreadable WHERE message_id='m1'").fetchone()
    conn.close()
    assert row[0] == "not_found"
