"""Explicit recovery through current metadata, owner preview and revision fences."""
import copy
import json
from datetime import datetime,timezone,timedelta
import pytest
import hermes_tingle as plugin
import hermes_tingle_sources as sources
import hermes_tingle_store as store
from test_lookback import env
from test_lookback_quality import FakeLlm,verdict,ASK

ACCOUNT='a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf'


def setup(env,monkeypatch,*,known=True):
    item=store.add_item(title='Review the synthetic form',source_type='email',source_ref=f'gmail:{ACCOUNT}/thread#anchor',completion_signal='source_email_checkpoint')
    stamp=int((datetime.now(timezone.utc)-timedelta(days=2)).timestamp()*1000)
    payload={'id':'thread','messages':[{'id':'anchor','internalDate':str(stamp),'labelIds':['INBOX'],'snippet':ASK}]}
    monkeypatch.setattr(sources,'_guard_thread_lookback',lambda *_:None)
    calls=[]
    def bridge(*args,**kw):
        calls.append(args)
        if args[2]=='messages':
            mid=json.loads(args[-1])['id']
            return dict(next(m for m in payload['messages'] if m['id']==mid),threadId='thread',payload={'headers':[]})
        return copy.deepcopy(payload)
    monkeypatch.setattr(sources,'_gmail_bridge',bridge)
    # Body reader is exercised separately; this fake is the permitted body boundary.
    bodies=[]
    def body(mid):
        bodies.append(mid)
        assert not store.get_item(item['id'])['next_check_at']
        return {'id':mid,'thread_id':'thread','body':ASK}
    monkeypatch.setattr(sources,'read_email',body)
    def decide(record):
        value=verdict(record['id'],'action_needed',ASK,1,'Review the synthetic form')
        if known:
            value['review_plan']=dict(actor='me',outcome='Review the synthetic form',basis='inferred',anchor_message=1,after_days=5,timing_quote='')
        return value
    env[1].llm=FakeLlm(decide=decide)
    prompts=[]
    def ask(q,choices):
        prompts.append(q)
        assert not store.get_item(item['id'])['next_check_at']
        return 'review' if len(prompts)==1 else 'apply'
    env[1].ask_choice=ask
    return item,payload,calls,bodies,prompts


def run(env,item):
    return plugin.handle_command(env[1],'replan '+item['id'],source=env[0])


def test_explicit_recovery_makes_inert_item_due_later_without_reminder(env,monkeypatch):
    item,payload,calls,bodies,prompts=setup(env,monkeypatch)
    assert not store.due_items(now=datetime.now(timezone.utc)+timedelta(days=30))
    out=run(env,item)
    assert 'Contextual check scheduled' in out
    actual=store.get_item(item['id'])
    assert actual['next_check_at'] and actual['reminder_attempt_count']==0
    assert len(env[1].llm.calls)==1 and bodies==['anchor']
    assert len(prompts)==2 and 'billed' in prompts[0] and 'ledgers' in prompts[0]
    assert 'not a hard deadline' in prompts[1]
    assert item['title'] in prompts[1] and 'Proposed next action/outcome: Review the synthetic form' in prompts[1]
    assert 'title and summary stay unchanged' in prompts[1]
    assert store.due_items(now=datetime.now(timezone.utc)+timedelta(days=30))[0]['id']==item['id']


@pytest.mark.parametrize('change',['thread','revoke','edit','policy'])
@pytest.mark.parametrize('stage',['model','confirmation'])
def test_changes_before_apply_never_schedule(env,monkeypatch,change,stage):
    item,payload,*_=setup(env,monkeypatch)
    def mutate():
        if change=='thread':payload['messages'].append(dict(payload['messages'][0],id='new',internalDate=str(int(payload['messages'][0]['internalDate'])+1)))
        elif change=='revoke':store.disable_source('email')
        elif change=='edit':store.update_item(item['id'],summary='Changed by owner')
        else:
            from pathlib import Path
            path=__import__('hermes_constants').get_hermes_home()/'hermes-tingle.json'
            data=json.loads(path.read_text());data['timezone']='Europe/Berlin';path.write_text(json.dumps(data))
    if stage=='model':env[1].llm.hook=lambda _:mutate()
    else:
        original=env[1].ask_choice
        def ask(q,ch):
            answer=original(q,ch)
            if answer=='apply':mutate()
            return answer
        env[1].ask_choice=ask
    out=run(env,item)
    assert 'scheduled' in out or 'changed' in out or 'disabled' in out
    assert not store.get_item(item['id'])['next_check_at']


@pytest.mark.parametrize('defect',['unknown','missingbody','longthread','wrongaccount','truncated'])
def test_unknown_or_incomplete_never_fabricates_schedule(env,monkeypatch,defect):
    item,payload,calls,bodies,prompts=setup(env,monkeypatch,known=defect!='unknown')
    if defect=='missingbody':monkeypatch.setattr(sources,'read_email',lambda _: (_ for _ in ()).throw(ValueError('private error')))
    if defect=='truncated':monkeypatch.setattr(sources,'read_email',lambda _:dict(thread_id='thread',body='x'*sources.MAX_GMAIL_BODY_CHARS))
    if defect=='longthread':payload['messages']=[dict(payload['messages'][0],id='anchor' if n==0 else 'n'+str(n),internalDate=str(int(payload['messages'][0]['internalDate'])+n)) for n in range(5)]
    if defect=='wrongaccount':monkeypatch.setattr(sources,'body_account_identity',lambda:('other@example.invalid','b'*64))
    out=run(env,item)
    assert not store.get_item(item['id'])['next_check_at']
    assert len(prompts)==1 and 'private error' not in out
    assert 'unscheduled' in out or 'blocked' in out or 'account' in out.lower()


def test_decline_is_no_read_and_no_model(env,monkeypatch):
    item,payload,calls,bodies,prompts=setup(env,monkeypatch)
    env[1].ask_choice=lambda *a:'cancel'
    assert 'cancelled' in run(env,item)
    assert not calls and not bodies and not env[1].llm.calls


def test_nonowner_cannot_recover(env,monkeypatch):
    item,*_=setup(env,monkeypatch)
    env[0].user_id='not-owner'
    assert 'refused' in run(env,item)
    assert not env[1].llm.calls


@pytest.mark.parametrize('defect',['spam','list','duplicate_list','malformed','excluded'])
def test_body_adjacent_gates_precede_registration_and_body(env,monkeypatch,defect):
    item,payload,calls,bodies,prompts=setup(env,monkeypatch)
    original=sources._gmail_bridge
    def metadata(*args,**kw):
        value=original(*args,**kw)
        if args[2]=='messages':
            if defect=='spam':value['labelIds']=['SPAM']
            elif defect=='list':value['payload']['headers']=[dict(name='List-Id',value='bulk')]
            elif defect=='duplicate_list':value['payload']['headers']=[dict(name='List-Id',value='bulk'),dict(name='list-id',value='')]
            elif defect=='malformed':value['payload']['headers']=[dict(name=3,value='bad')]
            else:value['payload']['headers']=[dict(name='Subject',value='tracker.example.invalid')]
        return value
    monkeypatch.setattr(sources,'_gmail_bridge',metadata)
    out=run(env,item)
    assert 'blocked' in out and not bodies and not env[1].llm.calls
    conn=store._connect();assert not conn.execute('SELECT * FROM email_candidates').fetchall();conn.close()
    assert not store.get_item(item['id'])['next_check_at']


def test_recovery_real_existing_body_reader_and_shared_dedupe(env,monkeypatch):
    import base64
    actual_reader=sources.read_email
    item,payload,calls,bodies,prompts=setup(env,monkeypatch)
    bridge=sources._gmail_bridge
    def provider(*args,**kw):
        params=json.loads(args[-1])
        if args[2]=='messages' and params.get('format')=='full':
            return {'id':'anchor','threadId':'thread','payload':{'headers':[], 'mimeType':'text/plain',
                'body':{'data':base64.urlsafe_b64encode(ASK.encode()).decode()}}}
        return bridge(*args,**kw)
    monkeypatch.setattr(sources,'_gmail_bridge',provider)
    monkeypatch.setattr(sources,'read_email',actual_reader)
    assert 'Contextual check scheduled' in run(env,item)
    conn=store._connect();assert len(conn.execute('SELECT * FROM email_body_reads').fetchall())==1;conn.close()
    # Reusing a body is refused, even if an item has been deliberately paused again.
    store.update_item(item['id'],review_plan=dict(actor='unknown',outcome='',basis='unknown',review_at=None,review_needed=True))
    prompts.clear()
    out=run(env,item)
    assert 'body context unavailable or recently read' in out
    assert not store.get_item(item['id'])['next_check_at']
    assert len(env[1].llm.calls)==1
