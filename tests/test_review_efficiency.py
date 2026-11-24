"""Regression reversals of the synthetic fifth-review reproducers.

Run with a disposable copy of tests/conftest.py and tests/runtime_stubs.py,
PYTHONPATH=<checkout>:<checkout>/scripts; pytest --basetemp=<scratch>.
All sources and databases are fakes. No network or messages.
"""
import pytest
import json
from datetime import datetime, timedelta, timezone
import hermes_tingle as plugin
import hermes_tingle_store as store
import hermes_tingle_sources as sources
import source_snapshot


def isolate_sources():
    store.disable_source('email')
    store.disable_source('calendar')


@pytest.mark.skip(reason="pinned Drive-provider suppression via the removed document source; chat-equivalent receipt suppression is covered in test_efficiency.py")
def test_reviewed_drive_page_hides_bound_at_actual_helper(monkeypatch, capsys):
    isolate_sources()
    store.enable_source('document', lookback_days=7, enabled_by='synthetic')
    now = datetime.now(timezone.utc)
    store.add_item(title='Synthetic gym contract', next_check_at=now+timedelta(days=2))
    monkeypatch.setattr(sources, '_gws_bridge', lambda *a: {'files':[
        {'id':'synthetic-file','name':'gym-contract','modifiedTime':now.isoformat()}],
        'nextPageToken':'synthetic-next-page'})
    original = sources.collect_snapshot
    monkeypatch.setattr(sources, 'collect_snapshot', lambda: original(now=now))
    first = sources.collect_snapshot()
    assert first['document_review']['review_complete'] is False
    receipt = first['document_review']['matches'][0]['review_receipt']
    assert json.loads(plugin.handle_tool(None, {'action':'mark_context_reviewed','review_receipt':receipt}))['success']
    second = sources.collect_snapshot()
    assert second['document_review']['incomplete_reasons'] == ['bounded']
    assert not second['document_review']['matches'] and not second['errors']
    assert source_snapshot.main() == 0
    output = capsys.readouterr()
    print('bounded metadata:', second['document_review']['incomplete_reasons'])
    print('actual helper stdout:', output.out.strip())
    assert output.out.strip() == '{"wakeAgent": false}'


def test_consented_chat_reader_failure_hides_incomplete_coverage(monkeypatch, capsys):
    isolate_sources()
    store.enable_source('chat', lookback_days=7, enabled_by='synthetic')
    store.grant_chat_consent('telegram:42', lookback_days=7, granted_by='synthetic')
    store.add_item(title='Synthetic gym contract', next_check_at=datetime.now(timezone.utc)+timedelta(days=2))
    def fail(*args):
        raise RuntimeError('synthetic failure')
    sources.set_chat_history_interfaces(fail, lambda *a, limit=None: [])
    try:
        snapshot = sources.collect_snapshot()
        assert snapshot['chat_review']['incomplete_reasons'] == ['read_error']
        assert any('coverage is incomplete' in e for e in snapshot['errors'])
        assert source_snapshot.main() == 0
        output = capsys.readouterr()
        print('chat metadata:', snapshot['chat_review']['incomplete_reasons'])
        print('actual helper stdout:', output.out.strip())
        assert output.out.strip() == '{"wakeAgent": false}'
    finally:
        sources.set_chat_history_interfaces(None, None)


def test_agent_stale_due_snapshot_charges_edited_revision():
    isolate_sources()
    now=datetime.now(timezone.utc)
    item=store.add_item(title='Synthetic original action',next_check_at=now-timedelta(seconds=1))
    snapshot=sources.collect_snapshot(now=now)
    stale=next(x for x in snapshot['due_items'] if x['id']==item['id'])
    changed=store.update_item(item['id'], title='Synthetic revised action')
    result=json.loads(plugin.handle_tool(None, {'action':'mark_notified','item_ids':[item['id']], 'expected_versions':{item['id']:stale['reminder_revision']}}))
    current=store.get_item(item['id'])
    print('snapshot:', stale['title'], 'revision',stale['reminder_revision'])
    print('owner edit:', changed['title'], 'revision',changed['reminder_revision'])
    print('actual agent reservation:',result['success'],'attempts',current['reminder_attempt_count'])
    assert stale['reminder_revision'] != changed['reminder_revision']
    assert not result['success'] and current['reminder_attempt_count']==0


def test_reply_wait_changed_during_reverification_is_rejected(monkeypatch):
    now=datetime.now(timezone.utc)
    item=store.add_item(title='Synthetic wait',source_type='email',source_ref='gmail:fake-thread#sent',
                        status='waiting',owner='other',completion_signal='source_email_reply_received',
                        next_check_at=now-timedelta(seconds=1))
    def fake_check(items):
        store.update_item(item['id'], title='Synthetic owner edit during verification')
        return ([{'item_id':item['id'],'state':'no_reply'}],[])
    monkeypatch.setattr(sources, '_read_email_reply_checks', fake_check)
    result=json.loads(plugin.handle_tool(None, {'action':'mark_notified','item_ids':[item['id']], 'expected_versions':{item['id']:item['reminder_revision']}}))
    print('reverify interleaving success:', result['success'], 'attempts:',store.get_item(item['id'])['reminder_attempt_count'])
    assert not result['success'] and store.get_item(item['id'])['reminder_attempt_count']==0


def test_helper_serializes_owner_edit_through_flush(monkeypatch):
    import due_alerts, io, threading
    now=datetime.now(timezone.utc)
    item=store.add_item(title='Synthetic original',next_check_at=now-timedelta(seconds=1))
    entered=threading.Event(); release=threading.Event(); edit_done=threading.Event(); errors=[]
    class ControlledOutput(io.StringIO):
        def flush(self):
            entered.set()
            if not release.wait(5): raise RuntimeError('timed out waiting for test')
    output=ControlledOutput()
    def run_helper():
        try: assert due_alerts.main()==0
        except BaseException as exc: errors.append(exc)
    def edit():
        try: store.update_item(item['id'],title='Synthetic edited'); edit_done.set()
        except BaseException as exc: errors.append(exc)
    with monkeypatch.context() as patch:
        patch.setattr(due_alerts.sys,'stdout',output)
        helper=threading.Thread(target=run_helper);helper.start()
        assert entered.wait(5)
        owner=threading.Thread(target=edit);owner.start()
        try: assert not edit_done.wait(0.3)
        finally:
            release.set();helper.join(5);owner.join(5)
    assert not helper.is_alive() and not owner.is_alive() and not errors
    assert edit_done.is_set() and store.get_item(item['id'])['title']=='Synthetic edited'
    assert 'Synthetic original' in output.getvalue()
    print('helper flush precedes owner edit; both threads completed')
