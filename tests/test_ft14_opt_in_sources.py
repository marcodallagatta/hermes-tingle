from delivery_fakes import bind_synthetic_email
from delivery_fakes import confirmed_reminder, review_current_source
"""FT-14: opt-in source registry with scoped historical context."""
import sqlite3
from datetime import datetime, timezone

import pytest

import hermes_tingle_store as store
import hermes_tingle_sources as sources


@pytest.fixture
def isolated_db(tmp_path, monkeypatch, request):
    db_path = tmp_path / "hermes-tingle.db"

    def connect(_name: str):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(store, "plugin_db", connect)
    if request.node.name != "test_fresh_install_all_sources_off":
        store.enable_source("email", enabled_by="synthetic fixture consent")
        bind_synthetic_email(store)
        store.enable_source("calendar", enabled_by="synthetic fixture consent", calendar_context=False)
    return store


def test_fresh_install_all_sources_off(isolated_db):
    optins = {row["kind"]: row for row in isolated_db.list_source_optins()}
    assert all(row["enabled"] == 0 for row in optins.values())
    assert all(not row["enabled_by"] for row in optins.values())


def test_enable_disable_roundtrip(isolated_db):
    row = isolated_db.enable_source("chat", lookback_days=7, enabled_by="synthetic principal", note="synthetic consent")
    assert row["enabled"] == 1 and row["lookback_days"] == 7 and row["enabled_at"]
    assert isolated_db.source_enabled("chat")
    assert isolated_db.source_lookback_cap("chat") == 7
    row = isolated_db.disable_source("chat", note="synthetic revocation")
    assert row["enabled"] == 0 and row["disabled_at"]
    assert not isolated_db.source_enabled("chat")
    assert isolated_db.source_lookback_cap("chat") is None
    row = isolated_db.enable_source("chat", enabled_by="synthetic principal")
    assert row["enabled"] == 1 and row["lookback_days"] is None


def test_removed_document_kind_refused_but_legacy_row_manageable(isolated_db):
    with pytest.raises(ValueError):
        isolated_db.enable_source("document", enabled_by="synthetic principal")
    with pytest.raises(ValueError):
        isolated_db.source_enabled("document")
    row = isolated_db.disable_source("document", note="legacy cleanup")
    assert row["enabled"] == 0


def test_disable_never_enabled_kind(isolated_db):
    row = isolated_db.disable_source("chat")
    assert row["enabled"] == 0


def test_invalid_kind_and_lookback_rejected(isolated_db):
    for bad in ("gmail", "", "sources"):
        with pytest.raises(ValueError):
            isolated_db.enable_source(bad, enabled_by="synthetic principal")
    for bad in (0, 31, -1):
        with pytest.raises(ValueError):
            isolated_db.enable_source("chat", lookback_days=bad, enabled_by="synthetic principal")
    with pytest.raises(ValueError):
        isolated_db.source_enabled("unknown")


def test_enable_requires_authenticated_principal(isolated_db):
    with pytest.raises(TypeError):
        isolated_db.enable_source("chat")
    with pytest.raises(ValueError):
        isolated_db.enable_source("chat", enabled_by="  ")
    assert not isolated_db.source_enabled("chat")


def test_collect_snapshot_skips_disabled_email(isolated_db, monkeypatch, synthetic_settings):
    called = []
    monkeypatch.setattr(sources, "_collect_email_candidates",
                        lambda **kw: called.append(kw) or {"emails": [], "errors": [], "sync_mode": "test", "pending_count": 0})
    monkeypatch.setattr(sources, "_read_email_completion_signals", lambda: ([], []))
    monkeypatch.setattr(sources, "_read_email_reply_checks", lambda loops: ([], []))
    monkeypatch.setattr(sources, "_read_calendars", lambda: [])
    isolated_db.disable_source("email")
    snapshot = sources.collect_snapshot(now=datetime(2026, 11, 16, 9, tzinfo=timezone.utc))
    assert called == []
    assert snapshot["emails"] == []
    assert snapshot["email_sync"]["incomplete_reasons"] == ["disabled"]
    isolated_db.enable_source("email", enabled_by="synthetic principal")
    bind_synthetic_email(isolated_db)
    snapshot = sources.collect_snapshot(now=datetime(2026, 11, 16, 9, tzinfo=timezone.utc))
    assert len(called) == 1


def test_lookback_cap_scopes_historical_context(isolated_db, monkeypatch, synthetic_settings):
    seen = {}
    monkeypatch.setattr(sources, "_collect_email_candidates",
                        lambda **kw: seen.update(kw) or {"emails": [], "errors": [], "sync_mode": "test", "pending_count": 0})
    monkeypatch.setattr(sources, "_read_email_completion_signals", lambda: ([], []))
    monkeypatch.setattr(sources, "_read_email_reply_checks", lambda loops: ([], []))
    monkeypatch.setattr(sources, "_read_calendars", lambda: [])
    isolated_db.enable_source("email", lookback_days=3, enabled_by="synthetic principal")
    bind_synthetic_email(isolated_db)
    snapshot = sources.collect_snapshot(now=datetime(2026, 11, 16, 9, tzinfo=timezone.utc), lookback_days=14)
    assert seen["lookback_days"] == 3
    assert snapshot["lookback_days"] == 3


def test_collect_snapshot_skips_disabled_calendar(isolated_db, monkeypatch, synthetic_settings):
    monkeypatch.setattr(sources, "_collect_email_candidates",
                        lambda **kw: {"emails": [], "errors": [], "sync_mode": "test", "pending_count": 0})
    monkeypatch.setattr(sources, "_read_email_completion_signals", lambda: ([], []))
    monkeypatch.setattr(sources, "_read_email_reply_checks", lambda loops: ([], []))
    called = []
    monkeypatch.setattr(sources, "_read_calendars", lambda: called.append(1) or [])
    isolated_db.disable_source("calendar")
    snapshot = sources.collect_snapshot(now=datetime(2026, 11, 16, 9, tzinfo=timezone.utc))
    assert called == []
    assert snapshot["calendar_review"]["incomplete_reasons"] == ["disabled"]


def test_read_email_gated(isolated_db):
    isolated_db.disable_source("email")
    with pytest.raises(sources.SourceReadError):
        sources.read_email("synthetic-message-id")


def test_calendar_availability_gated(isolated_db):
    isolated_db.disable_source("calendar")
    with pytest.raises(sources.SourceReadError):
        sources.check_calendar_availability("2026-11-17")


def _configure_owners(tmp_path, principals):
    import json as _json
    settings = _json.loads((tmp_path / "hermes-tingle.json").read_text())
    settings["owner_principals"] = principals
    (tmp_path / "hermes-tingle.json").write_text(_json.dumps(settings))


class _FakePlatform:
    def __init__(self, value):
        self.value = value


class _FakeSource:
    """Minimal SessionSource stand-in for command-dispatch sender checks."""

    def __init__(self, *, user_id="424242", chat_type="dm", is_bot=False, platform="telegram", chat_id="1001"):
        self.user_id = user_id
        self.chat_type = chat_type
        self.is_bot = is_bot
        self.platform = _FakePlatform(platform)
        self.chat_id = chat_id


def test_model_tool_cannot_grant_or_revoke(isolated_db):
    import json
    import hermes_tingle
    result = json.loads(hermes_tingle.handle_tool(None, {"action": "list_sources"}))
    assert {row["kind"] for row in result["sources"]} == {"email", "calendar", "document", "chat"}
    for action in ("enable_source", "disable_source"):
        result = json.loads(hermes_tingle.handle_tool(None, {"action": action, "source_kind": "meeting", "lookback_days": 5}))
        assert result["success"] is False
    assert not isolated_db.source_enabled("meeting")


def test_command_grant_binds_authenticated_principal(isolated_db, tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "probe_source", lambda kind: None)
    import hermes_tingle
    _configure_owners(tmp_path, ["telegram:424242"])
    ctx = type("Ctx", (), {"ask_choice": lambda self,q,ch: "enable"})()
    reply = hermes_tingle.handle_command(ctx, "source enable calendar 7", source=_FakeSource())
    assert "opted in" in reply and "7 days" in reply
    row = {r["kind"]: r for r in isolated_db.list_source_optins()}["calendar"]
    assert row["enabled"] == 1 and row["lookback_days"] == 7
    assert "telegram user 424242 in dm" in row["enabled_by"]


def test_command_grant_refused_without_authenticated_sender(isolated_db, tmp_path):
    import hermes_tingle
    _configure_owners(tmp_path, ["telegram:424242"])
    reply = hermes_tingle.handle_command(None, "source enable meeting 7", source=None)
    assert "nothing changed" in reply and "identified sender" in reply
    assert not isolated_db.source_enabled("meeting")
    reply = hermes_tingle.handle_command(None, "source enable meeting 7", source=_FakeSource(is_bot=True))
    assert "must come from a person" in reply
    assert not isolated_db.source_enabled("meeting")
    reply = hermes_tingle.handle_command(None, "source enable meeting 7", source=_FakeSource(chat_type="group"))
    assert "direct message" in reply
    assert not isolated_db.source_enabled("meeting")
    reply = hermes_tingle.handle_command(None, "source enable meeting 7", source=_FakeSource(user_id=None))
    assert "nothing changed" in reply and "identified sender" in reply
    assert not isolated_db.source_enabled("meeting")


def test_revocation_purges_retained_source_data(isolated_db, tmp_path):
    import hermes_tingle
    _configure_owners(tmp_path, ["telegram:424242"])
    isolated_db.stage_gmail_sync(history_id="H1", message_ids=["m1", "m2"])
    conn = isolated_db._connect()
    conn.execute("INSERT INTO email_candidates(source_id,registered_at) VALUES('m1','2026-11-16T00:00:00+00:00')")
    conn.execute("INSERT INTO email_body_reads(source_id,read_at) VALUES('m1','2026-11-16T00:00:00+00:00')")
    conn.execute("INSERT INTO source_seen(source_type,source_id,source_version,reviewed_at) VALUES('email','m1','v','2026-11-16T00:00:00+00:00')")
    conn.execute("INSERT INTO source_seen(source_type,source_id,source_version,reviewed_at) VALUES('calendar','c1','v','2026-11-16T00:00:00+00:00')")
    conn.commit(); conn.close()
    reply = hermes_tingle.handle_command(None, "source disable email", source=_FakeSource())
    assert "opted out" in reply and "purged" in reply
    conn = isolated_db._connect()
    assert conn.execute("SELECT COUNT(*) FROM gmail_pending_messages").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM email_candidates").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM email_body_reads").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM source_seen WHERE source_type='email'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM plugin_meta WHERE key LIKE 'gmail_%'").fetchone()[0] == 0
    # an unrelated source's marks survive
    assert conn.execute("SELECT COUNT(*) FROM source_seen WHERE source_type='calendar'").fetchone()[0] == 1
    conn.close()


def test_narrowing_lookback_purges_retained_data(isolated_db, tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "probe_source", lambda kind: None)
    import hermes_tingle
    _configure_owners(tmp_path, ["telegram:424242"])
    hermes_tingle.handle_command(None, "source enable email 14", source=_FakeSource())
    isolated_db.stage_gmail_sync(history_id="H1", message_ids=["m1"])
    hermes_tingle.handle_command(None, "source enable email 3", source=_FakeSource())
    conn = isolated_db._connect()
    assert conn.execute("SELECT COUNT(*) FROM gmail_pending_messages").fetchone()[0] == 0
    conn.close()
    row = {r["kind"]: r for r in isolated_db.list_source_optins()}["email"]
    assert row["enabled"] == 1 and row["lookback_days"] == 3


def test_widening_via_command_does_not_purge(isolated_db, tmp_path):
    import hermes_tingle
    _configure_owners(tmp_path, ["telegram:424242"])
    hermes_tingle.handle_command(None, "source enable email 3", source=_FakeSource())
    isolated_db.stage_gmail_sync(history_id="H1", message_ids=["m1"])
    hermes_tingle.handle_command(None, "source enable email 14", source=_FakeSource())
    conn = isolated_db._connect()
    assert conn.execute("SELECT COUNT(*) FROM gmail_pending_messages").fetchone()[0] == 1
    conn.close()



def test_second_dm_human_is_not_the_owner(isolated_db, tmp_path):
    import hermes_tingle
    _configure_owners(tmp_path, ["telegram:424242"])
    other = _FakeSource(user_id="999999")
    reply = hermes_tingle.handle_command(None, "source enable meeting 7", source=other)
    assert "not a configured owner principal" in reply
    assert not isolated_db.source_enabled("meeting")
    reply = hermes_tingle.handle_command(None, "source disable email", source=other)
    assert "not a configured owner principal" in reply
    assert isolated_db.source_enabled("email")


def test_no_configured_owner_refuses_every_source_decision(isolated_db):
    import hermes_tingle
    reply = hermes_tingle.handle_command(None, "source enable meeting 7", source=_FakeSource())
    assert "no owner principal is configured" in reply
    assert not isolated_db.source_enabled("meeting")
    reply = hermes_tingle.handle_command(None, "source disable email", source=_FakeSource())
    assert "no owner principal is configured" in reply
    assert isolated_db.source_enabled("email")


def test_read_email_enforces_lookback_cap_before_body(isolated_db, monkeypatch):
    from datetime import timedelta
    isolated_db.enable_source("email", lookback_days=3, enabled_by="synthetic principal")
    bind_synthetic_email(isolated_db)
    body_reads = []
    monkeypatch.setattr(sources, "_read_bound_email", lambda *a, **k: body_reads.append(a) or {"id": a[0]})
    now = datetime.now(timezone.utc)
    def date_payload(days):
        return {"internalDate": str(int((now-timedelta(days=days)).timestamp()*1000))}
    isolated_db.register_email_candidates(["m1", "m2", "m3"], account_key='a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf')
    monkeypatch.setattr(sources, "_gws_bridge", lambda *a, **k: date_payload(4))
    with pytest.raises(sources.SourceReadError):
        sources.read_email("m1")
    assert body_reads == []
    monkeypatch.setattr(sources, "_gws_bridge", lambda *a, **k: date_payload(1))
    assert sources.read_email("m2")["id"] == "m2"
    assert len(body_reads) == 1
    monkeypatch.setattr(sources, "_gws_bridge", lambda *a, **k: {"internalDate": "invalid"})
    with pytest.raises(sources.SourceReadError):
        sources.read_email("m3")
    assert len(body_reads) == 1


def test_inflight_collectors_cannot_repopulate_revoked_caches(isolated_db):
    isolated_db.stage_gmail_sync(history_id="H1", message_ids=["m1"])
    isolated_db.disable_source("email")
    with pytest.raises(isolated_db.SourceOptInRevoked):
        isolated_db.stage_gmail_sync(history_id="H2", message_ids=["m2"])
    with pytest.raises(isolated_db.SourceOptInRevoked):
        isolated_db.register_email_candidates(["m2"], account_key='a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf')
    with pytest.raises(isolated_db.SourceOptInRevoked):
        isolated_db.claim_email_body_read("m2", account_key='a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf')
    assert review_current_source(isolated_db, "email", "m2") is False
    conn = isolated_db._connect()
    assert conn.execute("SELECT COUNT(*) FROM gmail_pending_messages").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM email_candidates").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM email_body_reads").fetchone()[0] == 0
    conn.close()
