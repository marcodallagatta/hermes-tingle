"""FT-14: chat collector - per-chat-consented reads of Hermes' own retained
session history via injected interfaces. The plugin ships no interface; a
chat without active consent is never touched; excerpts are bounded, capped,
untrusted-flagged leads.
"""

from __future__ import annotations

import itertools
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import hermes_tingle_sources as sources
import hermes_tingle_store as store

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)


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
    store.enable_source("chat", enabled_by="synthetic fixture consent")
    yield store
    sources.set_chat_history_interfaces(None, None)


def _consent(chat_ref="telegram:1001", days=7):
    return store.grant_chat_consent(chat_ref, lookback_days=days, granted_by="test")


def _msg(text, hours_ago=1, role="user"):
    return {"role": role, "content": text,
            "timestamp": (NOW - timedelta(hours=hours_ago)).timestamp()}


def _wire(sessions=("sess-1",), messages=()):
    def read_messages(session_id, limit):
        return list(itertools.islice(iter(messages), limit))

    sources.set_chat_history_interfaces(
        lambda platform, chat_id: list(sessions),
        read_messages,
    )


class TestGating:
    def test_no_known_chats_incomplete(self, isolated_store):
        _wire(messages=[_msg("hello")])
        review = sources._collect_chat_matches(now=NOW)
        assert review["review_complete"] is False
        assert review["incomplete_reasons"] == ["no_known_chats"]
        assert review["matches"] == []

    def test_no_interface_reports_and_reads_nothing(self, isolated_store):
        _consent()
        review = sources._collect_chat_matches(now=NOW)
        assert review["review_complete"] is False
        assert review["incomplete_reasons"] == ["no_history_interface"]
        assert review["chats"] == 1 and review["matches"] == []

    def test_revoked_consent_reads_nothing(self, isolated_store):
        _consent()
        store.revoke_chat_consent("telegram:1001")
        seen = []
        sources.set_chat_history_interfaces(
            lambda p, c: seen.append((p, c)) or ["sess-1"], lambda s, limit=None: [_msg("hi")])
        review = sources._collect_chat_matches(now=NOW)
        assert review["review_complete"] is False  # no active consents
        assert seen == []

    def test_interfaces_unwire_safely(self, isolated_store):
        _consent()
        _wire(messages=[_msg("hi")])
        sources.set_chat_history_interfaces(None, None)
        review = sources._collect_chat_matches(now=NOW)
        assert review["incomplete_reasons"] == ["no_history_interface"]
        with pytest.raises(ValueError):
            sources.set_chat_history_interfaces("not-callable", lambda: [])


def _item(store_mod, title="Renew passport appointment"):
    return store_mod.add_item(title=title)


class TestCollection:
    def test_bounded_excerpts_within_lookback(self, isolated_store):
        _consent(days=7)
        item = _item(isolated_store)
        _wire(messages=[_msg("passport " + "a" * 900), _msg("passport recent"),
                        _msg("passport old", hours_ago=24 * 10)])
        review = sources._collect_chat_matches(now=NOW)
        assert review["review_complete"] is True
        assert review["scanned"] == 2
        assert len(review["matches"][0]["excerpt"]) == 500
        assert all(m["untrusted_source"] for m in review["matches"])
        assert all(m["item_ids"] == [item["id"]] for m in review["matches"])

    def test_unmatched_content_is_never_ingested(self, isolated_store):
        _consent()
        _item(isolated_store)
        _wire(messages=[_msg("totally unrelated chatter"), _msg("passport question")])
        review = sources._collect_chat_matches(now=NOW)
        assert [m["excerpt"] for m in review["matches"]] == ["passport question"]

    def test_no_active_items_reads_nothing(self, isolated_store):
        _consent()
        seen = []
        sources.set_chat_history_interfaces(
            lambda p, c: seen.append("resolver") or ["s1"],
            lambda s, limit=None: seen.append("reader") or [_msg("anything")])
        review = sources._collect_chat_matches(now=NOW)
        assert review["matches"] == [] and review["skipped"] == "no_active_items"
        assert seen == []  # no resolver or reader call at all

    def test_only_user_role_is_candidate(self, isolated_store):
        _consent()
        _item(isolated_store)
        _wire(messages=[_msg("passport from system", role="system"),
                        _msg("passport from assistant", role="assistant"),
                        _msg("passport from tool", role="tool"),
                        _msg("passport from user", role="user")])
        review = sources._collect_chat_matches(now=NOW)
        assert [m["excerpt"] for m in review["matches"]] == ["passport from user"]

    def test_undated_and_empty_messages_skipped(self, isolated_store):
        _consent()
        _item(isolated_store)
        _wire(messages=[{"role": "user", "content": "passport no ts"},
                        {"role": "user", "content": "", "timestamp": NOW.timestamp()},
                        {"role": "user", "content": "   ", "timestamp": NOW.timestamp()},
                        _msg("passport kept")])
        review = sources._collect_chat_matches(now=NOW)
        assert [m["excerpt"] for m in review["matches"]] == ["passport kept"]

    def test_per_chat_lookback_cap_applies(self, isolated_store):
        _consent()  # legacy consent keeps the chat known
        isolated_store.enable_source("chat", lookback_days=2, enabled_by="synthetic cap")
        _item(isolated_store)
        _wire(messages=[_msg("passport in", hours_ago=40), _msg("passport out", hours_ago=60)])
        review = sources._collect_chat_matches(now=NOW)
        assert [m["excerpt"] for m in review["matches"]] == ["passport in"]

    def test_global_source_cap_tightens_per_chat_consent(self, isolated_store):
        _consent(days=7)
        _item(isolated_store)
        store.enable_source("chat", lookback_days=1, enabled_by="test")
        _wire(messages=[_msg("passport recent", hours_ago=20), _msg("passport old", hours_ago=24 * 3)])
        review = sources._collect_chat_matches(now=NOW)
        assert [m["excerpt"] for m in review["matches"]] == ["passport recent"]

    def test_reads_are_lazy_and_bounded(self, isolated_store):
        _consent()
        _item(isolated_store)
        seen = []

        def messages(session_id, limit=None):
            for i in range(1000):
                seen.append(i)
                yield _msg(f"passport m{i}", hours_ago=0.01)

        sources.set_chat_history_interfaces(lambda p, c: ["s1"], messages)
        review = sources._collect_chat_matches(now=NOW)
        assert review["scanned"] == 50  # per-chat collection cap
        assert len(seen) <= 51  # never materializes the full history
        assert review["review_complete"] is False
        assert "bounded" in review["incomplete_reasons"]

    def test_session_truncation_disclosed_incomplete(self, isolated_store):
        _consent()
        _item(isolated_store)
        _wire(sessions=[f"sess-{i}" for i in range(100)], messages=[])
        review = sources._collect_chat_matches(now=NOW)
        assert review["review_complete"] is False
        assert "bounded" in review["incomplete_reasons"]

    def test_reader_errors_mark_incomplete_not_crash(self, isolated_store):
        _consent()
        _item(isolated_store)
        sources.set_chat_history_interfaces(
            lambda p, c: (_ for _ in ()).throw(RuntimeError("db gone")), None or (lambda s, limit=None: []))
        review = sources._collect_chat_matches(now=NOW)
        assert review["review_complete"] is False
        assert "read_error" in review["incomplete_reasons"]

    def test_matches_active_items_as_leads(self, isolated_store):
        _consent()
        item = _item(isolated_store)
        _wire(messages=[_msg("did you book the passport appointment yet?")])
        review = sources._collect_chat_matches(now=NOW)
        assert review["matches"][0]["item_ids"] == [item["id"]]
