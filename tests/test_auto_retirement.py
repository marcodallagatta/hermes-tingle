from datetime import datetime, timedelta, timezone

import pytest

import hermes_tingle_store as store
import source_snapshot


NOW = datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "kwargs,expected_class,expected_close",
    [
        (
            {"due_at": NOW + timedelta(days=10)},
            "deadline",
            NOW + timedelta(days=17),
        ),
        (
            {"status": "waiting", "owner": "other"},
            "waiting",
            NOW + timedelta(days=270),
        ),
        (
            {},
            "default",
            NOW + timedelta(days=90),
        ),
    ],
)
def test_new_items_get_deterministic_auto_close_policy(kwargs, expected_class, expected_close):
    item = store.add_item(title="Synthetic retirement case", now=NOW, **kwargs)

    assert _policy(item["id"])["retirement_class"] == expected_class
    assert _policy(item["id"])["last_activity_at"] == NOW.isoformat(timespec="seconds")
    assert _policy(item["id"])["auto_close_at"] == expected_close.isoformat(timespec="seconds")


def _policy(item_id):
    state = store.retirement_state(item_id)
    assert state is not None
    return state


def _sql_update(item_id, **values):
    policy_names = {
        "retirement_class": "retirement_class",
        "retirement_last_activity_at": "last_activity_at",
        "auto_close_at": "auto_close_at",
        "retirement_evidence_key": "evidence_key",
    }
    policy_values = {policy_names[key]: values.pop(key) for key in list(values) if key in policy_names}
    connection = store._connect()
    try:
        if values:
            fields = ", ".join(f"{name}=?" for name in values)
            connection.execute(
                f"UPDATE hermes_tingle_items SET {fields} WHERE id=?",
                (*values.values(), item_id),
            )
        if policy_values:
            fields = ", ".join(f"{name}=?" for name in policy_values)
            connection.execute(
                f"UPDATE hermes_tingle_retirement SET {fields} WHERE item_id=?",
                (*policy_values.values(), item_id),
            )
        connection.commit()
    finally:
        connection.close()


def test_legacy_backfill_uses_creation_not_automatic_updated_timestamp():
    created = NOW - timedelta(days=400)
    updated = created + timedelta(days=350)
    item = store.add_item(title="Synthetic legacy item", now=created)
    _sql_update(
        item["id"],
        created_at=created.isoformat(timespec="seconds"),
        updated_at=updated.isoformat(timespec="seconds"),
        retirement_class="",
        retirement_last_activity_at=None,
        auto_close_at=None,
    )

    closed = store.auto_close_stale_items(now=NOW)

    assert closed == [item["id"]]
    result = store.get_item(item["id"])
    assert result["status"] == "auto_closed"
    assert _policy(item["id"])["retirement_class"] == "default"
    assert _policy(item["id"])["last_activity_at"] == created.isoformat(timespec="seconds")
    assert _policy(item["id"])["auto_close_at"] == (created + timedelta(days=90)).isoformat(timespec="seconds")


def test_legacy_backfill_deadline_is_stable_across_daily_audits():
    created = NOW - timedelta(days=10)
    item = store.add_item(title="Synthetic pending legacy item", now=created)
    _sql_update(item["id"], retirement_class="", retirement_last_activity_at=None, auto_close_at=None)

    assert store.auto_close_stale_items(now=NOW, protected_ids={item["id"]}) == []
    first_close_at = _policy(item["id"])["auto_close_at"]
    assert store.auto_close_stale_items(now=NOW + timedelta(days=1), protected_ids={item["id"]}) == []
    second = store.get_item(item["id"])

    assert _policy(item["id"])["retirement_class"] == "default"
    assert _policy(item["id"])["last_activity_at"] == created.isoformat(timespec="seconds")
    assert _policy(item["id"])["auto_close_at"] == first_close_at


def test_absolute_one_year_cap_overrides_recent_followup():
    created = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=400)
    item = store.add_item(title="Synthetic ongoing item", now=created)

    updated = store.update_item(item["id"], summary="Recent real progress")

    assert _policy(updated["id"])["last_activity_at"] > created.isoformat(timespec="seconds")
    assert _policy(updated["id"])["auto_close_at"] == (created + timedelta(days=365)).isoformat(timespec="seconds")
    closed = store.auto_close_stale_items(now=datetime.now(timezone.utc), protected_ids={item["id"]})
    assert closed == [item["id"]]


def test_recurring_completion_cannot_advance_past_one_year_tracking_cap():
    created = NOW - timedelta(days=400)
    due = created + timedelta(days=30)
    item = store.add_item(
        title="Synthetic recurring item", due_at=due, next_check_at=due,
        recur_interval=1, recur_unit="day", now=created,
    )

    completed = store.complete_item(item["id"], now=NOW)

    assert completed["status"] == "done"
    assert completed["due_at"] == due.isoformat(timespec="seconds")


def test_deadline_item_waits_for_three_reserved_reminders_unless_hard_cap():
    created = NOW - timedelta(days=30)
    due = NOW - timedelta(days=8)
    item = store.add_item(title="Synthetic expiring access", due_at=due, now=created)
    _sql_update(item["id"], reminder_attempt_count=2)

    assert store.auto_close_stale_items(now=NOW) == []
    assert store.get_item(item["id"])["status"] == "open"

    _sql_update(item["id"], reminder_attempt_count=3)
    assert store.auto_close_stale_items(now=NOW) == [item["id"]]
    assert store.get_item(item["id"])["status"] == "auto_closed"


def test_current_audit_evidence_protects_item_from_retirement():
    created = NOW - timedelta(days=300)
    item = store.add_item(title="Synthetic source-backed wait", status="waiting", owner="other", now=created)

    assert store.auto_close_stale_items(
        now=NOW, protected_ids={item["id"]}, protected_evidence={item["id"]: "source-v1"}
    ) == []
    assert store.get_item(item["id"])["status"] == "waiting"
    assert store.auto_close_stale_items(
        now=NOW + timedelta(seconds=1), protected_ids={item["id"]},
        protected_evidence={item["id"]: "source-v1"},
    ) == [item["id"]]


def test_reminder_reservation_does_not_reset_meaningful_activity():
    item = store.add_item(title="Synthetic reminder", now=NOW)
    activity_at = _policy(item["id"])["last_activity_at"]

    store.mark_notified([item["id"]], now=NOW + timedelta(days=8))

    assert _policy(item["id"])["last_activity_at"] == activity_at


def test_meaningful_owner_update_resets_inactivity_deadline(monkeypatch):
    item = store.add_item(title="Synthetic progressing item", now=NOW)
    update_time = NOW + timedelta(days=45)
    monkeypatch.setattr(store, "_now", lambda value=None: update_time)

    updated = store.update_item(item["id"], summary="Progress was confirmed")

    assert _policy(updated["id"])["last_activity_at"] == update_time.isoformat(timespec="seconds")
    assert _policy(updated["id"])["auto_close_at"] == (update_time + timedelta(days=90)).isoformat(timespec="seconds")


def test_auto_closed_item_stays_in_history_and_requires_explicit_reopen(monkeypatch):
    created = NOW - timedelta(days=100)
    item = store.add_item(title="Synthetic stale item", now=created)
    assert store.auto_close_stale_items(now=NOW) == [item["id"]]
    assert [row["id"] for row in store.list_items(status="auto_closed")] == [item["id"]]
    with pytest.raises(ValueError, match="explicitly set an active status"):
        store.update_item(item["id"], summary="Changed without reopening")

    reopen_time = NOW + timedelta(days=1)
    monkeypatch.setattr(store, "_now", lambda value=None: reopen_time)
    reopened = store.update_item(item["id"], status="open", summary="Still active")

    assert reopened["status"] == "open"
    assert reopened["completed_at"] is None
    assert reopened["next_check_at"] is not None
    assert reopened["reminder_attempt_count"] == 0
    assert _policy(reopened["id"])["last_activity_at"] == reopen_time.isoformat(timespec="seconds")
    assert store.list_items(status="auto_closed") == []
    assert store.list_items(status="active")[0]["id"] == item["id"]


def test_structured_step_progress_resets_stale_item_leash():
    created = NOW - timedelta(days=100)
    item = store.add_item(title="Synthetic goal", now=created)

    store.add_step(item["id"], title="Collect reply", why="The case depends on it")

    updated = store.get_item(item["id"])
    assert updated is not None
    assert _policy(updated["id"])["last_activity_at"] > created.isoformat(timespec="seconds")
    assert _policy(updated["id"])["auto_close_at"] > NOW.isoformat(timespec="seconds")


def test_auto_closed_wait_resolves_linked_step_without_completing_it():
    goal = store.add_item(title="Synthetic parent goal", now=NOW - timedelta(days=1))
    wait = store.add_item(
        title="Synthetic old reply wait", status="waiting", owner="other", source_type="email",
        source_ref="gmail:thread#sent", completion_signal="source_email_reply_received",
        next_check_at=NOW - timedelta(days=300), now=NOW - timedelta(days=300),
    )
    store.add_step(goal["id"], title="Receive the reply", why="Only then proceed", wait_item_id=wait["id"])
    assert _policy(goal["id"])["retirement_class"] == "waiting"

    assert store.auto_close_stale_items(now=NOW) == [wait["id"]]
    step = store.get_item(goal["id"])["steps"][0]
    assert step["complete"] is False
    assert step["skipped"] is True
    assert step["resolved"] is True
    reopened = store.snooze_item(wait["id"], until=NOW + timedelta(days=5), now=NOW)
    assert reopened["status"] == "waiting"
    assert reopened["owner"] == "other"
    assert reopened["next_check_at"] == (NOW + timedelta(days=5)).isoformat(timespec="seconds")
    assert reopened["reminder_attempt_count"] == 0


def test_deadline_close_is_fixed_to_due_date_even_after_owner_activity(monkeypatch):
    created = NOW - timedelta(days=30)
    due = NOW - timedelta(days=8)
    item = store.add_item(title="Synthetic deadline", due_at=due, now=created)
    _sql_update(item["id"], reminder_attempt_count=3)
    activity_time = NOW - timedelta(days=1)
    monkeypatch.setattr(store, "_now", lambda value=None: activity_time)

    updated = store.update_item(item["id"], summary="Recent edit")

    assert _policy(updated["id"])["auto_close_at"] == (due + timedelta(days=7)).isoformat(timespec="seconds")
    assert store.auto_close_stale_items(now=NOW) == [item["id"]]


def test_due_date_only_edit_recalculates_deadline_without_resetting_activity(monkeypatch):
    created = NOW - timedelta(days=30)
    old_due = NOW + timedelta(days=20)
    new_due = NOW + timedelta(days=40)
    item = store.add_item(title="Synthetic deadline shift", due_at=old_due, now=created)
    monkeypatch.setattr(store, "_now", lambda value=None: NOW)

    updated = store.update_item(item["id"], due_at=new_due)

    assert _policy(updated["id"])["last_activity_at"] == created.isoformat(timespec="seconds")
    assert _policy(updated["id"])["auto_close_at"] == (new_due + timedelta(days=7)).isoformat(timespec="seconds")


def test_unchanged_source_evidence_only_defers_retirement_once():
    created = NOW - timedelta(days=300)
    item = store.add_item(title="Synthetic stale wait", status="waiting", owner="other", now=created)
    key = "stable-source-evidence"

    assert store.auto_close_stale_items(now=NOW, protected_ids={item["id"]},
                                        protected_evidence={item["id"]: key}) == []
    assert store.auto_close_stale_items(now=NOW + timedelta(seconds=1), protected_ids={item["id"]},
                                        protected_evidence={item["id"]: key}) == [item["id"]]


def test_checkpoint_evidence_key_ignores_changing_receipt_nonce():
    import hermes_tingle_sources as sources
    base = {
        "email_checkpoint_checks": [{
            "item_id": "HT-AAAAAA", "state": "thread_changed", "receipt": "nonce-one",
            "messages": [{"source_id": "message-1", "date": "1730000000000", "direction": "received"}],
        }],
    }
    changed_nonce = {
        "email_checkpoint_checks": [{
            **base["email_checkpoint_checks"][0], "receipt": "nonce-two",
        }],
    }

    assert sources._retirement_evidence(base) == sources._retirement_evidence(changed_nonce)


def test_alternating_seen_source_evidence_cannot_defer_close_twice():
    created = NOW - timedelta(days=300)
    item = store.add_item(title="Synthetic alternating evidence", status="waiting", owner="other", now=created)

    assert store.auto_close_stale_items(now=NOW, protected_ids={item["id"]},
                                        protected_evidence={item["id"]: "evidence-A"}) == []
    assert store.auto_close_stale_items(now=NOW + timedelta(seconds=1), protected_ids={item["id"]},
                                        protected_evidence={item["id"]: "evidence-B"}) == []
    assert store.auto_close_stale_items(now=NOW + timedelta(seconds=2), protected_ids={item["id"]},
                                        protected_evidence={item["id"]: "evidence-A"}) == [item["id"]]


def test_chat_evidence_identity_includes_chat_and_role():
    import hermes_tingle_sources as sources
    common = {"item_ids": ["HT-AAAAAA"], "receipt": "same-receipt", "timestamp": 123.0}
    first = {"chat_review": {"matches": [{**common, "chat_ref": "telegram:one", "role": "user"}]}}
    second = {"chat_review": {"matches": [{**common, "chat_ref": "telegram:two", "role": "assistant"}]}}

    assert sources._retirement_evidence(first)[1]["HT-AAAAAA"] != sources._retirement_evidence(second)[1]["HT-AAAAAA"]


def test_chat_evidence_identity_ignores_expiring_review_receipt():
    import hermes_tingle_sources as sources

    first = {"chat_review": {"matches": [{
        "item_ids": ["HT-AAAAAA"], "chat_ref": "telegram:one", "role": "user",
        "timestamp": 1730000000.25, "receipt": "nonce-one",
    }]}}
    second = {"chat_review": {"matches": [{
        "item_ids": ["HT-AAAAAA"], "chat_ref": "telegram:one", "role": "user",
        "timestamp": 1730000000.25, "receipt": "nonce-two",
    }]}}

    assert sources._retirement_evidence(first) == sources._retirement_evidence(second)


def test_linked_parent_policy_is_decided_before_wait_auto_close(monkeypatch):
    ids = iter(("000002", "000001"))
    monkeypatch.setattr(store.secrets, "token_hex", lambda _count: next(ids))
    created = NOW - timedelta(days=300)
    parent = store.add_item(title="Synthetic old parent", now=created)
    wait = store.add_item(
        title="Synthetic linked reply wait", status="waiting", owner="other", source_type="email",
        source_ref="gmail:synthetic#sent", completion_signal="source_email_reply_received",
        next_check_at=NOW + timedelta(days=1), now=created,
    )
    store.add_step(parent["id"], title="Receive reply", why="Required", wait_item_id=wait["id"])
    _sql_update(parent["id"], retirement_class="default",
                retirement_last_activity_at=created.isoformat(timespec="seconds"),
                auto_close_at=(created + timedelta(days=90)).isoformat(timespec="seconds"))

    closed = store.auto_close_stale_items(now=NOW)

    assert set(closed) == {parent["id"], wait["id"]}
    result = store.get_item(parent["id"])
    assert result is not None
    assert _policy(parent["id"])["retirement_class"] == "waiting"
    assert _policy(parent["id"])["auto_close_at"] == (created + timedelta(days=270)).isoformat(timespec="seconds")


def test_parent_at_hard_cap_closes_a_younger_linked_wait_together():
    parent = store.add_item(title="Synthetic capped parent", now=NOW - timedelta(days=400))
    wait = store.add_item(
        title="Synthetic younger reply wait", status="waiting", owner="other", source_type="email",
        source_ref="gmail:synthetic-younger/thread#sent", completion_signal="source_email_reply_received",
        next_check_at=NOW + timedelta(days=5), now=NOW - timedelta(days=10),
    )
    store.add_step(parent["id"], title="Receive reply", why="Required", wait_item_id=wait["id"])

    assert set(store.auto_close_stale_items(now=NOW)) == {parent["id"], wait["id"]}
    parent_after = store.get_item(parent["id"])
    wait_after = store.get_item(wait["id"])
    assert parent_after is not None and parent_after["status"] == "auto_closed"
    assert wait_after is not None and wait_after["status"] == "auto_closed"
    assert parent_after["steps"][0]["resolved"] is True


def test_retired_chat_match_is_removed_from_the_same_review_snapshot():
    import hermes_tingle_sources as sources

    item = store.add_item(title="Synthetic old chat item", now=NOW - timedelta(days=400))
    snapshot = {"chat_review": {"matches": [{"item_ids": [item["id"], "HT-BBBBBB"], "receipt": "synthetic"}]}}

    sources._apply_retirement(snapshot, now=NOW)

    assert store.get_item(item["id"])["status"] == "auto_closed"
    assert snapshot["chat_review"]["matches"] == []


def test_item_and_case_merge_fingerprints_ignore_retirement_metadata():
    import hermes_tingle_case_merge as case_merge
    item = store.add_item(title="Synthetic fingerprint item", now=NOW)
    conn = store._connect()
    try:
        item_before = store._onboarding_fingerprint(conn, item["id"])
        rows_before = case_merge._rows(conn, [item["id"]])
        case_before = case_merge._fingerprint(rows_before)
        conn.execute("UPDATE hermes_tingle_retirement SET retirement_class='waiting', last_activity_at=?, auto_close_at=?, evidence_key=? WHERE item_id=?",
                     ((NOW + timedelta(days=1)).isoformat(), (NOW + timedelta(days=2)).isoformat(), '["synthetic-key"]', item["id"]))
        conn.commit()
        assert store._onboarding_fingerprint(conn, item["id"]) == item_before
        assert case_merge._fingerprint(case_merge._rows(conn, [item["id"]])) == case_before
    finally:
        conn.close()


def test_retirement_policy_state_never_leaks_into_item_or_agent_payloads():
    import json
    import hermes_tingle

    item = store.add_item(title="Synthetic private lifecycle", now=NOW)
    row = store.get_item(item["id"])
    assert row is not None
    assert store.retirement_state(item["id"]) is not None
    tool_payload = hermes_tingle._json({"item": row})
    shared_payload = source_snapshot.build_agent_input({"due_items": [row], "errors": []})
    encoded = json.dumps({"tool": json.loads(tool_payload), "shared": json.loads(shared_payload)})

    for field in ("retirement_class", "last_activity_at", "auto_close_at", "evidence_key"):
        assert field not in row
        assert field not in encoded


def test_reopening_linked_wait_under_closed_parent_is_blocked():
    parent = store.add_item(title="Synthetic closed parent", now=NOW - timedelta(days=100))
    wait = store.add_item(
        title="Synthetic closed wait", status="waiting", owner="other", source_type="email",
        source_ref="gmail:synthetic#sent", completion_signal="source_email_reply_received",
        next_check_at=NOW - timedelta(days=10), now=NOW - timedelta(days=100),
    )
    store.add_step(parent["id"], title="Wait for reply", why="Dependency", wait_item_id=wait["id"])
    _sql_update(parent["id"], status="dismissed")
    _sql_update(wait["id"], status="auto_closed")

    with pytest.raises(ValueError, match="closed goal"):
        store.snooze_item(wait["id"], until=NOW + timedelta(days=3), now=NOW)


def test_update_reopen_linked_wait_under_closed_parent_is_blocked():
    parent = store.add_item(title="Synthetic closed parent", now=NOW - timedelta(days=100))
    wait = store.add_item(
        title="Synthetic closed reply wait", status="waiting", owner="other", source_type="email",
        source_ref="gmail:synthetic#sent", completion_signal="source_email_reply_received",
        next_check_at=NOW - timedelta(days=10), now=NOW - timedelta(days=100),
    )
    store.add_step(parent["id"], title="Wait for reply", why="Dependency", wait_item_id=wait["id"])
    _sql_update(parent["id"], status="dismissed")
    _sql_update(wait["id"], status="auto_closed")

    with pytest.raises(ValueError, match="closed goal"):
        store.update_item(wait["id"], status="waiting")


def test_update_reopen_reply_wait_requires_waiting_on_other():
    wait = store.add_item(
        title="Synthetic reply wait", status="waiting", owner="other", source_type="email",
        source_ref="gmail:synthetic#sent", completion_signal="source_email_reply_received",
        next_check_at=NOW + timedelta(days=3), now=NOW - timedelta(days=100),
    )
    _sql_update(wait["id"], status="auto_closed")

    with pytest.raises(ValueError, match="reopen a reply-wait as waiting"):
        store.update_item(wait["id"], status="open")


def test_review_receipt_binding_ignores_retirement_lifecycle_changes():
    import hermes_tingle_review as review
    item = store.add_item(title="Synthetic chat review item", now=NOW)
    store.enable_source("chat", enabled_by="synthetic review fixture")
    conn = store._connect()
    try:
        scope = {"item_ids": [item["id"]], "chat_ref": "telegram:synthetic"}
        before = review.binding(conn, "chat", scope)
        conn.execute("UPDATE hermes_tingle_retirement SET retirement_class='waiting',last_activity_at=?,auto_close_at=?,evidence_key=? WHERE item_id=?",
                     (NOW.isoformat(), (NOW + timedelta(days=1)).isoformat(), '["synthetic"]', item["id"]))
        conn.commit()
        assert review.binding(conn, "chat", scope) == before
    finally:
        conn.close()


def test_source_purge_clears_retirement_evidence_hashes():
    item = store.add_item(title="Synthetic purge item", now=NOW)
    _sql_update(item["id"], retirement_evidence_key='["content-free-hash"]')
    assert _policy(item["id"])["evidence_key"]

    store.purge_source_retained("chat")

    assert _policy(item["id"])["evidence_key"] == ""

    store.enable_source("chat", enabled_by="synthetic revoke fixture")
    _sql_update(item["id"], retirement_evidence_key='["revocation-hash"]')
    store.disable_source("chat", note="synthetic revoke")

    assert _policy(item["id"])["evidence_key"] == ""


def test_noop_structured_update_does_not_reset_inactivity(monkeypatch):
    item = store.add_item(title="Synthetic unchanged document", now=NOW)
    document = store.add_document(item["id"], name="Synthetic form")
    old_activity = NOW - timedelta(days=20)
    _sql_update(item["id"], retirement_last_activity_at=old_activity.isoformat(timespec="seconds"))
    monkeypatch.setattr(store, "_now", lambda value=None: NOW + timedelta(days=30))

    store.update_document(item["id"], position=document["position"], state="needed")

    assert _policy(item["id"])["last_activity_at"] == old_activity.isoformat(timespec="seconds")


def test_unchanged_owner_update_does_not_reset_inactivity(monkeypatch):
    item = store.add_item(title="Synthetic unchanged update", summary="Same summary", now=NOW)
    later = NOW + timedelta(days=20)
    monkeypatch.setattr(store, "_now", lambda value=None: later)

    updated = store.update_item(item["id"], summary="Same summary")

    assert _policy(updated["id"])["last_activity_at"] == NOW.isoformat(timespec="seconds")


def test_skipped_linked_wait_does_not_keep_parent_in_waiting_class():
    parent = store.add_item(title="Synthetic skipped-wait parent", now=NOW)
    wait = store.add_item(
        title="Synthetic reply wait", status="waiting", owner="other", source_type="email",
        source_ref="gmail:synthetic#sent", completion_signal="source_email_reply_received",
        next_check_at=NOW + timedelta(days=3), now=NOW,
    )
    store.add_step(parent["id"], title="Receive reply", why="Dependency", wait_item_id=wait["id"])
    store.skip_step(parent["id"], 1)

    result = store.get_item(parent["id"])
    assert result is not None
    assert _policy(parent["id"])["retirement_class"] == "default"


def test_reopening_an_expired_auto_closed_item_is_blocked_by_one_year_cap():
    created = NOW - timedelta(days=400)
    item = store.add_item(title="Synthetic capped item", now=created)
    assert store.auto_close_stale_items(now=NOW) == [item["id"]]

    with pytest.raises(ValueError, match="one-year tracking cap"):
        store.update_item(item["id"], status="open")
    with pytest.raises(ValueError, match="one-year tracking cap"):
        store.snooze_item(item["id"], until=NOW + timedelta(days=1), now=NOW)


def test_checkpoint_reopen_preserves_contextual_review_plan():
    plan = dict(actor="other", outcome="Receive the signed form", basis="inferred",
                review_at=(NOW - timedelta(days=30)).isoformat(), review_needed=False)
    created = NOW - timedelta(days=300)
    item = store.add_item(title="Synthetic checkpoint", source_type="email",
        source_ref="gmail:a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf/thread#anchor", status="waiting", owner="other",
        completion_signal="source_email_checkpoint", review_plan=plan,
        next_check_at=NOW - timedelta(days=30), now=created)
    original_plan = item["email_review_plan"]
    assert store.auto_close_stale_items(now=NOW) == [item["id"]]

    reopened = store.update_item(item["id"], status="waiting")

    assert reopened["email_review_plan"] == original_plan
    assert reopened["next_check_at"] == plan["review_at"]


def test_update_reopen_actual_retired_reply_wait_with_fresh_check():
    created = NOW - timedelta(days=300)
    wait = store.add_item(
        title="Synthetic retired reply wait", status="waiting", owner="other", source_type="email",
        source_ref="gmail:synthetic/thread#sent", completion_signal="source_email_reply_received",
        next_check_at=NOW - timedelta(days=20), now=created,
    )
    assert store.auto_close_stale_items(now=NOW) == [wait["id"]]

    reopened = store.update_item(wait["id"], status="waiting", next_check_at=NOW + timedelta(days=5))

    assert reopened["status"] == "waiting"
    assert reopened["owner"] == "other"
    assert reopened["next_check_at"] == (NOW + timedelta(days=5)).isoformat(timespec="seconds")


def test_retirement_policy_is_absent_from_shared_review_input():
    import json

    item = store.add_item(title="Synthetic shared item", now=NOW)
    row = store.get_item(item["id"])
    assert row is not None
    payload = json.loads(source_snapshot.build_agent_input({"due_items": [row], "errors": []}))
    encoded = json.dumps(payload)

    assert item["id"] in encoded
    for field in ("retirement_class", "last_activity_at", "auto_close_at", "evidence_key"):
        assert field not in encoded


def test_auto_closed_status_is_available_only_as_a_history_filter():
    import schemas

    list_status = schemas.HERMES_TINGLE["parameters"]["properties"]["list_status"]["enum"]
    assert "auto_closed" in list_status
    assert "auto_closed" not in store._ACTIVE_STATUSES


def test_invalid_configuration_blocks_soft_retirement_but_enforces_hard_cap(monkeypatch):
    import hermes_tingle_sources as sources
    item = store.add_item(title="Synthetic invalid-config item", now=NOW - timedelta(days=100))
    capped = store.add_item(title="Synthetic invalid-config capped item", now=NOW - timedelta(days=400))

    def invalid_settings():
        raise ValueError("synthetic invalid profile configuration")

    def unexpected_source_read(**_kwargs):
        pytest.fail("invalid configuration must prevent every source read")

    monkeypatch.setattr(sources, "load_settings", invalid_settings)
    monkeypatch.setattr(sources, "_collect_chat_matches", unexpected_source_read)
    monkeypatch.setattr(sources, "_collect_email_candidates", unexpected_source_read)
    snapshot = sources.collect_snapshot(now=NOW, discovery_enabled=True)

    assert snapshot["errors"]
    result = store.get_item(item["id"])
    capped_result = store.get_item(capped["id"])
    assert result is not None and result["status"] == "open"
    assert capped_result is not None and capped_result["status"] == "auto_closed"
    assert _policy(item["id"])["auto_close_at"] is not None


def test_incomplete_source_review_blocks_soft_close_but_not_the_hard_cap():
    import hermes_tingle_sources as sources

    item = store.add_item(title="Synthetic source-incomplete stale item", now=NOW - timedelta(days=100))
    snapshot = {
        "errors": ["Gmail: read failed"],
        "email_sync": {"enabled": True, "review_complete": False},
        "chat_review": {"enabled": False, "review_complete": False},
        "calendar_review": {"enabled": False, "review_complete": False},
    }

    sources._apply_retirement(snapshot, now=NOW)
    item_after = store.get_item(item["id"])
    assert item_after is not None and item_after["status"] == "open"

    closed = store.auto_close_stale_items(now=NOW + timedelta(days=301), allow_soft_close=False)
    assert closed == [item["id"]]
    item_after = store.get_item(item["id"])
    assert item_after is not None and item_after["status"] == "auto_closed"


def test_scan_sources_tool_does_not_run_retirement(monkeypatch):
    import json
    import hermes_tingle

    observed = {}

    def collect(**kwargs):
        observed.update(kwargs)
        return {"errors": []}

    monkeypatch.setattr(hermes_tingle.sources, "collect_snapshot", collect)
    response = json.loads(hermes_tingle.handle_tool(None, {"action": "scan_sources"}))

    assert response["success"] is True
    assert observed.get("discovery_enabled", False) is False


def test_copied_daily_helper_invalid_config_does_not_retire_or_crash(monkeypatch, capsys):
    from pathlib import Path
    import hermes_tingle_config as config
    import hermes_tingle_discovery as discovery
    import hermes_tingle_discovery_assess as assess
    import hermes_tingle_sources as sources
    import hermes_tingle_wiring as wiring

    audit_now = datetime.now(timezone.utc).replace(microsecond=0)
    item = store.add_item(title="Synthetic old reminder", now=audit_now - timedelta(days=100))

    def invalid_settings():
        raise ValueError("synthetic invalid profile configuration")

    monkeypatch.setattr(source_snapshot, "_plugin_root", lambda: Path(__file__).resolve().parents[1])
    monkeypatch.setattr(sources, "load_settings", invalid_settings)
    monkeypatch.setattr(config, "local_timezone", invalid_settings)
    monkeypatch.setattr(discovery, "stage_local_candidates", lambda _snapshot: None)
    monkeypatch.setattr(assess, "evaluate", lambda _snapshot: None)
    monkeypatch.setattr(wiring, "wire_from_runtime", lambda: None)

    assert source_snapshot.main() == 0

    output = capsys.readouterr().out
    assert store.get_item(item["id"])["status"] == "open"
    assert item["id"] not in output
    assert '"source_snapshot"' in output


def test_copied_daily_helper_runs_retirement_on_valid_config(monkeypatch, capsys):
    from pathlib import Path
    import hermes_tingle_discovery as discovery
    import hermes_tingle_discovery_assess as assess
    import hermes_tingle_sources as sources
    import hermes_tingle_wiring as wiring

    audit_now = datetime.now(timezone.utc).replace(microsecond=0)
    item = store.add_item(title="Synthetic old reminder", now=audit_now - timedelta(days=100))
    monkeypatch.setattr(source_snapshot, "_plugin_root", lambda: Path(__file__).resolve().parents[1])
    monkeypatch.setattr(store, "source_enabled", lambda _kind: False)
    monkeypatch.setattr(discovery, "stage_local_candidates", lambda _snapshot: None)
    monkeypatch.setattr(assess, "evaluate", lambda _snapshot: None)
    monkeypatch.setattr(wiring, "wire_from_runtime", lambda: None)

    assert source_snapshot.main() == 0

    output = capsys.readouterr().out
    result = store.get_item(item["id"])
    assert result is not None and result["status"] == "auto_closed"
    assert item["id"] not in output
    assert item["title"] not in output


def test_current_source_evidence_protects_old_item_during_daily_review(monkeypatch, capsys):
    from pathlib import Path
    from types import SimpleNamespace
    import hermes_tingle_config as config
    import hermes_tingle_discovery as discovery
    import hermes_tingle_discovery_assess as assess
    import hermes_tingle_sources as sources
    import hermes_tingle_wiring as wiring

    audit_now = datetime.now(timezone.utc).replace(microsecond=0)
    item = store.add_item(title="Synthetic item with new source evidence", now=audit_now - timedelta(days=100))
    snapshot = {
        "due_items": [item],
        "emails": [],
        "tracked_email_context": [{"item_id": item["id"], "messages": [{"source_id": "synthetic"}]}],
        "email_completion_signals": [],
        "email_reply_checks": [{"item_id": item["id"], "state": "no_reply"}],
        "email_checkpoint_checks": [],
        "calendar_events": [],
        "errors": [],
        "chat_review": {"matches": [], "discovery": {"matches": []}},
    }

    sources._apply_retirement(snapshot, now=audit_now)

    result = store.get_item(item["id"])
    assert result is not None and result["status"] == "open"
    assert _policy(item["id"])["evidence_key"]


@pytest.mark.parametrize("key", ["email_sync", "chat_review", "calendar_review"])
def test_incomplete_enabled_source_blocks_soft_close_without_any_error(key):
    import hermes_tingle_sources as sources
    soft = store.add_item(title="Synthetic softly stale item", now=NOW - timedelta(days=100))
    capped = store.add_item(title="Synthetic capped item", now=NOW - timedelta(days=400))
    reviews = {
        "email_sync": {"enabled": False, "review_complete": False},
        "chat_review": {"enabled": False, "review_complete": False},
        "calendar_review": {"enabled": False, "review_complete": False},
    }
    reviews[key] = {"enabled": True, "review_complete": False}

    sources._apply_retirement({"errors": [], **reviews}, now=NOW)

    soft_after = store.get_item(soft["id"])
    capped_after = store.get_item(capped["id"])
    assert soft_after is not None and soft_after["status"] == "open"
    assert capped_after is not None and capped_after["status"] == "auto_closed"


def test_failed_source_review_still_enforces_hard_cap_through_apply_retirement():
    import hermes_tingle_sources as sources
    soft = store.add_item(title="Synthetic failed-review stale item", now=NOW - timedelta(days=100))
    capped = store.add_item(title="Synthetic failed-review capped item", now=NOW - timedelta(days=400))
    snapshot = {
        "errors": ["Gmail: read failed"],
        "email_sync": {"enabled": True, "review_complete": False},
        "chat_review": {"enabled": False, "review_complete": False},
        "calendar_review": {"enabled": False, "review_complete": False},
    }

    sources._apply_retirement(snapshot, now=NOW)

    soft_after = store.get_item(soft["id"])
    capped_after = store.get_item(capped["id"])
    assert soft_after is not None and soft_after["status"] == "open"
    assert capped_after is not None and capped_after["status"] == "auto_closed"


def test_checkpoint_evidence_identity_is_nonempty_stable_and_message_bound():
    import hermes_tingle_sources as sources

    def snapshot(message_id, receipt):
        return {"email_checkpoint_checks": [{
            "item_id": "HT-AAAAAA", "state": "thread_changed", "receipt": receipt,
            "messages": [{"source_id": message_id, "date": "1730000000000", "direction": "received"}],
        }]}

    first = sources._retirement_evidence(snapshot("message-1", "nonce-one"))
    same = sources._retirement_evidence(snapshot("message-1", "nonce-two"))
    changed = sources._retirement_evidence(snapshot("message-2", "nonce-one"))

    assert "HT-AAAAAA" in first[0]
    assert first[1]["HT-AAAAAA"]
    assert first == same
    assert first[1]["HT-AAAAAA"] != changed[1]["HT-AAAAAA"]


def test_source_purge_before_sweep_does_not_restore_revoked_evidence():
    import hermes_tingle_sources as sources

    item = store.add_item(title="Synthetic revoked-evidence item", now=NOW - timedelta(days=100))
    store.enable_source("chat", enabled_by="synthetic evidence fixture")
    epoch = store.source_epoch_snapshot()
    snapshot = {
        "errors": ["Gmail: read failed"],
        "email_sync": {"enabled": False, "review_complete": False},
        "chat_review": {"enabled": True, "review_complete": True, "matches": [{
            "item_ids": [item["id"]], "chat_ref": "telegram:synthetic",
            "role": "user", "timestamp": 1.0,
        }]},
        "calendar_review": {"enabled": False, "review_complete": False},
    }
    store.disable_source("chat", note="synthetic mid-flight revoke")

    sources._apply_retirement(snapshot, now=NOW, source_epoch=epoch)

    assert _policy(item["id"])["evidence_key"] == ""


def test_completing_recurring_auto_closed_linked_wait_is_blocked():
    parent = store.add_item(title="Synthetic capped parent", now=NOW - timedelta(days=400))
    wait = store.add_item(
        title="Synthetic recurring reply wait", status="waiting", owner="other", source_type="email",
        source_ref="gmail:synthetic-recurring/thread#sent", completion_signal="source_email_reply_received",
        due_at=NOW - timedelta(days=10), next_check_at=NOW - timedelta(days=10),
        recur_interval=30, recur_unit="day", now=NOW - timedelta(days=100),
    )
    store.add_step(parent["id"], title="Receive reply", why="Dependency", wait_item_id=wait["id"])

    assert set(store.auto_close_stale_items(now=NOW)) == {parent["id"], wait["id"]}
    with pytest.raises(ValueError, match="closed goal"):
        store.complete_item(wait["id"], now=NOW)
    after = store.get_item(wait["id"])
    assert after is not None and after["status"] == "auto_closed"


def test_invalid_configuration_without_daily_gate_does_not_close_items(monkeypatch):
    import hermes_tingle_sources as sources
    capped = store.add_item(title="Synthetic scan-sources capped item", now=NOW - timedelta(days=400))

    def invalid_settings():
        raise ValueError("synthetic invalid profile configuration")

    monkeypatch.setattr(sources, "load_settings", invalid_settings)
    sources.collect_snapshot(now=NOW)

    after = store.get_item(capped["id"])
    assert after is not None and after["status"] == "open"
