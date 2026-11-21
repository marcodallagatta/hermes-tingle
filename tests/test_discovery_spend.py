"""Private evaluator accounting: synthetic usage, no provider or account data."""
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

import hermes_tingle as plugin
import hermes_tingle_discovery as discovery
import hermes_tingle_discovery_assess as assess
import hermes_tingle_store as store
from test_chat_discovery import discovery_env
from test_discovery_assess import evaluation_env, set_provider, snapshot_for


def test_usage_based_estimate_daily_monthly_totals_and_cap_reservation(evaluation_env, monkeypatch):
    _, _, _ = evaluation_env
    def send(request, **kw):
        entries = json.loads(request)['input']['excerpts']
        content = json.dumps({'results': [dict(id=e['id'], decision='drop', label=None, evidence=None) for e in entries]})
        return assess.TransportResult(content, 100, 20)
    set_provider(evaluation_env, monkeypatch, send)
    snapshot = snapshot_for(['I will send the signed form'])
    assess.evaluate(snapshot)
    assert discovery.stage_local_candidates(snapshot) == 0
    summary = assess.spend_summary()
    assert summary['calls'] == 1 and summary['estimated_microeur'] == 140
    assert summary['reserved_microeur'] == 28192
    assert summary['remaining_microeur'] == 4971808
    assert summary['uncertain_calls'] == 0
    assert summary['daily'][0]['estimated_microeur'] == 140
    assert summary['recent'][0]['estimated_microeur'] == 140
    conn = store._connect()
    try:
        row = dict(conn.execute('SELECT * FROM discovery_evaluation_spend').fetchone())
        assert row['input_tokens'] == 100 and row['output_tokens'] == 20
        assert row['input_rate'] == 1 and row['output_rate'] == 2
        assert all(value not in str(row) for value in ('signed form', 'telegram:', 'synthetic-pinned', 'results', 'evidence'))
    finally: conn.close()


def test_failed_call_charged_conservatively_and_budget_refusal_has_no_call_row(evaluation_env, monkeypatch):
    _, calls, path = evaluation_env
    def fail(request, **kw):
        calls.append(1)
        raise TimeoutError('PRIVATE PROVIDER CONTENT')
    set_provider(evaluation_env, monkeypatch, fail)
    snapshot = snapshot_for(['I will send the signed form'])
    assess.evaluate(snapshot)
    assert discovery.stage_local_candidates(snapshot) == 0
    summary = assess.spend_summary()
    assert summary['estimated_microeur'] == summary['reserved_microeur'] == 28192
    assert summary['uncertain_calls'] == 1
    settings = json.loads(path.read_text())
    settings['private_chat_evaluation']['monthly_limit_microeur'] = 28192
    path.write_text(json.dumps(settings))
    assess.evaluate(snapshot_for(['I will arrange a synthetic meeting']))
    assert calls == [1]
    assert assess.spend_summary()['calls'] == 1
    assert assess.spend_summary()['remaining_microeur'] == 0


def test_spend_retention_hidden_then_cleaned_and_current_month_never_refunded(evaluation_env):
    snapshot = snapshot_for(['I will send the signed form'])
    now = store._now()
    assess.evaluate(snapshot, now=now)
    assert assess.spend_summary(now=now)['calls'] == 1
    future = now + timedelta(days=63)
    assert assess.spend_summary(now=future)['calls'] == 0
    conn = store._connect()
    try:
        discovery.cleanup(conn, now=future)
        conn.commit()
        assert conn.execute('SELECT COUNT(*) FROM discovery_evaluation_spend').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM discovery_evaluation_reservations').fetchone()[0] == 0
    finally: conn.close()


def test_owner_debug_spend_works_without_enabling_verbose_debug(discovery_env):
    text = plugin.handle_command(None, 'debug spend', source=discovery_env)
    assert 'Discovery evaluator spend' in text and 'EUR 5.000000' in text
    assert 'Remaining reserved budget' in text and 'UTC' in text
    assert 'not invoices' in text


@pytest.mark.parametrize('kind,user', [('group', '42'), ('dm', '99')])
def test_debug_spend_refuses_group_or_nonowner_before_read(discovery_env, monkeypatch, kind, user):
    calls = []
    monkeypatch.setattr(assess, 'spend_summary', lambda **kw: calls.append(1), raising=False)
    source = SimpleNamespace(platform='telegram', chat_id='42' if kind == 'dm' else '-1009',
                             user_id=user, chat_type=kind, is_bot=False)
    reply = plugin.handle_command(None, 'debug spend', source=source)
    assert calls == [] and ('owner' in reply.lower() or 'direct message' in reply.lower())


@pytest.mark.parametrize('chat_type,chat_id', [('unknown', '42'), ('dm', None), ('', '42')])
def test_debug_spend_refuses_unverified_conversation_shape(discovery_env, monkeypatch, chat_type, chat_id):
    calls = []
    monkeypatch.setattr(assess, 'spend_summary', lambda **kw: calls.append(1), raising=False)
    source = SimpleNamespace(platform='telegram', chat_id=chat_id, user_id='42',
                             chat_type=chat_type, is_bot=False)
    reply = plugin.handle_command(None, 'debug spend', source=source)
    assert calls == [] and ('direct message' in reply.lower() or 'verified' in reply.lower())


@pytest.mark.parametrize('usage', [(True, 2), (-1, 2), (20001, 2), (10, 4097), ('PRIVATE', 2), (None, None)])
def test_invalid_usage_never_persists_arbitrary_fields_or_refunds(evaluation_env, usage):
    snapshot = snapshot_for(['The form needs sending'])
    assess.evaluate(snapshot)
    conn = store._connect()
    try:
        reservation = conn.execute('SELECT id FROM discovery_evaluation_reservations').fetchone()[0]
    finally: conn.close()
    assess._account_usage(reservation, assess.TransportResult('PRIVATE RESPONSE', *usage))
    summary = assess.spend_summary()
    assert summary['uncertain_calls'] == 1
    assert summary['estimated_microeur'] == summary['reserved_microeur']
    assert 'PRIVATE' not in json.dumps(summary)


def test_daily_and_monthly_rollover_use_utc_and_debug_purge_cannot_reset_cap(evaluation_env):
    import hermes_tingle_debug as debug
    base = store._now().replace(day=1, hour=12)
    for offset in (0, 1):
        assess.evaluate(snapshot_for(['I will send the synthetic form ' + str(offset)]), now=base+timedelta(days=offset))
    summary = assess.spend_summary(now=base+timedelta(days=1))
    assert summary['calls'] == 2 and len(summary['daily']) == 2
    assert sum(row['estimated_microeur'] for row in summary['daily']) == summary['estimated_microeur']
    reserved = summary['reserved_microeur']
    debug.purge()
    debug.set_enabled(False)
    store.disable_source('chat')
    assert assess.spend_summary(now=base+timedelta(days=1))['reserved_microeur'] == reserved
    next_month = (base+timedelta(days=32)).replace(day=1)
    assert assess.spend_summary(now=next_month)['calls'] == 0
    assert assess.spend_summary(now=next_month)['remaining_microeur'] == 5_000_000


def test_real_default_adapter_flows_usage_only_into_private_accounting(evaluation_env, monkeypatch, capsys):
    import io
    import http.client
    from test_discovery_runtime_transport import install_core
    _, _, path = evaluation_env
    settings = json.loads(path.read_text())
    settings['private_chat_evaluation'] = {k: v for k, v in settings['private_chat_evaluation'].items()
                                           if k not in ('model', 'pricing_revision')}
    settings['private_chat_evaluation']['route'] = 'runtime-default'
    path.write_text(json.dumps(settings))
    install_core(monkeypatch, {'model': {'default': 'own-model', 'provider': 'own-provider',
                                        'base_url': 'https://api.example.invalid/v1'}})
    route = assess.resolve_profile_default()
    monkeypatch.setattr(assess, '_pricing', lambda route:
        assess.PricingBound('synthetic-only', 1, 2, 'synthetic-stable'))
    monkeypatch.setattr(assess, '_runtime_credentials', lambda default: 'synthetic-key')
    requests = []
    class Connection:
        def __init__(self, *a, **kw): self.sock = SimpleNamespace(settimeout=lambda t: None)
        def connect(self): pass
        def close(self): pass
        def request(self, method, endpoint, body, headers): requests.append(json.loads(body))
        def getresponse(self):
            entries = json.loads(requests[-1]['messages'][1]['content'])['excerpts']
            content = json.dumps({'results': [dict(id=e['id'], decision='drop', label=None, evidence=None) for e in entries]})
            raw = json.dumps({'usage': {'prompt_tokens': 100, 'completion_tokens': 20},
                              'choices': [{'finish_reason': 'stop', 'message': {'content': content}}]}).encode()
            return SimpleNamespace(status=200, read1=io.BytesIO(raw).read)
    monkeypatch.setattr(http.client, 'HTTPSConnection', Connection)
    snapshot = snapshot_for(['I will send the signed form'])
    assess.evaluate(snapshot)
    assert discovery.stage_local_candidates(snapshot) == 0
    assert snapshot['chat_review']['discovery']['evaluation_status'] == 'complete'
    summary = assess.spend_summary()
    assert len(requests) == 1 and summary['estimated_microeur'] == 140 and summary['uncertain_calls'] == 0
    import source_snapshot
    assert source_snapshot.build_agent_input(snapshot) == '{"wakeAgent": false}'
    assert capsys.readouterr() == ('', '')
