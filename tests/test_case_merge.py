"""Synthetic end-to-end owner confirmation, preservation, undo and source fences."""
import copy
import json
from datetime import timedelta
from types import SimpleNamespace
import pytest
import hermes_tingle as plugin
import hermes_tingle_store as store
import hermes_tingle_case_merge as cm
import hermes_tingle_sources as sources
from test_lookback import env


def pair(*,email=False,tag=''):
    plan={'actor':'other','outcome':'Await synthetic service outcome','basis':'inferred','review_at':(store._now()+timedelta(days=5)).isoformat(),'review_needed':False}
    rows=[]
    for n in range(2):
        rows.append(store.add_item(title='Await synthetic service outcome '+tag+str(n),summary='Separate historical synthetic update '+tag+str(n),owner='other',status='waiting',
                    source_type='email' if email else 'conversation',source_ref=('gmail:'+'a'*64+f'/thread{tag}{n}#anchor{tag}{n}') if email else 'synthetic:'+tag+str(n),
                    completion_signal='source_email_checkpoint' if email else '',review_plan=plan if email else None,
                    next_check_at=plan['review_at']))
    return rows


def proposal(rows):
    return cm.propose([r['id'] for r in rows],rows[0]['id'],'Same current synthetic outcome, earlier records are historical updates')['proposal_id']


def command(env,mid,*,answer='merge',undo=False):
    pages=[]
    def ask(text,choices):
        pages.append(text)
        return 'next' if any(c['value']=='next' for c in choices) else answer
    env[1].ask_choice=ask
    out=plugin.handle_command(env[1],('undo merge ' if undo else 'merge ')+mid,source=env[0])
    return out,pages


def test_model_proposal_never_merges_then_owner_preview_one_followup_and_undo(env):
    rows=pair();before=[store.get_item(r['id']) for r in rows]
    out=json.loads(plugin.handle_tool(env[1],{'action':'propose_case_merge','item_ids':[r['id'] for r in rows],'canonical_id':rows[0]['id'],'reason':'One current synthetic outcome'}))
    assert out['success'] and len(store.list_items())==2
    mid=out['proposal_id']
    result,pages=command(env,mid)
    assert 'Merged into' in result and len(store.list_items())==1
    assert '🤝 Merge these into one reminder?' in pages[0]
    shown='\n'.join(pages)
    for r in rows: assert r['title'] in shown and r['id'] in shown
    assert 'One reminder stays; all records kept.' in shown and 'not verified' in shown
    canonical=store.get_item(rows[0]['id']);child=store.get_item(rows[1]['id'])
    assert child['status']=='merged' and child['next_check_at'] is None
    assert canonical['next_check_at']==rows[0]['next_check_at']
    assert len(canonical['case_members'])==2
    for r in canonical['case_members']:
        original=next(v for v in before if v['id']==r['id'])
        for k in ('source_ref','source_url','title','summary','email_review_plan'): assert r[k]==original[k]
    assert canonical['case_merge']['undo_command'].endswith(mid)
    result,pages=command(env,mid,answer='undo',undo=True)
    assert 'Restored' in result and len(store.list_items())==2
    for r in rows:
        current=store.get_item(r['id'])
        assert current['status']==r['status'] and current['next_check_at']==r['next_check_at']
        assert 'case_merge' not in current
        assert current['reminder_attempt_count']==r['reminder_attempt_count']


def test_cancel_changes_nothing(env):
    rows=pair();mid=proposal(rows)
    result,_=command(env,mid,answer='cancel')
    assert 'unchanged' in result
    assert len(store.list_items())==2
    assert all(store.get_item(r['id'])['status']=='waiting' for r in rows)


@pytest.mark.parametrize('kind',['group','other','bot','missing'])
def test_only_configured_owner_dm_can_commit(env,kind):
    rows=pair();mid=proposal(rows)
    source=copy.copy(env[0])
    if kind=='group':source.chat_type='group'
    if kind=='other':source.user_id='not-owner'
    if kind=='bot':source.is_bot=True
    if kind=='missing':source=None
    out=plugin.handle_command(env[1],'merge '+mid,source=source)
    assert 'Merged into' not in out and len(store.list_items())==2


@pytest.mark.parametrize('mutation',['item','grant','policy','owner'])
def test_confirmation_rechecks_changes(env,mutation):
    rows=pair();mid=proposal(rows)
    def ask(text,choices):
        if mutation=='item':store.update_item(rows[1]['id'],summary='Changed privately')
        elif mutation=='grant':store.disable_source('email')
        elif mutation=='owner':env[0].user_id='not-owner'
        else:
            import hermes_constants
            p=hermes_constants.get_hermes_home()/'hermes-tingle.json';data=json.loads(p.read_text());data['timezone']='Europe/Berlin';p.write_text(json.dumps(data))
        return 'merge'
    env[1].ask_choice=ask
    out=plugin.handle_command(env[1],'merge '+mid,source=env[0])
    assert 'Merged into' not in out and len(store.list_items())==2


def test_stale_preview_and_expired_proposal_fail_closed(env):
    rows=pair();mid=proposal(rows);p=cm.preview(mid)
    store.update_item(rows[1]['id'],summary='Now a distinct obligation')
    with pytest.raises(ValueError):cm.commit(mid,p['token'],'synthetic-owner')
    conn=store._connect();conn.execute("UPDATE case_merge_proposals SET expires='2000-01-01' WHERE id=?",(mid,));conn.commit();conn.close()
    with pytest.raises(ValueError):cm.preview(mid)


def test_undo_never_overwrites_intervening_item_changes(env):
    rows=pair();mid=proposal(rows);command(env,mid)
    store.update_item(rows[0]['id'],summary='New important context')
    with pytest.raises(ValueError):cm.preview(mid,undo=True)
    with pytest.raises(ValueError):store.update_item(rows[1]['id'],status='open')
    assert len(store.list_items())==1


@pytest.mark.parametrize('change',['done','deadline','recurrence','actor','step','different-account','reply-watch'])
def test_incompatible_or_distinct_steps_are_not_mergeable(env,change):
    rows=pair(email=True)
    if change=='done':store.complete_item(rows[1]['id'])
    elif change=='deadline':store.update_item(rows[1]['id'],due_at='2030-01-01')
    elif change=='recurrence':store.update_item(rows[1]['id'],due_at='2030-01-01',recur_interval=1,recur_unit='month')
    elif change=='actor':store.update_item(rows[1]['id'],owner='me')
    elif change=='step':store.add_step(rows[1]['id'],title='Separate real obligation',why='Must remain active')
    else:
        conn=store._connect()
        if change=='different-account':conn.execute('UPDATE hermes_tingle_items SET source_ref=? WHERE id=?',('gmail:'+'b'*64+'/thread#anchor',rows[1]['id']))
        else:conn.execute("UPDATE hermes_tingle_items SET completion_signal='source_email_reply_received' WHERE id=?",(rows[1]['id'],))
        conn.commit();conn.close()
    with pytest.raises(ValueError):proposal(rows)


def test_cannot_merge_an_existing_merge_or_closed_member(env):
    rows=pair();mid=proposal(rows);command(env,mid)
    third=store.add_item(title='Another outcome',owner='other')
    with pytest.raises(ValueError):cm.propose([rows[0]['id'],third['id']],rows[0]['id'],'Not a nested merge')


def test_schema_limits_and_semantics_instruction_not_keyword_classifier(env):
    rows=pair()
    for ids in ([rows[0]['id']], [r['id'] for r in rows]*2):
        with pytest.raises(ValueError):cm.propose(ids,rows[0]['id'],'Reason')
    assert 'SAME current awaited outcome' in cm.INSTRUCTIONS
    assert 'Distinct obligations' in cm.INSTRUCTIONS
    assert 'never update/dismiss/complete' in cm.INSTRUCTIONS


def test_merged_email_check_covers_every_constituent_and_receipt(env,monkeypatch):
    rows=pair(email=True);mid=proposal(rows);command(env,mid)
    seen=[]
    def raw(items,**kwargs):
        seen.extend(r['id'] for r in items)
        return [dict(item_id=r['id'],receipt='receipt-'+r['id'],state='no_thread_update',messages=[],partial=False) for r in items],[]
    monkeypatch.setattr(sources,'_read_email_checkpoint_checks_raw',raw)
    checks,errors=sources._read_email_checkpoint_checks([store.get_item(rows[0]['id'])])
    assert not errors and set(seen)=={r['id'] for r in rows}
    assert len(checks)==1 and len(checks[0]['members'])==2
    assert checks[0]['item_id']==rows[0]['id']
    receipt=checks[0]['receipt']
    def changed(items,**kwargs):
        result,_=raw(items,**kwargs);result[1]['receipt']='new-context';result[1]['state']='thread_changed';return result,[]
    monkeypatch.setattr(sources,'_read_email_checkpoint_checks_raw',changed)
    newer,errors=sources._read_email_checkpoint_checks([store.get_item(rows[0]['id'])])
    assert newer[0]['receipt']!=receipt and newer[0]['state']=='thread_changed'
    def missing(items,**kwargs):return [],['No access']
    monkeypatch.setattr(sources,'_read_email_checkpoint_checks_raw',missing)
    assert sources._read_email_checkpoint_checks([store.get_item(rows[0]['id'])])[0]==[]


def test_constituents_share_existing_read_budget_and_replan_not_single_source(env,monkeypatch):
    rows=pair(email=True);mid=proposal(rows);command(env,mid)
    monkeypatch.setattr(sources,'MAX_EMAIL_REPLY_CHECKS',1)
    monkeypatch.setattr(sources,'_read_email_checkpoint_checks_raw',lambda items,**kw:([],[]) if not items else pytest.fail('Budget exceeded'))
    checks,errors=sources._read_email_checkpoint_checks([store.get_item(rows[0]['id'])])
    assert not checks and errors
    checks,errors=sources._read_email_checkpoint_checks([store.get_item(rows[0]['id'])],recovery=True)
    assert not checks and errors


def test_canonical_edit_cannot_turn_off_source_checks_or_add_deadline(env):
    rows=pair(email=True);mid=proposal(rows);command(env,mid)
    for updates in ({'completion_signal':''},{'due_at':'2030-01-01'},{'recur_interval':1,'recur_unit':'month'}):
        with pytest.raises(ValueError):store.update_item(rows[0]['id'],**updates)
    assert store.get_item(rows[0]['id'])['completion_signal']=='source_email_checkpoint'


def test_decline_suppresses_repeat_with_reordered_members(env):
    rows=pair();mid=proposal(rows);command(env,mid,answer='cancel')
    repeat=cm.propose([r['id'] for r in reversed(rows)],rows[0]['id'],'Still same synthetic outcome')
    assert repeat['proposal_id']==mid and repeat['review_command'] is None
    assert 'declined' in repeat['note']


def test_merged_state_not_general_add_or_update(env):
    with pytest.raises(ValueError):store.add_item(title='Bypass',status='merged')
    rows=pair()
    with pytest.raises(ValueError):store.update_item(rows[0]['id'],status='merged')


def test_preview_pagination_cancel_any_page_never_merges(env,monkeypatch):
    rows=pair();mid=proposal(rows)
    monkeypatch.setattr('hermes_tingle_lookback._preview_pages',lambda text:[text[:len(text)//2],text[len(text)//2:]])
    pages=[]
    def ask(text,choices):
        pages.append(text)
        return 'next' if len(pages)==1 else 'cancel'
    env[1].ask_choice=ask
    out=plugin.handle_command(env[1],'merge '+mid,source=env[0])
    assert len(pages)>1 and 'unchanged' in out and len(store.list_items())==2


def test_structured_change_after_merge_blocks_undo_without_losing_change(env):
    rows=pair();mid=proposal(rows);command(env,mid)
    store.add_document(rows[0]['id'],name='New synthetic proof')
    with pytest.raises(ValueError):cm.preview(mid,undo=True)
    assert store.get_item(rows[0]['id'])['documents'][0]['name']=='New synthetic proof'


def test_reminder_revision_and_attempt_history_not_reset_by_merge_or_undo(env):
    rows=pair();conn=store._connect()
    conn.execute('UPDATE hermes_tingle_items SET reminder_attempt_count=2,reminder_count=2 WHERE id=?',(rows[1]['id'],));conn.commit();conn.close()
    mid=proposal(rows);command(env,mid)
    assert store.get_item(rows[1]['id'])['reminder_attempt_count']==2
    command(env,mid,answer='undo',undo=True)
    assert store.get_item(rows[1]['id'])['reminder_attempt_count']==2
    assert store.get_item(rows[1]['id'])['reminder_revision']>rows[1]['reminder_revision']


def test_case_merge_guidance_does_not_wake_otherwise_quiet_snapshot(env):
    from source_snapshot import build_agent_input
    assert build_agent_input({'case_merge_context':{'items':[{'title':'Synthetic'}]}})=='{"wakeAgent": false}'
    payload=json.loads(build_agent_input({'due_items':[{'id':'synthetic'}]}))
    assert 'case_merge_guidance' in payload
    assert 'SAME current awaited outcome' in payload['case_merge_guidance']


def test_owner_can_reach_original_summaries_and_sources_in_case_details(env):
    rows=pair();mid=proposal(rows);command(env,mid)
    out=plugin.handle_command(env[1],'case '+rows[0]['id'],source=env[0])
    assert 'all sources retained' in out
    for row in rows:assert row['source_ref'] in out and row['summary'] in out
    assert '/tingle undo merge' not in out and mid not in out


def test_undo_fails_after_structured_dependency_and_permission_change(env):
    rows=pair(email=True);mid=proposal(rows);command(env,mid)
    store.disable_source('email')
    with pytest.raises(ValueError):cm.preview(mid,undo=True)
    assert len(store.list_items())==1


def test_preview_is_short_friendly_and_hides_raw_locators(env):
    rows=pair();mid=proposal(rows)
    result,pages=command(env,mid)
    shown='\n'.join(pages)
    assert '🤝 Merge these into one reminder?' in shown
    assert 'not verified' in shown
    assert 'One reminder stays; all records kept.' in shown
    for r in rows:
        assert r['id'] in shown and r['title'] in shown
        assert r['source_ref'] not in shown and r['summary'] not in shown
    assert 'December' not in shown and 'Keep every source and historical record' not in shown
    assert len(shown.splitlines()) <= 12
    assert result.startswith('✅ Merged into') and result.count('CM-') == 1


def test_bare_merge_resolves_a_single_pending_proposal(env):
    rows=pair();mid=proposal(rows)
    env[1].ask_choice=lambda text,choices:'merge'
    out=plugin.handle_command(env[1],'merge',source=env[0])
    assert 'Merged into' in out and mid in out and len(store.list_items())==1


def test_bare_merge_lists_multiple_pending_proposals_without_changing_anything(env):
    first=pair();m1=proposal(first)
    second=pair(tag='b');m2=proposal(second)
    out=plugin.handle_command(env[1],'merge',source=env[0])
    assert m1 in out and m2 in out
    assert all(store.get_item(r['id'])['status']=='waiting' for r in first+second)


def test_bare_merge_from_group_leaks_nothing(env):
    rows=pair();mid=proposal(rows)
    source=copy.copy(env[0]);source.chat_type='group'
    out=plugin.handle_command(env[1],'merge',source=source)
    assert mid not in out and '🤝' not in out
    assert len(store.list_items())==2


def test_undo_is_not_repeated_after_the_first_post_merge_message(env):
    rows=pair();mid=proposal(rows)
    command(env,mid)
    canonical=next(r['id'] for r in rows if store.get_item(r['id'])['status']=='waiting')
    env[1].ask_choice=lambda text,choices:'stop' if any(c['value']=='stop' for c in choices) else 'next'
    case_view=plugin.handle_command(env[1],'case '+canonical,source=env[0])
    listing=plugin.handle_command(env[1],'list',source=env[0])
    assert 'Undo:' not in case_view and mid not in case_view
    assert mid not in listing and 'undo merge' not in listing
    assert rows[0]['title'] in case_view or rows[1]['title'] in case_view


def test_malformed_merge_arguments_never_resolve_or_preview(env):
    rows=pair();mid=proposal(rows)
    env[1].ask_choice=lambda text,choices:'merge'
    for text in ('merge '+mid+' extra','merge '+mid+' '+mid,'merge not-an-id'):
        out=plugin.handle_command(env[1],text,source=env[0])
        assert 'CM-ABCDEF12' in out and '🤝' not in out and mid not in out
        assert all(store.get_item(r['id'])['status']=='waiting' for r in rows)
    conn=store._connect()
    try:
        state=conn.execute("SELECT state FROM case_merge_proposals WHERE id=?",(mid,)).fetchone()['state']
    finally:conn.close()
    assert state=='pending'
