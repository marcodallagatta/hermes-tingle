"""Code-enforced fresh thread checks, synthetic metadata only."""
from datetime import datetime, timedelta, timezone
import copy
import json
import pytest
import hermes_tingle_store as store
import hermes_tingle_sources as sources
import hermes_tingle as plugin
from scripts import due_alerts
ACCOUNT_KEY = 'a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf'

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def item(**kw):
    return store.add_item(title='Wait for the signed form', source_type='email',
        source_ref='gmail:'+ACCOUNT_KEY+'/thread#anchor', completion_signal='source_email_checkpoint',
        review_plan=dict(actor='other', outcome='Receive the signed form', basis='inferred',
                         review_at=(NOW-timedelta(days=1)).isoformat(), review_needed=False), **kw)


def metadata(new=False):
    messages=[dict(id='anchor', internalDate='1794000000000', labelIds=['INBOX'], snippet='Form processing has started.')]
    if new:
        messages.append(dict(id='reply', internalDate='1794100000000', labelIds=['INBOX'], snippet='A further document is required.'))
    return {'id':'thread','messages':messages}


def bind(monkeypatch,payload):
    monkeypatch.setattr(sources,'_guard_thread_lookback',lambda thread:None)
    monkeypatch.setattr(sources,'_gmail_bridge',lambda *a,**k:copy.deepcopy(payload))


def test_no_network_formatter_cannot_send_checkpoint(monkeypatch):
    i=item()
    assert due_alerts.alertable_due_items([i])==[]
    with pytest.raises(ValueError,match='verified model-assisted'):
        store.record_alert_emission([i['id']], expected_versions={i['id']:i['reminder_revision']})
    assert store.get_item(i['id'])['reminder_attempt_count']==0


def test_matching_fresh_check_reserves_only_contextual_next_time(monkeypatch):
    i=item();bind(monkeypatch,metadata())
    checks,errors=sources._read_email_checkpoint_checks([i]); assert not errors
    assert checks[0]['state']=='no_thread_update'
    nxt=(NOW+timedelta(days=21)).isoformat()
    rows=sources.prepare_reminder_batch([i['id']],expected_versions={i['id']:i['reminder_revision']},
        expected_email_checks={i['id']:checks[0]['receipt']},checkpoint_next_checks={i['id']:nxt})
    assert rows[0]['next_check_at']==nxt and rows[0]['reminder_attempt_count']==1


def test_changed_thread_invalidates_previously_reviewed_receipt(monkeypatch):
    i=item();bind(monkeypatch,metadata())
    checks,_=sources._read_email_checkpoint_checks([i])
    bind(monkeypatch,metadata(new=True))
    with pytest.raises(sources.SourceReadError,match='changed'):
        sources.prepare_reminder_batch([i['id']],expected_versions={i['id']:i['reminder_revision']},
            expected_email_checks={i['id']:checks[0]['receipt']},checkpoint_next_checks={i['id']:None})
    assert store.get_item(i['id'])['reminder_attempt_count']==0


def test_changed_thread_is_context_not_completion(monkeypatch):
    i=item();bind(monkeypatch,metadata(new=True))
    checks,errors=sources._read_email_checkpoint_checks([i])
    assert not errors and checks[0]['state']=='thread_changed'
    assert checks[0]['messages'][0]['source_id']=='reply'
    assert store.get_item(i['id'])['status']=='waiting'


@pytest.mark.parametrize('bad', [dict(id='wrong',messages=[]), dict(id='thread',messages=[]),
    dict(id='thread',messages=[dict(id='anchor',internalDate='bad',labelIds=['INBOX'])])])
def test_incomplete_source_never_verifies(monkeypatch,bad):
    i=item();bind(monkeypatch,bad)
    checks,errors=sources._read_email_checkpoint_checks([i])
    assert errors and not checks


def test_disabled_source_never_performs_thread_read(monkeypatch):
    i=item();store.disable_source('email')
    monkeypatch.setattr(sources,'_gmail_bridge',lambda *a,**k:pytest.fail('read after source disable'))
    with pytest.raises(sources.SourceReadError,match='disabled'):
        sources.prepare_reminder_batch([i['id']],expected_versions={i['id']:i['reminder_revision']},
            expected_email_checks={i['id']:'invented'},checkpoint_next_checks={i['id']:None})


def test_unknown_plan_not_generic_three_day_schedule():
    i=store.add_item(title='Review context',source_type='email',
        source_ref='gmail:'+ACCOUNT_KEY+'/thread#anchor',completion_signal='source_email_checkpoint')
    assert i['next_check_at'] is None and i['owner']=='unknown'
    assert json.loads(i['email_review_plan'])['review_needed']


def test_explicit_schedule_wins_and_close_purges_derived_plan():
    explicit=(NOW+timedelta(days=40)).isoformat()
    i=item(next_check_at=explicit)
    assert i['next_check_at']==explicit
    store.complete_item(i['id'])
    assert store.get_item(i['id'])['email_review_plan']==''


def test_unknown_repeat_pauses_and_is_visible_in_list(monkeypatch):
    i=item();bind(monkeypatch,metadata())
    checks,_=sources._read_email_checkpoint_checks([i])
    sources.prepare_reminder_batch([i['id']],expected_versions={i['id']:i['reminder_revision']},
        expected_email_checks={i['id']:checks[0]['receipt']},checkpoint_next_checks={i['id']:None})
    assert store.get_item(i['id'])['next_check_at'] is None


def test_existing_item_not_reclassified_by_new_schema():
    old=store.add_item(title='Historical task',source_type='email',source_ref='gmail:'+ACCOUNT_KEY+'/old')
    store._connect().close()
    assert store.get_item(old['id'])==old and old['completion_signal']==''


def test_model_cannot_remove_verification_gate():
    i=item()
    with pytest.raises(ValueError,match='cannot bypass'):
        store.update_item(i['id'],completion_signal='')


def test_unknown_check_pause_is_visible_to_owner(monkeypatch):
    i=store.add_item(title='Review timing',source_type='email',
        source_ref='gmail:'+ACCOUNT_KEY+'/thread#anchor',completion_signal='source_email_checkpoint')
    # List is a view, not a source fetch. Existing nonempty profile skips onboarding.
    out=plugin.handle_command(None,'list',source=None)
    assert 'Review timing' in out and 'no reminder scheduled' not in out
    # Diagnostic state remains intact, available in the owner-only timing view.
    assert not store.get_item(i['id'])['next_check_at']


def test_item_and_temporary_plan_migrations_are_additive(monkeypatch):
    i=store.add_item(title='Existing item',summary='Keep this data')
    conn=store._connect();conn.execute('ALTER TABLE hermes_tingle_items DROP COLUMN email_review_plan');conn.commit();conn.close()
    actual=store.get_item(i['id'])
    assert actual==i and actual['email_review_plan']==''
    conn=store._connect();assert conn.execute('PRAGMA integrity_check').fetchone()[0]=='ok';conn.close()


def test_archived_unlabelled_thread_metadata_is_valid(monkeypatch):
    i=item(); payload=metadata(); payload['messages'][0]['labelIds']=[]
    bind(monkeypatch,payload)
    checks,errors=sources._read_email_checkpoint_checks([i])
    assert not errors and checks[0]['state']=='no_thread_update'
