"""Look-back metadata batching boundaries, no account or network access."""
import json
from datetime import timedelta
import pytest
import hermes_tingle_lookback as lb
import hermes_tingle_sources as src
from test_lookback import env,run


def test_hundred_lookback_metadata_are_five_helper_requests(env,monkeypatch):
    old_bridge=src._gws_bridge;old_helper=src._run_json;chunks=[]
    def bridge(*args):
        if args[3]=='list':return {'messages':[{'id':f'm{i}'} for i in range(100)]}
        return old_bridge(*args)
    def helper(argv,**kw):
        req=json.loads(argv[-1])
        if req.get('mode')!='metadata':return old_helper(argv,**kw)
        chunks.append(req['ids'])
        return {'messages':[{'id':mid,'threadId':'t'+mid,'internalDate':env[4],
            'labelIds':['INBOX'],'snippet':'synthetic','payload':{'headers':[
                {'name':'Subject','value':'Synthetic'},{'name':'List-Id','value':'synthetic-list'}]}}
            for mid in req['ids']], 'errors':[],'http_calls':1,'inner_calls':len(req['ids'])}
    monkeypatch.setattr(src,'_gws_bridge',bridge);monkeypatch.setattr(src,'_run_json',helper)
    scan,candidate=run(env)
    assert scan['checked']==scan['staged']==100 and len(chunks)==5
    assert all(len(c)==20 for c in chunks)
    assert 'provider calls 12 (logical helper/batch calls)' in scan['coverage']
    assert 'inner API attempts 202' in scan['coverage']
    assert not any(p.get('format')=='full' for _,p in env[2])


def test_metadata_provider_cap_reserves_before_unknown_attempt(monkeypatch):
    monkeypatch.setattr(lb,'authorize_scan',lambda *a,**k:None)
    monkeypatch.setattr(src,'_metadata_batch',lambda *a,**kw:(_ for _ in ()).throw(src.SourceReadError('timeout')))
    token=lb._read_budget.set({'deadline':lb.now()+timedelta(seconds=120),'calls':249,'inner_calls':5})
    try:
        with pytest.raises(src.SourceReadError):list(lb.provider_metadata(None,'scan',None,None,['m1','m2']))
        assert lb._read_budget.get()['calls']==250 and lb._read_budget.get()['inner_calls']==7
    finally:lb._read_budget.reset(token)


def test_metadata_provider_budget_stops_before_helper(monkeypatch):
    monkeypatch.setattr(lb,'authorize_scan',lambda *a,**k:None)
    monkeypatch.setattr(src,'_metadata_batch',lambda *a,**kw:pytest.fail('no helper allowed'))
    token=lb._read_budget.set({'deadline':lb.now()+timedelta(seconds=120),'calls':250,'inner_calls':5})
    try:
        with pytest.raises(src.SourceReadError,match='budget'):list(lb.provider_metadata(None,'scan',None,None,['m1']))
        assert lb._read_budget.get()['inner_calls']==5
    finally:lb._read_budget.reset(token)


def test_revocation_during_metadata_cannot_stage_or_read_bodies(env,monkeypatch):
    import hermes_tingle_store as store
    original=src._run_json;chunks=[]
    def helper(argv,**kw):
        req=json.loads(argv[-1])
        if req.get('mode')=='metadata':
            chunks.append(req['ids']);store.disable_source('email')
        return original(argv,**kw)
    monkeypatch.setattr(src,'_run_json',helper)
    import hermes_tingle as plugin
    text=plugin.handle_command(env[1],'lookback start 7',source=env[0])
    assert 'consent changed' in text
    conn=lb.connect();candidate=conn.execute('SELECT * FROM candidates').fetchone();conn.close()
    assert chunks==[['m1']] and candidate is None
    assert not any(p.get('format')=='full' for _,p in env[2])
