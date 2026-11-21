"""Default route/transport tests are synthetic: never contact a model."""
import json

import pytest

import hermes_tingle_discovery as discovery
import hermes_tingle_discovery_assess as assess
import hermes_tingle_store as store
from test_chat_discovery import discovery_env
from test_discovery_assess import snapshot_for, counts, evaluation_env, set_provider


from hermes_tingle_config import private_chat_evaluation_policy


@pytest.mark.parametrize('limit,valid', [(5_000_000, True), (5_000_001, False), (True, False)])
def test_runtime_default_policy_has_five_euro_ceiling(limit, valid):
    policy = {'enabled': True, 'consent': 'owner-dm-excerpts-v1', 'approved_by': 'telegram:42',
              'route': 'runtime-default', 'monthly_limit_microeur': limit}
    settings = {'owner_principals': ['telegram:42'], 'private_chat_evaluation': policy}
    assert (private_chat_evaluation_policy(settings) == policy) is valid


@pytest.mark.parametrize('state', ['absent', 'unverified'])
def test_unconfigured_evaluator_preserves_conservative_local_discovery(discovery_env, monkeypatch, state):
    if state == 'unverified':
        import hermes_constants
        path = hermes_constants.get_hermes_home() / 'hermes-tingle.json'
        settings = json.loads(path.read_text())
        settings['private_chat_evaluation'] = {
            'enabled': True, 'consent': 'owner-dm-excerpts-v1', 'approved_by': 'telegram:42',
            'route': 'runtime-default', 'monthly_limit_microeur': 5_000_000,
        }
        path.write_text(json.dumps(settings))
    snapshot = snapshot_for(['I will send the signed form next week.',
                             'The signed form still needs sending',
                             'I will not send the form',
                             'She said "I will send the form"'])
    assess.evaluate(snapshot)
    assert discovery.stage_local_candidates(snapshot) == 1
    assert snapshot['chat_review']['discovery']['evaluation_status'] == 'not_configured'
    assert counts()['discovery_evaluation_reservations'] == 0
    assert store.list_items() == []


def test_attempt_failure_never_reenters_local_cue_path(evaluation_env, monkeypatch):
    calls = []
    def fail(*a, **kw):
        calls.append(1)
        raise TimeoutError('private provider content')
    set_provider(evaluation_env, monkeypatch, fail)
    snapshot = snapshot_for(['I will send the signed form next week.'])
    assess.evaluate(snapshot)
    assert snapshot['chat_review']['discovery']['evaluation_status'] == 'failed'
    assert discovery.stage_local_candidates(snapshot) == 0
    assert len(calls) == 1


def install_core(monkeypatch, config, *, detected='detected-provider', default='detected-model'):
    """Model only Hermes's read-only routing seams, never load live config/auth."""
    import sys
    from types import ModuleType
    from contextlib import nullcontext
    values = {
        'hermes_cli.config': {'load_config_readonly': lambda: config},
        'hermes_cli.auth': {'resolve_provider': lambda requested=None: detected},
        'hermes_cli.models': {'get_default_model_for_provider': lambda provider: default},
        'hermes_cli.providers': {'get_provider': lambda *a, **kw: type('P', (), {'base_url': 'https://api.example.invalid/v1'})(),
                                 'determine_api_mode': lambda provider, base, model: 'chat_completions'},
        'hermes_cli.model_switch': {'resolve_startup_model_route': lambda *a, **kw: None},
        'hermes_cli.runtime_provider_custom': {'named_custom_provider_config_path': lambda provider, config: None},
    }
    for name, attrs in values.items():
        mod = ModuleType(name)
        mod.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, mod)


@pytest.mark.parametrize('model,provider,expected', [
    ('profile-model', 'profile-provider', ('profile-provider', 'profile-model')),
    ('profile-model', '', ('detected-provider', 'profile-model')),
    ('', 'profile-provider', ('profile-provider', 'detected-model')),
])
def test_default_resolves_this_profiles_model_and_provider(monkeypatch, model, provider, expected):
    config = {'model': {'default': model, 'provider': provider, 'base_url': 'https://api.example.invalid/v1'}}
    install_core(monkeypatch, config)
    route = assess.resolve_profile_default()
    assert (route.provider, route.model) == expected
    assert route.base_url == 'https://api.example.invalid/v1'
    config['model']['default'] = 'second-profile-model'
    assert assess.resolve_profile_default().model == 'second-profile-model'


def test_runtime_default_unknown_price_is_not_a_route(monkeypatch):
    install_core(monkeypatch, {'model': {'default': 'unknown-model', 'provider': 'own-provider'}})
    settings = {'owner_principals': ['telegram:42'], 'private_chat_evaluation': {
        'enabled': True, 'consent': 'owner-dm-excerpts-v1', 'approved_by': 'telegram:42',
        'route': 'runtime-default', 'monthly_limit_microeur': 5_000_000}}
    with pytest.raises(ValueError, match='disabled'):
        assess._route(settings)


def test_profile_route_constructs_concrete_transport_only_with_verified_bounds(monkeypatch):
    from datetime import timedelta
    install_core(monkeypatch, {'model': {'default': 'own-model', 'provider': 'own-provider',
                                        'base_url': 'https://api.example.invalid/v1'}})
    default = assess.resolve_profile_default()
    bound = assess.PricingBound('synthetic-reviewed-v1', 1, 2,
                               (store._now() + timedelta(days=1)).isoformat())
    monkeypatch.setattr(assess, '_pricing', lambda route: bound)
    route = assess.runtime_default_route()
    assert route.model == 'own-model' and route.route == 'runtime-default'
    assert route.input_microeur_per_token == 1 and route.output_microeur_per_token == 2
    assert callable(route.send) and route.identity


@pytest.mark.parametrize('fault', [None, 'closed_socket', 'close_error', 'redirect', 'rate_limit', 'server', 'oversized', 'timeout', 'tools'])
def test_concrete_transport_exactly_one_bounded_tool_free_post(monkeypatch, capsys, fault):
    import io
    import http.client
    from datetime import timedelta
    from types import SimpleNamespace
    install_core(monkeypatch, {'model': {'default': 'own-model', 'provider': 'own-provider',
                                        'base_url': 'https://api.example.invalid/v1'}})
    default = assess.resolve_profile_default()
    bound = assess.PricingBound('synthetic-reviewed-v1', 1, 2,
                               (store._now() + timedelta(days=1)).isoformat())
    monkeypatch.setattr(assess, '_pricing', lambda route: bound)
    monkeypatch.setattr(assess, '_runtime_credentials', lambda route: 'synthetic-key')
    requests, connections = [], []
    class Connection:
        def __init__(self, host, port=None, **kw):
            assert host == 'api.example.invalid' and kw['timeout'] <= 30
            connections.append(self)
            self.sock = SimpleNamespace(settimeout=lambda timeout: None)
        def connect(self): pass
        def close(self):
            if fault == 'close_error': raise ValueError('PRIVATE CLOSE ERROR')
        def request(self, method, path, body, headers):
            assert self.auto_open == 0  # A timeout-close must never trigger an implicit reconnect.
            requests.append((method, path, json.loads(body), headers))
        def getresponse(self):
            if fault == 'closed_socket': self.sock = None
            if fault == 'timeout': raise TimeoutError('PRIVATE EXCERPT')
            message = {'content': '{"results": []}'}
            if fault == 'tools': message['tool_calls'] = [{'function': 'forbidden'}]
            data = json.dumps({'choices': [{'message': message, 'finish_reason': 'stop'}]}).encode()
            if fault == 'oversized': data = b'x' * (assess.MAX_RESPONSE_BYTES + 1)
            return SimpleNamespace(status={'redirect': 307, 'rate_limit': 429, 'server': 503}.get(fault, 200),
                                   read1=io.BytesIO(data).read)
    monkeypatch.setattr(http.client, 'HTTPSConnection', Connection)
    route = assess.runtime_default_route()
    request = assess._envelope(route, [{'id': 'a' * 32, 'excerpt': 'PRIVATE EXCERPT', 'clipped': False}], [])
    if fault not in (None, 'closed_socket', 'close_error'):
        with pytest.raises(ValueError, match='Private transport unavailable'):
            route.send(request, timeout_seconds=30, max_response_bytes=assess.MAX_RESPONSE_BYTES)
    else:
        assert route.send(request, timeout_seconds=30, max_response_bytes=assess.MAX_RESPONSE_BYTES).content == '{"results": []}'
    assert len(requests) == len(connections) == 1
    method, path, sent, headers = requests[0]
    assert method == 'POST' and path == '/v1/chat/completions'
    assert sent['model'] == 'own-model'
    assert sent['tools'] == [] and sent['tool_choice'] == 'none'
    assert sent['n'] == 1 and sent['stream'] is False and sent['store'] is False
    assert sent['service_tier'] == 'default'
    assert sent['max_completion_tokens'] == assess.MAX_OUTPUT_TOKENS
    assert sent['response_format']['type'] == 'json_schema'
    assert sent['response_format']['json_schema']['strict'] is True
    assert headers['Authorization'] == 'Bearer ' + 'synthetic-key'
    assert capsys.readouterr() == ('', '')


def test_legacy_cues_not_starved_by_broad_unevaluated_text_or_seen_cues(discovery_env):
    from test_chat_discovery import wire, message
    now = store._now()
    texts = ['Informational text ' + str(i) for i in range(60)] + ['I will send the signed form next week']
    messages = [message(text, now=now) for text in texts]
    wire(messages)
    import hermes_tingle_sources as sources
    snapshot = {'chat_review': sources._collect_chat_matches(now=now, discovery_enabled=True)}
    assess.evaluate(snapshot)
    assert discovery.stage_local_candidates(snapshot) == 1
    wire(messages + [message('I will arrange a synthetic meeting', now=now)])
    again = {'chat_review': sources._collect_chat_matches(now=now, discovery_enabled=True)}
    assess.evaluate(again)
    assert discovery.stage_local_candidates(again) == 1
    assert counts()['hermes_tingle_chat_discovery_candidates'] == 2


@pytest.mark.parametrize('when', ['dispatch', 'persist'])
def test_resolved_route_change_revokes_assessment(evaluation_env, monkeypatch, when):
    from dataclasses import replace
    snapshot = snapshot_for(['I will send the signed form next week'])
    old = assess.VERIFIED_ROUTES['synthetic-fixed']
    def change():
        monkeypatch.setitem(assess.VERIFIED_ROUTES, old.route, replace(old, identity='changed-profile-route'))
    if when == 'dispatch':
        reserve = assess._reserve
        def wrapped(*a, **kw):
            result = reserve(*a, **kw)
            change()
            return result
        monkeypatch.setattr(assess, '_reserve', wrapped)
    assess.evaluate(snapshot)
    if when == 'persist': change()
    try: discovery.stage_local_candidates(snapshot)
    except ValueError: pass
    assert counts()['hermes_tingle_chat_discovery_candidates'] == 0


def test_transport_inherits_profile_context_and_revalidates_after_auth(monkeypatch, capsys):
    import contextvars
    import http.client
    from datetime import timedelta
    from types import SimpleNamespace
    profile = contextvars.ContextVar('synthetic_profile', default='WRONG')
    token = profile.set('own-profile')
    try:
        install_core(monkeypatch, {'model': {'default': 'own-model', 'provider': 'own-provider',
                                            'base_url': 'https://api.example.invalid/v1'}})
        default = assess.resolve_profile_default()
        monkeypatch.setattr(assess, '_pricing', lambda route:
            assess.PricingBound('synthetic-v1', 1, 2, 'synthetic-stable'))
        events = []
        def credentials(route):
            events.append(('auth', profile.get()))
            return 'synthetic-key'
        monkeypatch.setattr(assess, '_runtime_credentials', credentials)
        class Connection:
            def __init__(self, *a, **kw):
                self.sock = SimpleNamespace(settimeout=lambda t: None)
            def connect(self): events.append(('connect', profile.get()))
            def request(self, *a, **kw): events.append(('SENT', profile.get()))
            def close(self): pass
        monkeypatch.setattr(http.client, 'HTTPSConnection', Connection)
        def revoke_check():
            events.append(('revalidate', profile.get()))
            raise ValueError('PRIVATE REVOCATION')
        route = assess.runtime_default_route()
        request = assess._envelope(route, [{'id': 'a'*32, 'excerpt': 'PRIVATE', 'clipped': False}], [])
        with pytest.raises(ValueError, match='Private transport unavailable'):
            route.send(request, timeout_seconds=30, max_response_bytes=32768, before_dispatch=revoke_check)
        assert events == [('auth', 'own-profile'), ('connect', 'own-profile'), ('revalidate', 'own-profile')]
        assert capsys.readouterr() == ('', '')
    finally:
        profile.reset(token)


def test_evaluate_supplies_last_dispatch_revalidation_after_transport_setup(evaluation_env, monkeypatch):
    from dataclasses import replace
    _, calls, policy_path = evaluation_env
    policy = json.loads(policy_path.read_text())
    value = policy['private_chat_evaluation']
    value.update(route='runtime-default')
    value.pop('model')
    value.pop('pricing_revision')
    policy_path.write_text(json.dumps(policy))
    old = assess.VERIFIED_ROUTES['synthetic-fixed']
    callbacks = []
    def send(request, *, before_dispatch=None, **kw):
        callbacks.append(callable(before_dispatch))
        assert callable(before_dispatch)
        store.exclude_chat('telegram:42', excluded_by='synthetic revocation after auth')
        before_dispatch()
        calls.append('NETWORK SENT')
        return '{}'
    monkeypatch.setattr(assess, 'runtime_default_route', lambda: replace(old, route='runtime-default', send=send))
    snapshot = snapshot_for(['I will send the signed form next week'])
    assess.evaluate(snapshot)
    assert calls == [] and callbacks == [True]
    assert snapshot['chat_review']['discovery']['evaluation_status'] == 'failed'
    assert discovery.stage_local_candidates(snapshot) == 0


@pytest.mark.parametrize('change', ['endpoint', 'mode', 'provider', 'actual_provider', 'missing_key'])
def test_core_auth_resolution_cannot_reroute_or_fallback(monkeypatch, change):
    import sys
    from types import ModuleType
    from contextlib import nullcontext
    install_core(monkeypatch, {'model': {'default': 'own-model', 'provider': 'own-provider',
                                        'base_url': 'https://api.example.invalid/v1'}})
    default = assess.resolve_profile_default()
    runtime = {'provider': 'own-provider', 'requested_provider': 'own-provider', 'base_url': default.base_url,
               'api_mode': 'chat_completions', 'api_key': 'synthetic-key'}
    field, value = {'endpoint': ('base_url', 'https://elsewhere.invalid/v1'),
                    'mode': ('api_mode', 'codex_responses'), 'provider': ('requested_provider', 'fallback'),
                    'actual_provider': ('provider', 'custom'),
                    'missing_key': ('api_key', '')}[change]
    runtime[field] = value
    calls = []
    module = ModuleType('hermes_cli.runtime_provider')
    module.frozen_runtime_config = lambda cfg: nullcontext()
    def resolve(**kw):
        calls.append(kw)
        return runtime
    module.resolve_runtime_provider = resolve
    monkeypatch.setitem(sys.modules, 'hermes_cli.runtime_provider', module)
    with pytest.raises(ValueError, match='Private transport unavailable'):
        assess._runtime_credentials(default)
    assert len(calls) == 1 and calls[0]['requested'] == 'own-provider'


def test_absolute_deadline_prevents_late_connect_dispatch(monkeypatch, capsys):
    import http.client
    import threading
    import time
    from datetime import timedelta
    from types import SimpleNamespace
    install_core(monkeypatch, {'model': {'default': 'own-model', 'provider': 'own-provider',
                                        'base_url': 'https://api.example.invalid/v1'}})
    default = assess.resolve_profile_default()
    monkeypatch.setattr(assess, '_pricing', lambda route:
        assess.PricingBound('synthetic-v1', 1, 2, 'synthetic-stable'))
    monkeypatch.setattr(assess, '_runtime_credentials', lambda route: 'synthetic-key')
    release, finished = threading.Event(), threading.Event()
    calls = []
    class Connection:
        def __init__(self, *a, **kw):
            self.sock = SimpleNamespace(settimeout=lambda timeout: None)
        def connect(self): release.wait(timeout=2)
        def request(self, *a, **kw): calls.append('LATE REQUEST')
        def close(self):
            if release.is_set(): finished.set()
    monkeypatch.setattr(http.client, 'HTTPSConnection', Connection)
    route = assess.runtime_default_route()
    request = assess._envelope(route, [{'id': 'a'*32, 'excerpt': 'PRIVATE', 'clipped': False}], [])
    started = time.monotonic()
    with pytest.raises(ValueError, match='Private transport unavailable'):
        route.send(request, timeout_seconds=0.05, max_response_bytes=32768)
    assert time.monotonic()-started < 1
    release.set()
    assert finished.wait(timeout=2)
    assert calls == [] and capsys.readouterr() == ('', '')
