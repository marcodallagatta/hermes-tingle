"""Chat source scoping: default ingestion with a per-chat exclusion list.

Chats the profile has seen are in scope by default once the chat source is
enabled; the owner excludes individual chats instead of granting each one.
Legacy per-chat consent records stay on file as history (record-layer tests
below pin that they keep working), but commands and collection use the
exclusion list.
"""

from __future__ import annotations

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
def owners(tmp_path, monkeypatch):
    import json
    import hermes_constants
    settings = {
        "calendar_exclude_substrings": [],
        "workstream_exclude_values": [],
        "source_exclude_substrings": [],
        "ticket_exclude_patterns": [],
        "delivery": "telegram:-1001234567890:42",
        "timezone": "Etc/UTC",
        "daily_schedule": "5 8,16 * * *",
        "date_only_time": "09:10",
        "availability_start": "09:00",
        "availability_end": "20:00",
        "deadline_poll_minutes": 30,
        "owner_principals": ["telegram:424242"],
    }
    (tmp_path / "hermes-tingle.json").write_text(json.dumps(settings))
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    return tmp_path


class _FakePlatform:
    def __init__(self, value="telegram"):
        self.value = value


class _FakeSource:
    def __init__(self, *, user_id="424242", chat_type="dm", is_bot=False,
                 platform="telegram", chat_id="1001"):
        self.user_id = user_id
        self.chat_type = chat_type
        self.is_bot = is_bot
        self.platform = _FakePlatform(platform)
        self.chat_id = chat_id


def _grant(chat_ref="telegram:555001", *, days=7, granted_by="dm:test"):
    return store.grant_chat_consent(chat_ref, lookback_days=days, granted_by=granted_by)


class TestConsentRecords:
    def test_grant_roundtrip(self, isolated_store):
        row = _grant(days=14)
        assert row["chat_ref"] == "telegram:555001"
        assert row["lookback_days"] == 14
        assert row["revoked_at"] is None
        assert [r["chat_ref"] for r in store.list_chat_consents()] == ["telegram:555001"]

    def test_ref_must_look_like_platform_chat(self, isolated_store):
        for bad in ("555001", "Telegram:555001", "", "telegram:"):
            with pytest.raises(ValueError):
                _grant(chat_ref=bad)

    def test_lookback_capped_and_principal_required(self, isolated_store):
        for bad in (0, 31, True, "7"):
            with pytest.raises(ValueError):
                _grant(days=bad)
        with pytest.raises(ValueError):
            _grant(granted_by="")

    def test_revoke_hides_from_active_and_regrant_resets(self, isolated_store):
        _grant(days=5)
        store.revoke_chat_consent("telegram:555001")
        assert store.list_chat_consents() == []
        assert len(store.list_chat_consents(include_revoked=True)) == 1
        row = _grant(days=9)
        assert row["lookback_days"] == 9 and row["revoked_at"] is None

    def test_revoke_unknown_raises(self, isolated_store):
        with pytest.raises(KeyError):
            store.revoke_chat_consent("telegram:nope")


class TestConsentCommands:
    def test_legacy_chatconsent_redirects(self, isolated_store, owners):
        reply = hermes_tingle.handle_command(
            None, "chatconsent grant telegram:555001 10", source=_FakeSource())
        assert "exclude" in reply.lower()

    def test_owner_excludes_and_lists(self, isolated_store, owners):
        store.note_known_chat("telegram:555001")
        reply = hermes_tingle.handle_command(
            None, "chat exclude telegram:555001", source=_FakeSource())
        assert "excluded" in reply.lower()
        listing = hermes_tingle.handle_command(None, "chats", source=_FakeSource())
        assert "telegram:555001" in listing
        # only the DM the command came from remains in scope
        assert store.chats_in_scope() == ["telegram:1001"]

    def test_chats_reply_discloses_local_discovery_and_model_consent(self, isolated_store, owners):
        listing = hermes_tingle.handle_command(None, "chats", source=_FakeSource())
        assert "bounded recent history" in listing
        assert "local commitment cues" in listing
        assert "Separately consented model evaluation" in listing
        assert "Only explicit owner-DM acceptance creates a shared item" in listing
        assert "only messages matching your tracked items are ever collected" not in listing

    def test_stranger_command_never_registers_their_chat(self, isolated_store, owners):
        for source in (None, _FakeSource(is_bot=True), _FakeSource(chat_type="group"),
                       _FakeSource(user_id=""), _FakeSource(user_id="999999")):
            hermes_tingle.handle_command(None, "chats", source=source)
        assert store.list_known_chats() == []
        assert store.chats_in_scope() == []

    def test_unauthenticated_or_wrong_senders_change_nothing(self, isolated_store, owners):
        for source in (None, _FakeSource(is_bot=True), _FakeSource(chat_type="group"),
                       _FakeSource(user_id=""), _FakeSource(user_id="999999")):
            reply = hermes_tingle.handle_command(
                None, "chatconsent grant telegram:555001", source=source)
            assert store.list_chat_consents() == [], reply

    def test_include_via_command(self, isolated_store, owners):
        store.note_known_chat("telegram:555001")
        hermes_tingle.handle_command(None, "chat exclude telegram:555001", source=_FakeSource())
        reply = hermes_tingle.handle_command(
            None, "chat include telegram:555001", source=_FakeSource())
        assert "in scope" in reply.lower() or "included" in reply.lower()
        assert store.chats_in_scope() == ["telegram:1001", "telegram:555001"]
