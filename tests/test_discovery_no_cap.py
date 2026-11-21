"""Synthetic owner consent and priced-route regressions; no live reads or calls."""
import json
import time
import threading

from decimal import Decimal
from types import SimpleNamespace
import io
import http.client
import pytest
import hermes_tingle_discovery_assess as assess
import hermes_tingle_discovery as discovery
import hermes_tingle_store as store
from test_chat_discovery import discovery_env
from test_discovery_assess import snapshot_for, counts
from test_discovery_runtime_transport import install_core
from test_discovery_usage_pricing import pricing_core, entry
from test_evaluation_consent import consent_env, CONSENT
from test_guided_setup import guided, preview, reply


def test_guided_explicit_no_cap_requires_destination_and_write(guided):
    home, data, source, ctx = guided
    out = preview(guided, evaluation='no-cap')
    assert 'NO monthly Tingle spending ceiling' in out
    staged = json.loads((data / 'setup-state.json').read_text())['policy']
    assert staged['private_chat_evaluation'] == {**CONSENT, 'consent': 'owner-dm-excerpts-v2', 'monthly_limit_microeur': 0}
    assert not (home / 'hermes-tingle.json').exists()
    reply(ctx, source, 'confirm-destination', 'telegram:123')
    assert 'policy saved' in reply(ctx, source, 'confirm', 'WRITE')
    assert json.loads((home / 'hermes-tingle.json').read_text()) == staged

from hermes_tingle_config import private_chat_evaluation_policy


def test_owner_explicit_no_cap_reconsent_keeps_v1_default_and_exact_v2(consent_env):
    path, before, source, ctx = consent_env
    path.write_text(json.dumps({**before, 'private_chat_evaluation': CONSENT}))
    raw = path.read_bytes()
    ctx.answer = 'yes'
    assert 'nothing changed' in ctx.command('evaluation enable', source=source)
    assert path.read_bytes() == raw
    ctx.answer = 'no-cap'
    assert 'consent enabled' in ctx.command('evaluation enable', source=source)
    saved = json.loads(path.read_text())
    expected = {**CONSENT, 'consent': 'owner-dm-excerpts-v2', 'monthly_limit_microeur': 0}
    assert saved == {**before, 'private_chat_evaluation': expected}
    assert private_chat_evaluation_policy(saved) == expected
    question, choices = ctx.questions[-1]
    assert {c['value'] for c in choices} == {'enable', 'no-cap', 'cancel'}
    for text in ('NO monthly Tingle spending ceiling', 'exceed €5', 'not invoices',
                 'no retries or fallback', 'bounded', 'local cues'):
        assert text in question
    for patch in ({'consent': 'owner-dm-excerpts-v1'}, {'monthly_limit_microeur': 1},
                  {'monthly_limit_microeur': False}, {'extra': True}):
        assert private_chat_evaluation_policy({**saved, 'private_chat_evaluation': {**expected, **patch}}) is None
    # Explicit recommended capped choice can restore v1, never a zero v1 sentinel.
    ctx.answer = 'enable'
    assert 'consent enabled' in ctx.command('evaluation enable', source=source)
    assert json.loads(path.read_text())['private_chat_evaluation'] == CONSENT


BASE = 'https://inference-api.nousresearch.com/v1'
MODEL = 'qwen/qwen4-max:free'  # Capability fixture, never a selected default.


def catalog(**changes):
    return entry(input_cost_per_million=Decimal(0), output_cost_per_million=Decimal(0),
        cache_read_cost_per_million=Decimal(0), cache_write_cost_per_million=Decimal(0),
        source='provider_models_api', source_url=BASE + '/models',
        pricing_version='openai-compatible-models-api', fetched_at=store._now(), **changes)


def nous_env(monkeypatch, *, usage=(100, 20), price=None, comparison=True, version=2):
    import sys
    import hermes_constants
    path = hermes_constants.get_hermes_home() / 'hermes-tingle.json'
    settings = json.loads(path.read_text())
    settings['private_chat_evaluation'] = {**CONSENT, 'consent': 'owner-dm-excerpts-v2' if version == 2 else 'owner-dm-excerpts-v1',
        'monthly_limit_microeur': 0 if version == 2 else 5_000_000}
    path.write_text(json.dumps(settings))
    config = {'model': {'default': MODEL, 'provider': 'nous', 'base_url': BASE}}
    install_core(monkeypatch, config)
    pricing_core(monkeypatch, None)
    prices = {MODEL: price or catalog()}
    if comparison:
        prices[MODEL[:-5]] = entry(input_cost_per_million=Decimal('1.425'), output_cost_per_million=Decimal('4.275'),
            cache_read_cost_per_million=Decimal('0.0713'), cache_write_cost_per_million=Decimal('1.7813'),
            source='provider_models_api', source_url=BASE + '/models', pricing_version='openai-compatible-models-api', fetched_at=store._now())
    metadata, requests = [], []
    def pricing(model, provider=None, base_url=None, api_key=None):
        assert provider == 'nous' and base_url == BASE and api_key is None
        metadata.append(model)
        return prices.get(model)
    # Exact endpoint attestation is separate from core's pricing lookup: its
    # generic fetcher may probe an alternate /models path and loses provenance.
    raw_overrides = {}
    def raw_catalog():
        data = {}
        for model, value in prices.items():
            if value is None: continue
            pricing = {key: str(getattr(value, attr) / Decimal(1_000_000)) for key, attr in (
                ('prompt', 'input_cost_per_million'), ('completion', 'output_cost_per_million'),
                ('cache_read', 'cache_read_cost_per_million'), ('cache_write', 'cache_write_cost_per_million'))
                if isinstance(getattr(value, attr), Decimal)}
            if value.request_cost is not None: pricing['request'] = str(value.request_cost)
            pricing.update(raw_overrides.get(model, {}))
            data[model] = pricing
        return data
    monkeypatch.setattr(assess, '_nous_catalog', raw_catalog, raising=False)
    monkeypatch.setattr(sys.modules['agent.usage_pricing'], 'get_pricing_entry', pricing)
    monkeypatch.setattr(assess, '_runtime_credentials', lambda default: 'synthetic-key')
    class Connection:
        def __init__(self, *a, **kw): self.sock = SimpleNamespace(settimeout=lambda t: None)
        def connect(self): pass
        def close(self): pass
        def request(self, method, endpoint, body, headers):
            assert method == 'POST' and endpoint == '/v1/chat/completions'
            requests.append(json.loads(body))
        def getresponse(self):
            excerpts = json.loads(requests[-1]['messages'][1]['content'])['excerpts']
            content = json.dumps({'results': [dict(id=e['id'], decision='drop', label=None, evidence=None) for e in excerpts]})
            data = {'choices': [{'finish_reason': 'stop', 'message': {'content': content}}]}
            if usage is not None: data['usage'] = dict(zip(('prompt_tokens', 'completion_tokens'), usage))
            return SimpleNamespace(status=200, read1=io.BytesIO(json.dumps(data).encode()).read)
    monkeypatch.setattr(http.client, 'HTTPSConnection', Connection)
    return path, config, prices, metadata, requests


def test_nous_price_refresh_counts_against_deadline_and_never_posts(discovery_env, monkeypatch):
    _, _, _, _, requests = nous_env(monkeypatch, comparison=False)
    route = assess.runtime_default_route()
    request = assess._envelope(route, [{'id': 'a' * 32, 'excerpt': 'Synthetic ordinary text', 'clipped': False}], [])
    original = assess._nous_catalog
    calls = [0]
    preflight_finished = threading.Event()
    def slow_catalog():
        calls[0] += 1
        data = original()
        if calls[0] == 1:
            time.sleep(0.10)
            preflight_finished.set()
        return data
    monkeypatch.setattr(assess, '_nous_catalog', slow_catalog)
    started = time.monotonic()
    failure = None
    try:
        route.send(request, timeout_seconds=0.02, max_response_bytes=assess.MAX_RESPONSE_BYTES)
    except ValueError as exc:
        failure = exc
    elapsed = time.monotonic() - started
    assert preflight_finished.wait(1.0)
    assert failure is not None
    assert elapsed < 0.08
    assert requests == []


def test_free_nous_v2_one_bounded_post_persists_tokens_and_labeled_paid_comparison(discovery_env, monkeypatch, capsys):
    path, config, prices, metadata, requests = nous_env(monkeypatch)
    assert assess.configured() is True
    snap = snapshot_for(['I will send the signed form'])
    assess.evaluate(snap)
    assess.evaluate(snap)
    assert discovery.stage_local_candidates(snap) == 0
    assert snap['chat_review']['discovery']['evaluation_status'] == 'complete'
    assert len(requests) == 1
    sent = requests[0]
    assert sent['max_tokens'] == assess.MAX_OUTPUT_TOKENS
    assert not {'max_completion_tokens', 'service_tier', 'store'} & sent.keys()
    assert sent['tools'] == [] and sent['tool_choice'] == 'none' and sent['n'] == 1 and sent['stream'] is False
    assert sent['response_format']['json_schema'] == {'name': 'discovery', 'strict': True, 'schema': assess.SCHEMA}
    data = assess.spend_summary()
    assert data['cap_mode'] == 'no_cap' and data['monthly_limit_microeur'] == 0 and data['remaining_microeur'] is None
    assert data['calls'] == 1 and data['input_tokens'] == 100 and data['output_tokens'] == 20
    assert data['estimated_microeur'] == data['reserved_microeur'] == 0
    assert data['paid_equivalent_microeur'] == 440  # ceil buffered max(input/cache)=3, output=7
    row = data['recent'][0]
    assert row['input_tokens'] == 100 and row['output_tokens'] == 20 and row['paid_equivalent_microeur'] == 440
    assert data['daily'][0]['input_tokens'] == 100
    text = assess.format_spend()
    assert 'NO Tingle spending cap' in text and 'Paid-equivalent comparison' in text and 'not actual charges' in text
    assert 'Remaining reserved budget' not in text and 'EUR 5.000000' not in text
    assert 'input 100 / output 20' in text and 'not invoices' in text
    assert MODEL in metadata and MODEL[:-5] in metadata
    conn = store._connect()
    try:
        stored = str([tuple(r) for r in conn.execute('SELECT * FROM discovery_evaluation_spend')])
        assert all(t not in stored for t in ('signed form', 'telegram:', 'results', 'synthetic-key'))
    finally: conn.close()
    assert capsys.readouterr() == ('', '')


def test_positive_catalog_transition_records_usage_without_five_euro_block_only_v2(discovery_env, monkeypatch):
    path, config, prices, metadata, requests = nous_env(monkeypatch)
    assert assess.configured()
    # A :free alias can become billed. The suffix MUST NOT keep it at zero.
    prices[MODEL] = catalog()
    prices[MODEL].input_cost_per_million = Decimal('300')
    prices[MODEL].output_cost_per_million = Decimal('2')
    snap = snapshot_for(['I will send the signed form'])
    assess.evaluate(snap)
    assert discovery.stage_local_candidates(snap) == 0
    assert snap['chat_review']['discovery']['evaluation_status'] == 'complete'
    data = assess.spend_summary()
    assert len(requests) == 1 and data['reserved_microeur'] > 5_000_000
    assert data['estimated_microeur'] == 45060 and data['paid_equivalent_microeur'] is None
    assert data['cap_mode'] == 'no_cap'
    assert requests[0]['max_tokens'] == assess.MAX_OUTPUT_TOKENS
    # Restoring v1 never grants this route, nor turns prior consent into no-cap.
    settings = json.loads(path.read_text())
    settings['private_chat_evaluation'] = CONSENT
    path.write_text(json.dumps(settings))
    assert not assess.configured()
    assess.evaluate(snapshot_for(['I will arrange a meeting']))
    assert len(requests) == 1
    assert assess.spend_summary()['cap_mode'] == 'capped'


@pytest.mark.parametrize('usage', [None, (0, 0), (0, 20), (100, 0), (True, 20), (-1, 20), (20001, 20), (100, 4097), ('private', 20)])
def test_free_missing_zero_or_invalid_usage_remains_unknown_not_zero_spend(discovery_env, monkeypatch, usage):
    _, _, _, _, requests = nous_env(monkeypatch, usage=usage)
    snap = snapshot_for(['I will send the signed form'])
    assess.evaluate(snap)
    discovery.stage_local_candidates(snap)
    data = assess.spend_summary()
    assert len(requests) == 1 and data['calls'] == 1
    assert data['estimated_microeur'] is None and data['paid_equivalent_microeur'] is None
    assert data['uncertain_calls'] == data['missing_usage_calls'] == 1
    assert data['recent'][0]['input_tokens'] is None and data['recent'][0]['output_tokens'] is None
    assert 'Month actual-route estimate: unknown' in assess.format_spend()
    assert 'private' not in json.dumps(data)


@pytest.mark.parametrize('mutation', ['unknown_fee', 'unknown_dimension', 'alternate_endpoint_price'])
def test_core_price_requires_exact_catalog_attestation_before_any_dispatch(discovery_env, monkeypatch, mutation):
    _, _, _, _, requests = nous_env(monkeypatch)
    assert assess.configured()
    original = assess._nous_catalog
    def changed():
        data = original()
        if mutation == 'unknown_fee': data[MODEL]['request'] = 'unknown'
        elif mutation == 'unknown_dimension': data[MODEL]['image'] = '1'
        else: data = {}  # Core's alternate-path cache does not attest this endpoint.
        return data
    monkeypatch.setattr(assess, '_nous_catalog', changed)
    snap = snapshot_for(['I will send the signed form'])
    assess.evaluate(snap)
    assert snap['chat_review']['discovery'].get('evaluation_status') == 'not_configured'
    assert requests == [] and counts()['discovery_evaluation_reservations'] == 0


@pytest.mark.parametrize('fault', ['missing', 'expired', 'future', 'models_dev', 'wrong_source', 'wrong_url',
    'cache_read', 'cache_write', 'input', 'output', 'fee', 'unknown_fee', 'schema', 'wire', 'proxy', 'alias', 'model', 'named', 'custom', 'v1'])
def test_nous_unverified_route_or_price_stays_local_before_reservation(discovery_env, monkeypatch, fault):
    import sys
    from datetime import timedelta
    path, config, prices, _, requests = nous_env(monkeypatch)
    assert assess.configured()  # Non-vacuous admitted control.
    price = prices[MODEL]
    if fault == 'missing': prices[MODEL] = None
    elif fault == 'expired': price.fetched_at = store._now() - timedelta(seconds=301)
    elif fault == 'future': price.fetched_at = store._now() + timedelta(seconds=60)
    elif fault == 'models_dev': price.source_url = 'https://models.dev'
    elif fault == 'wrong_source': price.source = 'official_docs_snapshot'
    elif fault == 'wrong_url': price.source_url = 'https://inference-api.nousresearch.com/models'
    elif fault in ('cache_read', 'cache_write', 'input', 'output'): setattr(price, fault + '_cost_per_million', None)
    elif fault == 'fee': price.request_cost = Decimal('0.001')
    elif fault == 'unknown_fee': price.request_cost = 'unknown'
    elif fault == 'schema': price.new_fee_dimension = Decimal(1)
    elif fault == 'wire': config['model']['api_mode'] = 'responses'
    elif fault == 'proxy': config['model']['base_url'] = 'https://proxy.example.invalid/v1'
    elif fault == 'alias': config['model']['provider'] = 'nous-alias'
    elif fault == 'model': config['model']['default'] = 'unreviewed:free'
    elif fault in ('named', 'custom'):
        config['providers' if fault == 'named' else 'custom_providers'] = {'nous': {}}
        monkeypatch.setattr(sys.modules['hermes_cli.runtime_provider_custom'], 'named_custom_provider_config_path', lambda *a: ('named',))
    elif fault == 'v1':
        settings = json.loads(path.read_text())
        settings['private_chat_evaluation'] = CONSENT
        path.write_text(json.dumps(settings))
    snap = snapshot_for(['I will send the signed form'])
    assess.evaluate(snap)
    assert snap['chat_review']['discovery']['evaluation_status'] == 'not_configured'
    assert requests == [] and counts()['discovery_evaluation_reservations'] == 0
    assert discovery.stage_local_candidates(snap) == 1


@pytest.mark.parametrize('change', ['price', 'policy', 'expired', 'named', 'policy_during_metadata'])
def test_nous_price_and_policy_revalidated_after_tls_before_post(discovery_env, monkeypatch, change):
    import sys
    from datetime import timedelta
    path, config, prices, _, requests = nous_env(monkeypatch)
    assert assess.configured()
    connection = http.client.HTTPSConnection
    now = store._now()
    connected = []
    catalog_reader = assess._nous_catalog
    def raced_catalog():
        if connected and change == 'policy_during_metadata':
            settings = json.loads(path.read_text())
            settings['private_chat_evaluation'] = CONSENT
            path.write_text(json.dumps(settings))
        return catalog_reader()
    monkeypatch.setattr(assess, '_nous_catalog', raced_catalog)
    class Changed(connection):
        def connect(self):
            connected.append(True)
            if change == 'price': prices[MODEL].input_cost_per_million = Decimal('0.0001')
            elif change == 'policy':
                settings = json.loads(path.read_text())
                settings['private_chat_evaluation'] = CONSENT
                path.write_text(json.dumps(settings))
            elif change == 'named':
                monkeypatch.setattr(sys.modules['hermes_cli.runtime_provider_custom'], 'named_custom_provider_config_path', lambda *a: ('named',))
            elif change == 'expired': monkeypatch.setattr(store, '_now', lambda *a: now + timedelta(seconds=61))
    monkeypatch.setattr(http.client, 'HTTPSConnection', Changed)
    snap = snapshot_for(['I will send the signed form'])
    assess.evaluate(snap)
    assert snap['chat_review']['discovery'].get('evaluation_status') == 'failed'
    assert requests == [] and counts()['discovery_evaluation_reservations'] == 1
    assert discovery.stage_local_candidates(snap) == 0


@pytest.mark.parametrize('mode', ['cancel', 'group', 'bot', 'unverified', 'malformed', 'stale', 'unknown_choice'])
def test_v2_no_cap_unsafe_consent_paths_never_write(consent_env, mode):
    path, before, source, ctx = consent_env
    ctx.answer = 'no-cap'
    if mode == 'cancel': ctx.answer = 'cancel'
    elif mode == 'group': source.chat_type = 'group'
    elif mode == 'bot': source.is_bot = True
    elif mode == 'unverified': source.chat_id = None
    elif mode == 'malformed': path.write_text(json.dumps({**before, 'private_chat_evaluation': {**CONSENT, 'monthly_limit_microeur': 0}}))
    elif mode == 'unknown_choice': ctx.answer = 'no cap please'
    raw = path.read_bytes()
    if mode == 'stale':
        def choose(*args):
            path.write_bytes(raw + b' ')
            return 'no-cap'
        ctx.ask_choice = choose
    assert 'nothing changed' in ctx.command('evaluation enable', source=source)
    assert path.read_bytes() == raw + (b' ' if mode == 'stale' else b'')
    assert not list(path.parent.glob('.tingle-policy-backup-*'))


@pytest.mark.parametrize('status', [200, 307, 429, 500])
def test_exact_public_catalog_get_has_no_auth_redirect_retry_or_alternate(monkeypatch, status):
    calls, closed = [], []
    class Connection:
        sock = None
        def __init__(self, host, **kw):
            assert host == 'inference-api.nousresearch.com' and kw['timeout'] == 10
        def request(self, method, path, headers): calls.append((method, path, headers))
        def getresponse(self):
            raw = json.dumps({'data': [{'id': MODEL, 'pricing': {'prompt': '0'}}]}).encode()
            return SimpleNamespace(status=status, read1=io.BytesIO(raw).read)
        def close(self): closed.append(True)
    monkeypatch.setattr(http.client, 'HTTPSConnection', Connection)
    if status == 200: assert assess._nous_catalog() == {MODEL: {'prompt': '0'}}
    else:
        with pytest.raises(ValueError): assess._nous_catalog()
    assert len(calls) == len(closed) == 1
    assert calls[0] == ('GET', '/v1/models', {'Accept': 'application/json', 'Accept-Encoding': 'identity', 'Connection': 'close'})


@pytest.mark.parametrize('fault', [None, 'missing_sibling', 'unpriced_sibling', 'wrong_sibling_endpoint', 'server'])
def test_paid_nous_and_optional_reference_have_one_post_no_fallback(discovery_env, monkeypatch, fault):
    path, config, prices, _, requests = nous_env(monkeypatch)
    if fault is None:
        config['model']['default'] = MODEL[:-5]
    elif fault == 'missing_sibling': prices.pop(MODEL[:-5])
    elif fault == 'unpriced_sibling': prices[MODEL[:-5]].cache_write_cost_per_million = None
    elif fault == 'wrong_sibling_endpoint': prices[MODEL[:-5]].source_url = 'https://models.dev'
    else:
        cls = http.client.HTTPSConnection
        class Failed(cls):
            def getresponse(self): return SimpleNamespace(status=503)
        monkeypatch.setattr(http.client, 'HTTPSConnection', Failed)
    assert assess.configured()
    snap = snapshot_for(['I will send the signed form'])
    assess.evaluate(snap)
    assess.evaluate(snap)
    assert discovery.stage_local_candidates(snap) == 0
    assert len(requests) == 1 and requests[0]['max_tokens'] == assess.MAX_OUTPUT_TOKENS
    assert not {'max_completion_tokens', 'service_tier', 'store'} & requests[0].keys()
    data = assess.spend_summary()
    assert data['paid_equivalent_microeur'] is None
    assert data['estimated_microeur'] == (440 if fault is None else None if fault == 'server' else 0)
    assert snap['chat_review']['discovery']['evaluation_status'] == ('failed' if fault == 'server' else 'complete')


def test_v1_capped_openai_then_v2_uncapped_keeps_old_month_reservations(discovery_env, monkeypatch):
    from dataclasses import replace
    import hermes_constants
    path = hermes_constants.get_hermes_home() / 'hermes-tingle.json'
    settings = json.loads(path.read_text())
    settings['private_chat_evaluation'] = CONSENT
    path.write_text(json.dumps(settings))
    install_core(monkeypatch, {'model': {'default': 'gpt-4o-mini', 'provider': 'openai-api',
        'base_url': 'https://api.openai.com/v1', 'api_mode': 'chat_completions'}})
    pricing_core(monkeypatch, entry())
    now = store._now()
    conn = store._connect()
    try:
        conn.execute('INSERT INTO discovery_evaluation_reservations VALUES(?,?,?,?,?)',
            ('synthetic-old-reservation', now.strftime('%Y-%m'), 4_999_999, now.isoformat(), 'failed'))
        conn.commit()
    finally: conn.close()
    calls = []
    original = assess.runtime_default_route
    def send(request, **kw):
        calls.append(1)
        entries = json.loads(request)['input']['excerpts']
        return assess.TransportResult(json.dumps({'results': [dict(id=e['id'], decision='drop', label=None, evidence=None) for e in entries]}), 100, 20)
    monkeypatch.setattr(assess, 'runtime_default_route', lambda: replace(original(), send=send))
    assert assess.configured()
    snap = snapshot_for(['I will send the signed form'])
    assess.evaluate(snap)
    assert snap['chat_review']['discovery']['evaluation_status'] == 'failed'
    assert discovery.stage_local_candidates(snap) == 0 and calls == []
    assert assess.spend_summary()['monthly_limit_microeur'] == 5_000_000
    settings['private_chat_evaluation'] = {**CONSENT, 'consent': 'owner-dm-excerpts-v2', 'monthly_limit_microeur': 0}
    path.write_text(json.dumps(settings))
    snap = snapshot_for(['I will send the signed form'])
    assess.evaluate(snap)
    discovery.stage_local_candidates(snap)
    assert snap['chat_review']['discovery']['evaluation_status'] == 'complete' and calls == [1]
    data = assess.spend_summary()
    assert data['reserved_microeur'] > 5_000_000 and data['remaining_microeur'] is None


def test_content_free_v1_accounting_migration_preserves_reservations_and_supports_zero(tmp_path):
    import sqlite3
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('CREATE TABLE discovery_evaluation_reservations (id TEXT PRIMARY KEY, month TEXT NOT NULL, cost_microeur INTEGER NOT NULL CHECK(cost_microeur>0), created_at TEXT NOT NULL, state TEXT NOT NULL)')
        conn.execute("CREATE TABLE discovery_evaluation_spend (request_id TEXT PRIMARY KEY, input_rate INTEGER NOT NULL CHECK(input_rate>0), output_rate INTEGER NOT NULL CHECK(output_rate>0), input_tokens INTEGER, output_tokens INTEGER, estimated_microeur INTEGER NOT NULL CHECK(estimated_microeur>=0), basis TEXT NOT NULL CHECK(basis IN ('reservation_bound','usage_bound')))")
        conn.execute("INSERT INTO discovery_evaluation_reservations VALUES('old','2026-11',24096,'2026-11-01','failed')")
        conn.execute("INSERT INTO discovery_evaluation_spend VALUES('old',1,1,100,20,120,'usage_bound')")
        conn.commit()
        store.ensure_schema(conn)
        store.ensure_schema(conn)
        assert tuple(conn.execute("SELECT * FROM discovery_evaluation_reservations WHERE id='old'").fetchone()) == ('old', '2026-11', 24096, '2026-11-01', 'failed')
        assert tuple(conn.execute("SELECT * FROM discovery_evaluation_spend WHERE request_id='old'").fetchone()) == ('old', 1, 1, 100, 20, 120, 'usage_bound', None, None, None)
        conn.execute("INSERT INTO discovery_evaluation_reservations VALUES('free','2026-11',0,'2026-11-01','reserved')")
        conn.execute("INSERT INTO discovery_evaluation_spend VALUES('free',0,0,NULL,NULL,NULL,'reservation_bound',3,7,NULL)")
        conn.commit()
    finally: conn.close()


def test_core_parser_nous_free_paid_and_unknown_request_contract(monkeypatch):
    # Optional compatibility probe uses ONLY installed core source plus fake
    # public metadata; no config, credential, provider network or inference.
    import sys
    from test_discovery_usage_pricing import test_installed_core_resolver_with_only_fake_metadata
    test_installed_core_resolver_with_only_fake_metadata(monkeypatch, 'models_api')
    core = sys.modules['agent.usage_pricing']
    data = {MODEL: {'pricing': dict(prompt='0', completion='0', cache_read='0', cache_write='0')}}
    def metadata(base_url, api_key=''):
        assert base_url == BASE and api_key == ''
        return data
    monkeypatch.setattr(core, 'fetch_endpoint_model_metadata', metadata)
    monkeypatch.setattr(assess, '_nous_catalog', lambda: {m: v['pricing'] for m, v in data.items()})
    default = assess.DefaultRoute('nous', MODEL, BASE)
    bound = assess._pricing(default)
    assert (bound.input_microeur_per_token, bound.output_microeur_per_token) == (0, 0)
    assert core.get_pricing_entry(MODEL, provider='nous', base_url=BASE, api_key=None).request_cost is None
    data[MODEL]['pricing'].update(prompt='0.000001425', completion='0.000004275',
        cache_read='0.0000000713', cache_write='0.0000017813')
    bound = assess._pricing(default)
    assert (bound.input_microeur_per_token, bound.output_microeur_per_token) == (3, 7)
    for invalid in ('-1', 'NaN', 'unknown', '0.01'):
        data[MODEL]['pricing']['request'] = invalid
        with pytest.raises(ValueError): assess._pricing(default)


def test_v1_openai_explicit_https_port_keeps_original_public_pricing_identity(monkeypatch):
    calls = pricing_core(monkeypatch, entry())
    bound = assess._pricing(assess.DefaultRoute('openai-api', 'gpt-4o-mini', 'https://api.openai.com:443/v1'))
    assert bound.input_microeur_per_token == bound.output_microeur_per_token == 1
    assert calls == [('gpt-4o-mini', 'openai', 'https://api.openai.com/v1', None)]
