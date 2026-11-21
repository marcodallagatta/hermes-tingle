"""FT-11 shopping: requirements, comparison options and approval-bound proposals."""
import json
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


def test_requirement_lifecycle(isolated_db):
    item = isolated_db.add_item(title="Synthetic backpack purchase")
    req = isolated_db.add_requirement(item["id"], text="Fits a synthetic 15-inch laptop", must=True)
    assert req["must"] == 1 and req["met"] == 0 and req["position"] == 1
    req = isolated_db.update_requirement(item["id"], position=1, met=True)
    assert req["met"] == 1
    req = isolated_db.update_requirement(item["id"], position=1, must=False)
    assert req["must"] == 0


def test_requirements_attached_to_item(isolated_db):
    item = isolated_db.add_item(title="Synthetic")
    isolated_db.add_requirement(item["id"], text="Synthetic requirement A", must=True)
    isolated_db.add_requirement(item["id"], text="Synthetic requirement B")
    fetched = isolated_db.get_item(item["id"])
    assert [r["text"] for r in fetched["requirements"]] == ["Synthetic requirement A", "Synthetic requirement B"]


def test_requirement_requires_text_and_existing_position(isolated_db):
    item = isolated_db.add_item(title="Synthetic")
    try:
        isolated_db.add_requirement(item["id"], text="  ")
        assert False, "expected ValueError"
    except ValueError:
        pass
    try:
        isolated_db.update_requirement(item["id"], position=9, met=True)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_option_comparison_lifecycle(isolated_db):
    item = isolated_db.add_item(title="Synthetic lamp purchase")
    opt = isolated_db.add_option(item["id"], name="Synthetic lamp A", detail="warm light", price_text="EUR 49 at example.invalid")
    assert opt["state"] == "candidate" and opt["position"] == 1
    opt = isolated_db.update_option(item["id"], position=1, state="shortlisted")
    assert opt["state"] == "shortlisted"
    opt = isolated_db.update_option(item["id"], position=1, state="rejected", note="synthetic reason")
    assert opt["state"] == "rejected"


def test_proposed_state_is_proposal_only(isolated_db):
    item = isolated_db.add_item(title="Synthetic")
    isolated_db.add_option(item["id"], name="Synthetic option")
    try:
        isolated_db.update_option(item["id"], position=1, state="proposed")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_propose_option_records_approval_and_never_buys(isolated_db):
    item = isolated_db.add_item(title="Synthetic chair purchase")
    isolated_db.add_option(item["id"], name="Synthetic chair A", price_text="EUR 189 at example.invalid")
    result = isolated_db.propose_option(item["id"], position=1,
                                        approver=("telegram", "424242", "1001"),
                                        requested_by="synthetic test",
                                        now="2026-11-16T09:00:00+00:00")
    assert result["option"]["state"] == "proposed"
    approval = result["approval"]
    assert approval["action_type"] == "purchase"
    assert approval["status"] == "pending"
    assert approval["approver_user_id"] == "424242"
    params = json.loads(approval["parameters_json"])
    assert params == {"option_position": 1, "option_name": "Synthetic chair A",
                      "price_text": "EUR 189 at example.invalid"}
    # Approval decides the recorded request only; the option state is untouched.
    decided = isolated_db.decide_approval(approval["id"], "approved",
                                          evidence="synthetic /tingle approve",
                                          decided_by=("telegram", "424242", "1001"),
                                          now="2026-11-16T10:00:00+00:00")
    assert decided["status"] == "approved"
    assert isolated_db.get_item(item["id"])["options"][0]["state"] == "proposed"


def test_propose_supersedes_earlier_pending_purchase_approval(isolated_db):
    item = isolated_db.add_item(title="Synthetic")
    isolated_db.add_option(item["id"], name="Synthetic option A")
    isolated_db.add_option(item["id"], name="Synthetic option B")
    first = isolated_db.propose_option(item["id"], position=1, now="2026-11-16T09:00:00+00:00")["approval"]
    second = isolated_db.propose_option(item["id"], position=2, now="2026-11-16T09:05:00+00:00")["approval"]
    assert first["id"] != second["id"]
    approvals = {a["id"]: a["status"] for a in isolated_db.list_approvals(limit=10,
                                                                          now="2026-11-16T09:06:00+00:00")}
    assert approvals[first["id"]] == "expired"
    assert approvals[second["id"]] == "pending"


def test_propose_rejects_wrong_state_and_position(isolated_db):
    item = isolated_db.add_item(title="Synthetic")
    isolated_db.add_option(item["id"], name="Synthetic option")
    isolated_db.update_option(item["id"], position=1, state="rejected")
    try:
        isolated_db.propose_option(item["id"], position=1)
        assert False, "expected ValueError"
    except ValueError:
        pass
    try:
        isolated_db.propose_option(item["id"], position=9)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_tool_actions_and_list_line(isolated_db):
    import hermes_tingle
    added = hermes_tingle.handle_tool(None, {"action": "add", "title": "Synthetic monitor purchase"})
    item_id = json.loads(added)["item"]["id"]
    hermes_tingle.handle_tool(None, {"action": "add_requirement", "item_id": item_id,
                                     "requirement_text": "Synthetic 4k panel", "must": True})
    hermes_tingle.handle_tool(None, {"action": "add_option", "item_id": item_id,
                                     "option_name": "Synthetic monitor A", "price_text": "EUR 229 at example.invalid"})
    hermes_tingle.handle_tool(None, {"action": "add_option", "item_id": item_id,
                                     "option_name": "Synthetic monitor B"})
    result = json.loads(hermes_tingle.handle_tool(None, {"action": "update_option", "item_id": item_id,
                                                         "option_position": 1, "option_state": "shortlisted"}))
    assert result["option"]["state"] == "shortlisted"
    proposed = json.loads(hermes_tingle.handle_tool(None, {"action": "propose_option", "item_id": item_id,
                                                           "option_position": 1}))
    assert proposed["option"]["state"] == "proposed"
    assert proposed["approval"]["status"] == "pending"
    listed = hermes_tingle.handle_command(None, "list")
    assert "🛒 Options: 0 shortlisted, 1 proposed of 2 - 1 must-have(s) unmet" in listed
