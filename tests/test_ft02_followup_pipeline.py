from delivery_fakes import mock_pending_reader
from delivery_fakes import bind_synthetic_email
from delivery_fakes import confirmed_reminder, review_current_source
"""FT-02 end-to-end verification: synthetic Gmail candidate -> snapshot -> fenced agent input.

The roadmap marks FT-02 "installed, live output unverified". These tests run
the full in-repo pipeline a generated follow-up travels: Gmail bootstrap ->
candidate normalization -> collect_snapshot -> build_agent_input, asserting
provenance, privacy fencing and review idempotence. The last hop (the Hermes
review agent composing the actual message, and any live inbox output) needs
separate consent and stays unverified here.
"""
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import hermes_tingle_sources as sources
import hermes_tingle_store as store
import source_snapshot


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    db_path = tmp_path / "tingle.db"

    def connect(_name):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(store, "plugin_db", connect)
    store.enable_source("email", enabled_by="synthetic fixture consent")
    bind_synthetic_email(store)
    store.enable_source("calendar", enabled_by="synthetic fixture consent")
    return store


SYNTHETIC_MESSAGE = {
    "id": "synthetic-msg-1",
    "threadId": "synthetic-thread-1",
    "labelIds": ["INBOX"],
    "internalDate": "1763300000000",
    "snippet": "Could you send the updated quote by Friday?",
    "payload": {"headers": [
        {"name": "From", "value": "Sender <sender@example.invalid>"},
        {"name": "To", "value": "user@example.invalid"},
        {"name": "Subject", "value": "Synthetic quote request"},
        {"name": "Date", "value": "Sun, 16 Nov 2026 00:00:00 +0000"},
    ]},
}

NOW = datetime(2026, 11, 16, tzinfo=timezone.utc)


def _mock_bootstrap(monkeypatch, history_ids=None):
    monkeypatch.setattr(sources, "_read_gmail_profile_history_id", lambda: "900")
    monkeypatch.setattr(
        sources, "_stage_reconciliation_pages",
        lambda days, *, now: store.stage_gmail_sync(
            history_id=None, message_ids=["synthetic-msg-1"], now=now))
    mock_pending_reader(monkeypatch,
                        lambda mid, **kwargs: sources._normalize_gmail_message(SYNTHETIC_MESSAGE, mid))
    monkeypatch.setattr(sources, "_read_gmail_history",
                        lambda cursor: (history_ids or [], "900"))
    monkeypatch.setattr(sources, "_read_calendars", lambda: [])


def test_synthetic_followup_flows_from_gmail_to_fenced_agent_input(isolated_store, monkeypatch):
    _mock_bootstrap(monkeypatch)
    snapshot = sources.collect_snapshot(now=NOW)

    assert snapshot["email_sync"]["sync_mode"] == "bootstrap"
    assert [email["subject"] for email in snapshot["emails"]] == ["Synthetic quote request"]
    candidate = snapshot["emails"][0]
    # Provenance survives the pipeline; content stays metadata + bounded snippet.
    assert candidate["source_ref"] == "gmail:a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf/synthetic-thread-1"
    assert candidate["source_url"].endswith("synthetic-thread-1")
    assert candidate["untrusted_source"] is True
    assert "body" not in candidate
    assert candidate["snippet"] == "Could you send the updated quote by Friday?"

    payload = json.loads(source_snapshot.build_agent_input(snapshot, profile_timezone="Etc/UTC"))
    fenced = payload["source_snapshot"]
    assert fenced["untrusted_source_data"] is True
    assert fenced["emails"][0]["subject"] == "Synthetic quote request"
    assert payload["profile_timezone"] == "Etc/UTC"
    # Approved feedback rules travel outside the untrusted fence, never inside.
    assert "approved_feedback_rules" not in fenced


def test_reviewed_candidate_does_not_reappear(isolated_store, monkeypatch):
    _mock_bootstrap(monkeypatch)
    first = sources.collect_snapshot(now=NOW)
    assert len(first["emails"]) == 1

    review_current_source(store, "email", "synthetic-msg-1", "synthetic-msg-1")
    second = sources.collect_snapshot(now=NOW)
    assert second["emails"] == []
    assert second["email_sync"]["sync_mode"] == "history"
    assert store.pending_gmail_message_ids() == []
