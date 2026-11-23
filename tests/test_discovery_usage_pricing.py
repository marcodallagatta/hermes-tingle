"""Hermes pricing fixtures only: no account/config read or live transport."""
from decimal import Decimal
from types import ModuleType, SimpleNamespace
import sys

import pytest

import hermes_tingle_discovery_assess as assess
import hermes_tingle_discovery as discovery
import hermes_tingle_sources as sources
import hermes_tingle_store as store
from test_discovery_assess import evaluation_env
from test_chat_discovery import discovery_env, message, wire


def entry(**changes):
    values = dict(input_cost_per_million=Decimal('0.15'),
        output_cost_per_million=Decimal('0.60'),
        cache_read_cost_per_million=Decimal('0.075'), cache_write_cost_per_million=None,
        request_cost=None, source='official_docs_snapshot',
        source_url='https://openai.com/api/pricing/', pricing_version='synthetic-snapshot-v1',
        fetched_at=None, tier_threshold_tokens=None,
        input_cost_per_million_above=None, output_cost_per_million_above=None,
        cache_read_cost_per_million_above=None, cache_write_cost_per_million_above=None)
    values.update(changes)
    return SimpleNamespace(**values)


def pricing_core(monkeypatch, value):
    calls = []
    module = ModuleType('agent.usage_pricing')
    def get_pricing_entry(model_name, provider=None, base_url=None, api_key=None):
        calls.append((model_name, provider, base_url, api_key))
        return value
    module.get_pricing_entry = get_pricing_entry
    monkeypatch.setitem(sys.modules, 'agent.usage_pricing', module)
    # Each test gets an isolated, content-free runtime pricing memo.
    monkeypatch.setattr(assess, '_PRICING_CACHE', {}, raising=False)
    return calls


def _valid_runtime_consent():
    # runtime-default consent must NOT carry model/pricing_revision; the strict
    # validator rejects those extra fields, which would mask the wire guard.
    return {'enabled': True, 'consent': 'owner-dm-excerpts-v1', 'approved_by': 'telegram:42',
            'route': 'runtime-default', 'monthly_limit_microeur': 5_000_000}


def _stub_openai_core(monkeypatch, model_cfg, detected_mode, *, providers=None,
                      custom_providers=None, named_path=None):
    cfg = {'model': model_cfg}
    if providers is not None:
        cfg['providers'] = providers
    if custom_providers is not None:
        cfg['custom_providers'] = custom_providers
    config_mod = ModuleType('hermes_cli.config')
    config_mod.load_config_readonly = lambda: cfg
    switch_mod = ModuleType('hermes_cli.model_switch')
    switch_mod.resolve_startup_model_route = lambda *a, **k: None
    models_mod = ModuleType('hermes_cli.models')
    models_mod.get_default_model_for_provider = lambda p: model_cfg.get('default', 'gpt-4o-mini')
    providers_mod = ModuleType('hermes_cli.providers')
    providers_mod.determine_api_mode = lambda provider, base, model: detected_mode
    runtime_mod = ModuleType('hermes_cli.runtime_provider')
    runtime_mod._configured_api_mode = lambda provider, mc: None
    custom_mod = ModuleType('hermes_cli.runtime_provider_custom')
    # Credential-free named-ownership lookup used by the admission gate.
    custom_mod.named_custom_provider_config_path = lambda provider, config: named_path
    for name, mod in [('hermes_cli.config', config_mod), ('hermes_cli.model_switch', switch_mod),
                      ('hermes_cli.models', models_mod), ('hermes_cli.providers', providers_mod),
                      ('hermes_cli.runtime_provider_custom', custom_mod),
                      ('hermes_cli.runtime_provider', runtime_mod)]:
        monkeypatch.setitem(sys.modules, name, mod)


def _write_consent(settings_extra=None):
    import json
    import hermes_constants
    policy = hermes_constants.get_hermes_home() / 'hermes-tingle.json'
    settings = json.loads(policy.read_text())
    settings['private_chat_evaluation'] = _valid_runtime_consent()
    policy.write_text(json.dumps(settings))


def test_omitted_api_mode_direct_openai_resolves_effective_wire_not_chat(discovery_env, monkeypatch):
    # Composed regression: a direct OpenAI default with NO model.api_mode must
    # resolve the core-mandated effective wire (codex_responses), not assume
    # chat_completions. Valid consent + real pricing stub so that removing the
    # wire guard would flip configured() to True (mutation-sensitive).
    _write_consent()
    pricing_core(monkeypatch, entry())
    _stub_openai_core(monkeypatch, {'provider': 'openai-api', 'default': 'gpt-4o-mini',
                                    'base_url': 'https://api.openai.com/v1'}, 'codex_responses')
    default = assess.resolve_profile_default()
    assert default.api_mode == 'codex_responses'
    assert assess.configured() is False


def test_unpriced_omitted_wire_preserves_local_cues_without_reservation(discovery_env, monkeypatch):
    _write_consent()
    pricing_core(monkeypatch, entry())
    _stub_openai_core(monkeypatch, {'provider': 'openai-api', 'default': 'gpt-4o-mini',
                                    'base_url': 'https://api.openai.com/v1'}, 'codex_responses')
    monkeypatch.setattr(assess, '_send_runtime', lambda *a, **kw: pytest.fail('unsupported wire dispatched'))
    now = store._now()
    wire([message('I will send the signed form once the client replies.', now=now)])
    snap = {'chat_review': sources._collect_chat_matches(now=now, discovery_enabled=True)}
    assess.evaluate(snap)
    private = snap['chat_review']['discovery']
    assert private.get('evaluation_status') == 'not_configured'
    assert discovery.stage_local_candidates(snap, now=now) == 1
    conn = store._connect()
    try:
        assert conn.execute('SELECT COUNT(*) FROM discovery_evaluation_reservations').fetchone()[0] == 0
    finally:
        conn.close()


def test_openai_api_is_not_treated_as_codex_app_server_eligible(discovery_env, monkeypatch):
    _write_consent()
    pricing_core(monkeypatch, entry())
    _stub_openai_core(monkeypatch, {'provider': 'openai-api', 'default': 'gpt-4o-mini',
        'base_url': 'https://api.openai.com/v1', 'api_mode': 'chat_completions',
        'openai_runtime': ' CODEX_APP_SERVER '}, 'codex_responses')
    route = assess.resolve_profile_default()
    # Direct API provider is not in core's openai_runtime eligibility set. Its
    # explicitly configured Chat Completions mode remains exactly that mode.
    assert route.api_mode == 'chat_completions'
    assert assess.configured() is True


@pytest.mark.parametrize('overlay,named_provider', [
    (None, False), (' CODEX_APP_SERVER ', False),
    (None, True), (' CODEX_APP_SERVER ', True),
])
def test_bare_openai_alias_stays_local_before_metadata_or_reservation(
        discovery_env, monkeypatch, overlay, named_provider):
    _write_consent()
    metadata = pricing_core(monkeypatch, entry())
    model_cfg = {'provider': 'openai-api', 'default': 'gpt-4o-mini',
                 'base_url': 'https://api.openai.com/v1', 'api_mode': 'chat_completions'}
    _stub_openai_core(monkeypatch, model_cfg, 'codex_responses')
    # Positive control: the SAME valid consent and pricing must admit the
    # registered explicit-chat route, otherwise the denied path is vacuous.
    assert assess.configured() is True
    metadata.clear()
    model_cfg = {**model_cfg, 'provider': 'openai'}
    if overlay is not None:
        model_cfg['openai_runtime'] = overlay
    providers = {'openai': {'base_url': 'https://api.openai.com/v1',
                            'api_mode': 'chat_completions'}} if named_provider else None
    _stub_openai_core(monkeypatch, model_cfg, 'codex_responses', providers=providers)
    sent = []
    def fake_send(*a, **kw):
        sent.append(1)
        # Model the late core identity/wire rejection without credentials or I/O.
        raise ValueError('synthetic alias route mismatch')
    monkeypatch.setattr(assess, '_send_runtime', fake_send)
    configured = assess.configured()
    now = store._now()
    wire([message('I will send the signed form once the client replies.', now=now)])
    snap = {'chat_review': sources._collect_chat_matches(now=now, discovery_enabled=True)}
    assess.evaluate(snap)
    staged = discovery.stage_local_candidates(snap, now=now)
    conn = store._connect()
    try:
        reservations = conn.execute('SELECT COUNT(*) FROM discovery_evaluation_reservations').fetchone()[0]
    finally:
        conn.close()
    assert configured is False
    assert snap['chat_review']['discovery'].get('evaluation_status') == 'not_configured'
    assert staged == 1
    assert reservations == 0
    assert sent == []
    assert metadata == []


@pytest.mark.parametrize('ownership', ['providers', 'custom_providers'])
def test_named_openai_api_override_stays_local_before_pricing_or_reservation(
        discovery_env, monkeypatch, ownership):
    # A named providers.openai-api / custom_providers entry can make core pair
    # its credential with a different endpoint/wire after Tingle admits the
    # built-in URL. Reject named/custom ownership BEFORE pricing/reservation.
    _write_consent()
    metadata = pricing_core(monkeypatch, entry())
    model_cfg = {'provider': 'openai-api', 'default': 'gpt-4o-mini',
                 'base_url': 'https://api.openai.com/v1', 'api_mode': 'chat_completions'}
    # Positive control: identical consent/pricing with NO named entry admits.
    _stub_openai_core(monkeypatch, model_cfg, 'chat_completions')
    assert assess.configured() is True
    metadata.clear()
    override = {'base_url': 'https://synthetic-proxy.invalid/v1',
                'api_mode': 'chat_completions', 'api_key': 'SYNTHETIC_PROXY_ONLY_KEY'}
    if ownership == 'providers':
        _stub_openai_core(monkeypatch, model_cfg, 'chat_completions',
                          providers={'openai-api': override}, named_path=('providers', 'openai-api'))
    else:
        _stub_openai_core(monkeypatch, model_cfg, 'chat_completions',
                          custom_providers=[{'name': 'openai-api', **override}],
                          named_path=('custom_providers', 0))
    monkeypatch.setattr(assess, '_send_runtime', lambda *a, **kw: pytest.fail('named override must not dispatch'))
    assert assess.configured() is False
    now = store._now()
    wire([message('I will send the signed form once the client replies.', now=now)])
    snap = {'chat_review': sources._collect_chat_matches(now=now, discovery_enabled=True)}
    assess.evaluate(snap)
    staged = discovery.stage_local_candidates(snap, now=now)
    conn = store._connect()
    try:
        reservations = conn.execute('SELECT COUNT(*) FROM discovery_evaluation_reservations').fetchone()[0]
    finally:
        conn.close()
    assert snap['chat_review']['discovery'].get('evaluation_status') == 'not_configured'
    assert staged == 1
    assert reservations == 0
    assert metadata == []


def test_official_snapshot_enables_exact_direct_default_with_upward_eur_buffer(monkeypatch):
    calls = pricing_core(monkeypatch, entry())
    default = assess.DefaultRoute('openai-api', 'gpt-4o-mini', 'https://api.openai.com/v1', api_key='PRIVATE KEY')
    monkeypatch.setattr(assess, 'resolve_profile_default', lambda: default)
    route = assess.runtime_default_route()
    assert (route.input_microeur_per_token, route.output_microeur_per_token) == (1, 1)
    assert 'official_docs_snapshot' in route.pricing_revision
    assert 'synthetic-snapshot-v1' in route.pricing_revision
    # No credential, source text, or private endpoint goes to metadata resolution.
    assert calls == [('gpt-4o-mini', 'openai', 'https://api.openai.com/v1', None)]
    assert '1.5' in route.pricing_revision and 'PRIVATE' not in repr(route)
    assert all(not cached.api_key for cached in assess._PRICING_CACHE)


@pytest.mark.parametrize('changes', [
    {'per_second_cost': Decimal('1')},
    {'request_cost_above': Decimal('1')},
    {'cache_write_1h_cost_per_million_above': Decimal('1')},
])
def test_future_unknown_price_dimensions_disable_instead_of_underreserve(monkeypatch, changes):
    pricing_core(monkeypatch, entry(**changes))
    with pytest.raises(ValueError):
        assess._pricing(assess.DefaultRoute('openai-api', 'gpt-4o-mini', 'https://api.openai.com/v1'))


_ABSENT_PRICE_FIELD = object()


@pytest.mark.parametrize('value', [_ABSENT_PRICE_FIELD, None])
def test_known_1h_cache_write_field_absent_or_none_is_admitted(monkeypatch, value):
    changes = {} if value is _ABSENT_PRICE_FIELD else {'cache_write_1h_cost_per_million': None}
    pricing_core(monkeypatch, entry(**changes))
    bound = assess._pricing(assess.DefaultRoute('openai-api', 'gpt-4o-mini', 'https://api.openai.com/v1'))
    assert (bound.input_microeur_per_token, bound.output_microeur_per_token) == (1, 1)


@pytest.mark.parametrize('value', [Decimal('0'), Decimal('2'), 'unknown'])
def test_known_1h_cache_write_rate_or_unusable_value_stays_disabled(monkeypatch, value):
    pricing_core(monkeypatch, entry(cache_write_1h_cost_per_million=value))
    with pytest.raises(ValueError, match='disabled'):
        assess._pricing(assess.DefaultRoute('openai-api', 'gpt-4o-mini', 'https://api.openai.com/v1'))


def test_missing_required_price_field_stays_disabled(monkeypatch):
    from types import SimpleNamespace
    values = vars(entry())
    values.pop('tier_threshold_tokens')
    pricing_core(monkeypatch, SimpleNamespace(**values))
    with pytest.raises(ValueError, match='disabled'):
        assess._pricing(assess.DefaultRoute('openai-api', 'gpt-4o-mini', 'https://api.openai.com/v1'))


@pytest.mark.parametrize('changes,expected', [
    ({'cache_read_cost_per_million': Decimal('2.00000001')}, (4, 1)),
    ({'cache_write_cost_per_million': Decimal('3')}, (5, 1)),
    ({'input_cost_per_million': Decimal('0'), 'output_cost_per_million': Decimal('0'),
      'cache_read_cost_per_million': Decimal('0')}, (1, 1)),
    ({'input_cost_per_million': Decimal('1.3333333333333333333333333333333333333333333333333333333333333334')}, (3, 1)),
])
def test_all_caches_and_free_prices_round_up_with_positive_accounting_floor(monkeypatch, changes, expected):
    pricing_core(monkeypatch, entry(**changes))
    from decimal import localcontext, ROUND_DOWN
    with localcontext() as context:
        context.prec = 3
        context.rounding = ROUND_DOWN
        bound = assess._pricing(assess.DefaultRoute('openai-api', 'gpt-4o-mini', 'https://api.openai.com/v1'))
    assert (bound.input_microeur_per_token, bound.output_microeur_per_token) == expected


def test_catalog_prices_with_user_overrides_or_missing_tiers_are_not_trusted(monkeypatch):
    pricing_core(monkeypatch, entry(source='provider_models_api', source_url='https://models.dev',
        pricing_version='models.dev', cache_write_cost_per_million=Decimal('1')))
    with pytest.raises(ValueError):
        assess._pricing(assess.DefaultRoute('openai-api', 'gpt-4o-mini', 'https://api.openai.com/v1'))


def test_provider_models_api_legacy_row_must_be_complete(monkeypatch):
    calls = pricing_core(monkeypatch, entry(source='provider_models_api', source_url='https://api.openai.com/v1/models',
        pricing_version='openai-compatible-models-api', request_cost=Decimal('0'), cache_write_cost_per_million=Decimal('2.1')))
    bound = assess._pricing(assess.DefaultRoute('openai-api', 'gpt-4.1-mini', 'https://api.openai.com/v1'))
    assert (bound.input_microeur_per_token, bound.output_microeur_per_token) == (4, 1)
    assert bound.revision.startswith('provider_models_api:openai-compatible-models-api:') and len(calls) == 1


@pytest.mark.parametrize('field,value', [
    ('input_cost_per_million', None), ('output_cost_per_million', None),
    ('input_cost_per_million', Decimal('-1')), ('output_cost_per_million', Decimal('NaN')),
    ('cache_read_cost_per_million', Decimal('-1')), ('cache_write_cost_per_million', Decimal('Infinity')),
    ('request_cost', Decimal('0.01')), ('request_cost', Decimal('-1')),
    ('request_cost', Decimal('NaN')), ('request_cost', 'unknown'),
    ('source', 'user_override'), ('source', 'none'),
    ('source_url', 'https://untrusted.invalid/prices'), ('pricing_version', ''),
])
def test_unusable_or_request_fee_prices_fail_closed(monkeypatch, field, value):
    pricing_core(monkeypatch, entry(**{field: value}))
    with pytest.raises(ValueError, match='disabled'):
        assess._pricing(assess.DefaultRoute('openai-api', 'gpt-4o-mini', 'https://api.openai.com/v1'))


@pytest.mark.parametrize('changes', [
    {'provider': 'openai'}, {'provider': 'custom'}, {'provider': 'openrouter'}, {'provider': 'openai-codex'},
    {'model': 'unknown-model'}, {'model': 'gpt-6.1-sol-pro'},
    {'base_url': 'https://proxy.invalid/v1'}, {'base_url': 'http://api.openai.com/v1'},
    {'base_url': 'https://api.openai.com/v2'}, {'base_url': 'https://api.openai.com:444/v1'},
    {'base_url': 'https://key' + chr(64) + 'api.openai.com/v1'}, {'base_url': 'https://api.openai.com/v1?key=PRIVATE'},
    {'api_mode': 'codex_responses'}, {'provider': 'anthropic'},
])
def test_unknown_endpoint_subscription_model_or_wire_rejected_before_metadata(monkeypatch, changes):
    from dataclasses import replace
    calls = pricing_core(monkeypatch, entry())
    default = assess.DefaultRoute('openai-api', 'gpt-4o-mini', 'https://api.openai.com/v1')
    with pytest.raises(ValueError):
        assess._pricing(replace(default, **changes))
    assert calls == []


def tiered_entry(**changes):
    return entry(tier_threshold_tokens=272000, input_cost_per_million_above=Decimal('4'),
        output_cost_per_million_above=Decimal('15'), cache_read_cost_per_million_above=Decimal('0.2'),
        cache_write_cost_per_million_above=Decimal('5'), cache_write_cost_per_million=Decimal('2.5'), **changes)


def test_context_tiers_and_write_cache_use_max_for_whole_reservation(monkeypatch):
    pricing_core(monkeypatch, tiered_entry())
    bound = assess._pricing(assess.DefaultRoute('openai-api', 'gpt-6.1-sol', 'https://api.openai.com/v1'))
    assert (bound.input_microeur_per_token, bound.output_microeur_per_token) == (8, 23)


@pytest.mark.parametrize('value', [None, entry(source='provider_models_api', source_url='https://models.dev'),
    entry(cache_write_cost_per_million=Decimal('2.5'))])
def test_unknown_or_incomplete_context_tier_stays_disabled(monkeypatch, value):
    pricing_core(monkeypatch, value)
    with pytest.raises(ValueError):
        assess._pricing(assess.DefaultRoute('openai-api', 'gpt-6.1-sol', 'https://api.openai.com/v1'))


def test_pricing_network_never_runs_under_source_lock_and_expiry_fails_closed(monkeypatch):
    import hermes_tingle_store as store
    from datetime import timedelta
    calls = pricing_core(monkeypatch, entry())
    default = assess.DefaultRoute('openai-api', 'gpt-4o-mini', 'https://api.openai.com/v1')
    with store.serialized_operation('sources'):
        with pytest.raises(ValueError): assess._pricing(default)
    assert calls == []
    assess._pricing(default)
    assert len(calls) == 1
    with store.serialized_operation('sources'):
        assess._pricing(default)
    now = store._now() + timedelta(hours=2)
    monkeypatch.setattr(store, '_now', lambda *a: now)
    with store.serialized_operation('sources'):
        with pytest.raises(ValueError): assess._pricing(default)
    assert len(calls) == 1


def test_buffered_pricing_provenance_is_retained_without_content(evaluation_env, monkeypatch):
    import json
    import hermes_tingle_store as store
    import hermes_tingle_discovery as discovery
    from test_discovery_assess import snapshot_for
    from dataclasses import replace
    _, calls, path = evaluation_env
    settings = json.loads(path.read_text())
    settings['private_chat_evaluation'].update(route='runtime-default')
    settings['private_chat_evaluation'].pop('model')
    settings['private_chat_evaluation'].pop('pricing_revision')
    path.write_text(json.dumps(settings))
    pricing_core(monkeypatch, entry())
    monkeypatch.setattr(assess, 'resolve_profile_default', lambda: assess.DefaultRoute(
        'openai-api', 'gpt-4o-mini', 'https://api.openai.com/v1'))
    original = assess.runtime_default_route
    def runtime():
        def send(request, **kw):
            calls.append(1)
            entries = json.loads(request)['input']['excerpts']
            return assess.TransportResult(json.dumps({'results': [dict(id=e['id'], decision='drop', label=None, evidence=None)
                for e in entries]}), 100, 20)
        return replace(original(), send=send)
    monkeypatch.setattr(assess, 'runtime_default_route', runtime)
    snapshot = snapshot_for(['I will send the signed form'])
    assess.evaluate(snapshot)
    assert discovery.stage_local_candidates(snapshot) == 0
    summary = assess.spend_summary()
    assert calls == [1] and summary['estimated_microeur'] == 120
    assert summary['reserved_microeur'] == 24096
    assert 'official_docs_snapshot:synthetic-snapshot-v1:eur-usd1.5' in summary['recent'][0]['pricing']
    assert 'signed form' not in json.dumps(summary) and 'telegram:' not in json.dumps(summary)
    text = assess.format_spend()
    assert '1.5 EUR/USD' in text and 'not invoices' in text and 'margin' in text
    # Source/debug purge cannot refund a reservation or its public provenance.
    import hermes_tingle_debug as debug
    debug.purge()
    assert assess.spend_summary()['recent'][0]['pricing'] == summary['recent'][0]['pricing']
    from datetime import timedelta
    conn = store._connect()
    try:
        discovery.cleanup(conn, now=store._now() + timedelta(days=63))
        conn.commit()
        assert conn.execute('SELECT COUNT(*) FROM discovery_evaluation_prices').fetchone()[0] == 0
    finally:
        conn.close()


def test_models_api_unknown_request_fee_does_not_become_zero(monkeypatch):
    pricing_core(monkeypatch, entry(source='provider_models_api', source_url='https://api.openai.com/v1/models',
        cache_write_cost_per_million=Decimal('0'), request_cost=None))
    with pytest.raises(ValueError):
        assess._pricing(assess.DefaultRoute('openai-api', 'gpt-4o-mini', 'https://api.openai.com/v1'))


@pytest.mark.parametrize('bad', [None, Decimal('-1'), Decimal('NaN')])
def test_models_api_ambiguous_cache_rates_are_not_treated_as_free(monkeypatch, bad):
    pricing_core(monkeypatch, entry(source='provider_models_api', source_url='https://api.openai.com/v1/models',
        cache_read_cost_per_million=bad, cache_write_cost_per_million=Decimal('0'), request_cost=Decimal('0')))
    with pytest.raises(ValueError):
        assess._pricing(assess.DefaultRoute('openai-api', 'gpt-4o-mini', 'https://api.openai.com/v1'))


@pytest.mark.parametrize('cap', [24096, 24095])
def test_buffered_default_enforces_configured_lower_cap_without_retry_or_local_fallback(evaluation_env, monkeypatch, cap):
    import json
    from dataclasses import replace
    import hermes_tingle_discovery as discovery
    from test_discovery_assess import snapshot_for
    _, calls, path = evaluation_env
    settings = json.loads(path.read_text())
    settings['private_chat_evaluation'] = {k: v for k, v in settings['private_chat_evaluation'].items()
                                           if k not in ('model', 'pricing_revision')}
    settings['private_chat_evaluation'].update(route='runtime-default', monthly_limit_microeur=cap)
    path.write_text(json.dumps(settings))
    pricing_core(monkeypatch, entry())
    monkeypatch.setattr(assess, 'resolve_profile_default', lambda: assess.DefaultRoute(
        'openai-api', 'gpt-4o-mini', 'https://api.openai.com/v1'))
    runtime = assess.runtime_default_route
    def fail(request, **kw):
        calls.append(1)
        raise TimeoutError('PRIVATE ERROR')
    monkeypatch.setattr(assess, 'runtime_default_route', lambda: replace(runtime(), send=fail))
    for text in ['I will send the signed form', 'I will arrange a synthetic meeting']:
        snapshot = snapshot_for([text])
        assess.evaluate(snapshot)
        assess.evaluate(snapshot)  # Same snapshot cannot retry.
        assert discovery.stage_local_candidates(snapshot) == 0
        assert snapshot['chat_review']['discovery']['evaluation_status'] == 'failed'
    assert calls == ([1] if cap == 24096 else [])
    summary = assess.spend_summary()
    assert summary['reserved_microeur'] <= min(cap, 5_000_000)
    assert summary['estimated_microeur'] == summary['reserved_microeur']


def test_unpriced_direct_default_preserves_local_cue_with_no_reservation(discovery_env, monkeypatch):
    import json
    import hermes_constants
    import hermes_tingle_discovery as discovery
    from test_discovery_assess import snapshot_for, counts
    path = hermes_constants.get_hermes_home() / 'hermes-tingle.json'
    settings = json.loads(path.read_text())
    settings['private_chat_evaluation'] = dict(enabled=True, consent='owner-dm-excerpts-v1',
        approved_by='telegram:42', route='runtime-default', monthly_limit_microeur=5_000_000)
    path.write_text(json.dumps(settings))
    pricing_core(monkeypatch, None)
    monkeypatch.setattr(assess, 'resolve_profile_default', lambda: assess.DefaultRoute(
        'openai-api', 'gpt-4o-mini', 'https://api.openai.com/v1'))
    monkeypatch.setattr(assess, '_send_runtime', lambda *a, **kw: pytest.fail('unpriced dispatch'))
    snapshot = snapshot_for(['I will send the signed form'])
    assess.evaluate(snapshot)
    assert discovery.stage_local_candidates(snapshot) == 1
    assert counts()['discovery_evaluation_reservations'] == 0


@pytest.mark.parametrize('mode', ['snapshot', 'models_api', 'variable_cache', 'variable_request'])
def test_installed_core_resolver_with_only_fake_metadata(monkeypatch, mode):
    """Opt-in source compatibility: load code, NEVER import config/auth/network."""
    import os
    import importlib.util
    from pathlib import Path
    source = os.environ.get('HERMES_PRICING_SOURCE')
    if not source:
        try:
            spec = importlib.util.find_spec('agent.usage_pricing')
        except ModuleNotFoundError:
            spec = None
        source = spec.origin if spec and spec.origin and spec.origin.endswith('.py') else None
    if not source:
        pytest.skip('source-only core compatibility test needs HERMES_PRICING_SOURCE or an installed Hermes core')
    metadata = ModuleType('agent.model_metadata')
    calls = []
    def fetch(base_url, api_key=''):
        calls.append((base_url, api_key))
        rates = dict(prompt='0.000001', completion='0.000002', cache_read='0.0000001', cache_write='0.000003', request='0')
        if mode == 'variable_cache': rates['cache_write'] = '-1'
        if mode == 'variable_request': rates['request'] = '-1'
        return {'gpt-4o-mini': {'pricing': rates}}
    metadata.fetch_endpoint_model_metadata = fetch
    metadata.fetch_model_metadata = lambda *a, **kw: pytest.fail('unintended OpenRouter metadata')
    metadata.strip_codex_context_variant_suffix = lambda name: name
    monkeypatch.setitem(sys.modules, 'agent.model_metadata', metadata)
    models = ModuleType('agent.models_dev')
    models.get_model_info = lambda *a, **kw: None
    monkeypatch.setitem(sys.modules, 'agent.models_dev', models)
    utils = ModuleType('utils')
    from urllib.parse import urlsplit
    utils.base_url_hostname = lambda url: urlsplit(url).hostname
    utils.base_url_host_matches = lambda url, domain: urlsplit(url).hostname == domain
    utils.base_url_origin = lambda url: (urlsplit(url).scheme, urlsplit(url).hostname, urlsplit(url).port or 443)
    monkeypatch.setitem(sys.modules, 'utils', utils)
    spec = importlib.util.spec_from_file_location('agent.usage_pricing', Path(source))
    core = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, 'agent.usage_pricing', core)
    monkeypatch.setattr(sys, 'dont_write_bytecode', True)  # Core source is read-only, including pycache.
    spec.loader.exec_module(core)
    monkeypatch.setattr(assess, '_PRICING_CACHE', {})
    if mode != 'snapshot': core._OFFICIAL_DOCS_PRICING.pop(('openai', 'gpt-4o-mini'))
    default = assess.DefaultRoute('openai-api', 'gpt-4o-mini', 'https://api.openai.com/v1')
    if mode.startswith('variable'):
        with pytest.raises(ValueError): assess._pricing(default)
    else:
        bound = assess._pricing(default)
        assert (bound.input_microeur_per_token, bound.output_microeur_per_token) == ((1, 1) if mode == 'snapshot' else (5, 3))
    assert calls == ([] if mode == 'snapshot' else [('https://api.openai.com/v1', '')])
