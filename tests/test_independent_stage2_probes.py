# Independent reviewer stress probes (synthetic). Synthetic only.
import json
from datetime import datetime, timedelta, timezone
import pytest
import hermes_tingle_store as store
import hermes_tingle_sources as sources
import hermes_tingle_lookback as lb

NOW = datetime.now(timezone.utc).replace(microsecond=0)

# 1. Legacy unbound grant: first verified check binds the account AND invalidates
#    old account-unqualified seen markers (never retroactive attribution).
def test_probe_legacy_unbound_first_read_binds_and_invalidates_markers(synthetic_settings):
    conn = store._connect()
    conn.execute("DELETE FROM plugin_meta WHERE key='calendar_account_binding'")
    conn.execute("INSERT INTO source_seen(source_type,source_id,reviewed_at) VALUES('calendar','evt-1','2026-11-01T00:00:00+00:00')")
    conn.commit(); conn.close()
    key, generation, _home, _policy = sources._calendar_identity_check()
    assert key == 'c' * 64 and generation == store.source_generation('calendar')
    bound = json.loads(store.get_plugin_meta('calendar_account_binding'))
    assert bound == {'generation': generation, 'account': 'c' * 64}
    conn = store._connect()
    assert not conn.execute("SELECT * FROM source_seen WHERE source_type='calendar'").fetchall()
    conn.close()

# 2. Same grant, different account: refuse before any event content.
def test_probe_account_switch_same_generation_refused(synthetic_settings, monkeypatch):
    monkeypatch.setattr(sources, 'calendar_account_identity', lambda: 'd' * 64)
    with pytest.raises(sources.SourceReadError, match='account changed'):
        sources._calendar_identity_check()

# 3. Re-enable binding a DIFFERENT account wipes account-unqualified seen markers;
#    prior account events can never suppress the new account's events.
def test_probe_different_account_reenable_wipes_markers(synthetic_settings):
    store.disable_source('calendar')
    store.enable_source('calendar', enabled_by='owner', account_key='c' * 64)
    conn = store._connect()
    conn.execute("INSERT INTO source_seen(source_type,source_id,reviewed_at) VALUES('calendar','evt-9','2026-11-01T00:00:00+00:00')")
    conn.commit(); conn.close()
    store.disable_source('calendar')
    store.enable_source('calendar', enabled_by='owner', account_key='d' * 64)
    conn = store._connect()
    rows = conn.execute("SELECT * FROM source_seen WHERE source_type='calendar'").fetchall()
    conn.close()
    assert rows == []
    bound = json.loads(store.get_plugin_meta('calendar_account_binding'))
    assert bound['account'] == 'd' * 64 and bound['generation'] == store.source_generation('calendar')

# 4. Cancelling a booking completes its EXISTING linked cancel item (not only orphans).
def test_probe_cancel_completes_existing_linked_item(synthetic_settings):
    item = store.add_item(title='trip', due_at=(NOW + timedelta(days=20)).isoformat(timespec='seconds'))
    booking = store.add_booking(item['id'], name='flight', cancel_by=(NOW + timedelta(days=10)).isoformat(timespec='seconds'))
    linked_id = booking['cancel_item_id']
    store.update_booking(item['id'], position=booking['position'], state='cancelled')
    assert store.get_item(linked_id)['status'] == 'done'

# 5. Cancelling with a NEW cancel_by in the same call creates no window item at all.
def test_probe_cancel_with_new_window_creates_nothing(synthetic_settings):
    item = store.add_item(title='trip2', due_at=(NOW + timedelta(days=20)).isoformat(timespec='seconds'))
    booking = store.add_booking(item['id'], name='hotel')
    updated = store.update_booking(item['id'], position=booking['position'], state='cancelled',
                                   cancel_by=(NOW + timedelta(days=5)).isoformat(timespec='seconds'))
    assert not updated.get('cancel_item_id')
    assert [i['id'] for i in store.list_items()] == [item['id']]

# 6. Hard deadline cross-guard: clearing due_at on a hard item refused both directions.
def test_probe_hard_deadline_requires_due_at(synthetic_settings):
    item = store.add_item(title='hard', due_at=(NOW + timedelta(days=2)).isoformat(timespec='seconds'), hard_deadline=True)
    with pytest.raises(ValueError, match='requires a due_at'):
        store.update_item(item['id'], due_at='')
    soft = store.add_item(title='soft', due_at=(NOW + timedelta(days=2)).isoformat(timespec='seconds'))
    with pytest.raises(ValueError, match='requires a due_at'):
        store.update_item(soft['id'], hard_deadline=True, due_at='')
    store.update_item(soft['id'], due_at='')  # soft clearing still allowed
    assert store.get_item(soft['id'])['due_at'] is None

# 7. Document validity: non-UTC offsets normalized; expiry compares instants.
def test_probe_document_valid_until_normalized(synthetic_settings):
    item = store.add_item(title='paperwork')
    doc = store.add_document(item['id'], name='form', valid_until='2026-11-20T09:00:00+01:00')
    assert doc['valid_until'] == '2026-11-20T08:00:00+00:00'
    store.update_document(item['id'], position=doc['position'], valid_until='2026-11-21T09:00:00+01:00')
    got = [d for d in store.get_item(item['id'])['documents'] if d['position'] == doc['position']][0]
    assert got['valid_until'] == '2026-11-21T08:00:00+00:00'
    expired = store.expire_documents(now='2026-11-21T08:30:00+00:00')
    assert any(d['item_id'] == item['id'] for d in expired)

# 8. Approval dedupe: same user id on different platform/chat is NOT a duplicate.
def test_probe_approval_dedupe_full_principal(synthetic_settings):
    r1 = store.request_approval('send', 'target', {'a': 1}, ttl_hours=1, approver=('telegram', '12345', 'chat1'))
    r2 = store.request_approval('send', 'target', {'a': 1}, ttl_hours=1, approver=('whatsapp', '12345', 'chat1'))
    r3 = store.request_approval('send', 'target', {'a': 1}, ttl_hours=1, approver=('telegram', '12345', 'chat2'))
    r4 = store.request_approval('send', 'target', {'a': 1}, ttl_hours=1, approver=('telegram', '12345', 'chat1'))
    ids = {r1['id'] if isinstance(r1, dict) else r1, r2['id'] if isinstance(r2, dict) else r2,
           r3['id'] if isinstance(r3, dict) else r3, r4['id'] if isinstance(r4, dict) else r4}
    assert len(ids) == 3  # r4 dedupes onto r1; r2/r3 are distinct principals
