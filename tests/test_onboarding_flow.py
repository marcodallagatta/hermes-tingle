"""Hands-off onboarding flow: one question, one bounded import, undo.

Synthetic mail and a scripted fake model only. Pins the fail-closed gates
(non-owner, group, missing capability), the single-question contract, the
auto-add boundary (only complete-context picks), idempotent re-entry, the
failure state and undo ownership.
"""
import json
from types import SimpleNamespace

import pytest

import hermes_tingle as plugin
import hermes_tingle_onboarding as onboarding
import hermes_tingle_store as store
from test_lookback import env
from test_lookback_quality import FakeLlm, ASK


def _yes(ctx, llm=None):
    if llm is not None:
        ctx.llm = llm
    ctx.ask_choice = lambda q, ch: "yes"
    return ctx


def test_prompt_only_for_owner_dm_with_empty_profile(env):
    sender, ctx = env[0], env[1]
    # non-owner, group and bot senders never see the question and record nothing
    for source in (None,
                   SimpleNamespace(platform="telegram", user_id="999", chat_id="999", chat_type="dm", is_bot=False),
                   SimpleNamespace(platform="telegram", user_id="42", chat_id="42", chat_type="group", is_bot=False),
                   SimpleNamespace(platform="telegram", user_id="42", chat_id="42", chat_type="dm", is_bot=True)):
        out = plugin.handle_command(ctx, "", source=source)
        assert "Import the last 30 days" not in out
    assert store.onboarding_state() is None
    # a non-empty profile is never asked
    store.add_item(title="Synthetic existing item")
    out = plugin.handle_command(ctx, "", source=sender)
    assert "Import the last 30 days" not in out
    assert store.onboarding_state() is None


def test_decline_records_once_and_never_repeats(env):
    sender, ctx = env[0], env[1]
    ctx.ask_choice = lambda q, ch: "no"
    out = plugin.handle_command(ctx, "", source=sender)
    assert "No import run" in out
    assert store.onboarding_state()["state"] == "declined"
    assert not store.list_items()
    again = plugin.handle_command(ctx, "", source=sender)
    assert "Import the last 30 days" not in again


def test_unavailable_without_email_capability_records_state(env, monkeypatch):
    import hermes_tingle_sources as sources
    monkeypatch.setattr(sources, "source_capabilities", lambda: set())
    ctx = env[1]
    ctx.ask_choice = lambda q, ch: pytest.fail("no question when Gmail is unavailable")
    out = plugin.handle_command(ctx, "", source=env[0])
    assert "can't run" in out
    assert store.onboarding_state()["state"] == "unavailable"


def test_yes_runs_one_bounded_scan_and_auto_adds_only_qualified(env):
    sender, ctx, calls = env[0], env[1], env[2]
    llm = FakeLlm()
    _yes(ctx, llm)
    out = plugin.handle_command(ctx, "", source=sender)
    assert "Onboarding import done" in out
    state = store.onboarding_state()
    assert state["state"] == "completed" and state["scan_id"].startswith("LB-")
    assert len(llm.calls) == 1  # exactly one model pick pass
    items = store.list_items()
    if state["items"]:
        assert len(items) == len(state["items"])
        for item in items:
            assert item["title"] in out and item["id"] not in out  # reply names titles, not ticket IDs
            assert "one-time onboarding import" in item["summary"] and "not a verified commitment" in item["summary"]
    # never asked again
    assert "Import the last 30 days" not in plugin.handle_command(ctx, "", source=sender)


def test_routine_mail_stays_private(env):
    llm = FakeLlm(decide=lambda conv: __import__("test_lookback_quality", fromlist=["verdict"]).verdict(conv["id"]))
    _yes(env[1], llm)
    out = plugin.handle_command(env[1], "", source=env[0])
    assert "Onboarding import done" in out
    state = store.onboarding_state()
    assert state["state"] == "completed" and not state["items"]
    assert not store.list_items()
    assert "nothing was added" in out.lower()


def test_failed_scan_records_failed_state_and_adds_nothing(env, monkeypatch):
    import hermes_tingle_lookback as lb
    _yes(env[1], FakeLlm())
    monkeypatch.setattr(lb, "_run_scan", lambda *a, **k: (_ for _ in ()).throw(ValueError("synthetic revoked")))
    out = plugin.handle_command(env[1], "", source=env[0])
    assert "did not complete" in out and "lookback start 30" in out
    assert store.onboarding_state()["state"] == "failed"
    assert not store.list_items()


def test_undo_removes_only_import_items_and_clears_marker(env):
    sender, ctx = env[0], env[1]
    _yes(ctx, FakeLlm())
    plugin.handle_command(ctx, "", source=sender)
    state = store.onboarding_state()
    other = store.add_item(title="Synthetic unrelated item")
    out = plugin.handle_command(ctx, "undo onboarding", source=sender)
    assert "will not offer the import again" in out
    remaining = [i["id"] for i in store.list_items()]
    assert other["id"] in remaining
    for item_id in state["items"]:
        assert item_id not in remaining
    assert store.onboarding_state()["state"] == "undone" and onboarding.due() is False
    # undo is not repeatable
    assert "no completed onboarding import" in plugin.handle_command(ctx, "undo onboarding", source=sender)


def test_undo_refused_for_stranger(env):
    sender, ctx = env[0], env[1]
    _yes(ctx, FakeLlm())
    plugin.handle_command(ctx, "", source=sender)
    stranger = SimpleNamespace(platform="telegram", user_id="999", chat_id="999", chat_type="dm", is_bot=False)
    out = plugin.handle_command(ctx, "undo onboarding", source=stranger)
    assert "not a configured owner" in out
    assert store.onboarding_state()["state"] == "completed"


def test_undo_leaves_provenance_mismatched_items_alone(env):
    sender, ctx = env[0], env[1]
    _yes(ctx, FakeLlm())
    plugin.handle_command(ctx, "", source=sender)
    state = store.onboarding_state()
    if not state["items"]:
        pytest.skip("synthetic model picked nothing")
    store.update_item(state["items"][0], summary="Rewritten by someone else")
    out = plugin.handle_command(ctx, "undo onboarding", source=sender)
    assert "left alone" in out
    assert store.get_item(state["items"][0])["summary"] == "Rewritten by someone else"


@pytest.mark.parametrize('failure', ['read', 'zero', 'cap', 'model'])
def test_incomplete_scan_never_claims_all_clear(env, monkeypatch, failure):
    import hermes_tingle_lookback as lb
    _yes(env[1], FakeLlm())
    original = lb._run_scan
    def scan(*args, **kwargs):
        sid = original(*args, **kwargs)
        conn = lb.connect()
        if failure == 'read':
            conn.execute("UPDATE scans SET checked=0,coverage='Partial/failed scan',error_detail='private provider diagnostic' WHERE id=?", (sid,))
        elif failure == 'zero':
            conn.execute('UPDATE scans SET checked=0 WHERE id=?', (sid,))
        elif failure == 'cap':
            conn.execute("UPDATE scans SET coverage='Partial coverage: candidate cap' WHERE id=?", (sid,))
        else:
            conn.execute("UPDATE scans SET assess_state='failed' WHERE id=?", (sid,))
        conn.commit(); conn.close()
        return sid
    monkeypatch.setattr(lb, '_run_scan', scan)
    out = plugin.handle_command(env[1], '', source=env[0])
    if failure == 'cap':
        assert 'Onboarding import done' in out and 'Coverage is incomplete' not in out
    else:
        assert 'Partial import added' in out and 'did not complete' not in out and 'lookback start 30' in out
        assert 'Coverage is incomplete' in out and 'Onboarding import done' not in out
    assert '(scan LB-' not in out
    assert 'nothing clearly needed' not in out and 'nothing qualified' not in out
    assert 'private provider' not in out
    assert store.onboarding_state()['state'] == 'partial'
    ids = store.onboarding_state()['items']
    assert ids and all(item not in out for item in ids) and 'Review the revised contract' in out
    plugin.handle_command(env[1], 'undo onboarding', source=env[0])
    assert all(store.get_item(item) is None for item in ids)


def test_actual_helper_failure_with_zero_checked_never_reports_success(env, monkeypatch):
    import hermes_tingle_sources as sources
    import hermes_tingle_lookback as lb
    _yes(env[1], FakeLlm())
    original = sources._run_json
    def fail(argv, **kwargs):
        if str(argv[1]).endswith('gws_batch_bridge.py'):
            return {'dates': [], 'calls': 3, 'unverified': 1,
                    'diagnostic': 'Gmail batch date gaps: 1/1 parts returned 503 backendError/UNAVAILABLE'}
        return original(argv, **kwargs)
    monkeypatch.setattr(sources, '_run_json', fail)
    out = plugin.handle_command(env[1], '', source=env[0])
    assert 'did not complete' in out and 'date checks' in out
    assert 'Nothing was added' in out and 'lookback start 30' in out
    conn = lb.connect(); scan = conn.execute('SELECT * FROM scans').fetchone(); conn.close()
    assert scan['checked'] == scan['staged'] == 0
    assert scan['coverage'].startswith('Partial') and '503' in scan['error_detail']
    assert not store.list_items()


def _scan_row(scan_id, checked, coverage):
    import hermes_tingle_lookback as lb
    conn = lb.connect()
    conn.execute("INSERT INTO scans(id,principal,policy,source_epoch,state,days,created,expires,"
                 "listed,checked,staged,coverage) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                 (scan_id, "p", "pol", 1, "done", 30, "2026-11-18T00:00:00+00:00",
                  "2026-11-19T00:00:00+00:00", 100, checked, 0, coverage))
    conn.commit()
    conn.close()


@pytest.mark.parametrize("marker", [
    {"state": "completed", "scan_id": "LB-STALE", "items": []},
    {"state": "partial", "scan_id": "LB-STALE", "items": []},
    {"state": "failed", "error": "synthetic"},
])
def test_marker_from_failed_scan_does_not_consume_the_offer(env, marker):
    _scan_row("LB-STALE", 0, "Partial/failed scan: synthetic")
    store.set_onboarding_state(marker)
    assert onboarding.due() is True


def test_partial_coverage_with_checked_dates_and_no_items_is_re_offered(env):
    _scan_row("LB-PART", 40, "Partial: synthetic size limit")
    store.set_onboarding_state({"state": "completed", "scan_id": "LB-PART", "items": []})
    assert onboarding.due() is True


@pytest.mark.parametrize("marker,scan", [
    ({"state": "completed", "scan_id": "LB-OK", "items": []}, (30, "Complete")),
    ({"state": "completed", "scan_id": "LB-ADD", "items": ["T-1"]}, (0, "Partial/failed scan")),
    ({"state": "declined"}, None),
    ({"state": "unavailable", "reason": "synthetic"}, None),
    ({"state": "completed", "scan_id": "LB-MISSING", "items": []}, None),
])
def test_valid_markers_still_block_the_offer(env, marker, scan):
    if scan:
        _scan_row(marker["scan_id"], *scan)
    store.set_onboarding_state(marker)
    assert onboarding.due() is False


def test_stale_marker_offers_import_again_through_actual_command(env):
    _scan_row("LB-STALE", 0, "Partial/failed scan: synthetic")
    store.set_onboarding_state({"state": "completed", "scan_id": "LB-STALE", "items": []})
    seen = []
    env[1].ask_choice = lambda q, ch: seen.append(q) or "no"
    plugin.handle_command(env[1], "", source=env[0])
    assert seen and store.onboarding_state()["state"] == "declined"
    # a genuine decline is never re-offered
    assert onboarding.due() is False


def test_undo_with_nothing_added_keeps_undone_state_and_wording_matches(env):
    store.set_onboarding_state({"state": "partial", "scan_id": "LB-X", "items": []})
    out = plugin.handle_command(env[1], "undo onboarding", source=env[0])
    assert "will not offer the import again" in out and "marker is cleared" not in out
    assert store.onboarding_state()["state"] == "undone"
    assert onboarding.due() is False


def test_welcome_shows_running_version_and_wait_notice_without_post_message(env):
    seen = []
    ctx = env[1]
    ctx.post_message = lambda text: (_ for _ in ()).throw(NotImplementedError("unsupported under host isolation"))
    ctx.ask_choice = lambda q, ch: seen.append(q) or "no"
    plugin.handle_command(ctx, "", source=env[0])
    assert seen
    assert f"Welcome to Tingle v{plugin.VERSION}." in seen[0]
    assert "can take a while" in seen[0] and "as soon as it finishes" in seen[0]
    assert store.onboarding_state()["state"] == "declined"


def test_yes_still_completes_when_post_message_is_unsupported(env):
    ctx = _yes(env[1], FakeLlm())
    ctx.post_message = lambda text: (_ for _ in ()).throw(NotImplementedError("unsupported"))
    out = plugin.handle_command(ctx, "", source=env[0])
    assert "Onboarding import done" in out or "did not complete" in out
    assert store.onboarding_state()["state"] in ("completed", "partial")


def test_unavailable_welcome_also_shows_the_version(env, monkeypatch):
    import hermes_tingle_sources as sources
    monkeypatch.setattr(sources, "source_capabilities", lambda: set())
    out = plugin.handle_command(env[1], "", source=env[0])
    assert f"Welcome to Tingle v{plugin.VERSION}." in out and "Gmail is not connected" in out


def test_model_failure_is_named_even_when_the_scan_also_hit_the_size_cap(env, monkeypatch):
    import hermes_tingle_lookback as lb
    _yes(env[1], FakeLlm(fail_after=0))     # every model request fails
    original = lb._run_scan
    def scan(*args, **kwargs):
        sid = original(*args, **kwargs)
        conn = lb.connect()
        conn.execute("UPDATE scans SET coverage='Partial coverage: candidate cap' WHERE id=?", (sid,))
        conn.commit(); conn.close()
        return sid
    monkeypatch.setattr(lb, '_run_scan', scan)
    out = plugin.handle_command(env[1], '', source=env[0])
    assert 'model review failed or timed out' in out and 'not assessed' in out
    assert 'size limit' not in out and 'Nothing was added' in out and 'SECRET' not in out
