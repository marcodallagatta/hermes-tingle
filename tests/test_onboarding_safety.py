"""Regressions for independently reproduced safety defects. Synthetic only."""
import json
from types import SimpleNamespace
import pytest
import setup_jobs
import hermes_tingle_store as store
import hermes_tingle_sources as sources
import hermes_tingle_lookback as lb
from test_lookback import env
from test_lookback_quality import seed, strong, assess_scan, verdict, ASK


def test_first_run_revocation_during_probe_wins(monkeypatch):
    monkeypatch.setattr(setup_jobs,'_first_run_sources_pristine',lambda:True)
    def probe(kind):
        if kind == 'email':
            store.disable_source('email')
        return 'b'*64
    monkeypatch.setattr(sources,'probe_source',probe)
    setup_jobs._offer_first_run_sources(SimpleNamespace(ask_choice=lambda q,ch:'all'),lambda:None,local=False)
    assert not store.source_enabled('email')


@pytest.mark.parametrize('kept', [0,1])
def test_missing_verdicts_are_incomplete_not_allclear(env, kept):
    sid=seed(env,strong(3))
    class Missing:
        def complete_structured(self,**kw):
            records=json.loads(kw['input'][0]['text'])['conversations']
            return SimpleNamespace(parsed={'results':[verdict(c['id'],'action_needed',ASK,1,'Review synthetic contract') for c in records[:kept]]})
    assess_scan(env,sid,Missing())
    conn=lb.connect(); row=conn.execute('SELECT * FROM scans WHERE id=?',(sid,)).fetchone()
    text=lb._summary_text(conn,row); assessment=lb._latest_assessment(conn,sid);conn.close()
    assert row['assess_state']=='partial'
    assert assessment['assessed']==kept
    assert f'{3-kept} conversations not assessed' in text
    assert 'No clear follow-ups found' not in text


def imported(title='Synthetic imported item'):
    return store.add_item(title=title,summary='Synthetic detail. '+lb.ONBOARDING_NOTE,
                          source_type='email',source_ref='gmail:synthetic/'+title,
                          onboarding_scan='LB-SYNTHETIC')


@pytest.mark.parametrize('change', ['title','summary','due','status','snooze','step','document','requirement','option','booking'])
def test_undo_preserves_any_modified_item_and_dependents(change):
    from datetime import datetime,timedelta,timezone
    i=imported(); untouched=imported('Untouched synthetic item')
    if change=='title': store.update_item(i['id'],title='Owner renamed item')
    elif change=='summary': store.update_item(i['id'],summary='Owner detail with '+lb.ONBOARDING_NOTE)
    elif change=='due': store.update_item(i['id'],due_at='2027-03-01T10:00:00+00:00')
    elif change=='status': store.update_item(i['id'],status='waiting')
    elif change=='snooze': store.snooze_item(i['id'],until=datetime.now(timezone.utc)+timedelta(days=10))
    elif change=='step': store.add_step(i['id'],title='Owner step',why='Needed')
    elif change=='document': store.add_document(i['id'],name='Synthetic form')
    elif change=='requirement': store.add_requirement(i['id'],text='Owner constraint')
    elif change=='option': store.add_option(i['id'],name='Synthetic choice')
    elif change=='booking': store.add_booking(i['id'],kind='other',name='Synthetic reservation')
    before=store.get_item(i['id'])
    result=store.undo_onboarding(expected_origin=lb.ONBOARDING_NOTE)
    assert store.get_item(i['id'])==before and store.get_item(untouched['id']) is None
    assert result['preserved']==[i['id']] and result['removed']==[untouched['id']]
    assert store.onboarding_state()['state']=='undone'


def test_unjournaled_legacy_import_never_gets_unsafe_baseline():
    i=store.add_item(title='Legacy example',summary=lb.ONBOARDING_NOTE)
    store.set_onboarding_state(dict(state='completed',items=[i['id']]))
    result=store.undo_onboarding(expected_origin=lb.ONBOARDING_NOTE)
    assert result['removed']==[] and result['preserved']==[i['id']]
    assert store.get_item(i['id'])==i


def test_atomic_undo_rolls_back_deletion_if_marker_write_fails(monkeypatch):
    i=imported(); before=store.onboarding_state()
    original=store._save_onboarding_in_transaction
    def crash(conn,state):
        if state.get('state')=='undone':raise RuntimeError('synthetic journal crash')
        return original(conn,state)
    monkeypatch.setattr(store,'_save_onboarding_in_transaction',crash)
    with pytest.raises(RuntimeError):store.undo_onboarding(expected_origin=lb.ONBOARDING_NOTE)
    assert store.get_item(i['id'])==i and store.onboarding_state()==before
    monkeypatch.setattr(store,'_save_onboarding_in_transaction',original)
    result=store.undo_onboarding(expected_origin=lb.ONBOARDING_NOTE)
    assert result['removed']==[i['id']] and store.get_item(i['id']) is None


def test_imported_item_and_marker_commit_atomically(monkeypatch):
    original=store._save_onboarding_in_transaction
    monkeypatch.setattr(store,'_save_onboarding_in_transaction',lambda *a:(_ for _ in ()).throw(RuntimeError('synthetic marker crash')))
    with pytest.raises(RuntimeError):imported()
    assert store.list_items()==[] and store.onboarding_state() is None
    conn=store._connect();assert not conn.execute('SELECT * FROM onboarding_item_journal').fetchall();conn.close()


def test_interrupted_autoaccept_has_durable_undo_marker(env,monkeypatch):
    from test_lookback_quality import FakeLlm
    sid=seed(env,strong(2));assess_scan(env,sid,FakeLlm())
    original=store.add_item;count=[]
    def crash(**kw):
        count.append(1)
        if len(count)>1:raise RuntimeError('synthetic second-item crash')
        return original(**kw)
    monkeypatch.setattr(store,'add_item',crash)
    with pytest.raises(RuntimeError):lb.auto_accept_qualified(env[0],lb.binding(env[0]),sid)
    items=store.list_items();assert len(items)==1
    assert store.onboarding_state()['items']==[items[0]['id']]
    assert store.onboarding_state()['state']=='partial'
    assert store.undo_onboarding(expected_origin=lb.ONBOARDING_NOTE)['removed']==[items[0]['id']]


def test_late_aggregate_marker_never_drops_committed_ids():
    a=imported('First synthetic item');b=imported('Second synthetic item')
    store.set_onboarding_state(dict(state='completed',items=[b['id']],scan_id='LB-SYNTHETIC'))
    assert set(store.onboarding_state()['items'])=={a['id'],b['id']}
