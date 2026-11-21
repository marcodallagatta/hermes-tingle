"""Proactive same-case ask and natural-language yes/no routing fences.

A yes to the plain-language question only opens the interactive owner review; it never
confirms the merge. A clear no is recorded so the group is not proposed again; an
ambiguous answer is neither. No item, check, budget or source changes on either path.
"""
import json
from pathlib import Path

import pytest

import hermes_tingle as plugin
import hermes_tingle_store as store
import hermes_tingle_case_merge as cm
from test_case_merge import pair, proposal
from test_lookback import env


def test_daily_review_prompt_proactively_asks_and_fences_yes(tmp_path, synthetic_settings):
    import setup_jobs
    prompt = setup_jobs.job_specs(tmp_path)[0]["prompt"]
    assert "case_merge_guidance" in prompt
    assert "review a possible duplicate case privately" in prompt
    assert "do not include item titles, item IDs, proposal IDs, reasons or the review command" in prompt
    assert "open_case_merge_review" in prompt
    assert "only in a verified owner DM and fails closed elsewhere" in prompt
    assert "only opens the interactive owner review" in prompt
    assert "never by the yes" in prompt
    assert "decline_case_merge" in prompt
    assert "an ambiguous answer is not a decline" in prompt
    assert "Do not scan beyond the supplied context to hunt for merges" in prompt
    assert "Never change, complete or dismiss items to simulate a merge" in prompt


def test_model_guidance_routes_yes_to_preview_and_no_to_decline():
    text = cm.INSTRUCTIONS
    assert "plain language" in text
    assert "the yes to the question never confirms the merge itself" in text
    assert "decline_case_merge" in text
    assert "an ambiguous answer is not a decline" in text
    assert "authenticated paginated preview" in text


def test_skill_documents_proactive_ask_and_natural_language_routing():
    skill = (Path(__file__).resolve().parents[1] / "skills" / "usage" / "SKILL.md").read_text()
    assert "review a possible duplicate case privately" in skill
    assert "open_case_merge_review" in skill
    assert "never put item titles, item IDs, proposal IDs, reasons or the review command" in skill
    assert "never confirms the merge itself" in skill
    assert "decline_case_merge" in skill
    assert "an ambiguous answer is neither a yes nor a no" in skill


def test_decline_records_no_and_suppresses_reproposal_without_touching_items(env):
    rows = pair()
    before = [store.get_item(r["id"]) for r in rows]
    mid = proposal(rows)
    out = cm.decline(mid)
    assert out["state"] == "declined"
    after = [store.get_item(r["id"]) for r in rows]
    assert [r["status"] for r in after] == [r["status"] for r in before]
    assert [r["next_check_at"] for r in after] == [r["next_check_at"] for r in before]
    again = cm.propose([r["id"] for r in rows], rows[0]["id"], "Same current synthetic outcome")
    assert again["proposal_id"] == mid
    assert again["review_command"] is None
    assert "declined" in again["note"].lower()
    assert len(store.list_items()) == 2


def test_decline_via_tool_action(env):
    rows = pair()
    mid = proposal(rows)
    out = json.loads(plugin.handle_tool(env[1], {"action": "decline_case_merge", "proposal_id": mid}))
    assert out["success"] and out["state"] == "declined"
    assert len(store.list_items()) == 2


def test_decline_fails_closed_on_unknown_applied_or_repeated(env):
    rows = pair()
    with pytest.raises(ValueError):
        cm.decline("CM-DEADBEEF")
    mid = proposal(rows)
    cm.decline(mid)
    with pytest.raises(ValueError):
        cm.decline(mid)
    with pytest.raises(ValueError):
        cm.decline("not-an-id")
