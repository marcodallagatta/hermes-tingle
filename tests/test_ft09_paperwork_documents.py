"""FT-09 paperwork: document checklists with validity windows on items."""
import sqlite3

import pytest

import hermes_tingle_store as store


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


def test_add_and_track_document_lifecycle(isolated_db):
    item = isolated_db.add_item(title="Synthetic flat application")
    doc = isolated_db.add_document(item["id"], name="Synthetic ID copy")
    assert doc["state"] == "needed"
    assert doc["position"] == 1
    doc = isolated_db.update_document(item["id"], position=1, state="collected")
    assert doc["state"] == "collected"
    doc = isolated_db.update_document(item["id"], position=1, state="submitted")
    assert doc["state"] == "submitted"


def test_documents_attached_to_item(isolated_db):
    item = isolated_db.add_item(title="Synthetic registration")
    isolated_db.add_document(item["id"], name="Synthetic form A")
    isolated_db.add_document(item["id"], name="Synthetic certificate B", valid_until="2027-01-01T00:00:00+00:00")
    fetched = isolated_db.get_item(item["id"])
    assert [d["name"] for d in fetched["documents"]] == ["Synthetic form A", "Synthetic certificate B"]


def test_document_requires_name_and_existing_position(isolated_db):
    item = isolated_db.add_item(title="Synthetic")
    try:
        isolated_db.add_document(item["id"], name="  ")
        assert False, "expected ValueError"
    except ValueError:
        pass
    try:
        isolated_db.update_document(item["id"], position=9, state="collected")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_expired_state_is_sweep_only(isolated_db):
    item = isolated_db.add_item(title="Synthetic")
    isolated_db.add_document(item["id"], name="Synthetic cert")
    try:
        isolated_db.update_document(item["id"], position=1, state="expired")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_validity_sweep_expires_stale_documents(isolated_db):
    item = isolated_db.add_item(title="Synthetic application")
    isolated_db.add_document(item["id"], name="Stale synthetic cert", valid_until="2026-11-01T00:00:00+00:00")
    isolated_db.add_document(item["id"], name="Fresh synthetic cert", valid_until="2027-01-01T00:00:00+00:00")
    isolated_db.add_document(item["id"], name="No validity synthetic form")
    expired = isolated_db.expire_documents(now="2026-11-16T09:00:00+00:00")
    assert [d["name"] for d in expired] == ["Stale synthetic cert"]
    docs = isolated_db.get_item(item["id"])["documents"]
    states = {d["name"]: d["state"] for d in docs}
    assert states == {"Stale synthetic cert": "expired", "Fresh synthetic cert": "needed",
                      "No validity synthetic form": "needed"}


def test_expired_document_keeps_item_open(isolated_db):
    item = isolated_db.add_item(title="Synthetic application", status="open")
    isolated_db.add_document(item["id"], name="Synthetic cert", valid_until="2026-11-01T00:00:00+00:00")
    isolated_db.expire_documents(now="2026-11-16T09:00:00+00:00")
    assert isolated_db.get_item(item["id"])["status"] == "open"


def test_submitted_documents_do_not_expire(isolated_db):
    item = isolated_db.add_item(title="Synthetic application")
    isolated_db.add_document(item["id"], name="Synthetic cert", valid_until="2026-11-01T00:00:00+00:00")
    isolated_db.update_document(item["id"], position=1, state="submitted")
    assert isolated_db.expire_documents(now="2026-11-16T09:00:00+00:00") == []
    assert isolated_db.get_item(item["id"])["documents"][0]["state"] == "submitted"


def test_due_items_runs_document_sweep(isolated_db):
    item = isolated_db.add_item(title="Synthetic application")
    isolated_db.add_document(item["id"], name="Synthetic cert", valid_until="2026-11-01T00:00:00+00:00")
    isolated_db.due_items(now="2026-11-16T09:00:00+00:00")
    assert isolated_db.get_item(item["id"])["documents"][0]["state"] == "expired"


def test_tool_actions_and_list_line(isolated_db):
    import hermes_tingle
    added = hermes_tingle.handle_tool(None, {"action": "add", "title": "Synthetic bureaucracy goal"})
    import json
    item_id = json.loads(added)["item"]["id"]
    hermes_tingle.handle_tool(None, {"action": "add_document", "item_id": item_id,
                                     "document_name": "Synthetic form"})
    hermes_tingle.handle_tool(None, {"action": "add_document", "item_id": item_id,
                                     "document_name": "Synthetic cert", "valid_until": "2026-11-01T00:00:00+00:00"})
    result = json.loads(hermes_tingle.handle_tool(None, {"action": "update_document", "item_id": item_id,
                                                         "document_position": 1, "document_state": "submitted"}))
    assert result["document"]["state"] == "submitted"
    isolated_db.expire_documents(now="2026-11-16T09:00:00+00:00")
    listed = hermes_tingle.handle_command(None, "list")
    assert "📄 Documents: 1/2 submitted - 1 expired, re-collect" in listed
