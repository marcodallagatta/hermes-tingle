"""Synthetic evidence/prompt contracts, not a real-model semantic evaluation."""
import copy
import json
from datetime import datetime, timezone
import pytest
import hermes_tingle_calendar_transitions as transitions
import hermes_tingle_sources as sources
import hermes_tingle_store as store
import setup_jobs
import source_snapshot

NOW = datetime(2027, 1, 4, 8, tzinfo=timezone.utc)


def event(key, start, end, **extra):
    return dict(source_id='synthetic:'+key,source_version='v1',title='Synthetic event '+key,
                start=f'2027-01-04T{start}:00+00:00',end=f'2027-01-04T{end}:00+00:00',
                location='Synthetic venue',description='',**extra)


def build(events,complete=True):
    return transitions.build(events,now=NOW,timezone_name='Europe/Berlin',coverage_complete=complete)


@pytest.mark.parametrize('description,arrival',[
    ('Synthetic verified context: pre-show lasts about twenty minutes.','16:32'),
    ('Synthetic verified context: pre-show lasts about twenty minutes.','16:55'),
    ('Actual activity start and seating buffer unknown.','16:32'),
    ('Synthetic pre-show timing differs by performance.','16:48'),
])
def test_listed_overlap_is_evidence_not_an_automatic_alarm(description,arrival):
    travel=event('travel','15:30',arrival)
    show=event('ticket','16:30','18:30');show['description']=description
    original=copy.deepcopy([travel,show]);out=build(original)
    assert len(out['pairs'])==1
    pair=out['pairs'][0]
    assert pair['is_conflict'] is None and pair['requires_context']
    assert pair['listed_gap_seconds']<0
    assert out['events'][pair['to_event']]['description']==description
    assert original==[travel,show]  # no mutation or fabricated actual start
    assert 'actual_start' not in json.dumps(out)


def test_closest_boundaries_keep_location_and_context_without_reducing_to_busy_union():
    a=event('a','10:00','11:00');b=event('b','11:00','12:00');b['location']='Different synthetic venue'
    out=build([b,a]);p=out['pairs'][0]
    assert p['listed_gap_seconds']==0 and out['events'][p['to_event']]['location']=='Different synthetic venue'
    assert len(out['events'])==2 and p['is_conflict'] is None


@pytest.mark.parametrize('change',['coverage','all_day','offsetless','reverse','duplicate','event_bound'])
def test_incomplete_never_claims_all_transitions_checked(change):
    events=[event('a','10:00','11:00'),event('b','11:05','12:00')]
    if change=='all_day':events.append(dict(source_id='day',source_version='v1',start='2027-01-04',end='2027-01-05'))
    if change=='offsetless':events[0]['start']='2027-01-04T10:00:00'
    if change=='reverse':events[0]['end']=events[0]['start']
    if change=='duplicate':events.append(events[0])
    if change=='event_bound':events*=31
    out=build(events,change!='coverage')
    assert not out['coverage_complete'] and out['incomplete_reasons']
    assert all(p['is_conflict'] is None for p in out['pairs'])


def test_pair_bound_is_explicit_and_no_hidden_unbounded_context():
    events=[event(str(n),'10:00','11:00') for n in range(60)]
    out=build(events)
    assert len(out['pairs'])==transitions.MAX_PAIRS
    assert 'pair_bound' in out['incomplete_reasons'] and not out['coverage_complete']
    assert len(out['events'])<=transitions.MAX_CONTEXT_EVENTS


def test_no_pair_across_different_local_days_and_no_past_finished_pair():
    first=event('first','10:00','11:00');second=event('second','11:00','12:00')
    second['start']='2027-01-05T11:00:00+00:00';second['end']='2027-01-05T12:00:00+00:00'
    assert not build([first,second])['pairs']
    first['start']='2027-01-03T10:00:00+00:00';first['end']='2027-01-03T11:00:00+00:00'
    assert not build([first,second])['pairs']


def test_reviewed_events_kept_from_same_single_provider_read(monkeypatch):
    records=[dict(id='trip',updated='v1',summary='Synthetic transit',start={'dateTime':'2027-01-04T15:00:00Z'},end={'dateTime':'2027-01-04T16:32:00Z'}),
             dict(id='event',updated='v1',summary='Synthetic performance',description='Pre-show duration unknown',start={'dateTime':'2027-01-04T16:30:00Z'},end={'dateTime':'2027-01-04T18:30:00Z'})]
    for record in records:store.mark_source_reviewed('calendar','synthetic:'+record['id'],'v1',source_generation=store.source_generation('calendar'))
    calls=[]
    monkeypatch.setattr(sources,'_gws_bridge',lambda *a:calls.append(a) or {'items':records})
    items=sources._read_calendar_events({'id':'synthetic','summary':'Synthetic'},NOW,7)
    assert not items and len(calls)==1
    assert len(items.transition_context)==2 and len(build(items.transition_context)['pairs'])==1


def test_snapshot_passes_reviewed_context_but_preserves_gate(monkeypatch):
    store.disable_source('email');store.disable_source('chat')
    monkeypatch.setattr(sources,'_read_calendars',lambda:[{'id':'synthetic','summary':'Synthetic'}])
    items=sources._CalendarEvents();items.transition_context=[event('a','10:00','11:00'),event('b','11:00','12:00')]
    monkeypatch.setattr(sources,'_read_calendar_events',lambda *a:items)
    snapshot=sources.collect_snapshot(now=NOW)
    assert not snapshot['calendar_events'] and len(snapshot['calendar_transitions']['pairs'])==1
    snapshot['errors']=[]
    assert source_snapshot.build_agent_input(snapshot)=='{"wakeAgent": false}'
    snapshot['emails']=[{'source_id':'synthetic'}]
    payload=json.loads(source_snapshot.build_agent_input(snapshot))['source_snapshot']
    assert len(payload['calendar_transitions']['pairs'])==1 and payload['untrusted_source_data']


def test_context_disabled_calendar_does_not_enable_or_read_it(monkeypatch):
    for kind in ('email','calendar','chat'):store.disable_source(kind)
    monkeypatch.setattr(sources,'_read_calendars',lambda:pytest.fail('disabled Calendar cannot be read'))
    out=sources.collect_snapshot(now=NOW)['calendar_transitions']
    assert out['events']==[] and out['pairs']==[] and not out['coverage_complete']


def test_no_additional_model_job_or_gate_and_prompt_requires_context(synthetic_settings):
    jobs=setup_jobs._job_specs(synthetic_settings)
    assert len(jobs)==2 and jobs[1]['no_agent'] is True
    prompt=jobs[0]['prompt']
    for instruction in ('not a conflict verdict','raw timestamp overlap alone','no universal cinema',
                        'arrival, journey and seating buffers','timing is unknown','reliable current read-only venue source',
                        'No login, contact, booking or external mutation','separate owner consent','coverage_complete is false'):
        assert instruction in prompt
    assert '20 minutes' not in prompt and 'twenty minutes' not in prompt
    assert jobs[0]['enabled_toolsets']==['hermes_tingle','web','no_mcp']
