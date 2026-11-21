"""Strict host batch boundary checks with a synthetic helper."""
import json
import pytest
import hermes_tingle_sources as src


def payload():
    return {'messages':[{'id':'m1','threadId':'t1','internalDate':'1763300000000',
        'snippet':'synthetic','payload':{'headers':[{'name':'Subject','value':'Synthetic'}]}}],
        'errors':[],'http_calls':1,'inner_calls':1}


def test_host_checks_before_and_after_one_helper_call(monkeypatch):
    checks=[];requests=[]
    def run(argv,**kw):
        requests.append(json.loads(argv[-1])); return payload()
    monkeypatch.setattr(src,'_run_json',run)
    out=src._metadata_batch('owner@example.invalid',['m1'],'standing',check=lambda:checks.append(1))
    assert out['m1']['id']=='m1' and len(checks)==3
    assert requests==[{'mode':'metadata','userId':'owner@example.invalid','ids':['m1'],'profile':'standing'}]


def test_revocation_after_helper_discards_output(monkeypatch):
    state=[]
    def check():
        if state:raise src.SourceReadError('consent changed')
    def run(*a,**kw):state.append(1);return payload()
    monkeypatch.setattr(src,'_run_json',run)
    with pytest.raises(src.SourceReadError,match='consent changed'):
        src._metadata_batch('owner@example.invalid',['m1'],'standing',check=check)


def test_helper_failure_still_rechecks_and_never_retries(monkeypatch):
    calls=[];checks=[]
    def run(*a,**kw):calls.append(1);raise src.SourceReadError('connection timed out')
    monkeypatch.setattr(src,'_run_json',run)
    with pytest.raises(src.SourceReadError):
        src._metadata_batch('owner@example.invalid',['m1'],'standing',check=lambda:checks.append(1))
    assert calls==[1] and checks==[1,1]


@pytest.mark.parametrize('mutate',[
    lambda p:p['messages'][0].update(id='other'),
    lambda p:p['messages'][0].update(threadId='../bad'),
    lambda p:p['messages'][0].update(internalDate='untrustworthy'),
    lambda p:p['messages'][0].update(body='not allowed'),
    lambda p:p['messages'][0]['payload'].update(body='not allowed'),
    lambda p:p['messages'][0]['payload']['headers'].append({'name':'Unexpected','value':'x'}),
    lambda p:p['messages'].append(p['messages'][0]),
    lambda p:p.update(inner_calls=0),
    lambda p:p.update(http_calls=2),
    lambda p:p.update(messages=[]),
])
def test_host_rejects_corrupt_identity_fields_and_accounting(monkeypatch,mutate):
    p=payload();mutate(p)
    monkeypatch.setattr(src,'_run_json',lambda *a,**kw:p)
    with pytest.raises(src.SourceReadError):
        src._metadata_batch('owner@example.invalid',['m1'],'standing',check=lambda:None)


@pytest.mark.parametrize('status,kind',[(404,'not_found'),(401,'auth'),(403,'rate_limit'),(403,'transient'),(503,'transient'),(418,'transient')])
def test_per_id_failures_keep_typed_dispatch(monkeypatch,status,kind):
    p={'messages':[],'errors':[{'id':'m1','status':status,'kind':kind}],'http_calls':1,'inner_calls':1}
    monkeypatch.setattr(src,'_run_json',lambda *a,**kw:p)
    out=src._metadata_batch('owner@example.invalid',['m1'],'standing',check=lambda:None)
    assert isinstance(out['m1'],src.SourceReadError) and out['m1'].kind==kind
    assert src._is_not_found_error(out['m1'])==(kind=='not_found')
    assert src._classify_read_error(out['m1'])==('auth' if kind=='auth' else 'transient')
