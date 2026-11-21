from delivery_fakes import bind_synthetic_email
"""Second review: entry-point outcomes, not just successful state mutations.

Synthetic SQLite only. Provider/socket/process guards are autouse in conftest.
Tests opt into receipts explicitly; preparing or flushing is never delivery.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import io
import json
import sqlite3
import threading
import subprocess
_REAL_POPEN = subprocess.Popen

import pytest
import due_alerts
import source_snapshot
import hermes_tingle as plugin
import hermes_tingle_sources as sources
import hermes_tingle_store as store

NOW = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)


@pytest.fixture
def owner_source(tmp_path):
    from test_hermes_tingle_tool import _FakeSource
    path = tmp_path/'hermes-tingle.json'
    config = json.loads(path.read_text())
    config['owner_principals'] = ['telegram:424242']
    path.write_text(json.dumps(config))
    return _FakeSource()


def scalar(query):
    conn = store._connect()
    try:
        return conn.execute(query).fetchone()[0]
    finally:
        conn.close()


def off():
    for kind in ('email', 'calendar', 'document', 'chat', 'meeting'):
        store.disable_source(kind)


def empty_email(monkeypatch):
    def bridge(*args, **kwargs):
        if 'getProfile' in args:
            return {'historyId': '101'}
        if 'list' in args:
            return {'messages': []}
        raise AssertionError('unexpected provider call')
    monkeypatch.setattr(sources, '_gws_bridge', bridge)
    monkeypatch.setattr(sources, '_read_email_completion_signals', lambda: ([], []))
    monkeypatch.setattr(sources, '_read_email_reply_checks', lambda items: ([], []))


def prepare(item, *, now=NOW):
    result = store.mark_notified([item['id']], now=now)[0]
    return result['delivery_batch_id'], result['receipt_command'].split()[-1]


@pytest.mark.parametrize('recurring', [False, True])
def test_resumed_job_and_snapshot_identify_missed_outcome(recurring, monkeypatch, capsys):
    off()
    item = store.add_item(title='Synthetic missed deadline', hard_deadline=True,
        due_at=NOW-timedelta(days=3), next_check_at=NOW-timedelta(days=4),
        **({'recur_interval': 1, 'recur_unit': 'year'} if recurring else {}))
    snapshot = sources.collect_snapshot(now=NOW)
    missed = [row for row in snapshot['due_items'] if row['status'] == 'missed']
    assert len(missed) == 1 and 'Synthetic missed deadline' in missed[0]['title']
    assert missed[0]['completed_at'] is None
    assert 'wakeAgent' not in source_snapshot.build_agent_input(snapshot)
    assert due_alerts.main() == 0
    text = capsys.readouterr().out
    assert missed[0]['id'] in text and 'Status: missed' in text
    assert 'Synthetic missed deadline' in text
    assert due_alerts.main() == 0
    assert capsys.readouterr().out == ''  # pending delivery never duplicates itself
    assert len(store.list_items(status='missed')) == 1


def test_existing_missed_outcome_recovery_migration_is_once():
    item = store.add_item(title='Synthetic existing missed', next_check_at=NOW)
    conn = store._connect()
    conn.execute("UPDATE hermes_tingle_items SET status='missed',next_check_at=NULL,completed_at=? WHERE id=?", (NOW.isoformat(), item['id']))
    conn.execute("DELETE FROM plugin_meta WHERE key='missed_schedule_recovery_v2'")
    conn.commit(); conn.close()
    assert store.get_item(item['id'])['next_check_at']
    # Recovery must not undo a subsequently exhausted, explicitly confirmed series.
    conn = store._connect()
    conn.execute("UPDATE hermes_tingle_items SET next_check_at=NULL,reminder_count=4 WHERE id=?", (item['id'],))
    conn.commit(); conn.close()
    assert store.get_item(item['id'])['next_check_at'] is None


def reply_wait():
    return store.add_item(title='Synthetic reply wait', source_type='email',
        source_ref='gmail:a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf/synthetic-thread#synthetic-sent', status='waiting', owner='other',
        completion_signal='source_email_reply_received', next_check_at=NOW)


@pytest.mark.parametrize('state', ['no_reply', 'reply_received', 'missing', 'failed'])
def test_actual_agent_entry_requires_fresh_reply_verification(state, monkeypatch):
    item = reply_wait()
    def checks(items):
        assert items[0]['id'] == item['id']
        if state == 'failed':
            return [], ['synthetic provider failure']
        return ([{'item_id': item['id'], 'state': state}] if state != 'missing' else []), []
    monkeypatch.setattr(sources, '_read_email_reply_checks', checks)
    result = json.loads(plugin.handle_tool(None, {'action': 'mark_notified', 'item_ids': [item['id']], 'expected_versions':{i:store.get_item(i)['reminder_revision'] for i in [item['id']]}}))
    assert result['success'] is (state == 'no_reply')
    assert scalar('SELECT count(*) FROM alert_emissions') == int(state == 'no_reply')
    assert store.get_item(item['id'])['reminder_count'] == 0


@pytest.mark.parametrize('outcome', ['failed', 'ambiguous'])
def test_agent_scheduler_failure_or_unknown_holds_batch_without_exhaustion(outcome, monkeypatch, capsys, owner_source):
    item = reply_wait()
    monkeypatch.setattr(sources, '_read_email_reply_checks', lambda items: ([{'item_id': item['id'], 'state': 'no_reply'}], []))
    result = json.loads(plugin.handle_tool(None, {'action': 'mark_notified', 'item_ids': [item['id']], 'expected_versions':{i:store.get_item(i)['reminder_revision'] for i in [item['id']]}}))
    assert result['success']
    # Fake scheduler either loses the final output or cannot report its outcome.
    def scheduler(_text):
        if outcome == 'failed':
            raise OSError('synthetic scheduler failure')
        return None
    try:
        scheduler(result['items'][0]['receipt_command'])
    except OSError:
        pass
    for _ in range(4):
        duplicate = json.loads(plugin.handle_tool(None, {'action': 'mark_notified', 'item_ids': [item['id']], 'expected_versions':{i:store.get_item(i)['reminder_revision'] for i in [item['id']]}}))
        assert not duplicate['success']
    assert store.get_item(item['id'])['reminder_count'] == 0
    assert [row['id'] for row in store.due_items(now=NOW+timedelta(days=30))] == [item['id']]
    assert store.get_item(item['id'])['reminder_attempt_count'] == 1
    assert scalar('SELECT count(*) FROM alert_emissions') == 1
    assert 'B1' in plugin.handle_command(None, 'delivery', source=owner_source)
    assert 'receipt_token' not in plugin.handle_command(None, 'delivery', source=owner_source)
    batch_id = result['items'][0]['delivery_batch_id']
    assert 'Batch released' in plugin.handle_command(None, f'retry B{batch_id}', source=owner_source)
    assert not store.get_alert_emission(batch_id)['acked_at']
    # Source-off still forbids a follow-up nudge after explicit recovery.
    store.disable_source('email')
    assert not json.loads(plugin.handle_tool(None, {'action': 'mark_notified', 'item_ids': [item['id']], 'expected_versions':{i:store.get_item(i)['reminder_revision'] for i in [item['id']]}}))['success']


def test_four_confirmed_agent_notices_and_repeat_ack(monkeypatch):
    item = reply_wait()
    monkeypatch.setattr(sources, '_read_email_reply_checks', lambda items: ([{'item_id': item['id'], 'state': 'no_reply'}], []))
    now = NOW
    for count in range(1, 5):
        batch, receipt = prepare(item, now=now)
        assert store.get_item(item['id'])['reminder_count'] == count-1
        assert store.acknowledge_alert_emissions(receipt, now=now) == 1
        assert store.acknowledge_alert_emissions(receipt, now=now) == 0
        current = store.get_item(item['id'])
        assert current['reminder_count'] == count
        if current['next_check_at']:
            now = datetime.fromisoformat(current['next_check_at'])
    assert current['next_check_at'] is None


def test_mixed_batches_never_bulk_ack_and_concurrent_creation():
    one = store.add_item(title='Synthetic one', next_check_at=NOW)
    two = store.add_item(title='Synthetic two', next_check_at=NOW)
    three = store.add_item(title='Synthetic three', next_check_at=NOW)
    unsent = store.record_alert_emission([one['id']], now=NOW)
    emitted = store.record_alert_emission([two['id']], now=NOW)
    store.mark_emission_flushed(emitted, now=NOW)
    _, agent_receipt = prepare(three)
    assert store.acknowledge_alert_emissions() == 0
    assert store.get_alert_emission(unsent)['acked_at'] is None  # no fabricated owner receipt
    assert store.acknowledge_alert_emissions(agent_receipt, now=NOW) == 1
    four = store.add_item(title='Synthetic concurrent new batch', next_check_at=NOW)
    with ThreadPoolExecutor(max_workers=2) as pool:
        ack = pool.submit(store.acknowledge_alert_emissions, store.get_alert_emission(emitted)['receipt_token'], now=NOW)
        created = pool.submit(store.mark_notified, [four['id']], now=NOW)
        assert ack.result() == 1
        assert created.result()[0]['reminder_count'] == 0
    assert store.get_alert_emission(unsent)['acked_at'] is None
    assert store.get_item(one['id'])['reminder_count'] == 0
    assert store.get_item(two['id'])['reminder_count'] == 1
    assert store.get_item(three['id'])['reminder_count'] == 1
    assert store.get_item(four['id'])['reminder_count'] == 0


def test_concurrent_preparations_reserve_once():
    item = store.add_item(title='Synthetic simultaneous alert', next_check_at=NOW)
    def attempt():
        try:
            store.mark_notified([item['id']], now=NOW)
            return True
        except ValueError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: attempt(), range(2))) == [False, True]
    assert scalar('SELECT count(*) FROM alert_emissions') == 1


def test_stale_batch_receipt_cannot_overwrite_rearmed_item_or_other_batch():
    one = store.add_item(title='Synthetic changed item', next_check_at=NOW)
    two = store.add_item(title='Synthetic unchanged item', next_check_at=NOW)
    prepared = store.mark_notified([one['id'], two['id']], now=NOW)
    receipt = prepared[0]['receipt_command'].split()[-1]
    target = NOW+timedelta(days=10)
    store.update_item(one['id'], next_check_at=target)
    assert store.acknowledge_alert_emissions(receipt, now=NOW) == 1
    assert store.get_item(one['id'])['next_check_at'] == target.isoformat(timespec='seconds')
    assert store.get_item(one['id'])['reminder_count'] == 0
    assert store.get_item(two['id'])['reminder_count'] == 1


@pytest.mark.parametrize('kind', ['email', 'calendar'])
def test_old_snapshot_epoch_rejected_via_actual_tool_after_regrant(kind):
    epoch = store.source_generation(kind)
    store.disable_source(kind)
    store.enable_source(kind, enabled_by='synthetic new grant')
    args = {'action': 'mark_source_reviewed', 'source_type': kind, 'source_id': 'synthetic-old',
            'source_version': 'v1', 'source_generation': epoch}
    assert not json.loads(plugin.handle_tool(None, args))['success']
    args.pop('source_generation')
    assert not json.loads(plugin.handle_tool(None, args))['success']
    assert not store.source_reviewed(kind, 'synthetic-old', 'v1')
    args['source_generation'] = store.source_generation(kind)
    assert json.loads(plugin.handle_tool(None, args))['success']


def test_body_claim_and_read_atomic_with_revoke_regrant(monkeypatch):
    store.register_email_candidates(['synthetic-body'], account_key='a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf')
    claimed = threading.Event(); finish = threading.Event(); revoking = threading.Event(); revoked = threading.Event()
    original = store.claim_email_body_read
    calls = []
    def claim(source_id, **kwargs):
        result = original(source_id, **kwargs)
        claimed.set()
        assert finish.wait(5)
        return result
    monkeypatch.setattr(store, 'claim_email_body_read', claim)
    monkeypatch.setattr(sources, '_email_timestamp_preflight', lambda source_id, **kwargs: calls.append('timestamp'))
    monkeypatch.setattr(sources, '_read_bound_email', lambda *a, **k: calls.append('body') or {'id': 'synthetic-body'})
    def revoke():
        revoking.set()
        store.disable_source('email')
        store.enable_source('email', enabled_by='synthetic new grant')
        bind_synthetic_email(store)
        revoked.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        read = pool.submit(plugin.handle_tool, None, {'action': 'read_email', 'source_id': 'synthetic-body'})
        assert claimed.wait(5)
        revocation = pool.submit(revoke)
        assert revoking.wait(5) and not revoked.wait(.05)
        finish.set()
        assert json.loads(read.result())['success']
        revocation.result()
    assert revoked.is_set() and calls == ['timestamp', 'body']
    assert scalar('SELECT count(*) FROM email_body_reads') == 0
    assert not json.loads(plugin.handle_tool(None, {'action': 'read_email', 'source_id': 'synthetic-body'}))['success']
    assert calls == ['timestamp', 'body']
    monkeypatch.setattr(store, 'claim_email_body_read', original)
    store.register_email_candidates(['synthetic-body'], account_key='a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf')
    # User-authorized revocation clears both source caches and shared body audit.
    assert json.loads(plugin.handle_tool(None, {'action': 'read_email', 'source_id': 'synthetic-body'}))['success']
    assert not json.loads(plugin.handle_tool(None, {'action': 'read_email', 'source_id': 'synthetic-body'}))['success']
    assert calls.count('body') == 2


def test_completed_reentrant_regrant_invalidates_old_claim_before_external_call(monkeypatch):
    store.register_email_candidates(['synthetic-body'], account_key='a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf')
    original = store.claim_email_body_read
    def interleave(source_id):
        claimed = original(source_id)
        store.disable_source('email')
        store.enable_source('email', enabled_by='synthetic new grant')
        bind_synthetic_email(store)
        return claimed
    monkeypatch.setattr(store, 'claim_email_body_read', interleave)
    calls = []
    monkeypatch.setattr(sources, '_email_timestamp_preflight', lambda *a: calls.append('timestamp'))
    result = json.loads(plugin.handle_tool(None, {'action': 'read_email', 'source_id': 'synthetic-body'}))
    assert not result['success'] and not calls


def test_provider_failure_never_all_clear_then_recovers(monkeypatch):
    empty_email(monkeypatch)
    normal = sources._gws_bridge
    def fail(*args, **kwargs):
        if 'list' in args:
            raise sources.SourceReadError('synthetic reconciliation failure')
        return normal(*args, **kwargs)
    monkeypatch.setattr(sources, '_gws_bridge', fail)
    result = sources._collect_email_candidates(now=NOW, lookback_days=1)
    assert result['errors'] and not result['review_complete']
    assert 'provider_or_sync_failure' in result['incomplete_reasons']
    monkeypatch.setattr(sources, '_gws_bridge', normal)
    assert sources._collect_email_candidates(now=NOW, lookback_days=1)['review_complete']


def test_bounded_gap_persists_after_success_without_widening_and_notice_is_bounded(monkeypatch):
    store.enable_source('email', lookback_days=1, enabled_by='synthetic grant')
    bind_synthetic_email(store)
    store.stage_gmail_sync(history_id='99', message_ids=[], full_reconcile_at=NOW-timedelta(days=2))
    empty_email(monkeypatch)
    result = sources._collect_email_candidates(now=NOW, lookback_days=30)
    assert not result['review_complete'] and 'unprovable_elapsed_gap' in result['incomplete_reasons']
    assert any('lookback' in error for error in result['errors'])
    again = sources._collect_email_candidates(now=NOW+timedelta(minutes=30), lookback_days=30)
    assert not again['review_complete'] and not again['errors']
    assert store.source_lookback_cap('email') == 1


@pytest.mark.parametrize('cycle', [0, 1, 2])
def test_fresh_source_off_is_honest_and_does_not_wake(cycle):
    off()
    for i in range(cycle+1):
        snap = sources.collect_snapshot(now=NOW+timedelta(minutes=i))
        assert not snap['errors']
        assert snap['email_sync']['incomplete_reasons'] == ['disabled']
        assert snap['calendar_review']['incomplete_reasons'] == ['disabled']
        assert not snap['calendar_review']['review_complete']
        assert source_snapshot.build_agent_input(snap) == '{"wakeAgent": false}'


def test_partial_optin_empty_enabled_calendar_remains_actionable(monkeypatch):
    off()
    store.enable_source('calendar', enabled_by='synthetic grant')
    monkeypatch.setattr(sources, '_read_calendars', lambda: [])
    snap = sources.collect_snapshot(now=NOW)
    assert 'no_accessible_calendars' in snap['calendar_review']['incomplete_reasons']
    assert not snap['calendar_review']['review_complete']
    assert 'no accessible configured calendars' in ' '.join(snap['errors'])
    assert source_snapshot.build_agent_input(snap) != '{"wakeAgent": false}'


def test_actual_source_helper_is_quiet_when_sources_off(capsys):
    off()
    assert source_snapshot.main() == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == '{"wakeAgent": false}'
    assert captured.err == ''


def test_calendar_candidate_epoch_survives_snapshot_and_stale_ack_does_not_suppress(monkeypatch):
    store.disable_source('email')
    monkeypatch.setattr(sources, '_read_calendars', lambda: [{'id': 'synthetic-cal', 'summary': 'Synthetic calendar'}])
    monkeypatch.setattr(sources, '_read_calendar_events', lambda *a: [{'source_id': 'synthetic-cal:event', 'source_version': 'v1'}])
    old = sources.collect_snapshot(now=NOW)['calendar_events'][0]
    store.disable_source('calendar')
    store.enable_source('calendar', enabled_by='synthetic new grant')
    args = dict(old, action='mark_source_reviewed', source_type='calendar')
    assert not json.loads(plugin.handle_tool(None, args))['success']
    fresh = sources.collect_snapshot(now=NOW)['calendar_events']
    assert len(fresh) == 1 and fresh[0]['source_generation'] != old['source_generation']


def test_separate_process_revoke_waits_for_actual_body_read(tmp_path, monkeypatch):
    import select
    import subprocess
    import sys
    from pathlib import Path
    store.register_email_candidates(['synthetic-body'], account_key='a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf')
    conn = store._connect()
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    conn.close()
    entered = threading.Event(); finish = threading.Event()
    def preflight(source_id, **kwargs):
        entered.set()
        assert finish.wait(5)
    monkeypatch.setattr(sources, '_email_timestamp_preflight', preflight)
    monkeypatch.setattr(sources, '_read_bound_email', lambda *a, **k: {'id': 'synthetic-body'})
    script = '''import sys,sqlite3,socket
import runtime_stubs
runtime_stubs.install()
import hermes_tingle_store as s
s.plugin_db=lambda _: sqlite3.connect(sys.argv[1])
def forbidden(*a,**k): raise AssertionError('network forbidden')
socket.socket.connect=forbidden
print('ready',flush=True)
s.disable_source('email')
s.enable_source('email',enabled_by='synthetic new process grant')
print('revoked-and-regranted',flush=True)
'''
    root = Path(__file__).resolve().parents[1]
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(tmp_path), 'HERMES_HOME': str(tmp_path),
           'PYTHONPATH': str(root)+':'+str(root/'tests')}
    with ThreadPoolExecutor(max_workers=1) as pool:
        read = pool.submit(plugin.handle_tool, None, {'action': 'read_email', 'source_id': 'synthetic-body'})
        assert entered.wait(5)
        child = _REAL_POPEN([sys.executable, '-c', script, path], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=env)
        try:
            assert child.stdout.readline().strip() == 'ready'
            assert not select.select([child.stdout], [], [], .05)[0]
            finish.set()
            assert json.loads(read.result(5))['success']
            out, err = child.communicate(timeout=10)
            assert child.returncode == 0, err
            assert out.strip() == 'revoked-and-regranted'
        finally:
            finish.set()
            if child.poll() is None:
                child.kill(); child.communicate()
    assert scalar('SELECT count(*) FROM email_candidates') == 0
    assert scalar('SELECT count(*) FROM email_body_reads') == 0
    assert not json.loads(plugin.handle_tool(None, {'action': 'read_email', 'source_id': 'synthetic-body'}))['success']


def test_alias_migration_repoints_revision_bound_outbox_and_entrypoints(owner_source):
    conn = store._connect()
    stamp = NOW.isoformat(timespec='seconds')
    conn.execute("INSERT INTO hermes_tingle_items(id,title,next_check_at,created_at,updated_at,reminder_revision) VALUES('OL-ABC123','Synthetic alias reminder',?,?,?,7)", (stamp,stamp,stamp))
    conn.execute("INSERT INTO alert_emissions(item_ids,item_versions,emitted_at,channel,receipt_token) VALUES(?,?,?,'agent',?)", ('["OL-ABC123"]','{"OL-ABC123":7}',stamp,'abcdef012345abcdef012345'))
    conn.commit(); conn.close()
    item = store.get_item('OL-ABC123')
    assert item['id'].startswith('HT-')
    batch = store.get_alert_emission(1)
    assert json.loads(batch['item_versions']) == {item['id']: 7}
    assert store.held_reminder_item_ids() == set()
    store.resume_automatic_reminders(now=NOW)
    assert store.get_item(item['id'])['reminder_attempt_count'] == 1
    assert json.loads(plugin.handle_tool(None, {'action':'get','item_id':'OL-ABC123'}))['item']['id'] == item['id']
    assert 'Acknowledged' in plugin.handle_command(None, 'ack abcdef012345abcdef012345', source=owner_source)
    assert store.get_item('OL-ABC123')['reminder_count'] == 1
    conn = store._connect()
    assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    conn.close()


@pytest.mark.parametrize('command', ['ack abcdef012345abcdef012345', 'delivery', 'retry B1'])
def test_delivery_controls_refuse_missing_or_nonowner_or_group(command, owner_source):
    from test_hermes_tingle_tool import _FakeSource
    for source in (None, _FakeSource(user_id='99999'), _FakeSource(chat_type='group'), _FakeSource(is_bot=True)):
        answer = plugin.handle_command(None, command, source=source)
        assert 'Acknowledged' not in answer and 'Hold released' not in answer
        assert 'Held reminder batches' not in answer


@pytest.mark.parametrize('helper', ['due_alerts.py', 'source_snapshot.py'])
@pytest.mark.parametrize('evaluation_enabled', [False, True])
def test_installed_copied_helper_bootstraps_without_repository_pythonpath(helper, tmp_path, evaluation_enabled):
    from pathlib import Path
    import shutil
    import sys
    root = Path(__file__).resolve().parents[1]
    plugin_root = tmp_path/'plugins'/'hermes-tingle'
    plugin_root.mkdir(parents=True)
    for name in ('hermes_tingle_debug.py', 'hermes_tingle.py', 'hermes_tingle_calendar_transitions.py', 'hermes_tingle_store.py', 'hermes_tingle_sources.py', 'hermes_tingle_config.py', 'hermes_tingle_review.py', 'hermes_tingle_discovery.py', 'hermes_tingle_discovery_assess.py', 'hermes_tingle_wiring.py', 'hermes_tingle_resources.py', 'hermes_tingle_expiry.py', 'hermes_tingle_expiry_preferences.py', 'hermes_tingle_case_merge.py'):
        shutil.copy2(root/name, plugin_root/name)
    installed = tmp_path/'scripts'/'copied_helper.py'
    installed.parent.mkdir()
    shutil.copy2(root/'scripts'/helper, installed)
    runtime = tmp_path/'synthetic-runtime'
    (runtime/'plugins').mkdir(parents=True)
    (runtime/'plugins'/'__init__.py').write_text('')
    (runtime/'plugins'/'plugin_storage.py').write_text('''import sqlite3,os
from pathlib import Path
def plugin_db(_):
    c=sqlite3.connect(Path(os.environ['HERMES_HOME'])/'default-test.db')
    c.execute('PRAGMA foreign_keys=ON')
    return c
''')
    (runtime/'hermes_constants.py').write_text('''import os
from pathlib import Path
def get_hermes_home(): return Path(os.environ['HERMES_HOME'])
''')
    (runtime/'sitecustomize.py').write_text('''import socket,subprocess
def forbidden(*a,**k): raise AssertionError('synthetic helper: external access forbidden')
socket.socket.connect=forbidden
socket.socket.connect_ex=forbidden
subprocess.Popen=forbidden
''')
    off()
    discovery_text = None
    if helper == 'source_snapshot.py':
        import hermes_constants
        from types import SimpleNamespace
        from hermes_tingle_discovery import observe_source
        config_path = hermes_constants.get_hermes_home()/'hermes-tingle.json'
        config = json.loads(config_path.read_text())
        config['owner_principals'] = ['telegram:42']
        if evaluation_enabled:
            config['private_chat_evaluation'] = {
                'enabled': True, 'consent': 'owner-dm-excerpts-v1', 'approved_by': 'telegram:42',
                'route': 'synthetic-fixed', 'model': 'synthetic-pinned', 'pricing_revision': 'synthetic-v1',
                'monthly_limit_microeur': 5_000_000}
        config_path.write_text(json.dumps(config))
        store.enable_source('chat', enabled_by='synthetic owner')
        store.note_known_chat('telegram:42', origin='inbound')
        observe_source(SimpleNamespace(platform='telegram', user_id='42', chat_id='42',
                                       chat_type='dm'))
        discovery_text = 'I will send the signed form next week.'
        timestamp = store._now().timestamp() - 60
        (runtime/'hermes_state.py').write_text(f'''DEFAULT_DB_PATH = "synthetic-state.db"
class SessionDB:
    def __init__(self, path, read_only=False):
        assert read_only is True
    def find_session_by_origin(self, *, platform, chat_id):
        return "session-synthetic" if platform == "telegram" and chat_id == "42" else None
    def get_messages(self, session_id, limit=None, latest=False, include_ancestors=None):
        assert session_id == "session-synthetic" and latest is True and include_ancestors is False
        return [{{"role":"user", "content":{discovery_text!r}, "timestamp":{timestamp!r}}}]
''')
    if helper == 'source_snapshot.py' and evaluation_enabled:
        # A separate synthetic bootstrap installs a FAKE verified adapter; the
        # copied production helper still performs wiring/collection/evaluation/
        # atomic staging/sanitizing with repository PYTHONPATH absent.
        with (runtime/'sitecustomize.py').open('a') as boot:
            boot.write(f'\nimport sys\nsys.path.insert(0, {str(plugin_root)!r})\n')
            boot.write('''import json
import hermes_tingle_discovery_assess as assess
def send(request, **kwargs):
    entries=json.loads(request)['input']['excerpts']
    assert len(entries)==1
    assert set(entries[0])=={'id','excerpt','clipped'}
    return json.dumps({'results':[{'id':e['id'],'decision':'keep','label':'Send the signed form','evidence':e['excerpt']} for e in entries]})
route=assess.VerifiedRoute('synthetic-fixed','synthetic-pinned','synthetic-v1',1,2,send)
assess.VERIFIED_ROUTES={route.route:route}
''')
    item = store.add_item(title='Synthetic copied-helper missed deadline', hard_deadline=True,
                         due_at=NOW-timedelta(days=1), next_check_at=NOW-timedelta(days=2))
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(tmp_path), 'HERMES_HOME': str(tmp_path),
           'TMPDIR': str(tmp_path), 'PYTHONPATH': str(runtime)}
    child = _REAL_POPEN([sys.executable, str(installed)], cwd=tmp_path, env=env,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out, err = child.communicate(timeout=10)
    assert child.returncode == 0, err
    assert item['id'] in out and 'Synthetic copied-helper missed deadline' in out
    assert 'missed' in out
    if helper == 'source_snapshot.py':
        payload=json.loads(out)
        assert 'case_merge_guidance' in payload
        assert 'SAME current awaited outcome' in payload['case_merge_guidance']
        assert 'Distinct obligations' in payload['case_merge_guidance']
        assert 'never confirms the merge itself' in payload['case_merge_guidance']
        assert 'authenticated paginated preview' in payload['case_merge_guidance']
        assert 'case_merge_context' in payload['source_snapshot']
        assert 'discovery' not in payload['source_snapshot']['chat_review']
        assert discovery_text is not None
        assert discovery_text.casefold() not in out.casefold()
        conn = store._connect()
        try:
            candidates = [dict(row) for row in conn.execute(
                'SELECT * FROM hermes_tingle_chat_discovery_candidates')]
            assert conn.execute('SELECT COUNT(*) FROM hermes_tingle_items').fetchone()[0] == 1
        finally:
            conn.close()
        # Both verified evaluation and unconfigured legacy discovery preserve the suggestion.
        assert len(candidates) == 1 and candidates[0]['action_label'] == 'Send the signed form', (out, err)
        assert candidates[0]['source_locator'] not in out
        assert 'CR-' not in out and 'evaluation_status' not in out
    assert store.get_item(item['id'])['status'] == 'missed'


def test_multiple_elapsed_recurring_windows_each_get_unresolved_identity():
    item = store.add_item(title='Synthetic daily obligation', hard_deadline=True,
        due_at=NOW-timedelta(days=4), recur_interval=1, recur_unit='day')
    outcomes = store.sweep_missed_deadlines(now=NOW)
    assert len(outcomes) == 4
    missed = store.list_items(status='missed')
    assert len(missed) == 4 and len({row['id'] for row in missed}) == 4
    assert {row['due_at'] for row in missed} == {(NOW-timedelta(days=i)).isoformat(timespec='seconds') for i in range(1,5)}
    assert all(row['completed_at'] is None for row in missed)
    assert store.sweep_missed_deadlines(now=NOW) == []
    assert store.get_item(item['id'])['due_at'] == NOW.isoformat(timespec='seconds')


def test_bounded_recurrence_catchup_persists_next_unprocessed_window(monkeypatch, capsys):
    off()
    item = store.add_item(title='Synthetic long downtime', hard_deadline=True,
        due_at=NOW-timedelta(days=205), recur_interval=1, recur_unit='day')
    first = store.sweep_missed_deadlines(now=NOW)
    assert len(first) == 100 and first[-1]['catchup_pending']
    assert not store.deadline_catchup_state(now=NOW)['complete']
    assert scalar('SELECT count(*) FROM recurrence_completions') == 100
    snapshot = sources.collect_snapshot(now=NOW)
    assert not snapshot['deadline_catchup']['complete']
    assert any('catch-up is incomplete' in error for error in snapshot['errors'])
    assert item['id'] not in {row['id'] for row in snapshot['due_items']}
    assert scalar('SELECT count(*) FROM recurrence_completions') == 200
    last = store.sweep_missed_deadlines(now=NOW)
    assert len(last) == 5 and not last[-1]['catchup_pending']
    assert store.deadline_catchup_state(now=NOW)['complete']
    assert scalar('SELECT count(*) FROM recurrence_completions') == 205
    assert len(store.list_items(status='missed', limit=100)) == 100  # display is paged, records are not lost
    assert scalar("SELECT count(DISTINCT source_ref) FROM hermes_tingle_items WHERE status='missed'") == 205


def test_missed_recurring_occurrence_respects_existing_future_snooze():
    item = store.add_item(title='Synthetic snoozed recurrence', hard_deadline=True,
        due_at=NOW-timedelta(days=2), recur_interval=1, recur_unit='day')
    store.snooze_item(item['id'], until=NOW+timedelta(days=3), now=NOW-timedelta(days=3))
    store.sweep_missed_deadlines(now=NOW)
    assert all(row['snoozed_until'] for row in store.list_items(status='missed'))
    assert not store.due_items(now=NOW)


@pytest.mark.parametrize('initial_failure', [False, True])
def test_delayed_first_poll_uses_grant_origin_even_without_successful_reconcile(monkeypatch, initial_failure):
    store.enable_source('email', lookback_days=1, enabled_by='synthetic owner')
    bind_synthetic_email(store)
    conn = store._connect()
    conn.execute("UPDATE hermes_tingle_source_optins SET enabled_at=? WHERE kind='email'", ((NOW-timedelta(days=5)).isoformat(),))
    conn.commit(); conn.close()
    empty_email(monkeypatch)
    if initial_failure:
        normal = sources._gws_bridge
        def fail(*args, **kwargs):
            if 'list' in args:
                raise sources.SourceReadError('synthetic first-poll failure')
            return normal(*args, **kwargs)
        monkeypatch.setattr(sources, '_gws_bridge', fail)
        result = sources._collect_email_candidates(now=NOW-timedelta(days=1), lookback_days=1)
        assert not result['review_complete']
        empty_email(monkeypatch)
    result = sources._collect_email_candidates(now=NOW, lookback_days=1)
    assert not result['review_complete']
    assert 'unprovable_elapsed_gap' in result['incomplete_reasons']
    assert store.source_lookback_cap('email') == 1


@pytest.mark.parametrize('mutation', ['check', 'snooze', 'complete', 'edit'])
def test_selected_script_revision_cannot_bind_to_later_owner_change(mutation, monkeypatch, capsys):
    off()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    item = store.add_item(title='Synthetic selected revision', next_check_at=now-timedelta(days=1))
    future = now+timedelta(days=60)
    original = due_alerts._render_selected
    def change(*args, **kwargs):
        if mutation == 'check':
            store.update_item(item['id'], next_check_at=future)
        elif mutation == 'snooze':
            store.snooze_item(item['id'], until=future, now=now)
        elif mutation == 'complete':
            store.complete_item(item['id'], now=now)
        else:
            store.update_item(item['id'], title='Synthetic changed meaning')
        return original(*args, **kwargs)
    monkeypatch.setattr(due_alerts, '_render_selected', change)
    assert due_alerts.main() == 0
    assert capsys.readouterr().out == ''
    assert scalar('SELECT count(*) FROM alert_emissions') == 0
    assert store.get_item(item['id'])['reminder_count'] == 0
    if mutation in ('check', 'snooze'):
        assert store.get_item(item['id'])['next_check_at'] == future.isoformat(timespec='seconds')


def test_model_verified_selection_cannot_bind_to_changed_owner_schedule(monkeypatch):
    item = reply_wait()
    future = NOW+timedelta(days=60)
    def interleave(items):
        store.update_item(item['id'], next_check_at=future)
        return [{'item_id': item['id'], 'state': 'no_reply'}], []
    monkeypatch.setattr(sources, '_read_email_reply_checks', interleave)
    result = json.loads(plugin.handle_tool(None, {'action': 'mark_notified', 'item_ids': [item['id']], 'expected_versions':{i:store.get_item(i)['reminder_revision'] for i in [item['id']]}}))
    assert not result['success'] and 'changed during reminder preparation' in result['error']
    assert scalar('SELECT count(*) FROM alert_emissions') == 0
    assert store.get_item(item['id'])['next_check_at'] == future.isoformat(timespec='seconds')


def test_history_reset_cannot_erase_historical_gap_on_next_success(monkeypatch):
    store.enable_source('email', enabled_by='synthetic owner')
    bind_synthetic_email(store)
    store.stage_gmail_sync(history_id='old', message_ids=[], full_reconcile_at=NOW-timedelta(days=1))
    def history(cursor):
        if cursor == 'old':
            raise sources.SourceReadError('synthetic expired history cursor')
        return [], 'new'
    monkeypatch.setattr(sources, '_read_gmail_history', history)
    monkeypatch.setattr(sources, '_read_gmail_profile_history_id', lambda: 'new')
    monkeypatch.setattr(sources, '_stage_reconciliation_pages', lambda *a, **k: None)
    for i in range(4):
        result = sources._collect_email_candidates(now=NOW+timedelta(minutes=30*i), lookback_days=1)
        assert not result['review_complete']
        if i >= 2:
            assert 'unprovable_history_reset' in result['incomplete_reasons']
    assert store.gmail_sync_state()['history_id'] == 'new'


def test_calendar_completion_explicitly_means_upcoming_scope_not_historical_gap(monkeypatch):
    off()
    store.enable_source('calendar', enabled_by='synthetic owner')
    monkeypatch.setattr(sources, '_read_calendars', lambda: [{'id': 'synthetic-cal', 'summary': 'Synthetic calendar'}])
    monkeypatch.setattr(sources, '_read_calendar_events', lambda *a: [])
    result = sources.collect_snapshot(now=NOW)['calendar_review']
    assert result['review_complete']
    assert not result['historical_coverage_verified']
    assert 'upcoming events' in result['coverage_scope']
