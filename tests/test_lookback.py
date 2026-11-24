from delivery_fakes import bind_synthetic_email
"""Private one-off flow with synthetic provider metadata only."""
import contextlib
import base64
import json
import re
import sqlite3
from datetime import timedelta
from types import SimpleNamespace

import pytest
import hermes_tingle as plugin
import hermes_tingle_lookback as lb
import hermes_tingle_store as store
import hermes_tingle_sources as sources


@pytest.fixture
def env(tmp_path, synthetic_settings, monkeypatch):
    data = {**synthetic_settings, 'owner_principals': ['telegram:42']}
    (tmp_path/'hermes-tingle.json').write_text(json.dumps(data))
    original = store.plugin_db
    def db(name):
        if name in ('hermes-tingle-lookback','hermes-tingle-body-audit'):
            c = sqlite3.connect(tmp_path/(name+'.db'));c.row_factory=sqlite3.Row;return c
        return original(name)
    monkeypatch.setattr(store,'plugin_db',db)
    monkeypatch.setattr(sources,'source_capabilities',lambda:{'email'})
    sender=SimpleNamespace(platform='telegram',user_id='42',chat_id='42',chat_type='dm',is_bot=False)
    calls=[]
    stamp=str(int((lb.now()-timedelta(days=1)).timestamp()*1000))
    def bridge(*args):
        params=json.loads(args[-1]);calls.append((args,params))
        if args[2]=='getProfile':return {'emailAddress':'owner@example.invalid'}
        if args[2]=='threads':return {'id':'t1','messages':[{'id':'m1'}]}
        if args[3]=='list':return {'messages':[{'id':'m1'}]}
        if params['format']=='full':return {'id':'m1','internalDate':stamp,'threadId':'t1','payload':{'mimeType':'text/plain','body':{'data':base64.urlsafe_b64encode(b'Please review the draft by Friday.').decode()}}}
        if params['format']=='minimal':return {'id':'m1','internalDate':stamp}
        return {'id':'m1','internalDate':stamp,'threadId':'t1','labelIds':['INBOX'],
                'snippet':'Could you review the revised contract?','payload':{'headers':[{'name':'Subject','value':'Review request'}]}}
    monkeypatch.setattr(sources,'_gws_bridge',bridge)
    original_run_json=sources._run_json
    def run_json(argv,**kwargs):
        if len(argv)>1 and str(argv[1]).endswith('gws_batch_bridge.py'):
            payload=json.loads(argv[-1])
            if payload.get('mode')=='metadata':
                items=[sources._gws_bridge('gmail','users','messages','get','--params',json.dumps({
                    'userId':payload['userId'],'id':mid,'format':'metadata',
                    'metadataHeaders':(['From','To','Subject','Date'] + (['List-Id','List-Unsubscribe','Auto-Submitted'] if payload['profile']=='lookback' else []))})) for mid in payload['ids']]
                return {'messages':items,'errors':[],'http_calls':1,'inner_calls':len(payload['ids'])}
            calls.append((['batch','messages','get'],
                          {'ids':payload['ids'],'format':'minimal','userId':payload['userId']}))
            return [{'id':mid,'internalDate':stamp} for mid in payload['ids']]
        return original_run_json(argv,**kwargs)
    monkeypatch.setattr(sources,'_run_json',run_json)
    class Ctx:
        def __init__(self):
            self.messages = []
            self.questions = []
            self.answers = []
        def post_message(self, text):
            self.messages.append(text)
        def ask_choice(self,q,choices):
            self.questions.append((q, choices))
            assert 'Nothing is auto-added' in q or 'Nothing is added without a final confirmation' in q or 'SHARED' in q
            if self.answers:
                return self.answers.pop(0)
            values=[c['value'] for c in choices]
            if 'scan' in values:
                return 'scan'
            if 'stop' in values:
                return 'stop'
            return choices[0]['value']
    return sender,Ctx(),calls,data,stamp


def test_scan_start_prompt_is_short_and_moves_directly_into_private_review(env, monkeypatch):
    sender, ctx = env[0], env[1]
    calls = []
    monkeypatch.setattr(lb, "_run_scan", lambda context, source, expected, days, *, grant_assess:
                        calls.append((days, grant_assess)) or "LB-ABCDEF12")
    monkeypatch.setattr(lb, "review", lambda context, source, scan_id:
                        calls.append(("review", scan_id)) or "Review paused; resume command present.")
    monkeypatch.setattr(lb, "_results_reply", lambda *a, **k: "Scan summary only.")

    out = lb.scan(ctx, sender, 30)

    question, choices = ctx.questions[0]
    assert len(question) < 650
    assert "bounded scan, not full mailbox coverage" in question.casefold()
    assert "text stays in private DM staging for 7 days" in question
    assert "possibly cloud/billed" in question
    assert "Scan only sends nothing to a model" in question
    assert "Nothing is auto-added" in question
    assert "`/tingle lookback help`" in question
    assert [choice["value"] for choice in choices] == ["assess", "scan", "cancel"]
    assert calls == [(30, False), ("review", "LB-ABCDEF12")]
    assert out == "Review paused; resume command present."


def test_review_decision_timeout_is_resumable_with_the_exact_command(env):
    import re

    sender, ctx = env[0], env[1]
    text = plugin.handle_command(ctx, "lookback start 7", source=sender)
    match = re.search(r"LB-[0-9A-F]{8}", text)
    assert match, text
    scan_id = match.group(0)
    ctx.answers = [None]

    paused = lb.review(ctx, sender, scan_id)

    assert "Review paused" in paused
    assert f"`/tingle lookback review {scan_id}`" in paused
    assert "nothing is added" in ctx.questions[-1][0].lower()
    assert f"If this decision times out, resume with `/tingle lookback review {scan_id}`" in ctx.questions[-1][0]
    assert not store.list_items()


def test_scan_posts_progress_message(env):
    run(env)
    assert any("Starting" in message for message in env[1].messages)


def run(env):
    sender,ctx,*_=env
    text=plugin.handle_command(ctx,'lookback start 7',source=sender)
    assert (('lookback review LB-' in text) or ('No possible follow-ups in the checked messages' in text)
            or ('scan failed or revoked' in text)),text
    conn=lb.connect();scan=conn.execute('SELECT * FROM scans').fetchone();candidate=conn.execute('SELECT * FROM candidates').fetchone();conn.close()
    return scan,candidate


def snapshot():
    conn=store._connect()
    result={r[0]:list(conn.execute('SELECT * FROM '+r[0])) for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    # chat-scope bookkeeping records the command chat itself; it is not scan data
    result.pop('known_chats', None)
    result.pop('chat_source_exclusions', None)
    return result


def test_scan_isolated_private_consumed_and_bounded_body(env):
    before=snapshot();scan,c=run(env)
    assert snapshot()==before
    assert scan['state']=='consumed' and scan['listed']==scan['checked']==scan['staged']==1
    assert len([x for x in env[2] if x[1].get('format')=='full'])==1
    assert env[2][2][1]['format']=='minimal' and env[2][2][1]['ids']==['m1']
    assert all(x[1]['userId']=='owner@example.invalid' for x in env[2][1:])
    assert not store.list_items()
    assert json.loads(plugin.handle_tool(None,{'action':'get','item_id':c['id']}))['success'] is False
    text=plugin.handle_command(env[1],f"lookback list {scan['id']}",source=env[0])
    assert 'UNTRUSTED' in text and 'Review request' in text
    env[0].user_id='visitor'
    assert 'Review request' not in plugin.handle_command(env[1],f"lookback list {scan['id']}",source=env[0])


def test_first_pass_date_checks_use_one_batch_call(env):
    scan,c=run(env)
    batches=[p for _,p in env[2] if 'ids' in p]
    per_message=[p for _,p in env[2] if p.get('fields')=='id,internalDate']
    assert len(batches)==1 and batches[0]['ids']==['m1']
    # The one remaining date read is the second-pass thread member, not a first-pass date check.
    assert len(per_message)==1
    assert batches[0]['userId']=='owner@example.invalid'


def test_hundred_ids_use_five_bounded_batch_date_calls(env,monkeypatch):
    ids=[f'm{i}' for i in range(100)]
    stamp=env[4]
    def bridge(*args):
        p=json.loads(args[-1])
        if args[2]=='getProfile':return {'emailAddress':'owner@example.invalid'}
        if args[3]=='list':return {'messages':[{'id':mid} for mid in ids]}
        mid=p['id']
        if p.get('format')=='minimal':return {'id':mid,'internalDate':stamp}
        return {'id':mid,'internalDate':stamp,'threadId':mid,'labelIds':['INBOX'],'snippet':'x',
                'payload':{'headers':[{'name':'Subject','value':'x'},
                                      {'name':'List-Id','value':'noise.example.invalid'}]}}
    monkeypatch.setattr(sources,'_gws_bridge',bridge)
    run(env)
    batches=[p for _,p in env[2] if 'ids' in p]
    assert len(batches)==5 and all(len(batch['ids'])==20 for batch in batches)
    assert [mid for batch in batches for mid in batch['ids']] == ids
    assert not [p for _,p in env[2] if p.get('fields')=='id,internalDate']


@pytest.mark.parametrize('change',[{'user_id':'other'},{'chat_type':'group'},{'is_bot':True}])
def test_nonowner_refuses_before_read(env,change):
    for k,v in change.items():setattr(env[0],k,v)
    assert 'Scan LB-' not in plugin.handle_command(env[1],'lookback start 7',source=env[0])
    assert not env[2]


def test_cancel_creates_no_scan(env):
    env[1].ask_choice=lambda *a:'cancel'
    assert 'cancelled' in plugin.handle_command(env[1],'lookback start 7',source=env[0])
    conn=lb.connect();assert not conn.execute('SELECT * FROM scans').fetchall();conn.close()
    assert not env[2]


def test_accept_explicit_shared_fields_and_no_inference(env):
    scan,c=run(env)
    result=plugin.handle_command(env[1],f'lookback accept {c["id"]} "Review contract" "Check what is actually needed" "-"',source=env[0])
    assert 'Accepted as shared' in result
    items=store.list_items();assert len(items)==1
    assert items[0]['title']=='Review contract' and items[0]['completion_signal']=='source_email_checkpoint'
    assert items[0]['source_ref']==sources.gmail_ref(scan['account_key'],'t1','m1') and not items[0]['due_at']
    assert len(env[2])==9


def test_skip_is_not_rule(env):
    scan,c=run(env)
    assert 'no permanent preference' in plugin.handle_command(env[1],f'lookback skip {c["id"]}',source=env[0])
    assert not store.list_feedback_candidates() if hasattr(store,'list_feedback_candidates') else not store.list_items()


def test_expired_candidates_never_list_or_accept(env):
    scan,c=run(env)
    conn=lb.connect();conn.execute('UPDATE scans SET expires=?',((lb.now()-timedelta(seconds=1)).isoformat(),));conn.commit();conn.close()
    assert 'expired' in plugin.handle_command(env[1],f'lookback list {scan["id"]}',source=env[0])
    assert 'unavailable' in plugin.handle_command(env[1],f'lookback accept {c["id"]} "x" "y" "-"',source=env[0]).lower()
    assert not store.list_items()


def test_prelock_revocation_never_probes(env,monkeypatch):
    original=store.serialized_operation
    @contextlib.contextmanager
    def lock(category):
        with original(category):
            monkeypatch.setattr(store,'serialized_operation',original)
            store.disable_source('email',note='later revoke')
            yield
    monkeypatch.setattr(store,'serialized_operation',lock)
    assert 'changed' in plugin.handle_command(env[1],'lookback start 7',source=env[0])
    assert not env[2]


def test_midread_policy_change_stops_metadata(env,monkeypatch,tmp_path):
    original=sources._gws_bridge
    def change(*args):
        value=original(*args)
        (tmp_path/'hermes-tingle.json').write_text(json.dumps({**env[3],'delivery':'telegram:99'}))
        return value
    monkeypatch.setattr(sources,'_gws_bridge',change)
    assert 'changed' in plugin.handle_command(env[1],'lookback start 7',source=env[0])
    assert len(env[2])==1
    conn=lb.connect();assert conn.execute('SELECT state FROM scans').fetchone()[0]=='consumed';assert not conn.execute('SELECT * FROM candidates').fetchall();conn.close()


@pytest.mark.parametrize('days',[-1,0,31])
def test_invalid_range_no_read(env,days):
    assert '1-30' in plugin.handle_command(env[1],f'lookback start {days}',source=env[0])
    assert not env[2]


@pytest.mark.parametrize('offset',[-31,1])
def test_timestamp_preflight_blocks_old_or_future_metadata(env,monkeypatch,offset):
    original=sources._run_json
    def shifted(argv,**kwargs):
        result=original(argv,**kwargs)
        if len(argv)>1 and str(argv[1]).endswith('gws_batch_bridge.py'):
            stamp=str(int((lb.now()+timedelta(days=offset)).timestamp()*1000))
            return [dict(item,internalDate=stamp) for item in result]
        return result
    monkeypatch.setattr(sources,'_run_json',shifted)
    scan,c=run(env);assert c is None and scan['staged']==0 and len(env[2])==3


def test_provider_failure_audit_only(env,monkeypatch):
    before=snapshot()
    monkeypatch.setattr(sources,'_gws_bridge',lambda *a: {'error':'private account detail'})
    text=plugin.handle_command(env[1],'lookback start 7',source=env[0])
    assert 'failed' in text and 'private account detail' not in text
    assert snapshot()==before


def test_discard_never_purges_standing(env):
    scan,c=run(env);before=snapshot()
    assert 'discarded' in plugin.handle_command(env[1],f'lookback discard {scan["id"]}',source=env[0])
    assert snapshot()==before
    conn=lb.connect();assert not conn.execute('SELECT * FROM candidates').fetchall();conn.close()


def test_revoke_during_provider_read_discards_previously_staged(env,monkeypatch):
    original=sources._gws_bridge
    def revoke(*args):
        value=original(*args)
        conn=lb.connect();row=conn.execute('SELECT id FROM scans').fetchone();conn.close()
        assert 'revoked' in plugin.handle_command(env[1],f'lookback revoke {row[0]}',source=env[0])
        return value
    monkeypatch.setattr(sources,'_gws_bridge',revoke)
    text=plugin.handle_command(env[1],'lookback start 7',source=env[0])
    assert len(env[2])==1 and 'failed' in text
    conn=lb.connect();assert conn.execute('SELECT state FROM scans').fetchone()[0]=='revoked';assert not conn.execute('SELECT * FROM candidates').fetchall();conn.close()


def test_revoke_during_accept_confirmation_stops_shared_write(env):
    scan,c=run(env)
    def cancel(*args):
        plugin.handle_command(None,f'lookback revoke {scan["id"]}',source=env[0])
        return 'accept'
    env[1].ask_choice=cancel
    assert 'revoked' in plugin.handle_command(env[1],f'lookback accept {c["id"]} "x" "y" "-"',source=env[0])
    assert not store.list_items()


def test_disable_reenable_invalidates_old_candidates(env):
    scan,c=run(env)
    store.disable_source('email',note='owner revoked')
    store.enable_source('email',enabled_by='new explicit grant')
    bind_synthetic_email(store)
    text=plugin.handle_command(env[1],f'lookback list {scan["id"]}',source=env[0])
    assert 'older policy/consent epoch' in text and 'Review request' not in text


def test_partial_cap_disclosed(env,monkeypatch):
    original=sources._gws_bridge
    def read(*args):
        value=original(*args)
        if len(args)>3 and args[2]=='messages' and args[3]=='list':value['nextPageToken']='another-page'
        return value
    monkeypatch.setattr(sources,'_gws_bridge',read)
    scan,c=run(env)
    assert 'Partial coverage' in scan['coverage'] and len(env[2])==9


def test_no_model_scan_or_private_candidate_read_api(env):
    scan,c=run(env)
    for action in ('lookback','get_lookback','lookback_list'):
        result=plugin.handle_tool(None,{'action':action,'item_id':c['id']})
        assert 'Review request' not in result and json.loads(result)['success'] is False


def test_explicit_preference_pending_only(env):
    text=plugin.handle_command(env[1],'lookback preference "Skip generic newsletters"',source=env[0])
    assert 'pending, not active' in text and 'rule approve' in text


def test_accept_cancel_no_item(env):
    scan,c=run(env);env[1].ask_choice=lambda *a:'cancel'
    assert 'cancelled' in plugin.handle_command(env[1],f'lookback accept {c["id"]} "x" "y" "-"',source=env[0])
    assert not store.list_items()


def test_body_context_is_private_paginated_not_dumped_in_list(env):
    scan,c=run(env)
    listed=plugin.handle_command(env[1],f'lookback list {scan["id"]}',source=env[0])
    assert 'Please review the draft by Friday.' not in listed
    viewed=plugin.handle_command(env[1],f'lookback view {c["id"]}',source=env[0])
    assert 'Please review the draft by Friday.' in viewed and 'UNTRUSTED' in viewed
    env[0].user_id='visitor'
    assert 'Please review' not in plugin.handle_command(env[1],f'lookback view {c["id"]}',source=env[0])


def test_list_mail_noise_skips_bodies_but_discloses_flag(env,monkeypatch):
    original=sources._gws_bridge
    def read(*args):
        result=original(*args)
        if json.loads(args[-1]).get('format')=='metadata':result['payload']['headers'].append({'name':'List-Id','value':'newsletter'})
        return result
    monkeypatch.setattr(sources,'_gws_bridge',read)
    scan,c=run(env)
    assert 'false positive' in json.loads(c['payload'])['context_status']
    assert not [v for v in env[2] if v[1].get('format')=='full']


def test_shared_ledger_dedupes_both_directions_and_accounts(env):
    import hermes_tingle_body_audit as audit
    with store.serialized_operation('sources'):
        assert audit.claim('new-id','account-A',origin='lookback')
        assert not audit.claim('new-id','account-A',origin='standing')
        assert audit.claim('new-id','account-B',origin='standing')
        assert not audit.claim('new-id','account-B',origin='lookback')


def test_atomic_body_claim_single_winner(env):
    import hermes_tingle_body_audit as audit
    from concurrent.futures import ThreadPoolExecutor
    def claim(_):
        with store.serialized_operation('sources'):
            return audit.claim('racing','account-A')
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(claim,range(4)))==1


def test_legacy_unqualified_body_blocks_conservatively(env):
    scan,c=run(env)
    # Fresh second scan must not refetch the body even though its metadata is revisited.
    before=len([v for v in env[2] if v[1].get('format')=='full'])
    scan,c=run(env)
    assert len([v for v in env[2] if v[1].get('format')=='full'])==before
    conn=lb.connect();payload=json.loads(conn.execute('SELECT payload FROM candidates ORDER BY rowid DESC LIMIT 1').fetchone()[0]);conn.close()
    assert 'not fetched again' in payload['context_status']


def test_failed_body_attempt_still_deduped_and_disclosed(env,monkeypatch):
    original=sources._gws_bridge
    def read(*args):
        if json.loads(args[-1]).get('format')=='full':
            env[2].append((args,json.loads(args[-1])))
            raise sources.SourceReadError('lost response')
        return original(*args)
    monkeypatch.setattr(sources,'_gws_bridge',read)
    scan,c=run(env)
    assert 'reservation retained 14 days' in json.loads(c['payload'])['context_status']
    assert len([v for v in env[2] if v[1].get('format')=='full'])==1
    run(env)
    assert len([v for v in env[2] if v[1].get('format')=='full'])==1


def test_missing_label_ids_do_not_abort_the_scan(env,monkeypatch):
    original=sources._gws_bridge
    def read(*args):
        value=original(*args)
        if json.loads(args[-1]).get('format')=='metadata':
            value.pop('labelIds',None)
        return value
    monkeypatch.setattr(sources,'_gws_bridge',read)
    scan,c=run(env)
    assert c is not None and scan['staged']==1
    assert not scan['coverage'].startswith('Partial/failed')


def test_body_account_change_fails_before_full_read(env,monkeypatch):
    original=sources._gws_bridge;identities=[]
    def read(*args):
        value=original(*args)
        if args[2]=='getProfile':
            identities.append(1)
            if len(identities)>1:value['emailAddress']='different@example.invalid'
        return value
    monkeypatch.setattr(sources,'_gws_bridge',read)
    scan,c=run(env)
    assert c is None and 'failed' in scan['coverage']
    assert not [v for v in env[2] if v[1].get('format')=='full']


def test_failed_scan_stores_and_shows_exception_detail(env,monkeypatch):
    original=sources._gws_bridge;identities=[]
    def read(*args):
        value=original(*args)
        if args[2]=='getProfile':
            identities.append(1)
            if len(identities)>1:value['emailAddress']='different@example.invalid'
        return value
    monkeypatch.setattr(sources,'_gws_bridge',read)
    scan,c=run(env)
    assert c is None and 'failed' in scan['coverage']
    conn=lb.connect()
    stored=conn.execute('SELECT error_detail FROM scans WHERE id=?',(scan['id'],)).fetchone()['error_detail']
    conn.close()
    assert 'Gmail account changed' in stored
    detail=plugin.handle_command(env[1],f"lookback list {scan['id']} details",source=env[0])
    assert 'Gmail account changed' in detail


def test_failure_detail_is_one_bounded_line(env,monkeypatch):
    original=sources._gws_bridge
    def read(*args):
        if json.loads(args[-1]).get('format')=='metadata':
            raise sources.SourceReadError('bad\nresponse ' + 'A'*500)
        return original(*args)
    monkeypatch.setattr(sources,'_gws_bridge',read)
    scan,c=run(env)
    assert c is None and 'failed' in scan['coverage']
    conn=lb.connect()
    stored=conn.execute('SELECT error_detail FROM scans WHERE id=?',(scan['id'],)).fetchone()['error_detail']
    conn.close()
    assert '\n' not in stored and 'bad response' in stored and len(stored)<=200


def test_thread_out_of_window_is_omitted_before_metadata(env,monkeypatch):
    original=sources._gws_bridge
    def read(*args):
        value=original(*args)
        if args[2]=='threads':value['messages'].append({'id':'too-old'})
        if json.loads(args[-1]).get('id')=='too-old':
            assert json.loads(args[-1])['format']=='minimal'
            return {'id':'too-old','internalDate':'1000'}
        return value
    monkeypatch.setattr(sources,'_gws_bridge',read)
    scan,c=run(env)
    assert 'out-of-window' in json.loads(c['payload'])['context_status']


def test_standing_identity_failure_does_not_reserve(env,monkeypatch):
    import hermes_tingle_body_audit as audit
    store.set_plugin_meta('gmail_account_binding',json.dumps({'generation':store.source_generation('email'),'account':'a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf'}))
    store.register_email_candidates(['new-message'], account_key='a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf')
    before=snapshot()
    monkeypatch.setattr(sources,'body_account_identity',lambda: (_ for _ in ()).throw(sources.SourceReadError('no account')))
    with pytest.raises(sources.SourceReadError):sources.read_email('new-message')
    assert snapshot()==before
    conn=audit.connect();assert not conn.execute('SELECT * FROM reads').fetchall();conn.close()


def test_standing_uses_shared_lookback_reservation(env,monkeypatch):
    import hermes_tingle_body_audit as audit
    store.set_plugin_meta('gmail_account_binding',json.dumps({'generation':store.source_generation('email'),'account':'a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf'}))
    store.register_email_candidates(['new-message'], account_key='a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf')
    with store.serialized_operation('sources'):assert audit.claim('new-message','a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf')
    with pytest.raises(ValueError,match='recently reserved/read'):sources.read_email('new-message')
    assert not env[2]


def test_shared_audit_contains_hashes_not_message_or_body(env):
    import hermes_tingle_body_audit as audit
    scan,c=run(env)
    conn=audit.connect();rows=[dict(r) for r in conn.execute('SELECT * FROM reads')];conn.close()
    assert len(rows)==1 and len(rows[0]['message_id'])==64 and len(rows[0]['account_key'])==64
    assert 'm1' not in rows[0].values() and 'Please review' not in json.dumps(rows)


def test_bound_standing_body_parser_prefers_plain_and_skips_attachment(env,monkeypatch):
    import base64
    def encoded(text):return base64.urlsafe_b64encode(text.encode()).decode()
    calls=[]
    def read(*args):
        calls.append(json.loads(args[-1]))
        return {'id':'m1','threadId':'t1','payload':{'mimeType':'multipart/mixed','headers':[{'name':'Subject','value':'X'}],
                'parts':[{'mimeType':'text/html','body':{'data':encoded('<b>duplicate</b>')}},
                         {'mimeType':'text/plain','body':{'data':encoded('real plain')}},
                         {'mimeType':'text/plain','filename':'attachment.txt','body':{'data':encoded('do not include')}}]}}
    monkeypatch.setattr(sources,'_gws_bridge',read)
    result=sources._read_bound_email('m1','owner@example.invalid')
    assert result['body']=='real plain' and result['subject']=='X'
    assert calls[0]['userId']=='owner@example.invalid' and calls[0]['format']=='full'


def test_real_identity_helper_validates_and_hashes_after_read(env,monkeypatch):
    # Bypass autouse fake identity deliberately, call original module function by reload-free source.
    import hashlib
    import importlib.util
    # The fixture's monkeypatched function is replaced with a tiny exact helper invocation check via standalone loader.
    spec=importlib.util.spec_from_file_location('body_identity_probe',sources.__file__)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    seen=[]
    monkeypatch.setattr(module,'_gws_bridge',lambda *a: seen.append(json.loads(a[-1])) or {'emailAddress':'Owner@example.invalid'})
    address,key=module.body_account_identity()
    assert address=='Owner@example.invalid' and key==hashlib.sha256(b'owner@example.invalid').hexdigest()
    assert seen==[{'userId':'me','fields':'emailAddress'}]


def test_email_source_revoke_clears_shared_audit_and_regrant_can_read(env):
    import hermes_tingle_body_audit as audit
    run(env)
    conn=audit.connect();assert conn.execute('SELECT count(*) FROM reads').fetchone()[0]==1;conn.close()
    store.disable_source('email',note='explicit user revocation')
    conn=audit.connect();assert not conn.execute('SELECT * FROM reads').fetchall();conn.close()
    store.enable_source('email',enabled_by='new explicit optin')
    bind_synthetic_email(store)
    run(env)
    assert len([v for v in env[2] if v[1].get('format')=='full'])==2


def test_oneoff_revoke_clears_its_audit_only(env):
    import hermes_tingle_body_audit as audit
    scan,c=run(env)
    with store.serialized_operation('sources'):
        assert audit.claim('standing-other','other-account',origin='standing')
    plugin.handle_command(None,f'lookback revoke {scan["id"]}',source=env[0])
    conn=audit.connect();rows=conn.execute('SELECT origin FROM reads').fetchall();conn.close()
    assert [r[0] for r in rows]==['standing']


def test_revoke_during_full_body_does_not_recreate_audit(env,monkeypatch):
    import hermes_tingle_body_audit as audit
    original=sources._gws_bridge
    def read(*args):
        result=original(*args)
        if json.loads(args[-1]).get('format')=='full':
            conn=lb.connect();sid=conn.execute('SELECT id FROM scans').fetchone()[0];conn.close()
            assert 'revoked' in plugin.handle_command(None,f'lookback revoke {sid}',source=env[0])
        return result
    monkeypatch.setattr(sources,'_gws_bridge',read)
    run(env)
    conn=audit.connect();assert not conn.execute('SELECT * FROM reads').fetchall();conn.close()


def test_body_dedupe_expiry_allows_new_claim(env):
    import hermes_tingle_body_audit as audit
    with store.serialized_operation('sources'):
        assert audit.claim('stale','account',now=lb.now()-timedelta(days=15))
        assert audit.claim('stale','account',now=lb.now())
        assert not audit.claim('stale','account',now=lb.now())


def test_time_budget_stops_without_unbounded_scan(env,monkeypatch):
    original=sources._gws_bridge
    def read(*args):
        value=original(*args)
        lb._read_budget.get()['deadline']=lb.now()-timedelta(seconds=1)
        return value
    monkeypatch.setattr(sources,'_gws_bridge',read)
    scan,c=run(env)
    assert c is None and scan['state']=='consumed' and len(env[2])==1
    assert 'Partial/failed' in scan['coverage']


@pytest.mark.parametrize('bad',[{'mimeType':'text/plain','body':[]}, {'mimeType':'text/plain','body':'bad'}, []])
def test_malformed_body_fails_redacted(bad):
    with pytest.raises(sources.SourceReadError):
        lb.body_text(bad)


@pytest.mark.parametrize('key,value',[('labelIds',[{}]),('payload',['bad'])])
def test_malformed_metadata_fails_closed(env,monkeypatch,key,value):
    original=sources._gws_bridge
    def bridge(*args):
        result=original(*args)
        if json.loads(args[-1]).get('format')=='metadata':result[key]=value
        return result
    monkeypatch.setattr(sources,'_gws_bridge',bridge)
    scan,c=run(env)
    assert scan['state']=='consumed' and not c
    assert 'Partial/failed' in scan['coverage']
    assert not any(p.get('format')=='full' for _,p in env[2])


def test_audit_cleanup_failure_is_not_success_and_revoke_can_retry(env,monkeypatch):
    import hermes_tingle_body_audit as audit
    run(env)
    original=audit.clear
    def fail(**kwargs):raise sqlite3.OperationalError('synthetic cleanup unavailable')
    monkeypatch.setattr(audit,'clear',fail)
    with pytest.raises(sqlite3.OperationalError):store.disable_source('email')
    assert not store.source_enabled('email')
    conn=audit.connect();assert conn.execute('SELECT count(*) FROM reads').fetchone()[0]==1;conn.close()
    monkeypatch.setattr(audit,'clear',original)
    store.disable_source('email')
    conn=audit.connect();assert conn.execute('SELECT count(*) FROM reads').fetchone()[0]==0;conn.close()


def test_expired_scan_owner_can_revoke_audit_without_reopening_content(env):
    import hermes_tingle_body_audit as audit
    scan,c=run(env)
    conn=lb.connect();conn.execute('UPDATE scans SET expires=?',((lb.now()-timedelta(seconds=1)).isoformat(),));conn.commit();conn.close()
    assert 'expired' in plugin.handle_command(env[1],f'lookback list {scan["id"]}',source=env[0])
    conn=audit.connect();assert conn.execute('SELECT count(*) FROM reads').fetchone()[0]==1;conn.close()
    env[0].user_id='other'
    assert 'revoked and' not in plugin.handle_command(None,f'lookback revoke {scan["id"]}',source=env[0])
    env[0].user_id='42'
    for _ in range(2):
        assert 'revoked and' in plugin.handle_command(None,f'lookback revoke {scan["id"]}',source=env[0])
    conn=audit.connect();assert not conn.execute('SELECT * FROM reads').fetchall();conn.close()
    assert 'expired' in plugin.handle_command(env[1],f'lookback view {c["id"]}',source=env[0]) or 'unavailable' in plugin.handle_command(env[1],f'lookback view {c["id"]}',source=env[0]).lower()


def test_oneoff_cleanup_failure_invalidates_grant_and_idempotent_retry(env,monkeypatch):
    import hermes_tingle_body_audit as audit
    scan,c=run(env)
    original=audit.clear
    monkeypatch.setattr(audit,'clear',lambda **kw: (_ for _ in ()).throw(sqlite3.OperationalError('synthetic failure')))
    with pytest.raises(sqlite3.OperationalError):lb.handle(None,['revoke',scan['id']],env[0])
    conn=lb.connect();assert conn.execute('SELECT state FROM scans').fetchone()[0]=='revoked';assert not conn.execute('SELECT * FROM candidates').fetchall();conn.close()
    conn=audit.connect();assert conn.execute('SELECT count(*) FROM reads').fetchone()[0]==1;conn.close()
    monkeypatch.setattr(audit,'clear',original)
    assert 'revoked and' in lb.handle(None,['revoke',scan['id']],env[0])
    conn=audit.connect();assert not conn.execute('SELECT * FROM reads').fetchall();conn.close()


def test_preview_discloses_body_storage_and_cleanup(env):
    previews=[]
    env[1].ask_choice=lambda prompt,choices: previews.append(prompt) or 'cancel'
    plugin.handle_command(env[1],'lookback start 7',source=env[0])
    assert 'text stays in private DM staging' in previews[0] and '7 days' in previews[0]
    assert 'physical cleanup happens on the next look-back operation' not in previews[0]
    assert 'inline MIME' not in previews[0] and 'No attachment-download endpoint' not in previews[0]
    help_text=plugin.handle_command(env[1],'lookback help',source=env[0])
    assert 'physical cleanup happens on the next look-back operation' in help_text
    assert 'inline MIME' in help_text and 'No attachment-download endpoint' in help_text


def test_preview_is_short_with_consent_essentials(env):
    previews=[]
    env[1].ask_choice=lambda prompt,choices: previews.append(prompt) or 'cancel'
    plugin.handle_command(env[1],'lookback start 30',source=env[0])
    preview=previews[0]
    assert len(preview) < 1400  # Fits one control prompt with its choice labels; model details live in help.
    assert 'short excerpts to your configured Hermes model' in preview and "can't cap or see the cost" in preview
    for phrase in ('last 30 days', 'Bounded scan', 'not full mailbox coverage',
                   'Nothing is auto-added', 'confirmed items are shared on this profile',
                   'Ongoing source settings stay unchanged', 'visible only to you',
                   'Full details: `/tingle lookback help`', 'possibly cloud/billed',
                   'host retries may add requests', 'Provider/history/backups', '45 days',
                   'Scan only sends nothing to a model'):
        assert phrase in preview, phrase
    # Detailed numeric limits remain in the explicit disclosure, not the first screen.
    help_text=plugin.handle_command(env[1],'lookback help',source=env[0])
    for material in ('at most 6 requests of 12','60 s timeout','2500 output tokens','retry or fall back','1800 characters','72 of these conversations'):
        assert material in help_text,material
    assert '120-second' not in preview


def test_help_carries_full_disclosure_and_usage(env):
    text=plugin.handle_command(env[1],'lookback help',source=env[0])
    for phrase in ('120-second/250-provider-call','not full mailbox coverage','physical cleanup','inline MIME',
                   'lookback start','lookback list LB-ID [page], or add `details`','lookback accept LC-ID','lookback discard LB-ID'):
        assert phrase in text,phrase


def test_scan_start_goes_straight_to_review_and_details_stay_available(env):
    text=plugin.handle_command(env[1],'lookback start 7',source=env[0])
    assert text.startswith('⏸ Review paused:')
    assert 'Track it, skip it, or stop here?' in env[1].questions[-1][0]
    match = re.search(r'LB-[0-9A-F]{8}', text)
    assert match, text
    scan_id=match.group(0)
    summary=lb._results_reply(env[0], lb.binding(env[0]), scan_id)
    assert '1 possible follow-up in the checked messages' in summary and 'nothing added' in summary
    assert 'provider calls' not in text and 'listed IDs' not in text
    details=plugin.handle_command(env[1],f'lookback list {scan_id} details',source=env[0])
    assert 'provider calls' in details and 'Listed 1' in details and 'date-checked 1' in details


def test_scan_summary_suggested_commands_are_backticked(env):
    text=plugin.handle_command(env[1],'lookback start 7',source=env[0])
    match = re.search(r'LB-[0-9A-F]{8}', text)
    assert match, text
    scan_id=match.group(0)
    assert f'`/tingle lookback review {scan_id}`' in text and f'`/tingle lookback discard {scan_id}`' in text
    assert 'track-top' not in text and 'track-all' not in text


def test_lead_list_pagination_hint_is_backticked(env):
    scan,c=run(env)
    conn=lb.connect()
    payload=conn.execute('SELECT payload FROM candidates WHERE id=?',(c['id'],)).fetchone()[0]
    for i in range(10):
        conn.execute('INSERT INTO candidates(id,scan_id,generation,payload) VALUES(?,?,1,?)',
                     (f'LC-EXTRA{i:02d}',scan['id'],json.dumps({**json.loads(payload), 'thread_id': f'extra{i}'})))
    conn.commit();conn.close()
    text=plugin.handle_command(env[1],f'lookback list {scan["id"]}',source=env[0])
    assert f'More: `/tingle lookback list {scan["id"]} 2`' in text


def test_list_entries_are_readable_not_raw_json(env):
    import re as _re
    scan,c=run(env)
    conn=lb.connect()
    payload=json.loads(conn.execute('SELECT payload FROM candidates WHERE id=?',(c['id'],)).fetchone()[0])
    payload['subject']='It&#39;s &quot;synthetic&quot; &amp; escaped'
    payload['from']='Synthetic Sender &lt;sender@example.invalid&gt;'
    payload['snippet']='A fully synthetic snippet &gt; nothing real'
    payload['context_status']='Body/thread cap reached; metadata-only lead, context incomplete.'
    conn.execute('UPDATE candidates SET payload=? WHERE id=?',(json.dumps(payload),c['id']))
    conn.commit();conn.close()
    text=plugin.handle_command(env[1],f'lookback list {scan["id"]}',source=env[0])
    assert '&#39;' not in text and '&quot;' not in text and '&amp;' not in text and '&lt;' not in text
    assert 'It\'s "synthetic" & escaped' in text
    assert 'Synthetic Sender' in text
    assert '{"subject"' not in text
    assert 'UNTRUSTED' in text
    assert 'Context incomplete' in text
    assert _re.search(r'\d{1,2} \w{3} \d{4}',text)
    assert '/tingle lookback help' in text
    assert 'Accept requires your own reviewed fields' not in text


@pytest.mark.parametrize('payload',[
    {'body':'bad'}, {'body':[1]}, {'headers':'bad'}, {'parts':'bad'},
    {'parts':[{'body':[1]}]}, {'parts':[{'headers':{}}]}, {'parts':[{'parts':'bad'}]},
    {'parts':[1]}, {'body':{'data':123}}, {'headers':[{'name':'Subject'}]},
    {'body':{'data':'@@bad'}},
])
def test_both_body_parsers_fail_redacted_for_malformed_mime(env,monkeypatch,payload):
    with pytest.raises(sources.SourceReadError,match='Malformed'):
        lb.body_text(payload)
    monkeypatch.setattr(sources,'_gws_bridge',lambda *a:{'id':'m1','threadId':'t1','payload':payload})
    with pytest.raises(sources.SourceReadError,match='Malformed'):
        sources._read_bound_email('m1','owner@example.invalid')


@pytest.mark.parametrize('title,summary',[('TASK-123 excluded','Summary'),('Normal','TASK-123 excluded')])
def test_final_reviewed_fields_obey_ticket_exclusions(env,title,summary):
    scan,c=run(env)
    text=plugin.handle_command(env[1],f'lookback accept {c["id"]} "{title}" "{summary}" "-"',source=env[0])
    assert 'excluded' in text and not store.list_items()


def test_thread_context_reused_for_each_lead_without_provider_reread(env,monkeypatch):
    original=sources._gws_bridge
    def bridge(*args):
        p=json.loads(args[-1]);v=original(*args)
        if args[3]=='list':return {'messages':[{'id':'m1'},{'id':'m2'}]}
        if p.get('id')=='m2':v['id']='m2'
        return v
    monkeypatch.setattr(sources,'_gws_bridge',bridge)
    run(env)
    conn=lb.connect();rows=conn.execute('SELECT payload FROM candidates').fetchall();conn.close()
    assert len(rows)==2
    assert all(json.loads(row[0])['thread_context'][0]['body'] for row in rows)
    assert sum(p.get('format')=='full' for _,p in env[2])==1


@pytest.mark.parametrize('same_fields',[True,False])
def test_cross_account_unqualified_item_collision_refuses_honestly(env,monkeypatch,same_fields):
    scan,c=run(env)
    assert 'Accepted' in plugin.handle_command(env[1],f'lookback accept {c["id"]} "Review" "summary" "-"',source=env[0])
    original=sources._gws_bridge
    def second(*args):
        if args[2]=='getProfile':return {'emailAddress':'second@example.invalid'}
        return original(*args)
    monkeypatch.setattr(sources,'_gws_bridge',second)
    run(env)
    conn=lb.connect();c2=conn.execute("SELECT id FROM candidates WHERE state='pending'").fetchone()[0];conn.close()
    title='Review' if same_fields else 'Different'
    text=plugin.handle_command(env[1],f'lookback accept {c2} "{title}" "summary" "-"',source=env[0])
    assert 'Accepted' in text
    assert len(store.list_items())==2
    assert len({x['source_ref'] for x in store.list_items()})==2


def test_legacy_unqualified_item_collision_is_not_claimed_as_acceptance(env):
    scan,c=run(env)
    store.add_item(title='Review',summary='summary',source_type='email',source_ref='gmail:t1#m1')
    text=plugin.handle_command(env[1],f'lookback accept {c["id"]} "Review" "summary" "-"',source=env[0])
    assert 'Nothing accepted' in text and len(store.list_items())==1


def test_concurrent_unqualified_item_insert_not_falsely_accepted(env,monkeypatch):
    scan,c=run(env)
    original=store.add_item
    def raced(**kwargs):
        original(**kwargs)
        return original(**kwargs)
    monkeypatch.setattr(store,'add_item',raced)
    text=plugin.handle_command(env[1],f'lookback accept {c["id"]} "Review" "summary" "-"',source=env[0])
    assert 'Concurrent account-unqualified' in text and len(store.list_items())==1
    conn=lb.connect();assert conn.execute('SELECT state FROM candidates WHERE id=?',(c['id'],)).fetchone()[0]=='pending';conn.close()


def test_help_is_sectioned_not_a_wall(env):
    text=plugin.handle_command(env[1],'lookback help',source=env[0])
    for heading in ('What it does','What it can miss','Where it is kept','What you stay in control of','Privacy','Commands'):
        assert f'\n{heading}\n' in text,heading
    assert 'Example: /tingle lookback start 14' in text
    assert max(len(p) for p in text.split('\n\n')) < 1100
    for phrase in ('Nothing is auto-added','visible to everyone on this profile','STORED','Explicit discard',
                   'Removal does not erase history','Turning a source off stops future reads',
                   "can't pull back content already sent", 'Google may refresh its saved sign-in on this profile',
                   'content-free body-read audit','no built-in Tingle-operated upload service'.replace('no built-in','has no built-in')):
        assert phrase in text,phrase


def test_list_explains_staged_vs_tracked_and_next_steps(env):
    scan,c=run(env)
    text=plugin.handle_command(env[1],f'lookback list {scan["id"]}',source=env[0])
    assert 'Nothing tracked yet' in text
    for step in ('Nothing tracked yet','Private for 7 days','`/tingle lookback help`'):
        assert step in text,step
    assert len(text)<900


def test_epoch_refusal_and_dedupe_label_say_what_to_do():
    import inspect
    src=inspect.getsource(lb)
    assert 'Discard it with /tingle lookback discard LB-ID, then start a new one' in src


def test_bare_accept_prepares_for_confirmation_and_never_adds_silently(env):
    scan,c=run(env)
    prompts=[]
    env[1].ask_choice=lambda prompt,choices: prompts.append(prompt) or 'cancel'
    text=plugin.handle_command(env[1],f'lookback accept {c["id"]}',source=env[0])
    assert 'cancelled; nothing added' in text
    assert prompts and 'SHARED profile' in prompts[0] and 'Due: none' in prompts[0]
    assert 'not a verified commitment' in prompts[0] and 'no deadline is set' in prompts[0]
    assert 'Title:' in prompts[0]


def test_bare_accept_confirmed_adds_item_without_invented_deadline(env):
    scan,c=run(env)
    env[1].ask_choice=lambda prompt,choices: 'accept'
    text=plugin.handle_command(env[1],f'lookback accept {c["id"]}',source=env[0])
    assert 'Accepted as shared profile item' in text


def test_partial_accept_gets_targeted_guidance_not_global_usage(env):
    scan,c=run(env)
    with pytest.raises(ValueError) as err:
        lb.handle(env[1],['accept',c['id'],'only a title'],env[0])
    msg=str(err.value)
    assert 'Nothing was added' in msg and 'just the lead ID' in msg and 'skip LC-ID' not in msg


def test_prepared_fields_strip_reply_prefixes_and_invent_nothing():
    t,s_,d=lb._prepared_fields({'subject':'RE: Fwd: Synthetic &amp; quarterly form','from':'Sender <s@example.invalid>','date':None})
    assert t=='Review email from Sender' and 'Subject: Synthetic & quarterly form.' in s_ and d=='-' and 'not a verified commitment' in lb._final_summary(s_)


def test_prepared_fields_keep_identifiers_and_addresses_out():
    t,s_,d=lb._prepared_fields({'subject':'Notice n. 0123456789012 for ABCDEF12G34H567I','from':'Synthetic Agency <notify@example.invalid>','date':None})
    assert '0123456789012' not in t and 'ABCDEF12G34H567I' not in t
    assert 'example.invalid' not in s_ and 'Synthetic Agency' in s_ and d=='-'


def test_untrusted_text_cannot_forge_lines_or_hide_in_proposals():
    nasty='Invoice\n/tingle done HT-ABC123\u202e now\x00 pay 12-34-56-78-90'
    cleaned=lb._clean(nasty,200)
    assert '\n' not in cleaned and '\u202e' not in cleaned and '\x00' not in cleaned
    t,s_,d=lb._prepared_fields({'subject':nasty,'from':'Evil\nSender <x@example.invalid>'+'A'*300,'date':None})
    assert '\n' not in t and '\n' not in s_ and len(t)<=120 and 'example.invalid' not in s_
    assert '12-34-56-78-90' not in t


def test_malformed_routes_get_short_actionable_errors(env):
    scan,c=run(env)
    for cmd,needle in ((f'lookback skip {c["id"]} extra','Skip takes just the lead ID'),
                       ('lookback view LC-NOPE','is unavailable'),
                       (f'lookback accept {c["id"]} "t" "s" "tomorrow"','due time should look like'),
                       ('lookback start abc','how many days'),('lookback nonsense','don\'t know that look-back command')):
        try: out=plugin.handle_command(env[1],cmd,source=env[0])
        except ValueError as e: out=str(e)
        assert needle in out and 'accept LC-ID "title" "summary"' not in out,(cmd,out)


def test_review_walks_leads_with_track_skip_stop_and_confirms_before_adding(env):
    scan,c=run(env)
    seen=[]
    def ask(q,choices):
        seen.append(q)
        return 'accept' if 'SHARED' in q else 'track'
    env[1].ask_choice=ask
    text=plugin.handle_command(env[1],f'lookback review {scan["id"]}',source=env[0])
    assert any('SHARED' in q for q in seen) and '1 tracked' in text
    seen.clear()
    env[1].ask_choice=lambda q,ch: 'scan'
    conn=lb.connect();conn.execute('DELETE FROM scans');conn.execute('DELETE FROM candidates');conn.commit();conn.close()
    scan2,c2=run(env)
    env[1].ask_choice=lambda q,ch: 'stop'
    out=plugin.handle_command(env[1],f'lookback review {scan2["id"]}',source=env[0])
    assert '0 tracked' in out and 'still waiting' in out


def test_confirm_prompt_is_short_and_leads_with_the_item(env):
    scan,c=run(env)
    prompts=[]
    env[1].ask_choice=lambda q,ch: prompts.append(q) or 'cancel'
    plugin.handle_command(env[1],f'lookback accept {c["id"]}',source=env[0])
    q=prompts[0]
    assert q.index('Title:')<q.index('Summary:')<q.index('Due:')<q.index('Ref:')
    assert 'untrusted email' in q and 'SHARED profile' in q and len(q)<900


def test_bare_everyday_commands_say_what_is_missing(env):
    for cmd,needle in (('done','Which item is finished'),('snooze','how long'),('dismiss','stop reminding'),('chatconsent','included by default')):
        out=plugin.handle_command(env[1],cmd,source=env[0])
        assert needle in out and 'Nothing changed' in out,(cmd,out)


def test_all_clear_reports_enabled_sources_not_off(env):
    store.add_item(title='Synthetic tracked item')  # non-empty profile: no onboarding prompt
    store.complete_item(store.list_items()[0]['id'])
    out=plugin.handle_command(env[1],'',source=env[0])
    assert 'Sources on:' in out and 'off until you opt in' not in out


def test_mixed_date_failures_continue_verified_subset(env, monkeypatch):
    original = sources._gws_bridge
    def bridge(*args):
        if args[3] == 'list':
            return {'messages': [{'id': 'm1'}, {'id': 'm2'}]}
        return original(*args)
    monkeypatch.setattr(sources, '_gws_bridge', bridge)
    original_helper=sources._run_json
    def dates(argv, **kwargs):
        if json.loads(argv[-1]).get('mode')=='metadata':
            return original_helper(argv,**kwargs)
        assert json.loads(argv[-1])['ids'] == ['m1', 'm2']
        return {'dates': [{'id': 'm1', 'internalDate': env[4]}], 'calls': 3, 'unverified': 1,
                'diagnostic': 'Gmail batch date gaps: 1/2 parts returned 503 backendError/UNAVAILABLE'}
    monkeypatch.setattr(sources, '_run_json', dates)
    scan, candidate = run(env)
    assert candidate is not None and scan['checked'] == scan['staged'] == 1
    assert scan['coverage'].startswith('Partial') and '503' in scan['error_detail']
    assert not any(p.get('id') == 'm2' for _, p in env[2])


def test_helper_failure_does_not_discard_prior_chunk(env, monkeypatch):
    original = sources._gws_bridge
    ids = ['m1'] + [f'm{i}' for i in range(2, 22)]
    def bridge(*args):
        if args[3] == 'list':
            return {'messages': [{'id': mid} for mid in ids]}
        return original(*args)
    monkeypatch.setattr(sources, '_gws_bridge', bridge)
    original_helper=sources._run_json
    def dates(argv, **kwargs):
        if json.loads(argv[-1]).get('mode')=='metadata':
            return original_helper(argv,**kwargs)
        chunk = json.loads(argv[-1])['ids']
        if chunk == ['m21']:
            raise sources.SourceReadError('synthetic timeout')
        # All other IDs have dates outside the window, apart from m1.
        return [{'id': mid, 'internalDate': env[4] if mid == 'm1' else '1000000000000'} for mid in chunk]
    monkeypatch.setattr(sources, '_run_json', dates)
    scan, candidate = run(env)
    assert candidate is not None and scan['checked'] == 20 and scan['staged'] == 1
    assert scan['coverage'].startswith('Partial')


def test_date_retry_calls_respect_budget_and_skip_unverified_ids(env, monkeypatch):
    # Direct provider harness lets this test pin the cap without 250 fake reads.
    monkeypatch.setattr(lb, 'authorize_scan', lambda *a, **k: None)
    token = lb._read_budget.set({'deadline': lb.now() + timedelta(seconds=120), 'calls': 248})
    monkeypatch.setattr(sources, '_run_json', lambda *a, **k: pytest.fail('budget exhausted'))
    try:
        dates, gaps = lb.provider_dates(None, 'LB-FAKE', None, None, ['m1'])
        assert not dates and gaps and lb._read_budget.get()['calls'] == 248
    finally:
        lb._read_budget.reset(token)


def test_hundred_date_reads_mixed_503_integrates_real_helper(env, monkeypatch):
    import gws_batch_bridge as bb
    from test_batch_bridge import Response, _part
    monkeypatch.setattr(lb, 'authorize_scan', lambda *a, **k: None)
    requests = []
    def opener(req, **kwargs):
        ids = re.findall(r'/messages/([^?]+)\?', req.data.decode())
        requests.append(ids)
        return Response(''.join(_part(mid, env[4], '200 OK' if int(mid[1:]) < 42
                                      else '503 Service Unavailable') for mid in ids).encode())
    def helper(argv, **kwargs):
        data = json.loads(argv[-1])
        return bb.batch_dates(data['userId'], data['ids'], token='synthetic', opener=opener, sleep=lambda _: None)
    monkeypatch.setattr(sources, '_run_json', helper)
    token = lb._read_budget.set({'deadline': lb.now() + timedelta(seconds=120), 'calls': 2})
    try:
        dates, gaps = lb.provider_dates(None, 'LB-FAKE', None, None, [f'm{i}' for i in range(100)])
        assert len(dates) == 42 and gaps
        assert all(len(chunk) <= 20 for chunk in requests)
        assert len(requests) == 11 and lb._read_budget.get()['calls'] == 13
        assert all(int(mid[1:]) >= 42 for chunk in requests[3:5] for mid in chunk)
    finally:
        lb._read_budget.reset(token)
