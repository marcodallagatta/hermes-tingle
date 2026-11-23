"""Actual helper/owner boundaries for cycle budgets and historical uncertainty."""
from datetime import datetime, timedelta, timezone
import io
import json
import re
import pytest
import due_alerts
import hermes_tingle as plugin
import hermes_tingle_store as store

NOW = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)

@pytest.fixture
def clock(monkeypatch):
    value = [NOW]
    orig = store._now
    monkeypatch.setattr(store, '_now', lambda x=None: orig(x) if x is not None else value[0])
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return value[0] if tz else value[0].replace(tzinfo=None)
    monkeypatch.setattr(due_alerts, 'datetime', Clock)
    return value

@pytest.fixture
def owner(tmp_path):
    from test_hermes_tingle_tool import _FakeSource
    p=tmp_path/'hermes-tingle.json';cfg=json.loads(p.read_text());cfg['owner_principals']=['telegram:424242'];p.write_text(json.dumps(cfg))
    return _FakeSource()

def item(**kw):
    return store.add_item(title='Synthetic tracked item',next_check_at=NOW,now=NOW,**kw)

def emit(row, now=NOW):
    return store.mark_notified([row['id']],now=now)[0]

@pytest.mark.parametrize('before', [1,2,4])
@pytest.mark.parametrize('exact', [False,True])
def test_deadline_keeps_single_budget_helper(before,exact,clock,capsys):
    row=item(due_at=NOW+timedelta(hours=1),hard_deadline=True)
    for n in range(before):
        # Force due within the same cycle, without an explicit schedule update.
        c=store._connect();c.execute('UPDATE hermes_tingle_items SET next_check_at=? WHERE id=?',(NOW.isoformat(),row['id']));c.commit();c.close()
        emit(row)
    if exact:
        clock[0]=NOW+timedelta(hours=1);due_alerts.main();capsys.readouterr()
    clock[0]=NOW+timedelta(hours=1,seconds=1)
    for _ in range(5):
        assert due_alerts.main()==0;capsys.readouterr()
        current=store.get_item(row['id'])
        if current['next_check_at']:clock[0]=datetime.fromisoformat(current['next_check_at'])
    c=store._connect();assert c.execute('SELECT count(*) FROM alert_emissions').fetchone()[0]==4;c.close()
    assert store.get_item(row['id'])['status']=='missed'
    assert store.get_item(row['id'])['reminder_attempt_count']==4
    assert store.get_item(row['id'])['next_check_at'] is None
    assert any(x['id']==row['id'] for x in store.list_items(status='missed'))

@pytest.mark.parametrize('count',[1,4])
def test_recurring_transfer_budget_receipt_identity(count,clock):
    row=item(due_at=NOW+timedelta(hours=1),hard_deadline=True,recur_interval=1,recur_unit='day')
    receipts=[]
    for _ in range(count):
        c=store._connect();c.execute('UPDATE hermes_tingle_items SET next_check_at=? WHERE id=?',(NOW.isoformat(),row['id']));c.commit();c.close();receipts.append(emit(row))
    old=store.get_item(row['id'])
    outcomes=store.sweep_missed_deadlines(now=NOW+timedelta(days=3,hours=2))
    missed=store.get_item(outcomes[0]['occurrence_id']);anchor=store.get_item(row['id'])
    assert missed['reminder_attempt_count']==count and missed['reminder_cycle']==old['reminder_cycle']
    assert anchor['reminder_attempt_count']==0 and anchor['reminder_cycle']!=old['reminder_cycle']
    assert all(store.get_item(x['occurrence_id'])['reminder_attempt_count']==0 for x in outcomes[1:])
    anchor_check=anchor['next_check_at']
    assert store.acknowledge_alert_emissions(receipts[0]['receipt_command'].split()[-1])==1
    assert store.get_item(missed['id'])['reminder_count']==1
    assert store.get_item(row['id'])['reminder_count']==0 and store.get_item(row['id'])['next_check_at']==anchor_check
    assert not store.release_alert_emission(receipts[0]['delivery_batch_id'])

@pytest.mark.parametrize('recurring',[False,True])
def test_expiry_keeps_legacy_guard(recurring,clock):
    row=item(due_at=NOW+timedelta(hours=1),hard_deadline=True,**({'recur_interval':1,'recur_unit':'day'} if recurring else {}))
    c=store._connect();c.execute('UPDATE hermes_tingle_items SET reminder_budget_uncertain=1,reminder_attempt_count=3 WHERE id=?',(row['id'],));c.commit();c.close()
    outcome=store.sweep_missed_deadlines(now=NOW+timedelta(hours=2))[0]
    target=store.get_item(outcome['occurrence_id'] if recurring else row['id'])
    assert target['reminder_budget_uncertain']==1 and target['reminder_attempt_count']==3
    assert target['id'] not in [x['id'] for x in store.due_items(now=NOW+timedelta(hours=3))]

@pytest.mark.parametrize('change',['rename','step','snooze','schedule','done','dismiss'])
@pytest.mark.parametrize('flushed',[False,True])
def test_history_and_exact_receipt_survive_changes(change,flushed,owner,clock):
    row=item();batch=emit(row);bid=batch['delivery_batch_id'];token=batch['receipt_command'].split()[-1]
    if flushed:store.mark_emission_flushed(bid)
    if change=='rename':store.update_item(row['id'],title='Synthetic renamed')
    elif change=='step':store.add_step(row['id'],title='Check synthetic step',why='Synthetic reason')
    elif change=='snooze':store.snooze_item(row['id'],until=NOW+timedelta(days=2),now=NOW)
    elif change=='schedule':store.update_item(row['id'],next_check_at=NOW+timedelta(days=3))
    elif change=='done':store.complete_item(row['id'],now=NOW)
    else:store.update_item(row['id'],status='dismissed')
    after=store.get_item(row['id']);view=plugin.handle_command(None,'delivery',source=owner)
    assert f'B{bid}:' in view and token not in view and '/tingle ack ' not in view
    plugin.handle_command(None,f'retry B{bid}',source=owner)
    assert f'B{bid}:' in plugin.handle_command(None,'delivery',source=owner)
    assert 'Acknowledged' in plugin.handle_command(None,'ack '+token,source=owner)
    assert store.get_alert_emission(bid)['acked_at']
    current=store.get_item(row['id'])
    assert current['reminder_count']==after['reminder_count']
    assert current['reminder_attempt_count']==after['reminder_attempt_count']
    assert current['next_check_at']==after['next_check_at']
    assert 'Acknowledged' not in plugin.handle_command(None,'ack '+token,source=owner)

@pytest.mark.parametrize('budget',[1,3])
def test_preexisting_released_history_remains_visible(budget,owner,clock):
    row=item();batch=emit(row);bid=batch['delivery_batch_id'];token=batch['receipt_command'].split()[-1]
    store.update_item(row['id'],title='Synthetic renamed')
    c=store._connect();c.execute('UPDATE alert_emissions SET released_at=?,budget_reserved=?,item_cycles=NULL WHERE id=?',(NOW.isoformat(),budget,bid));c.commit();c.close()
    assert f'B{bid}:' in plugin.handle_command(None,'delivery',source=owner)
    assert store.acknowledge_alert_emissions(token)==1
    assert store.get_item(row['id'])['reminder_count']==0

def test_all_failed_attempts_stay_inspectable_after_edit(owner,clock,monkeypatch,capsys):
    class Broken(io.StringIO):
        def flush(self):raise OSError('synthetic')
    row=item()
    for _ in range(4):
        with monkeypatch.context() as m:
            m.setattr(due_alerts.sys,'stdout',Broken());assert due_alerts.main()==1
        current=store.get_item(row['id'])
        if current['next_check_at']:clock[0]=datetime.fromisoformat(current['next_check_at'])
    store.update_item(row['id'],title='Synthetic renamed')
    assert len(store.pending_alert_emissions())==4
    assert 'B4:' in plugin.handle_command(None,'delivery',source=owner)
    clock[0]+=timedelta(days=30);assert due_alerts.main()==0
    assert capsys.readouterr().out==""
    assert 'B4:' in plugin.handle_command(None,'delivery',source=owner)
    assert store.get_item(row['id'])['reminder_attempt_count']==4
    assert len(store.pending_alert_emissions())==4

def test_partial_batch_and_historical_pagination(owner,clock):
    rows=[store.add_item(title=f'Synthetic unique {i}',next_check_at=NOW,now=NOW) for i in range(32)]
    result=store.mark_notified([rows[0]['id'],rows[1]['id']],now=NOW)
    token=result[0]['receipt_command'].split()[-1]
    store.update_item(rows[0]['id'],title='Synthetic rename')
    for row in rows[2:]:emit(row);store.update_item(row['id'],title='Synthetic historical')
    one=plugin.handle_command(None,'delivery',source=owner);two=plugin.handle_command(None,'delivery B30',source=owner)
    assert 'B30:' in one and 'B31:' in two and token not in one+two
    assert store.acknowledge_alert_emissions(token)==1
    assert store.get_item(rows[0]['id'])['reminder_count']==0
    assert store.get_item(rows[1]['id'])['reminder_count']==1

def test_legacy_provenance_backfill_before_recurring_transfer(clock):
    row=item(due_at=NOW+timedelta(hours=1),hard_deadline=True,recur_interval=1,recur_unit='day')
    batch=emit(row);bid=batch['delivery_batch_id']
    # Exact pre-fourth schema state: no cycle provenance on recorded batch.
    c=store._connect();c.execute('UPDATE alert_emissions SET item_cycles=NULL WHERE id=?',(bid,));c.execute("DELETE FROM plugin_meta WHERE key='reminder_cycle_provenance_v1'");c.commit();c.close()
    outcome=store.sweep_missed_deadlines(now=NOW+timedelta(hours=2))[0]
    assert store.acknowledge_alert_emissions(batch['receipt_command'].split()[-1])==1
    assert store.get_item(outcome['occurrence_id'])['reminder_count']==1
    assert store.get_item(row['id'])['reminder_count']==0

def test_unknown_legacy_provenance_never_guesses_new_occurrence(clock):
    row=item();batch=emit(row);bid=batch['delivery_batch_id'];store.snooze_item(row['id'],until=NOW+timedelta(days=2),now=NOW)
    c=store._connect();c.execute('UPDATE alert_emissions SET item_cycles=NULL WHERE id=?',(bid,));c.execute("DELETE FROM plugin_meta WHERE key='reminder_cycle_provenance_v1'");c.commit();c.close()
    assert store.acknowledge_alert_emissions(batch['receipt_command'].split()[-1])==1
    assert store.get_item(row['id'])['reminder_count']==0

def test_deadline_receipt_never_resets_cadence(clock):
    row=item(due_at=NOW+timedelta(hours=1),hard_deadline=True);batch=emit(row)
    old=store.get_item(row['id']);store.sweep_missed_deadlines(now=NOW+timedelta(hours=2));current=store.get_item(row['id'])
    assert current['reminder_cycle']==old['reminder_cycle'] and current['reminder_attempt_count']==1
    assert store.acknowledge_alert_emissions(batch['receipt_command'].split()[-1])==1
    assert store.get_item(row['id'])['next_check_at']==current['next_check_at']
    assert store.get_item(row['id'])['reminder_attempt_count']==1

def test_new_recurring_occurrence_rearms_but_old_retry_cannot(clock):
    row=item(due_at=NOW+timedelta(hours=1),hard_deadline=True,recur_interval=1,recur_unit='day');batch=emit(row)
    store.sweep_missed_deadlines(now=NOW+timedelta(hours=2));anchor=store.get_item(row['id']);clock[0]=datetime.fromisoformat(anchor['next_check_at'])
    assert store.release_alert_emission(batch['delivery_batch_id'],now=clock[0])
    assert store.get_item(row['id'])['next_check_at']==anchor['next_check_at']
    emit(anchor,now=clock[0]);assert store.get_item(row['id'])['reminder_attempt_count']==1

def test_confirmed_legacy_guard_stays_discoverable_after_occurrence_transfer(owner,clock):
    row=item(due_at=NOW+timedelta(hours=1),hard_deadline=True,recur_interval=1,recur_unit='day')
    c=store._connect();c.execute('INSERT INTO alert_emissions(item_ids,emitted_at,acked_at) VALUES(?,?,?)',(json.dumps([row['id']]),NOW.isoformat(),NOW.isoformat()));c.commit();c.close()
    assert store.get_item(row['id'])['reminder_budget_uncertain']==1
    result=store.sweep_missed_deadlines(now=NOW+timedelta(hours=2))[0]
    assert store.get_item(result['occurrence_id'])['reminder_budget_uncertain']==1
    assert 'legacy cycle budget unknown' in plugin.handle_command(None,'delivery',source=owner)
    store.snooze_item(result['occurrence_id'],until=NOW+timedelta(days=3),now=NOW)
    assert not store.pending_alert_emissions()


def test_legacy_alias_cycle_map_renumbered():
 item=store.add_item(title='Synthetic alias cycle',next_check_at=NOW,now=NOW)
 c=store._connect();c.execute('UPDATE hermes_tingle_items SET id=? WHERE id=?',('OL-ABC123',item['id']))
 c.execute('INSERT INTO alert_emissions(item_ids,item_versions,receipt_token,emitted_at,budget_reserved) VALUES(?,?,?,?,1)',(json.dumps(['OL-ABC123']),json.dumps({'OL-ABC123':0}),'e'*24,NOW.isoformat()));c.execute("DELETE FROM plugin_meta WHERE key='reminder_cycle_provenance_v1'");c.commit();c.close()
 current=store.get_item('OL-ABC123')
 batch=store.get_alert_emission(1)
 assert set(json.loads(batch['item_cycles']))=={current['id']},batch

def test_alias_oldreceipt_follows_first_missed_occurrence():
 item=store.add_item(title='Synthetic alias recurring receipt',next_check_at=NOW,due_at=NOW+timedelta(hours=1),hard_deadline=True,recur_interval=1,recur_unit='day',now=NOW)
 c=store._connect();c.execute('UPDATE hermes_tingle_items SET id=?,reminder_attempt_count=1 WHERE id=?',('OL-ABC123',item['id']))
 c.execute('INSERT INTO alert_emissions(item_ids,item_versions,receipt_token,emitted_at,budget_reserved) VALUES(?,?,?,?,1)',(json.dumps(['OL-ABC123']),json.dumps({'OL-ABC123':0}),'f'*24,NOW.isoformat()));c.execute("DELETE FROM plugin_meta WHERE key='reminder_cycle_provenance_v1'");c.commit();c.close()
 current=store.get_item('OL-ABC123')
 out=store.sweep_missed_deadlines(now=NOW+timedelta(hours=2))[0]
 assert store.acknowledge_alert_emissions('f'*24,now=NOW+timedelta(hours=3))==1
 assert store.get_item(out['occurrence_id'])['reminder_count']==1
 assert store.get_item(current['id'])['reminder_count']==0


@pytest.mark.parametrize('retry_first',[False,True])
def test_ordered_retry_and_historical_ack_confirm_once(retry_first,clock):
    row=item();batch=emit(row);bid=batch['delivery_batch_id'];token=batch['receipt_command'].split()[-1]
    if retry_first:assert store.release_alert_emission(bid,now=NOW)
    assert store.acknowledge_alert_emissions(token,now=NOW)==1
    assert not store.release_alert_emission(bid,now=NOW)
    assert store.acknowledge_alert_emissions(token,now=NOW)==0
    assert store.get_item(row['id'])['reminder_count']==1
    assert store.get_item(row['id'])['reminder_attempt_count']==1

def test_fully_confirmed_exhausted_missed_item_in_owner_list(clock):
    row=item(due_at=NOW+timedelta(hours=1),hard_deadline=True)
    for _ in range(4):
        c=store._connect();c.execute('UPDATE hermes_tingle_items SET next_check_at=? WHERE id=?',(NOW.isoformat(),row['id']));c.commit();c.close()
        batch=emit(row);assert store.acknowledge_alert_emissions(batch['receipt_command'].split()[-1],now=NOW)==1
    store.sweep_missed_deadlines(now=NOW+timedelta(hours=2))
    assert not store.pending_alert_emissions()
    view=plugin.handle_command(None,'list')
    assert row['id'] in view and row['title'] in view and 'Missed window' in view
    assert not store.due_items(now=NOW+timedelta(days=30))

def test_owner_list_missed_items_beyond_page_bound(clock):
    for i in range(25):
        row=store.add_item(title=f'Synthetic missed {i:02}',next_check_at=NOW,due_at=NOW-timedelta(days=1),hard_deadline=True,now=NOW)
    store.sweep_missed_deadlines(now=NOW)
    one=plugin.handle_command(None,'list');two=plugin.handle_command(None,'list 2')
    first=set(re.findall(r'HT-[A-F0-9]{6}',one));second=set(re.findall(r'HT-[A-F0-9]{6}',two))
    assert len(first)==20 and len(second)==5 and not first&second
    assert '/tingle list 2' in one
