import json
from datetime import datetime,timedelta,timezone
import hermes_tingle_store as s
import hermes_tingle_sources as sources
import hermes_tingle as p
NOW=datetime(2026,11,16,12,tzinfo=timezone.utc)

def test_migration_content_edit_does_not_erase_legacy_attempt(monkeypatch):
    monkeypatch.setattr(s,'_now',lambda value=None: NOW if value is None else datetime.fromisoformat(str(value)))
    item=s.add_item(title='Synthetic migration edit',next_check_at=NOW-timedelta(seconds=1))
    c=s._connect()
    c.execute('UPDATE hermes_tingle_items SET reminder_count=3,reminder_attempt_count=3 WHERE id=?',(item['id'],))
    c.execute('INSERT INTO alert_emissions(item_ids,item_versions,receipt_token,emitted_at) VALUES(?,?,?,?)',(json.dumps([item['id']]),json.dumps({item['id']:item['reminder_revision']}),'a'*24,NOW.isoformat()))
    c.commit();c.close()
    # First post-upgrade action is a harmless title edit, before any due poll.
    s.update_item(item['id'],title='Synthetic renamed reminder')
    s.resume_automatic_reminders(now=NOW)
    assert s.get_item(item['id'])['reminder_attempt_count']==4

def test_create_reply_wait_preserves_nine_hour_repeat(monkeypatch):
    monkeypatch.setattr(s,'_now',lambda value=None: NOW if value is None else datetime.fromisoformat(str(value)))
    item=s.add_item(title='Synthetic nine hour reply',source_type='email',source_ref='gmail:a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf/synthetic#sent',status='waiting',owner='other',completion_signal='source_email_reply_received',next_check_at=NOW+timedelta(hours=9))
    assert item['reply_interval_seconds']==9*3600

def test_migration_edit_actual_tool_admits_fifth_attempt(monkeypatch):
    monkeypatch.setattr(s,'_now',lambda value=None: NOW if value is None else datetime.fromisoformat(str(value)))
    item=s.add_item(title='Synthetic upgraded fourth',next_check_at=NOW-timedelta(seconds=1))
    c=s._connect()
    c.execute('UPDATE hermes_tingle_items SET reminder_count=3,reminder_attempt_count=3 WHERE id=?',(item['id'],))
    c.execute('INSERT INTO alert_emissions(item_ids,item_versions,receipt_token,emitted_at) VALUES(?,?,?,?)',(json.dumps([item['id']]),json.dumps({item['id']:item['reminder_revision']}),'b'*24,NOW.isoformat()))
    c.commit();c.close()
    assert json.loads(p.handle_tool(None,{'action':'update','item_id':item['id'],'title':'Synthetic harmless rename'}))['success']
    result=json.loads(p.handle_tool(None,{'action':'mark_notified','item_ids':[item['id']], 'expected_versions':{i:s.get_item(i)['reminder_revision'] for i in [item['id']]}}))
    assert not result['success'], result

def test_creation_context_actual_tool_repeat_after_nine_hours(monkeypatch):
    clock=[NOW]
    old=s._now
    monkeypatch.setattr(s,'_now',lambda value=None: old(value) if value is not None else clock[0])
    create=json.loads(p.handle_tool(None,{'action':'add','title':'Synthetic context reply','source_type':'email','source_ref':'gmail:a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf/synthetic#sent','status':'waiting','owner':'other','completion_signal':'source_email_reply_received','next_check_at':(NOW+timedelta(hours=9)).isoformat()}))
    assert create['success'],create
    item=create['item']
    clock[0]+=timedelta(hours=9)
    calls=[]
    def provider(*args,**kwargs):
        params=json.loads(args[-1]);calls.append(params)
        return {'id':'synthetic','messages':[{'id':'sent','labelIds':['SENT'],'internalDate':str(int(NOW.timestamp()*1000))}]}
    monkeypatch.setattr(sources,'_gws_bridge',provider)
    result=json.loads(p.handle_tool(None,{'action':'mark_notified','item_ids':[item['id']], 'expected_versions':{i:s.get_item(i)['reminder_revision'] for i in [item['id']]}}))
    assert result['success'],result
    assert len(calls)==1 and calls[0]['format']=='metadata'
    assert datetime.fromisoformat(s.get_item(item['id'])['next_check_at'])==clock[0]+timedelta(hours=9)

def test_preupgrade_prior_content_edit_same_cycle_uncertain_fourth(monkeypatch):
    # The content edit can already exist in the old database, not only happen
    # after upgrade. Converting at start of new update_item is insufficient.
    monkeypatch.setattr(s,'_now',lambda value=None: NOW if value is None else datetime.fromisoformat(str(value)))
    item=s.add_item(title='Synthetic prior edited history',next_check_at=NOW-timedelta(seconds=1))
    c=s._connect()
    c.execute('UPDATE hermes_tingle_items SET reminder_count=3,reminder_attempt_count=3,reminder_revision=1 WHERE id=?',(item['id'],))
    c.execute('INSERT INTO alert_emissions(item_ids,item_versions,receipt_token,emitted_at) VALUES(?,?,?,?)',(json.dumps([item['id']]),json.dumps({item['id']:0}),'c'*24,NOW.isoformat()))
    c.commit();c.close()
    s.resume_automatic_reminders(now=NOW)
    result=json.loads(p.handle_tool(None,{'action':'mark_notified','item_ids':[item['id']], 'expected_versions':{i:s.get_item(i)['reminder_revision'] for i in [item['id']]}}))
    assert not result['success'], result
