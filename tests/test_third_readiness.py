from delivery_fakes import bind_synthetic_email
"""Third review regressions: real entrypoints, synthetic state, no receipts inferred.

Original portable probes asserted defects. These assert the required outcomes, with uncertainty and concurrent dependency mutation exposed.
"""
import io
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
import due_alerts
import hermes_tingle as plugin
import hermes_tingle_sources as sources
import hermes_tingle_store as store

NOW = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)


@pytest.fixture
def clock(monkeypatch):
    current = [NOW]
    original = store._now
    monkeypatch.setattr(store, '_now', lambda value=None: original(value) if value is not None else current[0])
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return current[0] if tz else current[0].replace(tzinfo=None)
    monkeypatch.setattr(due_alerts, 'datetime', Clock)
    return current


@pytest.fixture
def owner(tmp_path):
    from test_hermes_tingle_tool import _FakeSource
    path = tmp_path / 'hermes-tingle.json'
    config = json.loads(path.read_text()); config['owner_principals'] = ['telegram:424242']
    path.write_text(json.dumps(config))
    return _FakeSource()


def due(**kwargs):
    return store.add_item(title='Synthetic reminder', next_check_at=NOW-timedelta(seconds=1), now=NOW, **kwargs)


def scalar(sql):
    conn = store._connect()
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


def tool(item):
    return json.loads(plugin.handle_tool(None, {'action': 'mark_notified', 'item_ids': [item['id']], 'expected_versions':{i:store.get_item(i)['reminder_revision'] for i in [item['id']]}}))


def test_receipt_after_successful_flush_marker_crash(monkeypatch, capsys, owner, clock):
    item = due()
    monkeypatch.setattr(store, 'mark_emission_flushed', lambda *a, **k: (_ for _ in ()).throw(OSError('synthetic marker crash')))
    with pytest.raises(OSError):
        due_alerts.main()
    output = capsys.readouterr().out
    receipt = re.search(r'/tingle ack ([a-f0-9]{24})', output).group(1)
    current = store.get_item(item['id'])
    assert (current['reminder_attempt_count'], current['reminder_count']) == (1, 0)
    check = current['next_check_at']
    assert 'Acknowledged' in plugin.handle_command(None, 'ack '+receipt, source=owner)
    assert 'No eligible' in plugin.handle_command(None, 'ack '+receipt, source=owner)
    assert store.get_item(item['id'])['reminder_count'] == 1
    assert store.get_item(item['id'])['reminder_attempt_count'] == 1
    assert store.get_item(item['id'])['next_check_at'] == check
    assert not store.get_alert_emission(1)['emitted_to_gateway_at']
    assert store.emission_state(store.get_alert_emission(1)) == 'confirmed'


@pytest.mark.parametrize('outcome', ['no_output', 'flush_failure', 'success'])
def test_automatic_helper_four_attempts_without_ack(outcome, monkeypatch, capsys, clock):
    item = due()
    for count in range(1, 5):
        if outcome == 'no_output':
            # Crash just after reservation, before stdout was ever called.
            original = store.get_alert_emission
            monkeypatch.setattr(store, 'get_alert_emission', lambda *a, **k: (_ for _ in ()).throw(OSError('synthetic crash')))
            with pytest.raises(OSError):
                due_alerts.main()
            monkeypatch.setattr(store, 'get_alert_emission', original)
        elif outcome == 'flush_failure':
            class Broken(io.StringIO):
                def flush(self):
                    raise OSError('synthetic failed flush')
            with monkeypatch.context() as m:
                m.setattr(due_alerts.sys, 'stdout', Broken())
                assert due_alerts.main() == 1
        else:
            assert due_alerts.main() == 0
            assert item['id'] in capsys.readouterr().out
        current = store.get_item(item['id'])
        assert current['reminder_attempt_count'] == count
        assert current['reminder_count'] == 0 and not current['last_notified_at']
        assert scalar('SELECT count(*) FROM alert_emissions') == count
        # Immediate poll cannot consume another attempt. A diagnostic note is not
        # a new outbox batch and must not contain stale item content.
        assert due_alerts.main() == 0
        assert item['id'] not in capsys.readouterr().out
        if count < 4:
            clock[0] = datetime.fromisoformat(current['next_check_at'])
        else:
            assert current['next_check_at'] is None
    clock[0] += timedelta(days=30)
    due_alerts.main(); capsys.readouterr()
    assert scalar('SELECT count(*) FROM alert_emissions') == 4
    assert store.get_item(item['id'])['status'] == 'open'


@pytest.mark.parametrize('reply_wait', [False, True])
def test_retry_preserves_three_confirmed_and_exhausted_fourth(reply_wait, clock, owner, monkeypatch):
    item = due(**({'source_type': 'email', 'source_ref': 'gmail:a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf/synthetic#sent',
                  'status': 'waiting', 'owner': 'other', 'completion_signal': 'source_email_reply_received'} if reply_wait else {}))
    monkeypatch.setattr(sources, '_read_email_reply_checks', lambda items: ([{'item_id': item['id'], 'state': 'no_reply'}], []))
    for count in range(1, 5):
        result = tool(item); assert result['success']
        row = result['items'][0]
        if count < 4:
            assert 'Acknowledged' in plugin.handle_command(None, row['receipt_command'].split('/tingle ')[1], source=owner)
            clock[0] = datetime.fromisoformat(store.get_item(item['id'])['next_check_at'])
    assert store.get_item(item['id'])['reminder_count'] == 3
    assert 'Batch released' in plugin.handle_command(None, f"retry B{row['delivery_batch_id']}", source=owner)
    assert store.get_item(item['id'])['reminder_count'] == 3
    assert store.get_item(item['id'])['reminder_attempt_count'] == 4
    assert not tool(item)['success']
    assert store.get_item(item['id'])['next_check_at'] is None
    store.snooze_item(item['id'], until=clock[0]+timedelta(days=2), now=clock[0])
    assert not tool(item)['success']
    clock[0] += timedelta(days=2)
    assert tool(item)['success']
    assert store.get_item(item['id'])['reminder_attempt_count'] == 1


@pytest.mark.parametrize('state', ['no_reply', 'reply_received', 'missing', 'failure'])
def test_each_automatic_reply_nudge_reverifies(state, monkeypatch, clock):
    item = due(source_type='email', source_ref='gmail:a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf/synthetic#sent', status='waiting', owner='other', completion_signal='source_email_reply_received')
    checks = []
    def verify(items):
        checks.append(items[0]['id'])
        if len(checks) == 1 or state == 'no_reply':
            return [{'item_id': item['id'], 'state': 'no_reply'}], []
        return ([] if state in ('missing', 'failure') else [{'item_id': item['id'], 'state': state}]), (['synthetic failed check'] if state == 'failure' else [])
    monkeypatch.setattr(sources, '_read_email_reply_checks', verify)
    assert tool(item)['success']
    first = store.get_item(item['id']); assert first['reply_interval_seconds'] == 2*86400
    clock[0] = datetime.fromisoformat(first['next_check_at'])
    second = tool(item)
    assert second['success'] is (state == 'no_reply')
    assert checks == [item['id'], item['id']]
    assert store.get_item(item['id'])['reminder_attempt_count'] == (2 if state == 'no_reply' else 1)


@pytest.mark.parametrize('mutation', ['complete', 'add', 'skip', 'linked_done', 'linked_snooze'])
def test_helper_step_dependencies_invalidate_render(mutation, monkeypatch, capsys, clock):
    goal = due()
    wait = None
    if mutation.startswith('linked'):
        wait = store.add_item(title='Synthetic reply', source_type='email', source_ref='gmail:a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf/synthetic#sent', status='waiting', owner='other', completion_signal='source_email_reply_received', next_check_at=NOW)
    store.add_step(goal['id'], title='Collect synthetic first document', why='Synthetic first reason', wait_item_id=wait['id'] if wait else '')
    store.add_step(goal['id'], title='Review synthetic second document', why='Synthetic second reason')
    original = due_alerts._render_selected
    def render(*args, **kwargs):
        text = original(*args, **kwargs)
        if mutation == 'complete':
            assert json.loads(plugin.handle_tool(None, {'action': 'complete_step', 'item_id': goal['id'], 'step_position': 1}))['success']
        elif mutation == 'skip':
            store.skip_step(goal['id'], 1)
        elif mutation == 'add':
            store.add_step(goal['id'], title='Check synthetic third document', why='Synthetic reason')
        elif mutation == 'linked_done':
            store.complete_item(wait['id'])
        else:
            store.snooze_item(wait['id'], until=NOW+timedelta(days=2), now=NOW)
        return text
    monkeypatch.setattr(due_alerts, '_render_selected', render)
    assert due_alerts.main() == 0
    assert capsys.readouterr().out == ''
    assert scalar('SELECT count(*) FROM alert_emissions') == 0
    assert store.get_item(goal['id'])['reminder_attempt_count'] == 0


def test_model_tool_dependency_edit_between_selection_and_reservation(monkeypatch, clock):
    goal = due()
    store.add_step(goal['id'], title='Collect synthetic first document', why='Synthetic reason')
    original = store.mark_notified
    def changed(*args, **kwargs):
        store.complete_step(goal['id'], 1)
        return original(*args, **kwargs)
    monkeypatch.setattr(store, 'mark_notified', changed)
    assert not tool(goal)['success']
    assert scalar('SELECT count(*) FROM alert_emissions') == 0


def test_old_receipt_cannot_charge_later_step_revision(clock):
    goal = due(); store.add_step(goal['id'], title='Collect synthetic document', why='Synthetic reason')
    row = store.mark_notified([goal['id']], now=NOW)[0]
    store.complete_step(goal['id'], 1)
    check = store.get_item(goal['id'])['next_check_at']
    store.acknowledge_alert_emissions(row['receipt_command'].split()[-1])
    assert store.get_item(goal['id'])['reminder_count'] == 0
    assert store.get_item(goal['id'])['reminder_attempt_count'] == 1
    assert store.get_item(goal['id'])['next_check_at'] == check


def test_real_310_window_backlog_all_batches_discoverable(capsys, owner, clock):
    store.add_item(title='Synthetic daily backlog', hard_deadline=True, due_at=NOW-timedelta(days=309,hours=12), next_check_at=NOW-timedelta(days=311), recur_interval=1,recur_unit='day')
    for _ in range(31):
        assert due_alerts.main() == 0
        capsys.readouterr()
    assert scalar("SELECT count(*) FROM hermes_tingle_items WHERE status='missed'") == 310
    pending = store.pending_alert_emissions()
    assert len(pending) == 31
    one = plugin.handle_command(None, 'delivery', source=owner)
    assert len(re.findall(r'^B\d+:', one, re.M)) == 30
    assert 'More batches:' in one and '/tingle delivery B30' in one
    # Concurrent retirement of an earlier row does not shift the next ID page.
    assert store.release_alert_emission(1, now=NOW)
    new_item = store.add_item(title='Synthetic concurrent arrival',next_check_at=NOW)
    store.mark_notified([new_item['id']],now=NOW)
    two = plugin.handle_command(None, 'delivery B30', source=owner)
    assert re.search(r'^B31:', two, re.M) and re.search(r'^B32:', two, re.M)
    assert 'receipt_token' not in one+two and '/tingle ack ' not in one+two


@pytest.mark.parametrize('total', [0, 1, 30, 31, 60, 61])
def test_delivery_page_boundaries(total, owner, clock):
    for i in range(total):
        item = store.add_item(title=f'Synthetic batch {i}', next_check_at=NOW)
        store.mark_notified([item['id']], now=NOW)
    after = 0; observed = []
    while True:
        response = plugin.handle_command(None, 'delivery'+(f' B{after}' if after else ''), source=owner)
        ids = [int(x) for x in re.findall(r'^B(\d+):', response, re.M)]
        observed.extend(ids)
        if 'More batches:' not in response:
            break
        assert len(ids) == 30
        after = ids[-1]
    assert observed == list(range(1, total+1))


def test_retry_ack_race_and_concurrent_reservation_bounded(clock):
    item = due(); row = store.mark_notified([item['id']], now=NOW)[0]
    bid = row['delivery_batch_id']; receipt = row['receipt_command'].split()[-1]
    with ThreadPoolExecutor(max_workers=2) as pool:
        ack = pool.submit(store.acknowledge_alert_emissions, receipt, now=NOW)
        retry = pool.submit(store.release_alert_emission, bid, now=NOW)
        # Retry first may succeed, then its exact historical receipt may also
        # confirm. Ack first makes retry ineligible. Either order confirms once.
        assert ack.result() == 1
        assert retry.result() in (False, True)
    assert store.get_alert_emission(bid)['acked_at']
    assert store.get_item(item['id'])['reminder_count'] == 1
    assert store.get_item(item['id'])['reminder_attempt_count'] == 1
    assert store.get_item(item['id'])['reminder_attempt_count'] == 1
    assert store.get_item(item['id'])['reminder_count'] in (0, 1)
    # Race another attempt at its due check, without bypassing the production lock.
    current = store.get_item(item['id']); clock[0] = datetime.fromisoformat(current['next_check_at'])
    def reserve():
        try:
            store.mark_notified([item['id']], now=clock[0]); return True
        except ValueError:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(lambda _: reserve(), range(8))) == 1
    assert store.get_item(item['id'])['reminder_attempt_count'] == 2


def test_legacy_pending_migration_charges_once_preserves_history_snooze(clock):
    item = due()
    conn = store._connect()
    conn.execute('UPDATE hermes_tingle_items SET reminder_count=3,reminder_attempt_count=3 WHERE id=?', (item['id'],))
    conn.execute('INSERT INTO alert_emissions(item_ids,item_versions,receipt_token,channel,emitted_at) VALUES(?,?,?,?,?)', (json.dumps([item['id']]), json.dumps({item['id']:item['reminder_revision']}), 'abcdef012345abcdef012345','script',NOW.isoformat()))
    conn.commit(); conn.close()
    store.resume_automatic_reminders(now=NOW); store.resume_automatic_reminders(now=NOW)
    current = store.get_item(item['id'])
    assert (current['reminder_attempt_count'], current['reminder_count']) == (4,3)
    assert current['next_check_at'] is None
    assert store.acknowledge_alert_emissions('abcdef012345abcdef012345') == 1
    assert store.get_item(item['id'])['reminder_count'] == 4
    store.snooze_item(item['id'], until=NOW+timedelta(days=5), now=NOW)
    store.resume_automatic_reminders(now=NOW)
    current = store.get_item(item['id'])
    assert current['reminder_attempt_count'] == 0 and current['reminder_count'] == 0
    assert datetime.fromisoformat(current['snoozed_until']) == NOW+timedelta(days=5)


def queue_old(monkeypatch):
    store.enable_source('email',lookback_days=1,enabled_by='synthetic owner')
    bind_synthetic_email(store)
    staged = NOW-timedelta(minutes=30)
    internal = NOW-timedelta(days=1,minutes=10)
    store.stage_gmail_sync(history_id='99', message_ids=['queued-old'], full_reconcile_at=staged, now=staged)
    formats = []
    def bridge(*args, **kwargs):
        if 'getProfile' in args: return {'historyId':'100'}
        if 'list' in args or 'history' in args: return {'messages':[], 'history':[], 'historyId':'100'}
        if 'get' in args:
            params = json.loads(args[-1]); formats.append(params['format'])
            assert params['id']=='queued-old' and params['format']=='minimal'
            return {'internalDate':str(int(internal.timestamp()*1000))}
        raise AssertionError(args)
    monkeypatch.setattr(sources,'_gws_bridge',bridge)
    return formats


def test_expired_queued_candidate_sticky_gap_and_owner_ack(monkeypatch, owner, clock):
    formats = queue_old(monkeypatch)
    first = sources._collect_email_candidates(now=NOW,lookback_days=1)
    assert not first['review_complete'] and 'queued_candidate_expired' in first['incomplete_reasons']
    assert formats == ['minimal'] and store.pending_gmail_total()==0
    assert not store.source_reviewed('email','queued-old','queued-old')
    for offset in (1, 2, 5):
        later = sources._collect_email_candidates(now=NOW+timedelta(hours=offset),lookback_days=1)
        assert not later['review_complete'] and 'queued_candidate_expired' in later['incomplete_reasons']
    store.enable_source('email',lookback_days=1,enabled_by='synthetic regrant')
    bind_synthetic_email(store)
    again = sources._collect_email_candidates(now=NOW+timedelta(hours=6),lookback_days=1)
    assert 'queued_candidate_expired' in again['incomplete_reasons']
    assert 'Recorded your confirmation' in plugin.handle_command(None,'source gap-reviewed email',source=owner)
    recovered = sources._collect_email_candidates(now=NOW+timedelta(hours=7),lookback_days=1)
    assert 'queued_candidate_expired' not in recovered['incomplete_reasons']
    assert store.source_lookback_cap('email') == 1 and formats == ['minimal']


@pytest.mark.skip(reason="Drive provider removed as a source; field-filter behavior no longer exists")
@pytest.mark.parametrize('more', [False, True])
def test_drive_provider_field_filter_honors_more_pages(more, monkeypatch):
    store.disable_source('email'); store.disable_source('calendar')
    store.enable_source('document',lookback_days=1,enabled_by='synthetic owner')
    store.add_item(title='important zebra contract')
    requests=[]
    def bridge(*args, **kwargs):
        params=json.loads(args[-1]); requests.append(params)
        data={'files':[{'id':'synthetic-first','name':'other','mimeType':'text/plain','modifiedTime':'2026-11-16T10:00:00Z'}]}
        if more: data['nextPageToken']='synthetic-page-two-with-match'
        return {k:v for k,v in data.items() if k=='files' or k in params['fields'].split(',')}
    monkeypatch.setattr(sources,'_gws_bridge',bridge)
    review=sources.collect_snapshot(now=NOW)['document_review']
    assert requests[0]['fields']=='nextPageToken,files(id,name,mimeType,modifiedTime)'
    assert review['review_complete'] is (not more)
    assert review['incomplete_reasons']==(['bounded'] if more else [])
    assert review['scanned']==1 and review['matches']==[]
    assert len(requests)==1  # bounded metadata only, not silent complete and no content fetch


def test_snooze_completion_edit_and_unrelated_receipts_never_forge_delivery(clock):
    item = due(); other = due(summary='Synthetic unrelated item')
    row = store.mark_notified([item['id']], now=NOW)[0]
    unrelated = store.mark_notified([other['id']], now=NOW)[0]
    assert store.acknowledge_alert_emissions('0'*24) == 0
    assert store.acknowledge_alert_emissions('') == 0
    assert store.get_item(item['id'])['reminder_count'] == 0
    assert store.acknowledge_alert_emissions(unrelated['receipt_command'].split()[-1]) == 1
    assert store.get_item(item['id'])['reminder_count'] == 0
    store.snooze_item(item['id'],until=NOW+timedelta(days=5),now=NOW)
    assert store.acknowledge_alert_emissions(row['receipt_command'].split()[-1]) in (0,1)
    current=store.get_item(item['id'])
    assert current['reminder_count']==0 and current['reminder_attempt_count']==0
    assert datetime.fromisoformat(current['next_check_at'])==NOW+timedelta(days=5)
    store.release_alert_emission(row['delivery_batch_id'],now=NOW)
    assert store.get_item(item['id'])['next_check_at']==current['next_check_at']
    store.complete_item(item['id'])
    clock[0]+=timedelta(days=10)
    assert not tool(item)['success'] and store.get_item(item['id'])['status']=='done'


def test_explicit_schedule_update_new_cycle_content_update_not_budget_reset(clock):
    item=due()
    for _ in range(4):
        assert tool(item)['success']
        if store.get_item(item['id'])['next_check_at']:
            clock[0]=datetime.fromisoformat(store.get_item(item['id'])['next_check_at'])
    store.update_item(item['id'],title='Synthetic renamed item')
    assert not tool(item)['success']
    store.update_item(item['id'],next_check_at=clock[0]+timedelta(hours=12))
    assert not tool(item)['success']
    clock[0]+=timedelta(hours=12)
    assert tool(item)['success']
    assert store.get_item(item['id'])['reminder_attempt_count']==1


def test_reply_custom_interval_respected_on_every_automatic_attempt(clock,monkeypatch):
    item=due(source_type='email',source_ref='gmail:a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf/synthetic#sent',status='waiting',owner='other',completion_signal='source_email_reply_received')
    store.update_item(item['id'],next_check_at=NOW+timedelta(hours=9))
    monkeypatch.setattr(sources,'_read_email_reply_checks',lambda items:([{'item_id':item['id'],'state':'no_reply'}],[]))
    clock[0]+=timedelta(hours=9)
    for n in range(1,5):
        assert tool(item)['success']
        current=store.get_item(item['id'])
        assert current['reply_interval_seconds']==9*3600
        if n<4:
            assert datetime.fromisoformat(current['next_check_at'])==clock[0]+timedelta(hours=9)
            assert not tool(item)['success']
            clock[0]+=timedelta(hours=9)
        else:
            assert current['next_check_at'] is None


@pytest.mark.parametrize('offset_ms,expired',[(0,False),(1,True)])
def test_email_aging_exact_boundary_minimal_only(offset_ms,expired,monkeypatch,clock):
    store.enable_source('email',lookback_days=1,enabled_by='synthetic owner')
    bind_synthetic_email(store)
    stamp=NOW-timedelta(days=1,milliseconds=offset_ms)
    calls=[]
    def bridge(*args,**kwargs):
        params=json.loads(args[-1]); calls.append(params['format'])
        return {'internalDate':str(int(stamp.timestamp()*1000))}
    monkeypatch.setattr(sources,'_gws_bridge',bridge)
    if expired:
        with pytest.raises(sources.EmailOutsideScope):
            sources._email_timestamp_preflight('synthetic-boundary',now=NOW)
    else:
        assert sources._email_timestamp_preflight('synthetic-boundary',now=NOW)==int(stamp.timestamp()*1000)
    assert calls==['minimal']


def test_long_aged_queue_bounded_then_still_incomplete_after_drain(monkeypatch,clock):
    store.enable_source('email',lookback_days=1,enabled_by='synthetic owner')
    bind_synthetic_email(store)
    staged=NOW-timedelta(minutes=30)
    store.stage_gmail_sync(history_id='99',message_ids=[f'synthetic-old-{i:03}' for i in range(205)],full_reconcile_at=staged,now=staged)
    formats=[]
    def bridge(*args,**kwargs):
        if 'getProfile' in args:return {'historyId':'100'}
        if 'list' in args:return {'messages':[]}
        if 'get' in args:
            params=json.loads(args[-1]); formats.append(params['format'])
            assert params['format']=='minimal'
            return {'internalDate':str(int((NOW-timedelta(days=1,minutes=10)).timestamp()*1000))}
        raise AssertionError(args)
    monkeypatch.setattr(sources,'_gws_bridge',bridge)
    for count in (100,200,205,205):
        result=sources._collect_email_candidates(now=NOW,lookback_days=1)
        assert not result['review_complete'] and 'queued_candidate_expired' in result['incomplete_reasons']
        assert len(formats)==count
    assert store.pending_gmail_total()==0
    assert scalar('SELECT count(*) FROM source_seen')==0
    store.disable_source('email')
    assert scalar('SELECT count(*) FROM gmail_pending_messages')==0
    assert store.get_plugin_meta('coverage_email_aged_queue_gap')=='queued_candidate_expired'
    store.enable_source('email',lookback_days=1,enabled_by='synthetic new grant')
    bind_synthetic_email(store)
    assert 'queued_candidate_expired' in sources._collect_email_candidates(now=NOW,lookback_days=1)['incomplete_reasons']


@pytest.mark.skip(reason="Drive provider removed as a source; chat collector records read_error instead")
def test_drive_provider_error_does_not_report_complete(monkeypatch):
    store.enable_source('document',lookback_days=1,enabled_by='synthetic owner')
    monkeypatch.setattr(sources,'_gws_bridge',lambda *a,**k: (_ for _ in ()).throw(sources.SourceReadError('synthetic provider failure')))
    with pytest.raises(sources.SourceReadError):
        sources._collect_document_matches(now=NOW,lookback_days=1)


@pytest.mark.parametrize('legacy_versions', [None, {}, {'obsolete-item': 0}])
def test_unprovable_legacy_cycle_fail_closed_and_discoverable(legacy_versions,owner,clock):
    item=due()
    conn=store._connect()
    conn.execute('UPDATE hermes_tingle_items SET reminder_count=3,reminder_attempt_count=3 WHERE id=?',(item['id'],))
    conn.execute('INSERT INTO alert_emissions(item_ids,item_versions,receipt_token,emitted_at,surfaced_at) VALUES(?,?,?,?,?)', (json.dumps([item['id']]),json.dumps(legacy_versions) if legacy_versions is not None else None,'a'*24,NOW.isoformat(),NOW.isoformat()))
    conn.commit();conn.close()
    current=store.get_item(item['id'])
    assert current['reminder_budget_uncertain']==1
    assert current['reminder_attempt_count']==3 and current['reminder_count']==3
    assert store.due_items(now=NOW+timedelta(days=20))==[]
    assert not tool(item)['success']
    assert 'Legacy cycle budget is unknown' in plugin.handle_command(None,'retry B1',source=owner)
    view=plugin.handle_command(None,'delivery',source=owner)
    assert 'legacy cycle budget unknown' in view and '/tingle ack' not in view
    # A positive receipt confirms only the historical batch, not its unknown
    # relationship to the current cycle. Recovery must stay visible afterward.
    assert 'Acknowledged' in plugin.handle_command(None,'ack '+'a'*24,source=owner)
    assert store.get_item(item['id'])['reminder_budget_uncertain']==1
    assert 'legacy cycle budget unknown' in plugin.handle_command(None,'delivery',source=owner)
    assert not tool(item)['success']
    store.snooze_item(item['id'],until=NOW+timedelta(days=2),now=NOW)
    assert store.get_item(item['id'])['reminder_budget_uncertain']==0
    assert not tool(item)['success']
    clock[0]+=timedelta(days=2)
    assert tool(item)['success']
    assert store.get_item(item['id'])['reminder_attempt_count']==1


@pytest.mark.parametrize('mutation',['title','complete_step','add_step','skip_step','linked_done','linked_snooze'])
def test_first_post_upgrade_dependency_mutation_preserves_fourth_attempt(mutation,clock):
    goal=due()
    linked=store.add_item(title='Synthetic linked reply wait',source_type='email',source_ref='gmail:a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf/synthetic-legacy#sent',status='waiting',owner='other',completion_signal='source_email_reply_received',next_check_at=NOW)
    store.add_step(goal['id'],title='Collect synthetic first document',why='Synthetic reason',wait_item_id=linked['id'] if mutation.startswith('linked') else '')
    revision=store.get_item(goal['id'])['reminder_revision']
    conn=store._connect()
    conn.execute('UPDATE hermes_tingle_items SET reminder_count=3,reminder_attempt_count=3 WHERE id=?',(goal['id'],))
    conn.execute('INSERT INTO alert_emissions(item_ids,item_versions,receipt_token,emitted_at) VALUES(?,?,?,?)', (json.dumps([goal['id']]),json.dumps({goal['id']:revision}),'b'*24,NOW.isoformat()))
    conn.commit();conn.close()
    if mutation=='title':store.update_item(goal['id'],title='Synthetic renamed goal')
    elif mutation=='complete_step':store.complete_step(goal['id'],1)
    elif mutation=='add_step':store.add_step(goal['id'],title='Check synthetic next document',why='Synthetic reason')
    elif mutation=='skip_step':store.skip_step(goal['id'],1)
    elif mutation=='linked_done':store.complete_item(linked['id'])
    else:store.snooze_item(linked['id'],until=NOW+timedelta(days=2),now=NOW)
    current=store.get_item(goal['id'])
    assert current['reminder_count']==3 and current['reminder_attempt_count']==4
    assert current['next_check_at'] is None
    assert not tool(goal)['success']


def test_created_nine_hour_reply_wait_repeats_nine_hours(clock,monkeypatch):
    item=store.add_item(title='Synthetic nine hour creation',source_type='email',source_ref='gmail:a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf/synthetic-nine#sent',status='waiting',owner='other',completion_signal='source_email_reply_received',next_check_at=NOW+timedelta(hours=9),now=NOW)
    assert item['reply_interval_seconds']==9*3600
    monkeypatch.setattr(sources,'_read_email_reply_checks',lambda items:([{'item_id':item['id'],'state':'no_reply'}],[]))
    clock[0]+=timedelta(hours=9)
    assert tool(item)['success']
    assert datetime.fromisoformat(store.get_item(item['id'])['next_check_at'])==NOW+timedelta(hours=18)


@pytest.mark.parametrize('released_kind',['retry_erased_history','obsolete_after_content_edit'])
def test_released_legacy_history_is_not_proof_of_new_cycle(released_kind,owner,clock):
    item=due()
    conn=store._connect()
    # Faithful old-store outcomes: retry reset count+revision; retirement after
    # a content edit changed revision without a new cycle. Preserve timestamps.
    count=0 if released_kind=='retry_erased_history' else 3
    conn.execute('UPDATE hermes_tingle_items SET reminder_count=?,reminder_attempt_count=?,reminder_revision=1 WHERE id=?',(count,count,item['id']))
    released=(NOW-timedelta(hours=1)).isoformat()
    conn.execute('INSERT INTO alert_emissions(item_ids,item_versions,receipt_token,emitted_at,released_at) VALUES(?,?,?,?,?)', (json.dumps([item['id']]),json.dumps({item['id']:0}),'c'*24,(NOW-timedelta(hours=2)).isoformat(),released))
    conn.commit();conn.close()
    current=store.get_item(item['id'])
    assert current['reminder_budget_uncertain']==1
    assert current['reminder_count']==count and current['reminder_attempt_count']==count
    assert store.get_alert_emission(1)['released_at']==released
    assert not tool(item)['success']
    assert 'legacy cycle budget unknown' in plugin.handle_command(None,'delivery',source=owner)
    assert 'Legacy cycle budget is unknown' in plugin.handle_command(None,'retry B1',source=owner)
    store.update_item(item['id'],next_check_at=NOW+timedelta(hours=12))
    assert store.get_alert_emission(1)['released_at']==released
    clock[0]+=timedelta(hours=12)
    assert tool(item)['success']
    assert store.get_item(item['id'])['reminder_attempt_count']==1
