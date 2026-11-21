"""Copied synthetic predecessor-schema rehearsal, no account access."""
import importlib.util
import sqlite3
from pathlib import Path
import pytest
import hermes_tingle_store as store
import hermes_tingle_lookback as lb


@pytest.mark.parametrize("fixture",["precheckpoint-synthetic.sql","v0612-synthetic.sql"])
def test_copied_precombined_rows_preserve_fields_and_add_columns(tmp_path,monkeypatch,fixture):
    original=tmp_path/'predecessor.db'
    conn=sqlite3.connect(original)
    conn.executescript((Path(__file__).parent/'fixtures'/fixture).read_text())
    conn.row_factory=sqlite3.Row
    row=dict(conn.execute("SELECT * FROM hermes_tingle_items WHERE title='Synthetic preserved predecessor'").fetchone())
    conn.close()
    src=sqlite3.connect(original);copy=tmp_path/'copied.db';dst=sqlite3.connect(copy);src.backup(dst);src.close();dst.close()
    def copied(name):
        conn=sqlite3.connect(copy);conn.row_factory=sqlite3.Row;return conn
    monkeypatch.setattr(store,'plugin_db',copied)
    after=store.get_item(row['id'])
    assert all(after[k]==v for k,v in row.items()) and after['email_review_plan']==''
    assert not store.calendar_context_enabled() and store.get_plugin_meta('synthetic_custom')=='preserve'
    conn=store._connect();assert conn.execute('PRAGMA integrity_check').fetchone()[0]=='ok';assert conn.execute('PRAGMA foreign_key_check').fetchall()==[];conn.close()
    assert store.get_item(row['id'])==after
