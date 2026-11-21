"""Synthetic structural admissibility, never proof of real-model semantic judgment."""
import copy
from datetime import datetime,timezone
import pytest
import hermes_tingle_checkpoint as cp
import hermes_tingle_assess as assess
import hermes_tingle_lookback as lb
from test_lookback import env

NOW=datetime(2026,11,19,tzinfo=timezone.utc)
QUOTE='Processing takes three weeks.'

def case():
    members=[dict(message_id='old',date='2026-11-01T12:00:00+00:00',body_acquired=False),
             dict(message_id='anchor',date='2026-11-10T12:00:00+00:00',body_acquired=True),
             dict(message_id='latest',date='2026-11-11T12:00:00+00:00',body_acquired=True)]
    record=dict(context_complete=False,omitted_older_messages=1,messages=[
        dict(n=1,message_id='latest',date=members[2]['date'],body_acquired=True,direction='sent_by_owner',text='Thank you. I will wait.'),
        dict(n=2,message_id='anchor',date=members[1]['date'],body_acquired=True,direction='received',text=QUOTE)],
        context_provenance=dict(version=1,enumeration_complete=True,members=members,
            gaps=[dict(kind='body_dedupe',stage='acquisition',position=0,message_id='old')]))
    plan=dict(actor='other',outcome='Receive the decision',basis='stated',anchor_message=2,after_days=21,
              timing_quote=QUOTE,older_context_independent=True)
    return record,plan


def result(record,plan):
    return cp.diagnose(plan,record,{m['n']:(m['text'],m['direction']) for m in record['messages']},now=NOW)


def test_full_unclipped_anchor_through_newest_allows_only_stated_model_judgment():
    record,plan=case()
    got,reason=result(record,plan)
    assert reason=='scheduled_suffix_model_judgment' and got['review_at']=='2026-12-01T12:00:00+00:00'
    assert not record['context_complete']  # no global upgrade for expiry/relevance
    for change in [dict(basis='inferred'),dict(older_context_independent=False),dict(older_context_independent='true')]:
        assert result(record,dict(plan,**change))[0]==cp.unknown()


@pytest.mark.parametrize('defect',['missing_latest','missing_middle','equal_time','clipped_newest','cap_lost_anchor',
    'policy','list','scope','labels','unknown_date','future_date','unverified_enumeration','snippet_only',
    'legacy','wrong_anchor_date','unknown_direction','invented_id','duplicate_id','gap_newer'])
def test_counterexamples_never_auto_schedule(defect):
    record,plan=case();manifest=record['context_provenance'];members=manifest['members']
    if defect=='missing_latest':record['messages'].pop(0)
    elif defect=='missing_middle':members.append(dict(message_id='middle',date='2026-11-10T18:00:00+00:00',body_acquired=False))
    elif defect=='equal_time':members[0]['date']=members[1]['date']
    elif defect=='clipped_newest':record['messages'][0]['shortened']=True
    elif defect=='cap_lost_anchor':record['messages'].pop()
    elif defect in ('policy','list','scope','labels'):
        manifest['gaps'][0]['kind']={'policy':'policy_excluded','list':'list_mail','scope':'outside_scope','labels':'excluded_label'}[defect]
    elif defect=='unknown_date':members[0]['date']=None
    elif defect=='future_date':members[0]['date']='2027-01-01T00:00:00+00:00'
    elif defect=='unverified_enumeration':manifest['enumeration_complete']=False
    elif defect=='snippet_only':record['messages'][0]['body_acquired']=False
    elif defect=='legacy':record.pop('context_provenance')
    elif defect=='wrong_anchor_date':record['messages'][1]['date']='2026-11-09T12:00:00+00:00'
    elif defect=='unknown_direction':record['messages'][0]['direction']='unknown'
    elif defect=='invented_id':record['messages'][0]['message_id']='absent'
    elif defect=='duplicate_id':members.append(copy.deepcopy(members[0]))
    elif defect=='gap_newer':manifest['gaps'][0].update(position=2,message_id='latest')
    assert result(record,plan)[0]==cp.unknown()


@pytest.mark.parametrize('text',[
    'As agreed under the earlier conditions, processing takes three weeks.',
    'The previous agreement was conditional on approval. Please see the older message.',
    'The request has now been fulfilled. No further action is required.',
    'The request is cancelled; ignore the prior processing estimate.',
])
def test_semantic_uncertainty_requires_model_unknown_fixture_not_keyword_claim(text):
    record,plan=case();record['messages'][0]['text']=text
    # Synthetic model response follows explicit instructions. This does NOT test real-model behavior.
    plan.update(actor='unknown',basis='unknown',older_context_independent=False)
    assert result(record,plan)[0]==cp.unknown()
    assert all(term in cp.INSTRUCTIONS for term in ('conditional older agreement','as-agreed','cancellation','fulfilment'))


def test_record_preserves_overlapping_acquisition_and_model_gaps():
    messages=lb._ConversationMessages([
        dict(message_id='n'+str(n),date=f'2026-11-{18-n:02d}T12:00:00+00:00',text='short text' if n else 'x'*1000,
             direction='received',body_acquired=True) for n in range(6)])
    messages.provenance=dict(version=1,enumeration_complete=False,members=[],gaps=[dict(kind='id_cap',stage='acquisition',position=None,message_id=None)])
    record,_=assess.conversation_record('C1',messages,automated=False,acquisition_complete=False)
    kinds={g['kind'] for g in record['context_gaps']}
    assert {'id_cap','model_text_clipped','model_message_omitted'} <= kinds
    assert not record['context_complete'] and record['omitted_older_messages']==2


def test_only_older_model_omission_from_complete_acquisition_can_be_admissible():
    record,plan=case();manifest=record['context_provenance']
    manifest['gaps']=[];manifest['members'][0]['body_acquired']=True
    assert result(record,plan)[1]=='scheduled_suffix_model_judgment'
    # A model cutoff cannot omit an anchor or anything newer.
    record['messages'].pop(0)
    assert result(record,plan)[0]==cp.unknown()


def test_real_input_transport_preserves_acquisition_body_proof_without_global_upgrade():
    import json
    record,plan=case();manifest=record['context_provenance']
    data=dict(message_id='latest',date=manifest['members'][2]['date'],snippet='Thank you. I will wait.',
        outgoing=True,subject='Synthetic case',context_status='Bounded thread context. Incomplete: old body previously read',
        context_provenance=manifest,thread_context=[
            dict(message_id='latest',date=manifest['members'][2]['date'],body='Thank you. I will wait.',outgoing=True),
            dict(message_id='anchor',date=manifest['members'][1]['date'],body=QUOTE,outgoing=False)])
    messages,automated,complete=lb._conversation_input(dict(members=[dict(payload=json.dumps(data))],newest=data))
    transported,evidence=assess.conversation_record('C1',messages,automated=automated,acquisition_complete=complete)
    assert not transported['context_complete']
    assert cp.diagnose(plan,transported,evidence,now=NOW)[1]=='scheduled_suffix_model_judgment'


def test_missing_body_without_explicit_gap_inventory_is_unverifiable():
    record,plan=case();record['context_provenance']['gaps']=[]
    assert result(record,plan)[0]==cp.unknown()


def test_acquisition_attaches_typed_metadata_and_dedupe_provenance(env,monkeypatch):
    from test_lookback import run
    import json
    scan,candidate=run(env)
    payload=json.loads(candidate['payload'])
    manifest=payload['context_provenance']
    assert manifest['version']==1 and manifest['enumeration_complete']
    assert manifest['members'][0]['body_acquired'] is True and manifest['members'][0]['date']
    assert manifest['gaps']==[]


def prepared_suffix_pick(env):
    import json
    from test_lookback_quality import seed,assess_scan,FakeLlm,verdict
    record,plan=case()
    data=dict(date=record['messages'][0]['date'],snippet=record['messages'][0]['text'],outgoing=False,
        context_status='Bounded thread context. Incomplete: body previously read within 14 days',
        context_provenance=copy.deepcopy(record['context_provenance']),thread_context=[
            dict(message_id='m0',date=record['messages'][0]['date'],body=record['messages'][0]['text'],outgoing=False),
            dict(message_id='anchor',date=record['messages'][1]['date'],body=QUOTE,outgoing=False)])
    data['context_provenance']['members'][2]['message_id']='m0'
    sid=seed(env,[data])
    def decide(conv):
        value=verdict(conv['id'],'action_needed',QUOTE,2,'Wait for the synthetic decision')
        value['review_plan']=plan
        return value
    llm=FakeLlm(decide=decide)
    assess_scan(env,sid,llm)
    conn=lb.connect();row=conn.execute('SELECT * FROM candidates WHERE scan_id=?',(sid,)).fetchone();conn.close()
    return sid,dict(row),llm


def test_suffix_plan_is_pending_not_autoadded_then_explicitly_previewed(env):
    import json
    import hermes_tingle_store as store
    import hermes_tingle as plugin
    sid,candidate,llm=prepared_suffix_pick(env)
    stats={}
    got=lb.auto_accept_qualified(env[0],lb.binding(env[0]),sid,stats=stats)
    assert got[0]==[] and not store.list_items()
    assert stats['pending_confirmation']==1 and stats['accepted']==0
    conn=lb.connect()
    pending=conn.execute('SELECT state,payload FROM candidates WHERE id=?',(candidate['id'],)).fetchone()
    plan=lb._checkpoint_plan(conn,conn.execute('SELECT * FROM candidates WHERE id=?',(candidate['id'],)).fetchone())
    assert pending['state']=='pending' and json.loads(pending['payload'])['thread_context']
    assert plan['suffix_model_judgment'] is True
    conn.close()
    seen=[]
    def confirm(q,ch):
        seen.append(q)
        assert 'Partial history: older context is missing' in q
        assert 'not verified' in q and 'Receive the decision' in q
        assert not store.list_items()
        return 'accept'
    env[1].ask_choice=confirm
    out=plugin.handle_command(env[1],'lookback accept '+candidate['id'],source=env[0])
    assert 'Accepted' in out and len(seen)==1
    item=store.list_items()[0]
    assert item['next_check_at'] and json.loads(item['email_review_plan'])['suffix_model_judgment'] is True
    assert len(llm.calls)==1


def test_suffix_individual_preview_cancel_preserves_pending_plan(env):
    import hermes_tingle_store as store
    import hermes_tingle as plugin
    sid,candidate,llm=prepared_suffix_pick(env)
    env[1].ask_choice=lambda *a:'cancel'
    out=plugin.handle_command(env[1],'lookback accept '+candidate['id'],source=env[0])
    assert 'cancelled' in out.lower() and not store.list_items()
    conn=lb.connect();row=conn.execute('SELECT * FROM candidates WHERE id=?',(candidate['id'],)).fetchone()
    assert row['state']=='pending' and lb._checkpoint_plan(conn,row)['suffix_model_judgment'];conn.close()


def test_suffix_commit_cannot_use_onboarding_or_missing_exact_preview(env):
    import hermes_tingle_store as store
    sid,candidate,llm=prepared_suffix_pick(env)
    for onboarding in (True,False):
        conn=lb.connect()
        try:
            title,summary,due=lb._candidate_fields(conn,candidate)
            with pytest.raises(ValueError,match='per-item informed preview'):
                lb._commit_lead(conn,env[0],lb.binding(env[0]),candidate['id'],candidate['generation'],title,summary,due,None,onboarding=onboarding)
        finally:conn.close()
    assert not store.list_items()


def test_unknown_never_masks_partial_context_reason():
    record,plan=case();plan.update(actor='unknown',basis='unknown',older_context_independent=False)
    assert result(record,plan)[1]=='omitted_messages'
    record['omitted_older_messages']=0
    assert result(record,plan)[1]=='incomplete_acquisition'
    record['messages'][0]['shortened']=True
    assert result(record,plan)[1]=='clipped_context'


def test_onboarding_reports_pending_confirmation_not_imported(env,monkeypatch):
    import hermes_tingle as plugin
    import hermes_tingle_store as store
    sid,candidate,llm=prepared_suffix_pick(env)
    monkeypatch.setattr(lb,'_run_scan',lambda *a,**k:sid)
    env[1].ask_choice=lambda *a:'yes'
    out=plugin.handle_command(env[1],'',source=env[0])
    assert '1 partial-history timing proposals need individual review and confirmation' in out
    assert 'nothing was added for those picks' in out and 'lookback review '+sid in out
    assert not store.list_items()


def test_bulk_preview_has_specific_suffix_caveat_before_confirmation(env):
    import hermes_tingle_store as store
    import hermes_tingle as plugin
    sid,candidate,llm=prepared_suffix_pick(env)
    seen=[]
    def confirm(q,ch):
        seen.append(q)
        assert not store.list_items()
        return 'accept' if any(c['value']=='accept' for c in ch) else 'next'
    env[1].ask_choice=confirm
    out=lb._bulk_accept(env[1],[candidate['id']],env[0],lb.binding(env[0]))
    assert '1 of 1 added' in out
    alltext=' '.join(seen)
    assert 'Partial history: older context is missing' in alltext and 'not verified' in alltext
    assert store.list_items()[0]['next_check_at']


def test_marker_is_preserved_but_invalid_markers_fail_closed():
    record,proposal=case();plan,reason=result(record,proposal)
    assert cp.item_plan(plan)==plan and plan['suffix_model_judgment'] is True
    assert cp.item_plan(dict(plan,suffix_model_judgment='true'))==cp.unknown()
    assert cp.item_plan(dict(plan,basis='inferred'))==cp.unknown()
    assert 'Partial history' in lb._checkpoint_preview(plan)
    plain=dict(plan);plain.pop('suffix_model_judgment')
    assert 'Partial history' not in lb._checkpoint_preview(plain)
