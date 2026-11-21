"""FT-06: connect external monitors (price/job/appointment watches) to goals.

The monitor lives outside Tingle (a cron job, a URL watch); link_watch binds
it to the goal item, report_watch lands its latest finding, and /tingle list
surfaces the state under the item. History stays with the monitor's own cron
output - Tingle carries only the current truth.
"""
import json
import sqlite3

import pytest

import hermes_tingle_store as store
import hermes_tingle


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "hermes-tingle.db"

    def connect(_name: str):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(store, "plugin_db", connect)
    store.enable_source("email", enabled_by="synthetic fixture consent")
    store.enable_source("calendar", enabled_by="synthetic fixture consent")
    return store


def test_link_and_report(isolated_db):
    item = isolated_db.add_item(title="Buy synthetic gadget when cheap")
    linked = isolated_db.link_watch(item["id"], "cron:price-check")
    assert linked["watch_ref"] == "cron:price-check"
    reported = isolated_db.report_watch(item["id"], "price now 189, was 229")
    assert reported["watch_state"] == "price now 189, was 229"
    assert reported["watch_updated_at"] is not None


def test_relink_resets_state(isolated_db):
    item = isolated_db.add_item(title="Synthetic")
    isolated_db.link_watch(item["id"], "cron:old-watch")
    isolated_db.report_watch(item["id"], "stale finding")
    relinked = isolated_db.link_watch(item["id"], "url:https://example.invalid/watch")
    assert relinked["watch_ref"] == "url:https://example.invalid/watch"
    assert relinked["watch_state"] is None


def test_report_requires_a_link(isolated_db):
    item = isolated_db.add_item(title="Synthetic")
    with pytest.raises(ValueError, match="no linked watch"):
        isolated_db.report_watch(item["id"], "finding")


def test_link_and_report_validate(isolated_db):
    item = isolated_db.add_item(title="Synthetic")
    with pytest.raises(ValueError, match="watch_ref"):
        isolated_db.link_watch(item["id"], "")
    isolated_db.link_watch(item["id"], "cron:w")
    with pytest.raises(ValueError, match="1-500"):
        isolated_db.report_watch(item["id"], "x" * 501)
    with pytest.raises(KeyError):
        isolated_db.link_watch("HT-FFFFFF", "cron:w")


def test_tool_surface(isolated_db):
    added = json.loads(hermes_tingle.handle_tool(None, {"action": "add", "title": "Synthetic goal"}))
    item_id = added["item"]["id"]
    linked = json.loads(hermes_tingle.handle_tool(None, {
        "action": "link_watch", "item_id": item_id, "watch_ref": "cron:apt-check"}))
    assert linked["success"]
    reported = json.loads(hermes_tingle.handle_tool(None, {
        "action": "report_watch", "item_id": item_id, "state": "two slots on Thursday"}))
    assert reported["success"] and reported["item"]["watch_state"] == "two slots on Thursday"


def test_list_shows_watch_state(isolated_db):
    item = isolated_db.add_item(title="Synthetic goal")
    isolated_db.link_watch(item["id"], "cron:price-check")
    isolated_db.report_watch(item["id"], "price now 189")
    listed = hermes_tingle.handle_command(None, "list")
    assert "🔭 cron:price-check: price now 189" in listed


def test_closed_item_rejects_watch_changes(isolated_db):
    item = isolated_db.add_item(title="Synthetic")
    isolated_db.complete_item(item["id"])
    with pytest.raises(KeyError):
        isolated_db.link_watch(item["id"], "cron:w")
