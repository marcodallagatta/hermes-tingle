"""Fifth review: real entry points, synthetic provider-shaped accounts, no network."""
import base64
import hashlib
import importlib.util
import json
import threading
from datetime import datetime, timedelta, timezone

import pytest
import hermes_tingle as plugin
import hermes_tingle_sources as sources
import hermes_tingle_store as store
import hermes_tingle_lookback as lb
import hermes_tingle_resources as resources
import setup_jobs
import source_snapshot
from test_lookback import env, run


def key(address):return hashlib.sha256(address.encode()).hexdigest()


@pytest.fixture
def mailbox(monkeypatch):
    store.disable_source('calendar')
    current=['a@example.invalid'];calls=[]
    stamp=str(int(datetime.now(timezone.utc).timestamp()*1000))
    def bridge(*args):
        p=json.loads(args[-1]);calls.append((current[0],args[2],p))
        if args[2]=='getProfile':return {'emailAddress':current[0],'historyId':'100'}
        # Gmail account requests are pinned, not allowed to drift with me.
        assert p['userId']==current[0]
        if args[2]=='threads':
            return {'id':'thread','messages':[{'id':'sent','labelIds':['SENT'],'internalDate':stamp}]}
        if args[3]=='list':return {'messages':[{'id':'same-id'}]}
        if p['format']=='minimal':return {'id':'same-id','internalDate':stamp}
        if p['format']=='metadata':return {'id':'same-id','threadId':'thread','internalDate':stamp,'labelIds':['INBOX'],'snippet':'Synthetic lead','payload':{'headers':[]}}
        return {'id':'same-id','threadId':'thread','payload':{'mimeType':'text/plain','body':{'data':base64.urlsafe_b64encode(b'Synthetic bounded body').decode()}}}
    spec=importlib.util.spec_from_file_location('fifth_real_identity',sources.__file__)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    module.SourceReadError=sources.SourceReadError
    monkeypatch.setattr(module,'_gws_bridge',bridge)
    monkeypatch.setattr(sources,'body_account_identity',module.body_account_identity)
    monkeypatch.setattr(sources,'_gws_bridge',bridge)
    # Explicit fake transport for the new batch helper. Real host validation,
    # account binding and every entry-point boundary remain in the path.
    def batch_helper(argv,**kwargs):
        request=json.loads(argv[-1])
        assert request['mode']=='metadata'
        items=[bridge('gmail','users','messages','get','--params',json.dumps({
            'userId':request['userId'],'id':mid,'format':'metadata'})) for mid in request['ids']]
        return {'messages':items,'errors':[],'http_calls':1,'inner_calls':len(request['ids'])}
    monkeypatch.setattr(sources,'_run_json',batch_helper)
    store.enable_source('email',lookback_days=7,enabled_by='synthetic owner',account_key=key(current[0]))
    return current,calls


def collect():return sources._collect_email_candidates(now=datetime.now(timezone.utc),lookback_days=7)


def test_same_account_candidate_body_tool_and_pinned_requests(mailbox):
    scan=collect();assert scan['emails'][0]['source_ref']==sources.gmail_ref(key(mailbox[0][0]),'thread')
    result=json.loads(plugin.handle_tool(None,{'action':'read_email','source_id':'same-id'}))
    assert result['success'] and result['email']['body']=='Synthetic bounded body'
    assert all(p['userId']==address for address,kind,p in mailbox[1] if kind!='getProfile')


@pytest.mark.parametrize('action',['collect','body','reply','trash'])
def test_switch_under_same_grant_refuses_before_content(mailbox,action):
    collect();old=key(mailbox[0][0]);mailbox[0][0]='b@example.invalid';before=len(mailbox[1])
    item=store.add_item(title='Synthetic wait',source_type='email',source_ref=sources.gmail_ref(old,'thread','sent'),status='waiting',owner='other',completion_signal='source_email_reply_received' if action!='trash' else 'source_email_trashed',next_check_at='2020-01-01T00:00:00Z')
    with pytest.raises((ValueError,sources.SourceReadError)):
        if action=='collect':collect()
        elif action=='body':sources.read_email('same-id')
        elif action=='reply':sources._read_email_reply_checks([item])
        else:sources._read_email_completion_signals()
    assert all(kind=='getProfile' for _,kind,_ in mailbox[1][before:])
    assert store.get_item(item['id'])['reminder_attempt_count']==0


@pytest.mark.parametrize('legacy',[False,True])
def test_regrant_different_account_does_not_join_reply_or_complete(mailbox,legacy):
    old=key(mailbox[0][0]);ref='gmail:thread#sent' if legacy else sources.gmail_ref(old,'thread','sent')
    wait=store.add_item(title='Synthetic wait',source_type='email',source_ref=ref,status='waiting',owner='other',completion_signal='source_email_reply_received',next_check_at='2020-01-01T00:00:00Z')
    trash=store.add_item(title='Synthetic trash',source_type='email',source_ref=ref+'-trash',completion_signal='source_email_trashed')
    store.disable_source('email');mailbox[0][0]='b@example.invalid'
    store.enable_source('email',lookback_days=7,enabled_by='synthetic new grant',account_key=key(mailbox[0][0]))
    snapshot=sources.collect_snapshot()
    assert not snapshot['tracked_email_context'] and not snapshot['email_completion_signals'] and not snapshot['email_reply_checks']
    assert not any(kind=='threads' for _,kind,_ in mailbox[1])
    assert any('provenance' in e for e in snapshot['errors'])
    assert store.get_item(wait['id'])['status']=='waiting' and store.get_item(trash['id'])['status']=='open'


def test_regrant_needs_new_candidate_even_with_colliding_id(mailbox):
    collect();store.disable_source('email');mailbox[0][0]='b@example.invalid'
    store.enable_source('email',lookback_days=7,enabled_by='synthetic B consent',account_key=key(mailbox[0][0]))
    result=json.loads(plugin.handle_tool(None,{'action':'read_email','source_id':'same-id'}))
    assert not result['success'] and not any(p.get('format')=='full' for _,_,p in mailbox[1])
    collect();assert json.loads(plugin.handle_tool(None,{'action':'read_email','source_id':'same-id'}))['success']


def test_legacy_cursor_invalidated_with_visible_gap_no_history_reuse(mailbox):
    store.set_plugin_meta('gmail_account_binding','')
    store.stage_gmail_sync(history_id='legacy-cursor',message_ids=['legacy-id'])
    result=collect()
    assert 'unprovable_account_migration' in result['incomplete_reasons']
    assert any('legacy' in e for e in result['errors'])
    assert not any(kind=='history' for _,kind,_ in mailbox[1])
    assert 'legacy-id' not in store.pending_gmail_message_ids()


def test_unknown_identity_no_content_or_cache_reassignment(mailbox,monkeypatch):
    collect();before=store.get_plugin_meta('gmail_account_binding');calls=len(mailbox[1])
    monkeypatch.setattr(sources,'body_account_identity',lambda: (_ for _ in ()).throw(sources.SourceReadError('unknown identity')))
    with pytest.raises(sources.SourceReadError):collect()
    assert store.get_plugin_meta('gmail_account_binding')==before and len(mailbox[1])==calls


def test_candidate_old_epoch_and_unqualified_legacy_refused(mailbox):
    collect();db=store._connect();db.execute("UPDATE email_candidates SET generation=generation-1");db.commit();db.close()
    assert not json.loads(plugin.handle_tool(None,{'action':'read_email','source_id':'same-id'}))['success']
    generation=store.source_generation('email');db=store._connect();db.execute("UPDATE email_candidates SET generation=?,account_key=''",(generation,));db.commit();db.close()
    assert not json.loads(plugin.handle_tool(None,{'action':'read_email','source_id':'same-id'}))['success']
    assert not any(p.get('format')=='full' for _,_,p in mailbox[1])


def reserve(items,versions):
    return json.loads(plugin.handle_tool(None,{'action':'mark_notified','item_ids':[i['id'] for i in items],'expected_versions':versions}))


@pytest.mark.parametrize('change',['edit','snooze','complete','step'])
def test_stale_mixed_batch_is_atomic_and_no_new_cycle_charged(change):
    due='2020-01-01T00:00:00Z'
    a=store.add_item(title='Synthetic A',next_check_at=due);b=store.add_item(title='Synthetic B',next_check_at=due)
    versions={i['id']:i['reminder_revision'] for i in (a,b)}
    if change=='edit':store.update_item(b['id'],title='Synthetic changed')
    elif change=='snooze':store.snooze_item(b['id'],until='2099-01-01T00:00:00Z')
    elif change=='complete':store.complete_item(b['id'])
    else:store.add_step(b['id'],title='New dependency',why='Synthetic dependency change')
    baseline=[store.get_item(i['id']) for i in (a,b)]
    assert not reserve((a,b),versions)['success']
    assert [store.get_item(i['id']) for i in (a,b)]==baseline


@pytest.mark.parametrize('versions',[None,{},'0',{'unknown':0}])
def test_missing_or_invalid_expected_revision_refused(versions):
    item=store.add_item(title='Synthetic due',next_check_at='2020-01-01T00:00:00Z')
    assert not reserve([item],versions)['success']
    assert store.get_item(item['id'])['reminder_attempt_count']==0


def test_exact_snapshot_revision_success_and_schema_prompt():
    store.disable_source('email');store.disable_source('calendar')
    item=store.add_item(title='Synthetic due',next_check_at='2020-01-01T00:00:00Z')
    snapshot=sources.collect_snapshot();reviewed=next(i for i in snapshot['due_items'] if i['id']==item['id'])
    assert reserve([item],{item['id']:reviewed['reminder_revision']})['success']
    assert store.get_item(item['id'])['reminder_attempt_count']==1
    import schemas
    assert 'expected_versions' in json.dumps(schemas.TOOL_SCHEMA if hasattr(schemas,'TOOL_SCHEMA') else vars(schemas),default=str)
    assert 'expected_versions' in json.dumps(setup_jobs._job_specs(setup_jobs.load_settings()))


@pytest.mark.skip(reason="pinned repeated-error suppression via the removed Drive collector; rewrite against the email/chat error path is pending")
def test_coverage_helper_first_repeat_daily_recovery_disabled(monkeypatch,capsys):
    store.disable_source('calendar');store.enable_source('email',enabled_by='synthetic')
    clock=[datetime.now(timezone.utc)];broken=[True]
    def bridge(*a):
        if broken[0]:
            raise sources.SourceReadError('synthetic provider failure')
        return {'messages':[]}
    monkeypatch.setattr(sources,'_gws_bridge',bridge)
    original=sources.collect_snapshot;monkeypatch.setattr(sources,'collect_snapshot',lambda:original(now=clock[0]))
    def helper():
        assert source_snapshot.main()==0
        return json.loads(capsys.readouterr().out)
    first=helper();assert first['source_snapshot']['errors']
    assert helper()=={'wakeAgent':False}
    clock[0]+=timedelta(days=1);assert helper()['source_snapshot']['errors']
    broken[0]=False;assert helper()=={'wakeAgent':False}
    broken[0]=True;assert helper()['source_snapshot']['errors']
    store.disable_source('email');assert helper()=={'wakeAgent':False}


@pytest.mark.parametrize('subcommand',['view LC-NOTFOUND','skip LC-NOTFOUND','accept LC-NOTFOUND a b -','list LB-NOTFOUND',''])
def test_expiry_cleanup_even_on_missing_or_help_operation(env,subcommand):
    scan,c=run(env);db=lb.connect();db.execute('UPDATE scans SET expires=?',((lb.now()-timedelta(seconds=1)).isoformat(),));db.commit();db.close()
    plugin.handle_command(env[1],'lookback '+subcommand,source=env[0])
    db=lb.connect();assert not db.execute('SELECT payload FROM candidates').fetchall();db.close()
    assert not store.list_items()


@pytest.mark.parametrize('length',[255,256,257,1024,1025])
def test_exact_reference_store_boundary(length):
    ref='r'*length
    if length>1024:
        with pytest.raises(ValueError,match='at most 1024'):store.add_item(title='Synthetic ref',source_ref=ref)
        assert not store.list_items()
    else:assert store.add_item(title='Synthetic ref',source_ref=ref)['source_ref']==ref


def test_confirmation_wait_holds_no_profile_or_process_fence(tmp_path,monkeypatch):
    import hermes_constants
    entered=threading.Event();release=threading.Event();errors=[];result=[]
    class Ctx:
        def ask_choice(self,*a):entered.set();assert release.wait(5);return 'cancel'
    def setup():
        try:result.append(setup_jobs._activate_jobs(Ctx()))
        except BaseException as e:errors.append(e)
    t=threading.Thread(target=setup);t.start();assert entered.wait(3)
    try:
        with resources.mutation(tmp_path):pass
        other=tmp_path/'other';other.mkdir()
        with resources.mutation(other):pass
        assert store.list_items()==[]
    finally:release.set();t.join(5)
    assert not t.is_alive() and not errors and 'cancelled' in result[0]


def test_confirmed_setup_refuses_maintenance_during_wait(tmp_path):
    class Ctx:
        def ask_choice(self,*a):(tmp_path/resources.MARKER).write_text('synthetic');return 'create'
        def dispatch_tool(self,*a):pytest.fail('maintenance should refuse before Cron')
    assert 'maintenance' in setup_jobs._activate_jobs(Ctx())


def test_same_account_reply_and_tracked_join_preserved(mailbox):
    ref=sources.gmail_ref(key(mailbox[0][0]),'thread','sent')
    item=store.add_item(title='Synthetic wait',source_type='email',source_ref=ref,status='waiting',owner='other',completion_signal='source_email_reply_received',next_check_at='2020-01-01T00:00:00Z')
    snapshot=sources.collect_snapshot()
    assert snapshot['email_reply_checks']==[{'item_id':item['id'],'thread_id':'thread','state':'no_reply'}]
    assert snapshot['tracked_email_context'][0]['item_id']==item['id']
    assert all(m['source_ref']==ref.split('#')[0] for m in snapshot['tracked_email_context'][0]['messages'])


def test_context_marker_is_not_delivery_and_owner_recovery_requeues(env,monkeypatch):
    from types import SimpleNamespace
    store.disable_source('email');store.disable_source('calendar');store.enable_source('chat',enabled_by='synthetic')
    store.note_known_chat('telegram:1001')
    store.add_item(title='Synthetic gym renewal')
    now=datetime.now(timezone.utc)
    msg={'role':'user','content':'gym renewal is due','timestamp':now.timestamp()}
    sources.set_chat_history_interfaces(lambda p,c:['sess-1'],lambda s:[msg])
    review=sources._collect_chat_matches(now=now)
    marked=json.loads(plugin.handle_tool(None,{'action':'mark_context_reviewed','review_receipt':review['matches'][0]['review_receipt']}))
    assert marked['success'] and marked['delivery_confirmed'] is False
    assert not sources._collect_chat_matches(now=now)['matches']
    stranger=SimpleNamespace(platform='telegram',user_id='stranger',chat_id='stranger',chat_type='dm',is_bot=False)
    plugin.handle_command(None,'source review-again chat',source=stranger)
    assert not sources._collect_chat_matches(now=now)['matches']
    text=plugin.handle_command(None,'source review-again chat',source=env[0])
    assert 'Reset' in text and 'never proved delivery' in text
    assert sources._collect_chat_matches(now=now)['matches']
    assert store.source_enabled('chat') and len(store.list_items())==1
    sources.set_chat_history_interfaces(None,None)


def test_actual_enable_binds_verified_account_before_first_scan(env,mailbox,monkeypatch):
    store.disable_source('email')
    text=plugin.handle_command(env[1],'source enable email 7',source=env[0])
    assert 'opted in' in text
    binding=json.loads(store.get_plugin_meta('gmail_account_binding'))
    assert binding=={'account':key(mailbox[0][0]),'generation':store.source_generation('email')}
    mailbox[0][0]='b@example.invalid'
    with pytest.raises(sources.SourceReadError,match='account changed'):collect()
    assert not any(p.get('format') in ('metadata','full') for _,_,p in mailbox[1])


def test_changed_policy_invalidates_candidate_before_body(mailbox,tmp_path):
    collect()
    config=tmp_path/'hermes-tingle.json';policy=json.loads(config.read_text());policy['source_exclude_substrings'].append('Synthetic');config.write_text(json.dumps(policy))
    assert not json.loads(plugin.handle_tool(None,{'action':'read_email','source_id':'same-id'}))['success']
    assert not any(p.get('format')=='full' for _,_,p in mailbox[1])


def test_explicit_enable_cannot_adopt_legacy_cursor(mailbox):
    store.set_plugin_meta('gmail_account_binding','')
    store.stage_gmail_sync(history_id='legacy-A',message_ids=['legacy-A-id'])
    store.enable_source('email',lookback_days=7,enabled_by='synthetic owner regrant',account_key=key(mailbox[0][0]))
    assert not store.pending_gmail_message_ids()
    assert store.gmail_sync_state()['history_id'] is None
    assert store.get_plugin_meta('gmail_coverage_gap')=='unprovable_account_migration'


@pytest.mark.parametrize('resource',['config','plugin'])
def test_setup_refuses_same_policy_replacement_during_confirmation(tmp_path,resource):
    plugin_root=tmp_path/'plugins'/'hermes-tingle';plugin_root.mkdir(parents=True)
    class Ctx:
        def ask_choice(self,*a):
            if resource=='config':
                path=tmp_path/'hermes-tingle.json';replacement=tmp_path/'replacement';replacement.write_bytes(path.read_bytes());replacement.replace(path)
            else:
                plugin_root.rename(plugin_root.with_name('old-plugin'));plugin_root.mkdir()
            return 'create'
        def dispatch_tool(self,*a):pytest.fail('identity change must refuse before Cron')
    assert 'identity changed' in setup_jobs._activate_jobs(Ctx())
