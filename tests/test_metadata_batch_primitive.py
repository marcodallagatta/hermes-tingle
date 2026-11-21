"""Fixed metadata batch primitive; synthetic transport, no host/account reads."""
import json
from types import SimpleNamespace
import urllib.error
import pytest
import gws_batch_bridge as bb


def part(mid,status=200,reason='',extra=None):
    payload=({'id':mid,'threadId':'t1','internalDate':'1763300000000','labelIds':['INBOX'],
              'snippet':'synthetic','payload':{'headers':[]}} if status==200 else
             {'error':{'errors':[{'reason':reason}],'message':'PRIVATE-ERROR-BODY'}})
    payload.update(extra or {})
    return (f'--B\r\nContent-Type: application/http\r\n\r\nHTTP/1.1 {status} Status\r\n'
            f'Content-Type: application/json\r\n\r\n{json.dumps(payload)}\r\n').encode()


def response(raw):
    class R:
        headers={'Content-Type':'multipart/mixed; boundary=B'}
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def read(self,n):return raw
    return R()


@pytest.mark.parametrize('profile,headers',[('standing',['From','To','Subject','Date']),
    ('lookback',['From','To','Subject','Date','List-Id','List-Unsubscribe','Auto-Submitted'])])
def test_fixed_metadata_profile_fields(profile,headers):
    from urllib.parse import urlparse,parse_qs
    body=bb.build_metadata_body('owner@example.invalid',['m1'],'B',profile).decode()
    query=parse_qs(body.split('GET ',1)[1].split(' HTTP/',1)[0].split('?',1)[1])
    assert query['format']==['metadata'] and query['fields']==[bb.METADATA_FIELDS]
    assert query['metadataHeaders']==headers
    assert 'format=full' not in body and 'attachments' not in body


@pytest.mark.parametrize('ids',[[],['a']*2,['../x'],['x y'],[str(i) for i in range(21)]])
def test_metadata_ids_rejected_before_token_or_network(ids,monkeypatch):
    monkeypatch.setattr(bb,'_token',lambda:pytest.fail('no token read'))
    with pytest.raises(ValueError):bb.batch_metadata('owner@example.invalid',ids,'standing')


def test_success_one_http_many_inner_requests():
    seen=[]
    def open_(req,**kwargs):
        seen.append(req)
        return response(part('m1')+part('m2')+b'--B--\r\n')
    out=bb.batch_metadata('owner@example.invalid',['m1','m2'],'standing',token='synthetic',opener=open_)
    assert len(seen)==1 and out['http_calls']==1 and out['inner_calls']==2
    assert [m['id'] for m in out['messages']]==['m1','m2'] and not out['errors']


@pytest.mark.parametrize('status,reason,kind',[(404,'notFound','not_found'),(401,'authError','auth'),
    (403,'insufficientPermissions','auth'),(403,'rateLimitExceeded','rate_limit'),
    (403,'domainPolicy','transient'),(429,'','rate_limit'),(503,'backendError','transient'),
    (418,'unknownProviderReason','transient')])
def test_mixed_result_failure_kinds_never_retry_or_echo(status,reason,kind):
    seen=[]
    def open_(req,**kw):
        seen.append(req)
        return response(part('m1')+part('m2',status,reason)+b'--B--\r\n')
    out=bb.batch_metadata('owner@example.invalid',['m1','m2'],'lookback',token='synthetic',opener=open_)
    assert len(seen)==1 and out['messages'][0]['id']=='m1'
    assert out['errors']==[{'id':'m2','status':status,'kind':kind}]
    assert 'PRIVATE-ERROR-BODY' not in json.dumps(out)


@pytest.mark.parametrize('raw',[part('wrong'),part('m1',extra={'body':'not allowed'}),b'garbage'])
def test_bad_identity_fields_or_multipart_fail_closed(raw):
    with pytest.raises(ValueError):
        bb.parse_metadata_response(raw+b'--B--\r\n',['m1'],'multipart/mixed; boundary=B')


def test_network_error_never_retries():
    seen=[]
    def open_(*a,**kw):
        seen.append(1);raise urllib.error.URLError('sensitive network diagnostic')
    out=bb.batch_metadata('owner@example.invalid',['m1'],'standing',token='synthetic',opener=open_)
    assert seen==[1] and out['http_calls']==out['inner_calls']==1
    assert out['errors']==[{'id':'m1','status':0,'kind':'transient'}]
    assert 'sensitive' not in json.dumps(out)


@pytest.mark.parametrize('reason',[{},[],None,42])
def test_malformed_error_reason_is_sanitized(reason):
    out=bb.parse_metadata_response(part('m1',403,reason)+b'--B--\r\n',['m1'],'multipart/mixed; boundary=B')
    assert out=={'messages':[],'errors':[{'id':'m1','status':403,'kind':'transient'}]}


def test_metadata_response_byte_cap():
    with pytest.raises(ValueError,match='byte limit'):
        bb.batch_metadata('owner@example.invalid',['m1'],'standing',token='synthetic',
                          opener=lambda *a,**kw:response(b'x'*3_000_001))
