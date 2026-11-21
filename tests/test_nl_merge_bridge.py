"""Natural-language yes -> tool -> shared private preview bridge.

The bridge opens the SAME paginated preview as /tingle merge from a model tool call,
only for a host-derived configured-owner DM turn. Group, unknown-shape, non-owner and
unbound turns fail closed and leak nothing. Opening never commits; the final preview
choice and all stale/fingerprint/policy rechecks are unchanged.
"""
import json
from types import SimpleNamespace

import pytest

import hermes_tingle as plugin
import hermes_tingle_store as store
import hermes_tingle_case_merge as cm
from test_case_merge import pair, proposal
from test_lookback import env


@pytest.fixture
def turn(request, env, monkeypatch):
    def bind(value):
        monkeypatch.setattr(plugin, '_current_principal', lambda: value)
    return bind


def _unbound_host_ask_choice(*a, **k):
    """Actual hermes_cli PluginContext.ask_choice return semantics on a model tool turn
    with no bound asker: None. Verified by Hermes against the real host on 2026-11-19
    (probe: registered tool handler + real PluginContext.ask_choice, no bound asker ->
    None). hermes_cli is not importable in this repo environment; this replicates the
    host's verified current behavior, it does not stub a desired capability."""
    return None


def propose_via_tool(env, rows):
    out = json.loads(plugin.handle_tool(env[1], {'action': 'propose_case_merge',
                                                 'item_ids': [r['id'] for r in rows],
                                                 'canonical_id': rows[0]['id'],
                                                 'reason': 'Same current synthetic outcome'}))
    assert out['success']
    return out['proposal_id']


def test_owner_dm_yes_opens_shared_first_page_and_final_choice_commits(env, turn):
    rows = pair()
    mid = propose_via_tool(env, rows)
    turn(('telegram', '42', '42'))  # gateway-bound owner DM turn
    pages = []
    def ask(text, choices):
        pages.append(text)
        return 'next' if any(c['value'] == 'next' for c in choices) else 'merge'
    env[1].ask_choice = ask
    out = plugin.handle_tool(env[1], {'action': 'open_case_merge_review', 'proposal_id': mid})
    assert pages, 'tool call must reach the shared first preview page'
    shown = '\n'.join(pages)
    for r in rows:
        assert r['title'] in shown
    assert '🤝 Merge these into one reminder?' in pages[0]
    assert 'Merged into' in out
    assert len(store.list_items()) == 1


def test_unavailable_choice_is_nonmutating_abort_never_decline(env, turn):
    rows = pair()
    mid = propose_via_tool(env, rows)
    turn(('telegram', '42', '42'))  # gateway-bound owner DM turn
    env[1].ask_choice = _unbound_host_ask_choice
    out = plugin.handle_tool(env[1], {'action': 'open_case_merge_review', 'proposal_id': mid})
    assert 'could not continue' in out
    assert mid in out, 'the abort message must route the owner to the typed review command'
    conn = store._connect()
    try:
        state = conn.execute("SELECT state FROM case_merge_proposals WHERE id=?", (mid,)).fetchone()['state']
    finally:
        conn.close()
    assert state == 'pending', 'an unavailable chooser must never decline or consume the proposal'
    assert len(store.list_items()) == 2, 'an unavailable chooser must never change items'


def test_unavailable_choice_on_first_page_still_aborts_cleanly(env, turn):
    rows = pair()
    mid = propose_via_tool(env, rows)
    turn(('telegram', '42', '42'))
    env[1].ask_choice = _unbound_host_ask_choice
    out = plugin.handle_tool(env[1], {'action': 'open_case_merge_review'})
    assert 'could not continue' in out
    conn = store._connect()
    try:
        state = conn.execute("SELECT state FROM case_merge_proposals WHERE id=?", (mid,)).fetchone()['state']
    finally:
        conn.close()
    assert state == 'pending'
    assert len(store.list_items()) == 2


def test_unexpected_answer_is_nonmutating_abort_never_decline(env, turn):
    # Any non-advancing answer that is not an explicit 'cancel' (host garbage,
    # unexpected values) must abort without writes, exactly like an unavailable chooser.
    rows = pair()
    mid = propose_via_tool(env, rows)
    turn(('telegram', '42', '42'))
    env[1].ask_choice = lambda *a: 'bogus-unexpected-value'
    out = plugin.handle_tool(env[1], {'action': 'open_case_merge_review', 'proposal_id': mid})
    assert 'could not continue' in out
    conn = store._connect()
    try:
        state = conn.execute("SELECT state FROM case_merge_proposals WHERE id=?", (mid,)).fetchone()['state']
    finally:
        conn.close()
    assert state == 'pending', 'an unexpected answer must never decline or consume the proposal'
    assert len(store.list_items()) == 2


def test_unexpected_answer_on_undo_aborts_without_changes(env, turn):
    from test_case_merge import command
    rows = pair()
    mid = propose_via_tool(env, rows)
    out, _ = command(env, mid)
    assert 'Merged into' in out and len(store.list_items()) == 1
    env[1].ask_choice = lambda *a: 'bogus-unexpected-value'
    out = plugin.handle_command(env[1], 'undo merge ' + mid, source=env[0])
    assert 'could not continue' in out
    assert 'Restored' not in out and len(store.list_items()) == 1, 'an unexpected answer must never undo or change items'


def test_opening_alone_never_commits_and_cancel_declines(env, turn):
    rows = pair()
    mid = propose_via_tool(env, rows)
    turn(('telegram', '42', '42'))
    env[1].ask_choice = lambda *a: 'cancel'
    out = plugin.handle_tool(env[1], {'action': 'open_case_merge_review', 'proposal_id': mid})
    assert 'Kept unchanged' in out
    assert len(store.list_items()) == 2
    assert all(i['status'] == 'waiting' for i in store.list_items())


def test_group_topic_turn_fails_closed_and_leaks_nothing(env, turn):
    rows = pair()
    mid = propose_via_tool(env, rows)
    turn(('telegram', '42', '-100999'))  # owner identity, but a group/topic chat
    env[1].ask_choice = lambda *a: (_ for _ in ()).throw(AssertionError('preview must not open'))
    out = plugin.handle_tool(env[1], {'action': 'open_case_merge_review', 'proposal_id': mid})
    for r in rows:
        assert r['title'] not in out and r['id'] not in out
    assert mid not in out
    assert 'direct message' in out.casefold() or 'DM' in out
    assert len(store.list_items()) == 2


def test_unknown_chat_shape_nonowner_and_unbound_turns_refuse(env, turn):
    rows = pair()
    mid = propose_via_tool(env, rows)
    env[1].ask_choice = lambda *a: (_ for _ in ()).throw(AssertionError('preview must not open'))
    turn(('web', '42', 'session-x'))  # no verifiable chat shape
    out = plugin.handle_tool(env[1], {'action': 'open_case_merge_review', 'proposal_id': mid})
    assert rows[0]['title'] not in out
    turn(('telegram', '99', '99'))  # DM shape but not a configured owner
    out = plugin.handle_tool(env[1], {'action': 'open_case_merge_review', 'proposal_id': mid})
    assert 'not a configured owner' in out or 'refused' in out
    turn(None)  # unbound/absent session context
    out = plugin.handle_tool(env[1], {'action': 'open_case_merge_review', 'proposal_id': mid})
    assert "Couldn't verify" in out
    assert len(store.list_items()) == 2


def test_single_pending_proposal_resolves_without_id(env, turn):
    rows = pair()
    mid = propose_via_tool(env, rows)
    turn(('telegram', '42', '42'))
    env[1].ask_choice = lambda *a: 'cancel'
    out = plugin.handle_tool(env[1], {'action': 'open_case_merge_review'})
    assert 'Kept unchanged' in out  # resolved the only pending proposal and opened it


def test_multiple_pending_proposals_ask_which_without_opening(env, turn):
    from datetime import timedelta
    first = pair()
    plan_at = (store._now() + timedelta(days=5)).isoformat()
    second = [store.add_item(title='Await other synthetic outcome ' + n,
                             summary='Other synthetic update ' + n, owner='other', status='waiting',
                             source_type='conversation', source_ref='synthetic-other:' + n,
                             next_check_at=plan_at) for n in 'ab']
    mid1 = propose_via_tool(env, first)
    mid2 = propose_via_tool(env, second)
    turn(('telegram', '42', '42'))
    env[1].ask_choice = lambda *a: (_ for _ in ()).throw(AssertionError('must ask first'))
    out = json.loads(plugin.handle_tool(env[1], {'action': 'open_case_merge_review'}))
    ids = {p['proposal_id'] for p in out['choose_one']}
    assert ids == {mid1, mid2}
    assert len(store.list_items()) == 4


def test_host_without_interactive_tool_call_gets_exact_fallback(env, turn):
    rows = pair()
    mid = propose_via_tool(env, rows)
    turn(('telegram', '42', '42'))
    plain_ctx = SimpleNamespace(post_message=lambda *a: None)  # no ask_choice
    out = plugin.handle_tool(plain_ctx, {'action': 'open_case_merge_review', 'proposal_id': mid})
    assert '/tingle merge ' + mid in out
    assert 'nothing was shown or changed' in out
    assert len(store.list_items()) == 2


def test_expired_or_unknown_proposal_fails_closed(env, turn):
    turn(('telegram', '42', '42'))
    out = plugin.handle_tool(env[1], {'action': 'open_case_merge_review', 'proposal_id': 'CM-DEADBEEF'})
    assert 'unavailable or expired' in out
    out = plugin.handle_tool(env[1], {'action': 'open_case_merge_review'})
    assert 'No pending merge proposal' in out
