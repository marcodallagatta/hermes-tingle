# Independent reviewer stress probes (synthetic). Synthetic only.
import threading
import pytest
import hermes_tingle_store as store
import hermes_tingle_lookback as lb


def imported(title='Synthetic imported item', scan='LB-SYNTHETIC'):
    return store.add_item(title=title, summary='Synthetic detail. ' + lb.ONBOARDING_NOTE,
                          source_type='email', source_ref='gmail:synthetic/' + title,
                          onboarding_scan=scan)


# 1. Mid-loop crash rolls back ALL deletions and leaves the marker untouched.
def test_probe_mid_loop_crash_rolls_back_everything(monkeypatch):
    a = imported('First'); b = imported('Second')
    marker = store.onboarding_state()
    calls = []
    original = store._onboarding_fingerprint
    def flaky(conn, item_id):
        calls.append(item_id)
        if len(calls) == 2:
            raise RuntimeError('synthetic crash on second fingerprint')
        return original(conn, item_id)
    monkeypatch.setattr(store, '_onboarding_fingerprint', flaky)
    with pytest.raises(RuntimeError):
        store.undo_onboarding(expected_origin=lb.ONBOARDING_NOTE)
    assert store.get_item(a['id']) == a and store.get_item(b['id']) == b
    assert store.onboarding_state() == marker
    monkeypatch.setattr(store, '_onboarding_fingerprint', original)
    result = store.undo_onboarding(expected_origin=lb.ONBOARDING_NOTE)
    assert sorted(result['removed']) == sorted([a['id'], b['id']])


# 2. Two concurrent undos: exactly one runs the deletion; no double delete, no error.
def test_probe_concurrent_undo_serialized():
    imported('One'); imported('Two')
    outcomes, errors = [], []
    def run():
        try:
            outcomes.append(store.undo_onboarding(expected_origin=lb.ONBOARDING_NOTE))
        except Exception as exc:  # serialization must not surface as a failure here
            errors.append(repr(exc))
    t1, t2 = threading.Thread(target=run), threading.Thread(target=run)
    t1.start(); t2.start(); t1.join(20); t2.join(20)
    assert not t1.is_alive() and not t2.is_alive()
    assert not errors
    ran = [o for o in outcomes if o is not None]
    assert len(ran) == 1 and len(ran[0]['removed']) == 2
    assert store.list_items() == []


# 3. Second undo is a clean no-op.
def test_probe_second_undo_noop():
    imported('Only')
    first = store.undo_onboarding(expected_origin=lb.ONBOARDING_NOTE)
    assert first['removed']
    assert store.undo_onboarding(expected_origin=lb.ONBOARDING_NOTE) is None
    state = store.onboarding_state()
    assert state['state'] == 'undone'


# 4. Journal recovery: an item committed but dropped from the marker is still undoable
#    (union semantics), and a later edited one is preserved.
def test_probe_journal_recovers_marker_dropped_item():
    a = imported('Committed'); b = imported('Edited later')
    store.set_onboarding_state(dict(state='completed', items=[], scan_id='LB-SYNTHETIC'))
    store.update_item(b['id'], title='Owner renamed')
    result = store.undo_onboarding(expected_origin=lb.ONBOARDING_NOTE)
    assert result['removed'] == [a['id']] and result['preserved'] == [b['id']]
    assert store.get_item(a['id']) is None and store.get_item(b['id']) is not None


# 5. Legacy delete_items entry point now also fingerprint-gated.
def test_probe_legacy_delete_items_conservative():
    a = imported('Untouched'); b = imported('Edited')
    store.update_item(b['id'], title='Owner renamed')
    removed = store.delete_items([a['id'], b['id']], expected_origin=lb.ONBOARDING_NOTE)
    assert removed == [a['id']]
    assert store.get_item(b['id']) is not None


# 6. Fingerprint covers inbound step links: linking another goal to the item preserves it.
def test_probe_inbound_step_link_preserves():
    from datetime import datetime, timedelta, timezone
    key = 'a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf'
    target = store.add_item(title='Target', summary='Synthetic detail. ' + lb.ONBOARDING_NOTE,
                            source_type='email', source_ref='gmail:' + key + '/thread#anchor',
                            completion_signal='source_email_reply_received', status='waiting', owner='other',
                            next_check_at=(datetime.now(timezone.utc) + timedelta(days=2)).isoformat(timespec='seconds'),
                            onboarding_scan='LB-SYNTHETIC')
    goal = store.add_item(title='Separate goal')
    store.add_step(goal['id'], title='Wait on target', why='w', wait_item_id=target['id'])
    result = store.undo_onboarding(expected_origin=lb.ONBOARDING_NOTE)
    assert result['preserved'] == [target['id']]
    assert store.get_item(target['id']) is not None


# 7. Journal state machine: removed rows are never re-processed by a later undo attempt.
def test_probe_removed_journal_rows_not_reprocessed():
    a = imported('Gone')
    store.undo_onboarding(expected_origin=lb.ONBOARDING_NOTE)
    # Owner re-adds an item that happens to carry the note text, then marker is
    # manually completed again (synthetic) - the old journal row must not match.
    reborn = store.add_item(title='Reborn', summary='Owner wrote. ' + lb.ONBOARDING_NOTE)
    store.set_onboarding_state(dict(state='completed', items=[reborn['id']]))
    result = store.undo_onboarding(expected_origin=lb.ONBOARDING_NOTE)
    assert result['removed'] == [] and result['preserved'] == [reborn['id']]
    assert store.get_item(reborn['id']) is not None


# 8. Partial-then-complete marker union keeps every committed ID across scans.
def test_probe_partial_marker_unions_across_scans():
    a = imported('Scan A', scan='LB-A')
    b = imported('Scan B', scan='LB-B')
    store.set_onboarding_state(dict(state='completed', items=[b['id']], scan_id='LB-B'))
    items = set(store.onboarding_state()['items'])
    assert items == {a['id'], b['id']}
