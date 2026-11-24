"""Script-first gates, success receipts and honest measurement. Synthetic only."""
import json
from datetime import datetime,timedelta,timezone

import pytest
import hermes_tingle as plugin
import hermes_tingle_store as store
import hermes_tingle_sources as sources
import hermes_tingle_review as review
import source_snapshot
from runtime_support import hermes_import

NOW=datetime(2026,11,16,12,tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _pinned_review_clock(monkeypatch):
    """Receipts are issued at the pinned NOW; acknowledge must check them against
    the same clock, otherwise these tests start failing a day after they were
    written. Explicit `now=` overrides still pass through."""
    real_now = store._now
    monkeypatch.setattr(store, '_now', lambda value=None: real_now(value) if value is not None else NOW)


@pytest.fixture
def lead(monkeypatch):
    item=store.add_item(title='Renew gym contract',next_check_at='2027-01-01T10:00:00Z')
    store.enable_source('chat',lookback_days=7,enabled_by='fixture')
    store.note_known_chat('telegram:42')
    sources.set_chat_history_interfaces(
        lambda *a, **kwargs:['s1'],
        lambda *a, limit=None:[{'role':'user','content':'gym-contract','timestamp':NOW.timestamp()}])
    yield item
    sources.set_chat_history_interfaces(None,None)


def collect():return sources._collect_chat_matches(now=NOW)


def ack(receipt):return json.loads(plugin.handle_tool(None,{'action':'mark_context_reviewed','review_receipt':receipt}))


def test_emission_or_lost_agent_response_does_not_mark_reviewed(lead):
    first=collect();second=collect()
    assert first['matches']==second['matches']
    assert ack(first['matches'][0]['review_receipt'])['success']
    later=collect();assert not later['matches'] and later['unchanged_reviewed']==1
    assert source_snapshot.build_agent_input({'chat_review':later})=='{"wakeAgent": false}'


def test_changed_source_content_reappears(lead,monkeypatch):
    ack(collect()['matches'][0]['review_receipt'])
    sources.set_chat_history_interfaces(
        lambda *a, **kwargs:['s1'],
        lambda *a, limit=None:[{'role':'user','content':'gym-contract revised','timestamp':NOW.timestamp()+3600}])
    assert collect()['matches']


@pytest.mark.parametrize('change',['item','policy','regrant','profile'])
def test_old_receipt_rejected_on_binding_change(lead,monkeypatch,tmp_path,change):
    receipt=collect()['matches'][0]['review_receipt']
    if change=='item':store.update_item(lead['id'],summary='Changed scope')
    elif change=='policy':
        path=tmp_path/'hermes-tingle.json';data=json.loads(path.read_text());data['availability_end']='21:00';path.write_text(json.dumps(data))
    elif change=='regrant':
        store.disable_source('chat');store.enable_source('chat',enabled_by='new consent')
    else:
        import hermes_constants
        new=tmp_path/'another';new.mkdir();(new/'hermes-tingle.json').write_bytes((tmp_path/'hermes-tingle.json').read_bytes())
        monkeypatch.setattr(hermes_constants,'get_hermes_home',lambda:new)
    assert not ack(receipt)['success']


def test_item_change_after_success_reappears(lead):
    ack(collect()['matches'][0]['review_receipt'])
    store.update_item(lead['id'],summary='New facts')
    assert collect()['matches']


def test_duplicate_title_tokens_match_all_active_items(lead):
    other=store.add_item(title='Gym contract for another year',summary='different',next_check_at='2027-01-01T10:00:00Z')
    assert set(collect()['matches'][0]['item_ids'])=={lead['id'],other['id']}


def test_receipt_expiry_no_suppression(lead):
    receipt=collect()['matches'][0]['review_receipt']
    with pytest.raises(ValueError,match='expired'):review.acknowledge(receipt,now=NOW+timedelta(days=2))
    assert collect()['matches']


def test_chat_consent_invalidation_and_no_content_in_receipts(monkeypatch):
    store.add_item(title='Gym renewal')
    store.enable_source('chat',lookback_days=7,enabled_by='fixture')
    store.grant_chat_consent('telegram:42',lookback_days=7,granted_by='telegram:42')
    sources.set_chat_history_interfaces(lambda *a, **kwargs:['s1'],lambda *a, limit=None:[{'role':'user','content':'Gym renewal needs review','timestamp':NOW.timestamp()}])
    try:
        first=sources._collect_chat_matches(now=NOW)
        receipt=first['matches'][0]['review_receipt']
        assert ack(receipt)['success']
        assert not sources._collect_chat_matches(now=NOW)['matches']
        store.revoke_chat_consent('telegram:42')
        assert not ack(receipt)['success']
        store.grant_chat_consent('telegram:42',lookback_days=7,granted_by='telegram:42')
        assert sources._collect_chat_matches(now=NOW)['matches']
        conn=store._connect();rows=[dict(r) for r in conn.execute('SELECT * FROM context_review_receipts')];conn.close()
        assert 'Gym renewal needs review' not in json.dumps(rows)
    finally:sources.set_chat_history_interfaces(None,None)


def test_receipts_are_unforgeable_and_source_revoke_purges_markers(lead):
    assert not ack('CR-'+'0'*32)['success']
    receipt=collect()['matches'][0]['review_receipt'];ack(receipt)
    store.disable_source('chat')
    conn=store._connect();assert not conn.execute("SELECT * FROM source_seen WHERE source_type='chat'").fetchall();assert not conn.execute('SELECT * FROM context_review_receipts').fetchall();conn.close()


def test_discovery_support_preserves_legacy_update_receipt_identity(lead):
    match = collect()['matches'][0]
    conn = store._connect()
    try:
        row = dict(conn.execute('SELECT * FROM context_review_receipts WHERE receipt=?',
                                (match['review_receipt'],)).fetchone())
        assert json.loads(row['scope']) == {'item_ids': [lead['id']], 'chat_ref': match['chat_ref']}
        assert row['source_key'] == review.digest({'chat_ref': match['chat_ref'],
            'timestamp': match['timestamp'], 'role': match['role']})
        original = {k: v for k, v in match.items() if k != 'review_receipt'}
        assert row['version'] == review.digest({'lead': original, 'binding': row['binding']})
    finally:
        conn.close()


def test_discovery_only_gate_metrics_do_not_include_private_content_or_wake_agent():
    snapshot = {'chat_review': {'matches': [], 'discovery': {'matches': [
        {'excerpt':'private synthetic excerpt', 'action_label':'Synthetic action'}]}}}
    output = source_snapshot.build_agent_input(snapshot)
    metrics = source_snapshot.gate_metrics(snapshot, output)
    assert not metrics['wake_agent'] and metrics['counts']['chat_discovery'] == 1
    assert metrics['reasons'] == ['chat_discovery']
    assert 'private synthetic excerpt' not in json.dumps(metrics)
    assert 'Synthetic action' not in json.dumps(metrics)


def test_gate_bytes_are_measured_not_tokens_or_claimed_savings():
    snapshot={'emails':[{'snippet':'€'}]}
    text=source_snapshot.build_agent_input(snapshot)
    metrics=source_snapshot.gate_metrics(snapshot,text)
    assert metrics['payload_utf8_bytes']==len(text.encode())
    assert metrics['reasons']==['emails'] and metrics['wake_agent']
    assert not source_snapshot.gate_metrics({},source_snapshot.build_agent_input({}))['wake_agent']


def test_actual_pinned_cron_wake_gate_prevents_prompt_construction(monkeypatch):
    scheduler=hermes_import('cron.scheduler')
    import hermes_cli.config as config
    monkeypatch.setattr(config,'require_parseable_user_config',lambda:None)
    monkeypatch.setattr(scheduler,'_apply_monitor_gate',lambda *a:(None,None,None))
    monkeypatch.setattr(scheduler,'_run_job_script_with_claim_heartbeat',lambda *a,**kw:(True,source_snapshot.build_agent_input({})))
    monkeypatch.setattr(scheduler,'_resolve_job_workdir',lambda *a:None)
    monkeypatch.setattr(scheduler,'note_cron_skipped',lambda *a:None)
    monkeypatch.setattr(scheduler,'_build_job_prompt',lambda *a,**kw:pytest.fail('Model prompt must not be built'))
    job={'id':'synthetic','name':'test','prompt':'review','script':'fake.py'}
    early,prompt=scheduler._prepare_job_prompt(job,'synthetic','test',None,None)
    assert early[0] is True and 'agent skipped' in early[1] and prompt is None


def test_reviewed_matches_do_not_starve_later_leads(lead,monkeypatch):
    total=sources.MAX_CHAT_MESSAGES_PER_CHAT+10
    sources.set_chat_history_interfaces(lambda *a, **kwargs:['s1'],lambda *a, limit=None:[
        {'role':'user','content':f'gym-contract detail {i}','timestamp':NOW.timestamp()-i} for i in range(total)])
    first=collect();assert len(first['matches'])==sources.MAX_CHAT_MESSAGES_PER_CHAT
    for match in first['matches']:assert ack(match['review_receipt'])['success']
    second=collect();assert len(second['matches'])==total-sources.MAX_CHAT_MESSAGES_PER_CHAT
    assert {m['excerpt'] for m in first['matches']}.isdisjoint(m['excerpt'] for m in second['matches'])


def test_ack_crash_rolls_back_review_marker_and_lead_remains(lead,monkeypatch):
    receipt=collect()['matches'][0]['review_receipt']
    original=store._connect
    class Wrapped:
        def __init__(self,conn):self.conn=conn
        def __getattr__(self,key):return getattr(self.conn,key)
        def execute(self,sql,*args):
            if sql.startswith('DELETE FROM context_review_receipts WHERE receipt='):
                raise RuntimeError('synthetic loss before commit')
            return self.conn.execute(sql,*args)
    monkeypatch.setattr(store,'_connect',lambda:Wrapped(original()))
    with pytest.raises(RuntimeError):review.acknowledge(receipt)
    monkeypatch.setattr(store,'_connect',original)
    assert collect()['matches'] and ack(receipt)['success']


def test_ack_does_not_claim_report_delivery_or_complete_item(lead):
    receipt=collect()['matches'][0]['review_receipt']
    assert ack(receipt)['success']
    assert store.get_item(lead['id'])['status']=='open'
    assert store.get_item(lead['id'])['reminder_count']==0


def test_reviewed_chat_matches_do_not_starve_later_bounded_messages(monkeypatch):
    store.add_item(title='Gym renewal')
    store.enable_source('chat',lookback_days=7,enabled_by='fixture')
    store.grant_chat_consent('telegram:42',lookback_days=7,granted_by='telegram:42')
    count=sources.MAX_CHAT_MESSAGES_PER_CHAT+2
    sources.set_chat_history_interfaces(lambda *a, **kwargs:['s'],lambda *a, limit=None:[
        {'role':'user','content':f'Gym renewal detail {i}','timestamp':NOW.timestamp()-i} for i in range(count)])
    try:
        first=sources._collect_chat_matches(now=NOW)
        assert len(first['matches'])==sources.MAX_CHAT_MESSAGES_PER_CHAT
        for m in first['matches']:assert ack(m['review_receipt'])['success']
        second=sources._collect_chat_matches(now=NOW)
        assert len(second['matches'])==2
    finally:sources.set_chat_history_interfaces(None,None)


def test_optin_metrics_are_content_free_and_default_helper_stays_quiet(monkeypatch,capsys):
    monkeypatch.setattr(sources,'collect_snapshot',lambda **kwargs:{'emails':[{'snippet':'private synthetic content'}]})
    source_snapshot.main(metrics=True)
    output=capsys.readouterr()
    assert 'private synthetic content' in output.out
    assert 'private synthetic content' not in output.err
    assert 'payload_utf8_bytes' in output.err
    source_snapshot.main()
    assert capsys.readouterr().err==''


def rule():
    return store.capture_feedback_candidate('Prefer concrete gym follow-up context',source_ref='user:synthetic')


def decide(candidate,status):
    return store.resolve_feedback_candidate(candidate['id'],status,decided_by=('telegram','42','42'),evidence='synthetic explicit command')


def test_approved_rule_invalidates_pending_receipt_and_reexposes_reviewed_lead(lead):
    receipt=collect()['matches'][0]['review_receipt']
    candidate=rule();decide(candidate,'approved')
    assert not ack(receipt)['success']
    updated=collect()['matches'][0]['review_receipt'];assert ack(updated)['success']
    assert not collect()['matches']
    decide(candidate,'dismissed')
    assert collect()['matches']
    assert source_snapshot.build_agent_input({'approved_feedback_rules':store.approved_feedback_rules()})=='{"wakeAgent": false}'


def test_rule_reapprove_same_second_does_not_reuse_old_review_marker(lead):
    candidate=rule();decide(candidate,'approved')
    assert ack(collect()['matches'][0]['review_receipt'])['success']
    decide(candidate,'dismissed');decide(candidate,'approved')
    assert collect()['matches']


def test_approved_rule_text_change_invalidates_even_without_epoch_update(lead):
    candidate=rule();decide(candidate,'approved')
    receipt=collect()['matches'][0]['review_receipt']
    conn=store._connect();conn.execute('UPDATE feedback_candidates SET rule_text=? WHERE id=?',('Changed approved rule',candidate['id']));conn.commit();conn.close()
    assert not ack(receipt)['success'] and collect()['matches']
