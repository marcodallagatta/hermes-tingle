"""FT-01 end-to-end verification: the multi-step goal lifecycle, walked in order.

The roadmap marks FT-01 "installed, live workflow unverified". These tests run
the whole synthetic workflow - goal, ordered steps, reply-wait linkage, reply
arrival, guarded completion - through the public store API, the due-alert
renderer and /tingle, asserting what the user sees at each stage. Live inbox
and Telegram validation still need separate consent; this is the integrated
synthetic counterpart.
"""
from datetime import datetime, timezone

import pytest

import hermes_tingle_store as store
import hermes_tingle_sources as sources
import due_alerts
import hermes_tingle


def _render(now):
    return due_alerts.render_due_alerts(sources.all_due_items(now=now), get_item=store.get_item)


@pytest.fixture
def workflow():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    goal = store.add_item(title="Ship synthetic launch", source_type="manual",
                          due_at="2026-01-10T09:00:00+00:00",
                          next_check_at="2026-01-01T00:00:00+00:00", now=t0)
    wait = store.add_item(title="Await vendor reply", source_type="email",
                          source_ref="gmail:synthetic-thread#sent", status="waiting",
                          owner="other", completion_signal="source_email_reply_received",
                          next_check_at="2026-01-02T00:00:00+00:00", now=t0)
    store.add_step(goal["id"], title="Draft proposal", why="Needed before sending")
    store.add_step(goal["id"], title="Await vendor reply", why="Need their answer",
                   wait_item_id=wait["id"])
    store.add_step(goal["id"], title="Review outcome", why="Decide the next move")
    return goal, wait, t0


def test_multi_step_goal_lifecycle_end_to_end(workflow):
    goal, wait, t0 = workflow

    # Stage 1: the alert surfaces only the first incomplete step; later steps
    # and the reply-wait stay hidden until they are actionable.
    out = _render(t0)
    assert "Draft proposal" in out
    assert "Review outcome" not in out
    assert "Await vendor reply" not in out
    # /tingle list shows the goal's actionable step; the wait item appears as
    # its own Waiting row, not as something for the user to do.
    listing = hermes_tingle.handle_command(None, "list")
    assert "Next: Draft proposal" in listing
    assert "Next: Await vendor reply" not in listing

    # Guardrails hold from the start: no closing an unfinished goal, no
    # completing a wait-linked step directly, no skipping step order.
    with pytest.raises(ValueError, match="finish all goal steps"):
        store.complete_item(goal["id"])
    with pytest.raises(ValueError, match="linked reply-wait"):
        store.complete_step(goal["id"], 2)
    with pytest.raises(ValueError, match="in order"):
        store.complete_step(goal["id"], 3)

    # Stage 2: step 1 done; the alert now waits on the linked reply.
    store.complete_step(goal["id"], 1)
    out = _render(t0)
    assert "Waiting: `Await vendor reply`" in out
    assert "Review outcome" not in out
    assert "Next: Await vendor reply" in hermes_tingle.handle_command(None, "list")

    # Stage 3: the reply arrives (the terminal effect of the reply-check path).
    # The linked step completes, the goal does NOT: a reply never closes the
    # wider goal by itself.
    store.complete_item(wait["id"], now=datetime(2026, 1, 2, tzinfo=timezone.utc))
    goal_now = store.get_item(goal["id"])
    assert goal_now["status"] == "open"
    steps = goal_now["steps"]
    assert [s["complete"] for s in steps] == [True, True, False]
    out = _render(t0)
    assert "Review outcome" in out
    assert "Waiting:" not in out
    with pytest.raises(ValueError, match="finish all goal steps"):
        store.complete_item(goal["id"])

    # Stage 4: the final step completes, the goal closes, and the workflow
    # goes quiet - no more alerts, off the active list.
    store.complete_step(goal["id"], 3)
    closed = store.complete_item(goal["id"], now=datetime(2026, 1, 3, tzinfo=timezone.utc))
    assert closed["status"] == "done"
    assert "Ship synthetic launch" not in _render(t0)
    assert "Ship synthetic launch" not in hermes_tingle.handle_command(None, "list")


def test_wait_item_snoozed_mid_workflow_stays_hidden(workflow):
    goal, wait, t0 = workflow
    store.complete_step(goal["id"], 1)
    store.snooze_item(wait["id"], until="2099-01-01T00:00:00+00:00", now=t0)
    out = _render(t0)
    assert "Await vendor reply" not in out
    assert wait["id"] not in out
    assert goal["id"] in out


def test_dismissed_wait_item_leaves_step_unresolved_until_explicit_skip(workflow):
    # Dismissing the wait item ends the wait without the reply arriving; the
    # step is NOT complete ('done' is the only auto-completion signal), and the
    # goal stays blocked. The recovery path is an explicit skip - the user's
    # separate choice - which resolves the step without completing it and
    # never closes the goal by itself.
    goal, wait, t0 = workflow
    store.complete_step(goal["id"], 1)
    store.dismiss_item(wait["id"], now=datetime(2026, 1, 2, tzinfo=timezone.utc))
    steps = store.get_item(goal["id"])["steps"]
    assert steps[1]["complete"] is False and steps[1]["skipped"] is False
    with pytest.raises(ValueError, match="finish all goal steps"):
        store.complete_item(goal["id"])

    skipped = store.skip_step(goal["id"], 2)
    assert skipped["skipped"] is True and skipped["complete"] is False
    assert store.get_item(goal["id"])["status"] == "open"  # skipping never closes the goal

    # Ordering treats the skipped step as resolved: step 3 can complete,
    # and the goal can then close.
    store.complete_step(goal["id"], 3)
    closed = store.complete_item(goal["id"], now=datetime(2026, 1, 3, tzinfo=timezone.utc))
    assert closed["status"] == "done"
    # The skipped step remains distinguishable from completed work afterwards.
    assert [s["skipped"] for s in store.get_item(goal["id"])["steps"]] == [False, True, False]


def test_skipped_step_is_not_offered_as_the_next_action(workflow):
    goal, wait, t0 = workflow
    store.skip_step(goal["id"], 1)
    out = _render(t0)
    assert "Draft proposal" not in out
    listing = hermes_tingle.handle_command(None, "list")
    assert "Next: Draft proposal" not in listing
