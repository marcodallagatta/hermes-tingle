"""Synthetic regression tests for look-back recovery and setup boundaries.

The cases cover deduplication, grant narrowing, list-header filtering, reminder
revision invalidation, helper-input revalidation, and repeated narrowing while a
historical-coverage disclosure remains unresolved.

Synthetic only: fake mailboxes, @example.invalid identities and in-memory cron.
"""
import base64
import json
from datetime import datetime, timedelta, timezone

import pytest

import hermes_tingle as plugin
import hermes_tingle_lookback as lb
import hermes_tingle_sources as sources
import hermes_tingle_store as store
import setup_jobs
import source_snapshot
from test_lookback import env
from test_fifth_boundaries import mailbox, collect
from test_setup_jobs import FakeCron, fake_schedule


def _due_revision(item_id):
    snapshot = sources.collect_snapshot()
    return next(x for x in snapshot['due_items'] if x['id'] == item_id)['reminder_revision']


def _reserve_with_revision(item_id, revision):
    return json.loads(plugin.handle_tool(None, {
        'action': 'mark_notified', 'item_ids': [item_id],
        'expected_versions': {item_id: revision}}))


# ---- 1. reviewed candidate is not restaged by the next capped scan ----

def test_reviewed_candidate_not_restaged_by_next_capped_scan(mailbox, capsys):
    first = json.loads(plugin.handle_tool(None, {'action': 'scan_sources', 'lookback_days': 7}))
    lead = first['snapshot']['emails'][0]
    result = json.loads(plugin.handle_tool(None, {
        'action': 'mark_source_reviewed', 'source_type': 'email',
        'source_id': lead['source_id'], 'source_version': lead['source_version'],
        'source_generation': lead['source_generation']}))
    assert result['success']
    again = json.loads(plugin.handle_tool(None, {'action': 'scan_sources', 'lookback_days': 7}))
    import source_snapshot
    assert source_snapshot.main() == 0
    helper = json.loads(capsys.readouterr().out)
    assert helper == {'wakeAgent': False}
    assert again['snapshot']['emails'] == []


def test_other_account_same_message_id_is_not_falsely_suppressed(mailbox):
    """Account-qualified versions: a different account's version never dedupes."""
    first = json.loads(plugin.handle_tool(None, {'action': 'scan_sources', 'lookback_days': 7}))
    lead = first['snapshot']['emails'][0]
    assert json.loads(plugin.handle_tool(None, {
        'action': 'mark_source_reviewed', 'source_type': 'email',
        'source_id': lead['source_id'], 'source_version': lead['source_version'],
        'source_generation': lead['source_generation']}))['success']
    # Same message ID under another account key has a distinct version: no match.
    other_version = 'f' * 64 + ':' + lead['source_id']
    assert other_version != lead['source_version']
    assert not store.source_reviewed('email', lead['source_id'], other_version)
    # The exact qualified version recorded by the real tool call does match.
    assert store.source_reviewed('email', lead['source_id'], lead['source_version'])


# ---- 2/3. legacy migration and body-read dedupe survive owner commands ----

def _legacy_state():
    store.set_plugin_meta('gmail_account_binding', '')
    store.stage_gmail_sync(history_id='legacy-cursor', message_ids=['legacy-id'])


def test_legacy_migration_gap_survives_narrowing_owner_command(env, mailbox):
    _legacy_state()
    text = plugin.handle_command(env[1], 'source enable email 3', source=env[0])
    assert 'opted in' in text
    state = collect()
    assert 'unprovable_account_migration' in state['incomplete_reasons']
    assert store.get_plugin_meta('gmail_coverage_gap') == 'unprovable_account_migration'


def test_legacy_migration_gap_survives_same_scope_and_wider_regrant(env, mailbox):
    _legacy_state()
    assert 'opted in' in plugin.handle_command(env[1], 'source enable email 3', source=env[0])
    for command in ('source enable email 3', 'source enable email 7'):
        assert 'opted in' in plugin.handle_command(env[1], command, source=env[0])
        assert store.get_plugin_meta('gmail_coverage_gap') == 'unprovable_account_migration'
    assert 'unprovable_account_migration' in collect()['incomplete_reasons']


def test_legacy_body_read_dedupe_survives_owner_enable(env, mailbox):
    _legacy_state()
    db = store._connect()
    db.execute('INSERT OR REPLACE INTO email_body_reads(source_id,read_at) VALUES(?,?)',
               ('same-id', datetime.now(timezone.utc).isoformat(timespec='seconds')))
    db.commit(); db.close()
    assert 'opted in' in plugin.handle_command(env[1], 'source enable email 7', source=env[0])
    collect()
    output = json.loads(plugin.handle_tool(None, {'action': 'read_email', 'source_id': 'same-id'}))
    assert output['success'] is False
    assert not any(p.get('format') == 'full' for _, _, p in mailbox[1])


def test_expired_legacy_body_read_rows_drop_on_enable(env, mailbox):
    _legacy_state()
    db = store._connect()
    db.execute('INSERT OR REPLACE INTO email_body_reads(source_id,read_at) VALUES(?,?)',
               ('stale-id', (datetime.now(timezone.utc) - timedelta(days=15)).isoformat(timespec='seconds')))
    db.commit(); db.close()
    assert 'opted in' in plugin.handle_command(env[1], 'source enable email 7', source=env[0])
    db = store._connect()
    assert db.execute('SELECT COUNT(*) FROM email_body_reads').fetchone()[0] == 0
    db.close()


def test_revocation_still_purges_body_read_rows(env, mailbox):
    _legacy_state()
    db = store._connect()
    db.execute('INSERT OR REPLACE INTO email_body_reads(source_id,read_at) VALUES(?,?)',
               ('same-id', datetime.now(timezone.utc).isoformat(timespec='seconds')))
    db.commit(); db.close()
    assert 'opted out' in plugin.handle_command(env[1], 'source disable email', source=env[0])
    db = store._connect()
    assert db.execute('SELECT COUNT(*) FROM email_body_reads').fetchone()[0] == 0
    db.close()


# ---- 7. residual: an unacknowledged gap survives a SECOND narrowing ----

@pytest.mark.parametrize('scan_between', [False, True])
def test_unacknowledged_gap_survives_second_narrowing(env, mailbox, scan_between):
    _legacy_state()
    assert 'opted in' in plugin.handle_command(env[1], 'source enable email 3', source=env[0])
    assert store.get_plugin_meta('gmail_coverage_gap') == 'unprovable_account_migration'
    if scan_between:
        assert 'unprovable_account_migration' in collect()['incomplete_reasons']
    assert 'opted in' in plugin.handle_command(env[1], 'source enable email 2', source=env[0])
    assert store.get_plugin_meta('gmail_coverage_gap') == 'unprovable_account_migration'
    state = collect()
    assert 'unprovable_account_migration' in state['incomplete_reasons']
    assert state['review_complete'] is False


def test_repeated_narrowings_preserve_gap(env, mailbox):
    _legacy_state()
    assert 'opted in' in plugin.handle_command(env[1], 'source enable email 7', source=env[0])
    assert store.get_plugin_meta('gmail_coverage_gap') == 'unprovable_account_migration'
    for command in ('source enable email 3', 'source enable email 2', 'source enable email 1'):
        assert 'opted in' in plugin.handle_command(env[1], command, source=env[0])
        assert store.get_plugin_meta('gmail_coverage_gap') == 'unprovable_account_migration'
    assert 'unprovable_account_migration' in collect()['incomplete_reasons']


@pytest.mark.parametrize('cause', ['unprovable_elapsed_gap', 'unprovable_history_reset',
                                   'unprovable_initial_coverage'])
def test_recorded_gap_cause_preserved_verbatim_on_narrowing(env, mailbox, cause):
    _legacy_state()
    assert 'opted in' in plugin.handle_command(env[1], 'source enable email 3', source=env[0])
    store.set_plugin_meta('gmail_coverage_gap', cause)
    assert 'opted in' in plugin.handle_command(env[1], 'source enable email 2', source=env[0])
    assert store.get_plugin_meta('gmail_coverage_gap') == cause
    assert cause in collect()['incomplete_reasons']


def test_gap_notice_state_survives_second_narrowing(env, mailbox):
    _legacy_state()
    assert 'opted in' in plugin.handle_command(env[1], 'source enable email 3', source=env[0])
    collect()  # First scan records the coverage notice state.
    notice = store.get_plugin_meta('gmail_coverage_notice')
    assert notice
    assert 'opted in' in plugin.handle_command(env[1], 'source enable email 2', source=env[0])
    assert store.get_plugin_meta('gmail_coverage_gap') == 'unprovable_account_migration'
    # Notice state is restored with the marker: no instant duplicate notice,
    # the 24h cadence continues as if the purge never touched these keys.
    assert store.get_plugin_meta('gmail_coverage_notice') == notice


def test_acknowledged_gap_stays_cleared_after_narrowing(env, mailbox):
    _legacy_state()
    assert 'opted in' in plugin.handle_command(env[1], 'source enable email 3', source=env[0])
    assert 'confirmation' in plugin.handle_command(env[1], 'source gap-reviewed email', source=env[0])
    assert store.get_plugin_meta('gmail_coverage_gap') is None
    assert 'opted in' in plugin.handle_command(env[1], 'source enable email 2', source=env[0])
    # The documented owner review is final: a later narrowing revives nothing.
    assert store.get_plugin_meta('gmail_coverage_gap') is None


def test_disable_still_purges_unacknowledged_gap(env, mailbox):
    _legacy_state()
    assert 'opted in' in plugin.handle_command(env[1], 'source enable email 3', source=env[0])
    assert store.get_plugin_meta('gmail_coverage_gap') == 'unprovable_account_migration'
    store.disable_source('email')
    assert store.get_plugin_meta('gmail_coverage_gap') is None


def test_rejected_regrant_leaves_gap_and_grant_untouched(env, mailbox):
    _legacy_state()
    assert 'opted in' in plugin.handle_command(env[1], 'source enable email 3', source=env[0])
    before = store.list_source_optins()
    with pytest.raises(ValueError):
        store.enable_source('email', lookback_days=2, enabled_by='synthetic owner',
                            expected_generation=999999)
    # Rolled back transactionally: gap marker and the recorded grant survive.
    assert store.get_plugin_meta('gmail_coverage_gap') == 'unprovable_account_migration'
    assert store.list_source_optins() == before


def test_migration_gap_reaches_helper_after_second_narrowing(env, mailbox, monkeypatch, capsys):
    original = sources._gws_bridge

    def empty_mailbox(*args):
        if args[2] == 'messages' and args[3] == 'list':
            return {'messages': []}
        return original(*args)

    monkeypatch.setattr(sources, '_gws_bridge', empty_mailbox)
    _legacy_state()
    assert 'opted in' in plugin.handle_command(env[1], 'source enable email 3', source=env[0])
    assert store.get_plugin_meta('gmail_coverage_gap') == 'unprovable_account_migration'
    # No scan or helper has had a chance to disclose this gap yet.
    assert 'opted in' in plugin.handle_command(env[1], 'source enable email 2', source=env[0])
    assert source_snapshot.main() == 0
    after = json.loads(capsys.readouterr().out)
    snapshot = sources.collect_snapshot()
    assert snapshot['email_sync']['review_complete'] is False
    assert 'unprovable_account_migration' in snapshot['email_sync']['incomplete_reasons']
    assert any('legacy scan caches' in e for e in after.get('source_snapshot', {}).get('errors', []))


# ---- 4. duplicate list headers cannot bypass no-body-read filtering ----

@pytest.mark.parametrize('order', [['normal', 'list'], ['list', 'normal']])
def test_list_header_cannot_be_overridden_by_duplicate_empty_header(env, monkeypatch, order):
    stamp = env[4]; full = []

    def bridge(*args):
        p = json.loads(args[-1])
        if args[2] == 'getProfile': return {'emailAddress': 'owner@example.invalid'}
        if args[2] == 'threads': return {'id': 't1', 'messages': [{'id': m} for m in order]}
        if args[3] == 'list': return {'messages': [{'id': m} for m in order]}
        mid = p['id']
        if p['format'] == 'minimal': return {'id': mid, 'internalDate': stamp}
        if p['format'] == 'full':
            full.append(mid)
            return {'id': mid, 'threadId': 't1', 'internalDate': stamp,
                    'payload': {'mimeType': 'text/plain', 'body': {'data': base64.urlsafe_b64encode(b'synthetic').decode()}}}
        headers = [{'name': 'Subject', 'value': 'Synthetic'}]
        if mid == 'list': headers += [{'name': 'List-Id', 'value': 'list.example.invalid'}, {'name': 'List-Id', 'value': ''}]
        return {'id': mid, 'threadId': 't1', 'internalDate': stamp, 'labelIds': ['INBOX'],
                'snippet': 'synthetic', 'payload': {'headers': headers}}
    monkeypatch.setattr(sources, '_gws_bridge', bridge)
    plugin.handle_command(env[1], 'lookback start 7', source=env[0])
    assert 'list' not in full


def test_list_mail_flagging_scans_all_instances():
    flagged = {'headers': [{'name': 'List-Id', 'value': 'list.example.invalid'}, {'name': 'List-Id', 'value': ''}]}
    reversed_order = {'headers': [{'name': 'LIST-ID', 'value': ' '}, {'name': 'list-id', 'value': 'list.example.invalid'}]}
    clean = {'headers': [{'name': 'List-Id', 'value': ''}, {'name': 'Subject', 'value': 'Synthetic'}]}
    assert sources._list_mail_flagged(flagged)
    assert sources._list_mail_flagged(reversed_order)
    assert not sources._list_mail_flagged(clean)
    assert sources._list_mail_flagged({'headers': [{'name': 'List-Id'}]})
    assert sources._list_mail_flagged({'headers': 'not-a-list'})
    assert sources._list_mail_flagged('not-a-payload')


# ---- 5. substate changes invalidate a reviewed reminder revision ----

def _substate_case(mutate):
    store.disable_source('email'); store.disable_source('calendar')
    item = store.add_item(title='Synthetic substate item', next_check_at='2020-01-01T00:00:00Z')
    return item


def test_document_state_change_invalidates_reviewed_revision():
    item = _substate_case(None)
    store.add_document(item['id'], name='Synthetic certificate')
    revision = _due_revision(item['id'])
    store.update_document(item['id'], position=1, state='submitted')
    result = _reserve_with_revision(item['id'], revision)
    assert result['success'] is False
    current = store.get_item(item['id'])
    assert current['reminder_revision'] > revision
    assert current['reminder_attempt_count'] == 0  # stale reservation consumed no attempt


def test_document_add_and_expiry_sweep_invalidate_reviewed_revision():
    item = _substate_case(None)
    revision = _due_revision(item['id'])
    # valid_until must be after the real clock so the sweep inside
    # collect_snapshot does not expire the document before the explicit call.
    store.add_document(item['id'], name='Synthetic expiring certificate', valid_until='2027-01-01')
    assert _reserve_with_revision(item['id'], revision)['success'] is False
    revision = _due_revision(item['id'])
    store.expire_documents(now='2028-01-01T00:00:00Z')
    assert _reserve_with_revision(item['id'], revision)['success'] is False
    assert store.get_item(item['id'])['reminder_attempt_count'] == 0


def test_requirement_change_invalidates_reviewed_revision():
    item = _substate_case(None)
    store.add_requirement(item['id'], text='Synthetic requirement')
    revision = _due_revision(item['id'])
    store.update_requirement(item['id'], position=1, met=True)
    assert _reserve_with_revision(item['id'], revision)['success'] is False
    assert store.get_item(item['id'])['reminder_attempt_count'] == 0


def test_option_change_invalidates_reviewed_revision():
    item = _substate_case(None)
    store.add_option(item['id'], name='Synthetic option')
    revision = _due_revision(item['id'])
    store.update_option(item['id'], position=1, state='shortlisted')
    assert _reserve_with_revision(item['id'], revision)['success'] is False
    assert store.get_item(item['id'])['reminder_attempt_count'] == 0


def test_current_revision_still_reserves_within_budget():
    item = _substate_case(None)
    store.add_document(item['id'], name='Synthetic certificate')
    revision = _due_revision(item['id'])
    result = _reserve_with_revision(item['id'], revision)
    assert result['success'] is True
    assert store.get_item(item['id'])['reminder_attempt_count'] == 1


# ---- 6. setup revalidates the exact helpers captured before confirmation ----

def _helper_root(tmp_path):
    root = tmp_path / 'plugins' / 'hermes-tingle'
    scripts = root / 'scripts'
    scripts.mkdir(parents=True)
    (scripts / 'source_snapshot.py').write_text('# originally reviewed helper\n')
    (scripts / 'due_alerts.py').write_text('# synthetic due helper\n')
    return root, scripts


def test_setup_helper_changed_during_confirmation_is_refused(tmp_path, monkeypatch):
    root, scripts = _helper_root(tmp_path)

    class Context(FakeCron):
        def ask_choice(self, *args):
            (scripts / 'source_snapshot.py').write_text('# changed during confirmation\n')
            return 'create'
    ctx = Context()
    monkeypatch.setattr(setup_jobs, '_cron_api', lambda: (fake_schedule, ctx.read_job))
    result = setup_jobs._activate_jobs(ctx)
    target = tmp_path / 'scripts' / 'hermes_tingle_snapshot.py'
    assert 'stopped before changes' in result
    assert not target.exists()
    assert not ctx.jobs


def test_setup_unchanged_helpers_still_install(tmp_path, monkeypatch):
    root, scripts = _helper_root(tmp_path)
    ctx = FakeCron()
    monkeypatch.setattr(setup_jobs, '_cron_api', lambda: (fake_schedule, ctx.read_job))
    result = setup_jobs._activate_jobs(ctx)
    target = tmp_path / 'scripts' / 'hermes_tingle_snapshot.py'
    assert 'is scheduled' in result
    assert target.read_text() == '# originally reviewed helper\n'
    assert len(ctx.jobs) == 2


def test_setup_cancel_still_changes_nothing(tmp_path, monkeypatch):
    root, scripts = _helper_root(tmp_path)

    class Context(FakeCron):
        def ask_choice(self, *args):
            (scripts / 'source_snapshot.py').write_text('# changed during confirmation\n')
            return 'cancel'
    ctx = Context()
    monkeypatch.setattr(setup_jobs, '_cron_api', lambda: (fake_schedule, ctx.read_job))
    result = setup_jobs._activate_jobs(ctx)
    assert 'cancelled' in result
    assert not (tmp_path / 'scripts' / 'hermes_tingle_snapshot.py').exists()
    assert not ctx.jobs
