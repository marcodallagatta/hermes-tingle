"""Focused expected-safe behaviors from independent synthetic audit probes."""
import asyncio
import json
from datetime import datetime,timedelta,timezone
import pytest
import hermes_tingle_store as store
import hermes_tingle_assess as assess
import hermes_tingle_lookback as lb
import hermes_tingle_sources as sources


def future(days):return (datetime.now(timezone.utc)+timedelta(days=days)).isoformat(timespec='seconds')


def test_clearing_hard_deadline_requires_atomic_hardness_removal():
    i=store.add_item(title='Synthetic hard window',due_at=future(3),hard_deadline=True)
    with pytest.raises(ValueError):store.update_item(i['id'],due_at='')
    assert store.get_item(i['id'])['due_at']==i['due_at']
    out=store.update_item(i['id'],due_at='',hard_deadline=False)
    assert out['due_at'] is None and out['hard_deadline']==0


def test_earlier_booking_window_rearms_linked_check():
    goal=store.add_item(title='Synthetic trip')
    booking=store.add_booking(goal['id'],name='Synthetic flight',cancel_by=future(10))
    before=store.get_item(booking['cancel_item_id'])
    store.update_booking(goal['id'],position=booking['position'],cancel_by=future(3))
    after=store.get_item(booking['cancel_item_id'])
    assert after['next_check_at']<before['next_check_at']
    assert after['next_check_at']<after['due_at'] and after['reminder_attempt_count']==0


def test_cancelled_booking_does_not_create_alerting_orphan():
    i=store.add_item(title='Synthetic trip')
    b=store.add_booking(i['id'],name='Synthetic lodging')
    updated=store.update_booking(i['id'],position=b['position'],state='cancelled',cancel_by=future(5))
    assert not updated['cancel_item_id']
    assert len(store.list_items())==1


@pytest.mark.parametrize('args',[dict(recur_interval=2,recur_unit='month'),dict(hard_deadline=True)])
def test_dedupe_never_discards_requested_semantics(args):
    due=future(30);store.add_item(title='Synthetic renewal',summary='Same',due_at=due)
    with pytest.raises(ValueError,match='incompatible'):
        store.add_item(title='Synthetic renewal',summary='Same',due_at=due,**args)


def test_watch_booking_context_changes_invalidate_reminder_revision():
    i=store.add_item(title='Synthetic goal');store.link_watch(i['id'],'cron:synthetic')
    linked=store.get_item(i['id']);assert linked['reminder_revision']>i['reminder_revision']
    store.report_watch(i['id'],'Synthetic change')
    reported=store.get_item(i['id']);assert reported['reminder_revision']>linked['reminder_revision']
    b=store.add_booking(i['id'],name='Synthetic booking')
    before=store.get_item(i['id']);store.update_booking(i['id'],position=b['position'],state='confirmed')
    assert store.get_item(i['id'])['reminder_revision']>before['reminder_revision']


def test_document_validity_compares_instants_not_strings():
    i=store.add_item(title='Synthetic paperwork')
    store.add_document(i['id'],name='Synthetic certificate',valid_until='2026-11-18T13:00:00+02:00')
    expired=store.expire_documents(now='2026-11-18T11:30:00+00:00')
    assert len(expired)==1


def test_approval_dedupe_uses_whole_principal():
    kw=dict(action_type='send_message',target='synthetic',parameters={'x':1})
    a=store.request_approval(**kw,approver=('telegram','42','42'))
    b=store.request_approval(**kw,approver=('slack','42','42'))
    c=store.request_approval(**kw,approver=('telegram','42','99'))
    assert len({a['id'],b['id'],c['id']})==3


def test_snippet_prefix_is_not_counted_twice():
    body='Your voucher expires on 2027-06-01. Remaining synthetic detail.'
    payload=json.dumps(dict(message_id='m1',snippet=body[:40],date='2027-05-01',thread_context=[dict(message_id='m1',body=body)]))
    messages,_,_=lb._conversation_input(dict(members=[dict(payload=payload)],newest={'context_status':'Bounded thread context'}))
    assert ' '.join(m['text'] for m in messages).count('2027-06-01')==1


def test_cancelled_assessment_never_becomes_retryable_model_failure():
    class Facade:
        def complete_structured(self,**kw):raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):assess.request(type('Ctx',(),{'llm':Facade()})(),[])




def test_calendar_repeated_empty_page_terminates_incomplete(monkeypatch):
    monkeypatch.setattr(sources,'_read_calendars',lambda:[{'id':'synthetic','summary':'Synthetic'}])
    calls=[]
    monkeypatch.setattr(sources,'_gws_bridge',lambda *a:calls.append(a) or {'items':[],'nextPageToken':'same'})
    result=sources.check_calendar_availability('2027-01-04',['10:00'],now=datetime(2027,1,1,tzinfo=timezone.utc))
    assert len(calls)==2 and not result['coverage_complete']
    assert result['free_windows']==[] and result['candidate_results'][0]['available'] is None


def test_calendar_unknown_all_day_never_advertises_free_windows(monkeypatch):
    monkeypatch.setattr(sources,'_read_calendars',lambda:[{'id':'synthetic','summary':'Synthetic'}])
    monkeypatch.setattr(sources,'_gws_bridge',lambda *a:{'items':[{'id':'x','summary':'Synthetic marker','start':{'date':'2027-01-04'},'end':{'date':'2027-01-05'}}]})
    result=sources.check_calendar_availability('2027-01-04',['10:00'],now=datetime(2027,1,1,tzinfo=timezone.utc))
    assert not result['coverage_complete'] and result['all_day_uncertain']
    assert result['free_windows']==[] and result['candidate_results'][0]['available'] is None


def test_101st_active_item_makes_chat_term_coverage_incomplete(monkeypatch):
    monkeypatch.setattr(sources,'_chat_session_resolver',lambda:[{'id':'synthetic'}])
    monkeypatch.setattr(sources,'_chat_message_reader',lambda *a,**kw:[])
    for n in range(101):store.add_item(title=f'Synthetic token{n}')
    store.enable_source('chat',enabled_by='Synthetic owner')
    store.note_known_chat('telegram:42',origin='Synthetic admitted chat')
    out=sources._collect_chat_matches(now=datetime.now(timezone.utc))
    assert not out['review_complete'] and 'active_item_terms_bounded_to_100' in out['incomplete_reasons']


def test_calendar_switch_under_same_grant_refused_before_events(monkeypatch):
    monkeypatch.setattr(sources,'calendar_account_identity',lambda:'d'*64)
    calls=[]
    monkeypatch.setattr(sources,'_gws_bridge',lambda *a:calls.append(a) or {})
    with pytest.raises(sources.SourceReadError,match='account changed'):sources._read_calendars()
    assert not calls


def test_calendar_switch_during_events_discards_result(monkeypatch):
    identity=['c'*64]
    monkeypatch.setattr(sources,'calendar_account_identity',lambda:identity[0])
    def bridge(*a):
        identity[0]='d'*64
        return {'items':[{'id':'synthetic','summary':'Synthetic','updated':'v1'}]}
    monkeypatch.setattr(sources,'_gws_bridge',bridge)
    with pytest.raises(sources.SourceReadError,match='account changed'):
        sources._read_calendar_events({'id':'calendar','summary':'Synthetic'},datetime.now(timezone.utc),7)


def test_calendar_identity_uses_calendar_scope_and_hash_only(monkeypatch):
    import hashlib
    observed=[]
    original=sources.calendar_account_identity.__wrapped__ if hasattr(sources.calendar_account_identity,'__wrapped__') else None
    # Test production function despite suite's no-network identity stub.
    import inspect,ast
    tree=ast.parse(inspect.getsource(sources))
    node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='calendar_account_identity')
    ns=dict(sources.__dict__)
    ns['_gws_bridge']=lambda *a:observed.append(a) or {'id':'synthetic@example.invalid'}
    exec(compile(ast.Module(body=[node],type_ignores=[]),'synthetic-identity','exec'),ns)
    key=ns['calendar_account_identity']()
    assert key==hashlib.sha256(b'synthetic@example.invalid').hexdigest()
    assert observed[0][:3]==('calendar','calendars','get')
    assert json.loads(observed[0][-1])=={'calendarId':'primary','fields':'id'}


def test_empty_string_cannot_create_anchorless_hard_deadline():
    with pytest.raises(ValueError,match='anchor'):store.add_item(title='Synthetic invalid hard window',due_at='',hard_deadline=True)
