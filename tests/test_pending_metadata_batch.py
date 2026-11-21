"""Active pending metadata route, real host validation, synthetic helper only."""
import json
from datetime import datetime,timedelta,timezone
import pytest
import hermes_tingle_sources as src
import hermes_tingle_store as store
import hermes_constants
from delivery_fakes import bind_synthetic_email

NOW=datetime(2026,11,18,12,tzinfo=timezone.utc)


@pytest.fixture
def bound(synthetic_settings):
    bind_synthetic_email(store)
    account='owner@example.invalid'
    import hashlib
    binding=(account,hashlib.sha256(account.encode()).hexdigest(),store.source_generation('email'),
             hermes_constants.get_hermes_home().resolve(),src.load_settings())
    token=src._gmail_account.set(binding)
    try:yield binding
    finally:src._gmail_account.reset(token)


def message(mid,stamp=None,labels=None):
    return {'id':mid,'threadId':'t'+mid,'internalDate':stamp or str(int(NOW.timestamp()*1000)),
            'snippet':'synthetic','labelIds':labels or ['INBOX'],
            'payload':{'headers':[{'name':'Subject','value':'Synthetic'},{'name':'From','value':'Other'}]}}


def install_helper(monkeypatch,calls,*,errors=None,hook=None):
    def run(argv,**kwargs):
        req=json.loads(argv[-1]);calls.append(req)
        if hook:hook(req)
        bad=errors or {}
        return {'messages':[message(mid) for mid in req['ids'] if mid not in bad],
                'errors':[dict(id=mid,**bad[mid]) for mid in req['ids'] if mid in bad],
                'http_calls':1,'inner_calls':len(req['ids'])}
    monkeypatch.setattr(src,'_run_json',run)


def test_hundred_pending_metadata_use_five_bounded_chunks(bound,monkeypatch):
    calls=[];install_helper(monkeypatch,calls)
    rows=list(src._pending_email_metadata([f'm{i}' for i in range(100)],now=NOW))
    assert len(calls)==5 and all(len(c['ids'])==20 for c in calls)
    assert len(rows)==100 and all(row['source_id']==mid for mid,row in rows)
    assert all(c['userId']==bound[0] and c['profile']=='standing' for c in calls)
    assert all(row['source_version']==bound[1]+':'+mid for mid,row in rows)


def test_capped_dates_checked_before_any_rich_metadata(bound,monkeypatch):
    calls=[];dates=[]
    monkeypatch.setattr(store,'source_lookback_cap',lambda kind:1)
    def bridge(*args,**kwargs):
        p=json.loads(args[-1]);dates.append(p)
        assert p['format']=='minimal' and p['fields']=='internalDate'
        delta=10 if p['id']=='old' else 0
        return {'internalDate':str(int((NOW-timedelta(days=delta)).timestamp()*1000))}
    monkeypatch.setattr(src,'_gws_bridge',bridge)
    install_helper(monkeypatch,calls)
    out=dict(src._pending_email_metadata(['old','new'],now=NOW))
    assert isinstance(out['old'],src.EmailOutsideScope) and out['new']['source_id']=='new'
    assert len(dates)==2 and calls[0]['ids']==['new']


def test_revocation_during_chunk_stops_before_any_yield_or_next_chunk(bound,monkeypatch):
    calls=[]
    install_helper(monkeypatch,calls,hook=lambda req:store.disable_source('email'))
    with pytest.raises((src.SourceReadError,store.SourceOptInRevoked)):
        list(src._pending_email_metadata([f'm{i}' for i in range(25)],now=NOW))
    assert len(calls)==1


def test_mixed_errors_preserve_per_id_outcomes_without_retry(bound,monkeypatch):
    calls=[]
    install_helper(monkeypatch,calls,errors={
        'gone':{'status':404,'kind':'not_found'},'auth':{'status':401,'kind':'auth'},
        'rate':{'status':403,'kind':'rate_limit'}})
    out=dict(src._pending_email_metadata(['good','gone','auth','rate'],now=NOW))
    assert out['good']['source_id']=='good'
    assert src._is_not_found_error(out['gone'])
    assert src._classify_read_error(out['auth'])=='auth'
    assert src._classify_read_error(out['rate'])=='transient'
    assert len(calls)==1


def test_preflight_changed_timestamp_refuses_content_for_staging(bound,monkeypatch):
    calls=[]
    monkeypatch.setattr(store,'source_lookback_cap',lambda kind:1)
    monkeypatch.setattr(src,'_gws_bridge',lambda *a,**kw:{'internalDate':str(int(NOW.timestamp()*1000)-1000)})
    install_helper(monkeypatch,calls)
    out=dict(src._pending_email_metadata(['m1'],now=NOW))
    assert isinstance(out['m1'],src.SourceReadError) and 'date changed' in str(out['m1'])


def test_normalized_metadata_matches_single_reader(bound,monkeypatch):
    calls=[];install_helper(monkeypatch,calls)
    monkeypatch.setattr(src,'_gws_bridge',lambda *a,**kw:message(json.loads(a[-1])['id']))
    expected=[src._read_email_message(mid,now=NOW) for mid in ('m1','m2')]
    actual=[row for mid,row in src._pending_email_metadata(['m1','m2'],now=NOW)]
    assert actual==expected and len(calls)==1


def test_revocation_between_chunks_prevents_later_request(bound,monkeypatch):
    calls=[];install_helper(monkeypatch,calls)
    rows=src._pending_email_metadata([f'm{i}' for i in range(25)],now=NOW)
    assert len([next(rows) for _ in range(20)])==20
    store.disable_source('email')
    with pytest.raises((src.SourceReadError,store.SourceOptInRevoked)):next(rows)
    assert len(calls)==1


def test_policy_change_during_chunk_discards_results(bound,monkeypatch):
    calls=[]
    install_helper(monkeypatch,calls,hook=lambda req:monkeypatch.setattr(src,'load_settings',lambda:{}))
    with pytest.raises(src.SourceReadError,match='policy changed'):
        list(src._pending_email_metadata(['m1'],now=NOW))
    assert len(calls)==1
