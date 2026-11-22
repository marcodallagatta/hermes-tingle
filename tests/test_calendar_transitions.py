"""Synthetic evidence/prompt contracts, not a real-model semantic evaluation."""
import copy
import json
from datetime import datetime, timedelta, timezone
import pytest
import hermes_tingle_calendar_transitions as transitions
import hermes_tingle_sources as sources
import hermes_tingle_store as store
import setup_jobs
import source_snapshot
from schemas import HERMES_TINGLE

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
    assert payload['calendar_transitions']['pairs'][0]['transition_receipt'] == snapshot['calendar_transitions']['pairs'][0]['transition_receipt']


def test_reported_conflict_pair_is_suppressed_until_event_version_changes(monkeypatch):
    import hermes_tingle

    store.disable_source('email')
    store.disable_source('chat')
    monkeypatch.setattr(store, '_now', lambda value=None: NOW)
    pair_events = [event('a', '10:00', '11:00'), event('b', '11:00', '12:00')]
    items = sources._CalendarEvents()
    items.transition_context = pair_events
    monkeypatch.setattr(sources, '_read_calendars', lambda: [{'id': 'synthetic', 'summary': 'Synthetic'}])
    monkeypatch.setattr(sources, '_read_calendar_events', lambda *args: items)

    first = sources.collect_snapshot(now=NOW)
    first_pair = first['calendar_transitions']['pairs'][0]
    receipt = first_pair['transition_receipt']
    generation = first_pair['source_generation']
    marked = json.loads(hermes_tingle.handle_tool(None, {
        'action': 'mark_calendar_transition_alerted',
        'transition_receipt': receipt,
        'source_generation': generation,
    }))
    assert marked['success'] and marked['already_alerted'] is False and marked['delivery_confirmed'] is False
    replay = json.loads(hermes_tingle.handle_tool(None, {
        'action': 'mark_calendar_transition_alerted',
        'transition_receipt': receipt,
        'source_generation': generation,
    }))
    assert replay['success'] is False
    connection = store._connect()
    try:
        stored_keys = [row[0] for row in connection.execute('SELECT pair_key FROM calendar_transition_alerts')]
    finally:
        connection.close()

    expected_key = store._calendar_transition_pair_key(first['calendar_transitions']['events'], first_pair, generation)
    assert expected_key in stored_keys

    repeated = sources.collect_snapshot(now=NOW)
    assert repeated['calendar_transitions']['pairs'] == []
    assert repeated['calendar_transitions']['suppressed_pair_count'] == 1

    pair_events[1]['source_version'] = 'v2'
    changed = sources.collect_snapshot(now=NOW)
    changed_pair = changed['calendar_transitions']['pairs'][0]
    assert changed_pair["transition_receipt"] != receipt
    assert changed_pair["already_alerted"] is False


def test_stale_transition_receipt_cannot_mute_new_source_generation(monkeypatch, tmp_path):
    import hermes_tingle

    store.disable_source('email')
    store.disable_source('chat')
    monkeypatch.setattr(store, '_now', lambda value=None: NOW)
    pair_events = [event('a', '10:00', '11:00'), event('b', '11:00', '12:00')]
    items = sources._CalendarEvents()
    items.transition_context = pair_events
    monkeypatch.setattr(sources, '_read_calendars', lambda: [{'id': 'synthetic', 'summary': 'Synthetic'}])
    monkeypatch.setattr(sources, '_read_calendar_events', lambda *args: items)
    snapshot = sources.collect_snapshot(now=NOW)
    pair = snapshot['calendar_transitions']['pairs'][0]

    active_db = (tmp_path / 'default-test.db').read_bytes()
    assert b'synthetic:a' not in active_db and b'synthetic:b' not in active_db
    assert b'Synthetic event a' not in active_db and b'Synthetic event b' not in active_db
    assert pair['transition_receipt'].encode() not in active_db
    conn = store._connect()
    try:
        assert conn.execute('SELECT COUNT(*) FROM calendar_transition_receipts').fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM calendar_transition_receipts WHERE receipt_hash=?', (pair['transition_receipt'],)).fetchone()[0] == 0
    finally:
        conn.close()

    store.disable_source('calendar')
    store.enable_source('calendar', enabled_by='synthetic re-consent')
    stale = json.loads(hermes_tingle.handle_tool(None, {
        'action': 'mark_calendar_transition_alerted',
        'transition_receipt': pair['transition_receipt'],
        'source_generation': pair['source_generation'],
    }))
    assert stale['success'] is False
    conn = store._connect()
    try:
        assert conn.execute('SELECT COUNT(*) FROM calendar_transition_alerts').fetchone()[0] == 0
    finally:
        conn.close()

    refreshed = sources.collect_snapshot(now=NOW)
    new_pair = refreshed['calendar_transitions']['pairs'][0]
    assert new_pair['source_generation'] != pair['source_generation']
    assert new_pair['transition_receipt'] != pair['transition_receipt']
    db_bytes = (tmp_path / 'default-test.db').read_bytes()
    assert b'synthetic:a' not in db_bytes and b'synthetic:b' not in db_bytes
    assert b'Synthetic event a' not in db_bytes and b'Synthetic event b' not in db_bytes
    assert pair['transition_receipt'].encode() not in db_bytes


def test_policy_change_invalidates_calendar_transition_receipt(monkeypatch, tmp_path):
    import hermes_tingle

    store.disable_source('email')
    store.disable_source('chat')
    monkeypatch.setattr(store, '_now', lambda value=None: NOW)
    pair_events = [event('a', '10:00', '11:00'), event('b', '11:00', '12:00')]
    items = sources._CalendarEvents()
    items.transition_context = pair_events
    monkeypatch.setattr(sources, '_read_calendars', lambda: [{'id': 'synthetic', 'summary': 'Synthetic'}])
    monkeypatch.setattr(sources, '_read_calendar_events', lambda *args: items)
    snapshot = sources.collect_snapshot(now=NOW)
    pair = snapshot['calendar_transitions']['pairs'][0]

    config_path = tmp_path / 'hermes-tingle.json'
    settings = json.loads(config_path.read_text())
    settings['delivery'] = 'telegram:-1009876543210:99'
    config_path.write_text(json.dumps(settings))
    stale = json.loads(hermes_tingle.handle_tool(None, {
        'action': 'mark_calendar_transition_alerted',
        'transition_receipt': pair['transition_receipt'],
        'source_generation': pair['source_generation'],
    }))
    assert stale['success'] is False
    conn = store._connect()
    try:
        assert conn.execute('SELECT COUNT(*) FROM calendar_transition_alerts').fetchone()[0] == 0
    finally:
        conn.close()


def test_calendar_revoke_purges_alert_markers_and_pending_receipts(monkeypatch):
    import hermes_tingle

    store.disable_source('email')
    store.disable_source('chat')
    monkeypatch.setattr(store, '_now', lambda value=None: NOW)
    pair_events = [
        event('a', '10:00', '11:00'),
        event('b', '11:00', '12:00'),
        event('c', '12:00', '13:00'),
    ]
    items = sources._CalendarEvents()
    items.transition_context = pair_events
    monkeypatch.setattr(sources, '_read_calendars', lambda: [{'id': 'synthetic', 'summary': 'Synthetic'}])
    monkeypatch.setattr(sources, '_read_calendar_events', lambda *args: items)
    snapshot = sources.collect_snapshot(now=NOW)
    assert len(snapshot['calendar_transitions']['pairs']) == 2
    first_pair = snapshot['calendar_transitions']['pairs'][0]
    marked = json.loads(hermes_tingle.handle_tool(None, {
        'action': 'mark_calendar_transition_alerted',
        'transition_receipt': first_pair['transition_receipt'],
        'source_generation': first_pair['source_generation'],
    }))
    assert marked['success']
    remaining = sources.collect_snapshot(now=NOW)['calendar_transitions']
    assert len(remaining['pairs']) == 1 and remaining['suppressed_pair_count'] == 1
    remapped = remaining['pairs'][0]
    assert remapped['transition_receipt']
    assert {remaining['events'][remapped[index]]['source_id'] for index in ('from_event', 'to_event')} == {'synthetic:b', 'synthetic:c'}

    conn = store._connect()
    try:
        assert conn.execute('SELECT COUNT(*) FROM calendar_transition_alerts').fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM calendar_transition_receipts').fetchone()[0] == 1
    finally:
        conn.close()

    store.disable_source('calendar')
    conn = store._connect()
    try:
        assert conn.execute('SELECT COUNT(*) FROM calendar_transition_alerts').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM calendar_transition_receipts').fetchone()[0] == 0
    finally:
        conn.close()

    store.enable_source('calendar', enabled_by='synthetic restore')
    restored = sources.collect_snapshot(now=NOW)
    first_pair = restored['calendar_transitions']['pairs'][0]
    marked = json.loads(hermes_tingle.handle_tool(None, {
        'action': 'mark_calendar_transition_alerted',
        'transition_receipt': first_pair['transition_receipt'],
        'source_generation': first_pair['source_generation'],
    }))
    assert marked['success']
    assert store.purge_source_retained('calendar') >= 2
    conn = store._connect()
    try:
        assert conn.execute('SELECT COUNT(*) FROM calendar_transition_alerts').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM calendar_transition_receipts').fetchone()[0] == 0
    finally:
        conn.close()


def test_calendar_receipt_uses_generation_captured_before_event_fetch(monkeypatch):
    store.disable_source('email')
    store.disable_source('chat')
    initial_generation = store.source_generation('calendar')
    pair_events = [event('a', '10:00', '11:00'), event('b', '11:00', '12:00')]
    items = sources._CalendarEvents()
    items.transition_context = pair_events
    monkeypatch.setattr(sources, '_read_calendars', lambda: [{'id': 'synthetic', 'summary': 'Synthetic'}])

    def revoke_and_regrant_after_read(*args):
        store.disable_source('calendar')
        store.enable_source('calendar', enabled_by='synthetic re-consent')
        return items

    monkeypatch.setattr(sources, '_read_calendar_events', revoke_and_regrant_after_read)
    with pytest.raises(ValueError, match='stale calendar generation'):
        sources.collect_snapshot(now=NOW)
    assert store.source_generation('calendar') != initial_generation
    conn = store._connect()
    try:
        assert conn.execute('SELECT COUNT(*) FROM calendar_transition_receipts').fetchone()[0] == 0
    finally:
        conn.close()


def test_calendar_receipt_expires_and_cannot_be_replayed(monkeypatch):
    store.disable_source('email')
    store.disable_source('chat')
    monkeypatch.setattr(store, '_now', lambda value=None: value if value is not None else NOW)
    pair_events = [event('a', '10:00', '11:00'), event('b', '11:00', '12:00')]
    items = sources._CalendarEvents()
    items.transition_context = pair_events
    monkeypatch.setattr(sources, '_read_calendars', lambda: [{'id': 'synthetic', 'summary': 'Synthetic'}])
    monkeypatch.setattr(sources, '_read_calendar_events', lambda *args: items)
    snapshot = sources.collect_snapshot(now=NOW)
    pair = snapshot['calendar_transitions']['pairs'][0]

    with pytest.raises(ValueError, match='stale or unavailable'):
        store.mark_calendar_transition_alerted(
            pair['transition_receipt'], source_generation=pair['source_generation'],
            now=NOW + timedelta(hours=24))
    conn = store._connect()
    try:
        assert conn.execute('SELECT COUNT(*) FROM calendar_transition_alerts').fetchone()[0] == 0
    finally:
        conn.close()


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
                        'No login, contact, booking or external mutation','separate owner consent','coverage_complete is false',
                        'transition_receipt and source_generation','mark_calendar_transition_alerted',
                        'This records an alert attempt, not verified delivery','If no receipt is present, do not call the action',
                        'Receipt tokens and suppression/count fields are internal only'):
        assert instruction in prompt
    assert '20 minutes' not in prompt and 'twenty minutes' not in prompt
    assert jobs[0]['enabled_toolsets']==['hermes_tingle','web','no_mcp']
    assert 'mark_calendar_transition_alerted' in HERMES_TINGLE['parameters']['properties']['action']['enum']
