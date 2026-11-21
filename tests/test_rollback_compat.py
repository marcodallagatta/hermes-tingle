"""Rollback proof: the pre-stack baseline code runs against a DB migrated by the tip.

All stack migrations must stay additive so a code revert (reinstall the previous
revision) keeps working without unwinding data. This test migrates + writes with
the current store, then exercises the frozen baseline store's reads and writes
against the same database."""
import importlib.util
import sqlite3
from pathlib import Path

import pytest

import hermes_tingle_store as store


@pytest.fixture
def migrated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "rollback.db"

    def connect(_name):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(store, "plugin_db", connect)
    store.enable_source("email", enabled_by="synthetic fixture consent")
    store.enable_source("calendar", enabled_by="synthetic fixture consent")
    item = store.add_item(title="Synthetic rollback item", due_at="2026-11-20T12:00:00+00:00",
                          hard_deadline=True, recur_interval=1, recur_unit="month")
    store.add_document(item["id"], name="Synthetic doc")
    store.stage_gmail_sync(history_id="H1", message_ids=["m1", "m2"])
    store.defer_gmail_pending(["m1"], error_kind="transient", now="2026-11-16T09:00:00+00:00")
    for _ in range(5):
        store.defer_gmail_pending(["m2"], error_kind="transient", now="2026-11-16T09:00:00+00:00")
    assert store.quarantined_gmail_messages()[0]["message_id"] == "m2"
    store.enable_source("chat", enabled_by="synthetic principal")
    return store, connect, item


def _baseline(connect):
    spec = importlib.util.spec_from_file_location(
        "baseline_tingle_store_rb", Path(__file__).resolve().parent / "fixtures" / "baseline_store.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.plugin_db = connect
    return module


def test_baseline_reads_and_writes_migrated_db(migrated_db):
    new_store, connect, item = migrated_db
    old = _baseline(connect)
    old_item = old.add_item(title="Synthetic old-code item", summary="written by old code")
    listed = old.list_items(status="active")
    assert any(row["id"] == item["id"] for row in listed)
    assert any(row["id"] == old_item["id"] for row in listed)
    old.stage_gmail_sync(history_id="H2", message_ids=["m3"])
    pending = old.pending_gmail_message_ids()
    assert {"m1", "m3"} <= set(pending)
    old.defer_gmail_pending(["m1"], now="2026-11-16T09:30:00+00:00")
    old.clear_gmail_pending(["m1"])
    assert old.complete_item(old_item["id"])["status"] == "done"
    assert any(row["title"] == "Synthetic old-code item"
               for row in new_store.list_items(status="done"))


def test_discovery_migration_is_additive_and_legacy_chat_is_not_attested(tmp_path, monkeypatch):
    path = tmp_path / 'legacy-discovery.db'
    def connect(_name):
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn
    old = _baseline(connect)
    before = old.add_item(title='Synthetic existing commitment')
    conn = connect('legacy')
    try:
        conn.execute('CREATE TABLE known_chats (chat_ref TEXT PRIMARY KEY, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, origin TEXT NOT NULL DEFAULT "")')
        conn.execute('INSERT INTO known_chats VALUES(?,?,?,?)',
                     ('telegram:1001', 'synthetic-first', 'synthetic-last', 'command'))
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(store, 'plugin_db', connect)
    conn = store._connect()
    try:
        row = dict(conn.execute('SELECT * FROM known_chats').fetchone())
        assert row == {'chat_ref':'telegram:1001', 'first_seen':'synthetic-first',
                       'last_seen':'synthetic-last', 'origin':'command', 'owner_dm_principal':None}
        assert conn.execute('SELECT COUNT(*) FROM hermes_tingle_chat_discovery_candidates').fetchone()[0] == 0
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    finally:
        conn.close()
    # Old code can reopen and write without erasing the additive schema/data.
    after = old.add_item(title='Synthetic rollback write')
    assert {before['id'], after['id']} <= {row['id'] for row in store.list_items()}
    assert store.list_known_chats()[0]['owner_dm_principal'] is None


def test_baseline_quarantine_boundary_is_documented(migrated_db):
    """Known boundary: the baseline does not know quarantine, so a quarantined ID
    re-enters its retry loop. Data stays intact; this is behavior, not corruption."""
    _, connect, _ = migrated_db
    old = _baseline(connect)
    assert "m2" in old.pending_gmail_message_ids()
