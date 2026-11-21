"""Regression reversals of synthetic privacy/source probes.
Run from repo: PYTHONPATH=.:tests PYTHONDONTWRITEBYTECODE=1 python -m pytest -p conftest -p test_lookback -p no:cacheprovider /path/to/repro_privacy_boundaries.py -s -q --basetemp /isolated/scratch/path
"""
import pytest
import base64
import json
from datetime import timedelta
import hermes_tingle as plugin
import hermes_tingle_lookback as lb
import hermes_tingle_sources as sources
import hermes_tingle_store as store
from test_lookback import run, env


def test_expired_content_survives_next_lookback_operations(env):
    scan, c = run(env)
    conn=lb.connect(); conn.execute('UPDATE scans SET expires=?', ((lb.now()-timedelta(seconds=1)).isoformat(),)); conn.commit(); conn.close()
    res=plugin.handle_command(env[1], 'lookback view LC-NOTFOUND', source=env[0])
    conn=lb.connect(); row=conn.execute('SELECT payload FROM candidates WHERE id=?', (c['id'],)).fetchone(); conn.close()
    assert row is None
    assert 'unavailable' in res.lower()



def test_unqualified_standing_candidate_allows_new_account_body(env, monkeypatch):
    # Actually collect account A through the standing collector, then switch to B.
    store.enable_source('email',lookback_days=7,enabled_by='synthetic standing consent')
    account=['a@example.invalid']
    stamp=str(int(lb.now().timestamp()*1000))
    calls=[]
    def fake(*args):
        p=json.loads(args[-1]); calls.append((account[0],p))
        if args[2]=='getProfile':
            return {'historyId':'100'} if p.get('fields')=='historyId' else {'emailAddress':account[0]}
        if args[2]=='messages' and args[3]=='list': return {'messages':[{'id':'same-id'}]}
        if p.get('format')=='minimal': return {'id':'same-id','internalDate':stamp}
        if p.get('format')=='metadata': return {'id':'same-id','threadId':'thread-a','internalDate':stamp,'labelIds':['INBOX'],'snippet':'A candidate','payload':{'headers':[{'name':'Subject','value':'A'}]}}
        if p.get('format')=='full': return {'id':'same-id','threadId':'thread-b','payload':{'mimeType':'text/plain','body':{'data':base64.urlsafe_b64encode(b'B private body').decode()}}}
        raise AssertionError(p)
    monkeypatch.setattr(sources,'_gws_bridge',fake)
    # Fixture replaces the identity helper; reload the genuine helper into an isolated module.
    import importlib.util
    spec=importlib.util.spec_from_file_location('identity_probe',sources.__file__)
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    module.SourceReadError=sources.SourceReadError
    monkeypatch.setattr(module,'_gws_bridge',fake)
    monkeypatch.setattr(sources,'body_account_identity',module.body_account_identity)
    collected=sources._collect_email_candidates(now=lb.now(),lookback_days=7)
    assert collected['emails'][0]['snippet']=='A candidate'
    account[0]='b@example.invalid'
    with pytest.raises((ValueError,sources.SourceReadError)):
        sources.read_email('same-id')
    assert not any(p.get('format')=='full' for _,p in calls)



def test_accepted_long_exact_reference_is_truncated(env, monkeypatch):
    orig=sources._gws_bridge
    tid='t'*250
    def bridge(*args):
        p=json.loads(args[-1]); result=orig(*args)
        if args[2]=='threads': result['id']=tid
        elif isinstance(result,dict) and result.get('threadId')=='t1': result['threadId']=tid
        return result
    monkeypatch.setattr(sources,'_gws_bridge',bridge)
    scan,c=run(env)
    payload=json.loads(c['payload']); preview=[]
    env[1].ask_choice=lambda q,ch: preview.append(q) or 'accept'
    result=plugin.handle_command(env[1],f'lookback accept {c["id"]} "Review" "summary" "-"',source=env[0])
    item=store.list_items()[0]
    print('reference:', len(payload['source_ref']), len(item['source_ref']), 'accepted:',result)
    assert payload['source_ref'] in preview[0]
    assert 'Accepted' in result and item['source_ref']==payload['source_ref']
