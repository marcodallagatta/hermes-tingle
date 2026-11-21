"""Owner-facing evaluator consent, exercised via registered slash dispatch.

Only synthetic profile snapshots and a fake synchronous chooser are used.
"""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import hermes_tingle as plugin
from hermes_tingle_config import private_chat_evaluation_policy


CONSENT = {'enabled': True, 'consent': 'owner-dm-excerpts-v1', 'approved_by': 'telegram:42',
           'route': 'runtime-default', 'monthly_limit_microeur': 5_000_000}


@pytest.fixture
def consent_env(tmp_path, monkeypatch):
    path = tmp_path / 'hermes-tingle.json'
    settings = json.loads(path.read_text())
    settings.update(owner_principals=['telegram:42'],
                    opaque_extension={'preserve': [False, 7, 'synthetic']})
    path.write_text(json.dumps(settings))
    source = SimpleNamespace(platform='telegram', user_id='42', chat_id='42', chat_type='dm', is_bot=False)

    class Context:
        answer = 'cancel'
        def __init__(self): self.questions = []
        def register_tool(self, **kwargs): pass
        def register_command(self, name, **kwargs): self.command = kwargs['handler']
        def register_skill(self, *args): pass
        def ask_choice(self, question, choices):
            self.questions.append((question, choices))
            return self.answer

    ctx = Context()
    monkeypatch.setattr(plugin, 'integrate', lambda _: None)
    spec = importlib.util.spec_from_file_location('synthetic_consent_plugin', Path(__file__).parents[1] / '__init__.py')
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    entry.register(ctx)
    # Consent commands must bypass generic command observers and never open the DB,
    # resolve pricing/routes, probe sources, schedule work, or call a model.
    def forbidden(*args, **kwargs): pytest.fail('consent is policy-only')
    import hermes_tingle_discovery as discovery
    import hermes_tingle_sources as sources
    import hermes_tingle_store as store
    import hermes_tingle_discovery_assess as runtime
    monkeypatch.setattr(store, 'plugin_db', forbidden)
    monkeypatch.setattr(discovery, 'observe_source', forbidden)
    monkeypatch.setattr(sources, 'probe_source', forbidden)
    monkeypatch.setattr(sources, 'collect_snapshot', forbidden)
    monkeypatch.setattr(runtime, 'resolve_profile_default', forbidden)
    return path, settings, source, ctx


def test_enable_discloses_then_saves_exact_valid_consent_only(consent_env):
    path, before, source, ctx = consent_env
    raw = path.read_bytes()
    def enable(question, choices):
        assert path.read_bytes() == raw
        for text in ('OFF', 'bounded', 'eligible owner-DM', 'tool-free', 'default model/provider',
                     'verified Hermes pricing', '1.5 EUR/USD', 'local cues', 'cost nothing',
                     'not an invoice', '€5', 'fail', 'no retries or fallback', 'chosen provider'):
            assert text in question
        assert {c['value'] for c in choices} == {'enable', 'no-cap', 'cancel'}
        return 'enable'
    ctx.ask_choice = enable
    out = ctx.command('evaluation enable', source=source)
    saved = json.loads(path.read_text())
    assert 'consent enabled' in out
    assert saved == {**before, 'private_chat_evaluation': CONSENT}
    assert private_chat_evaluation_policy(saved) == CONSENT
    assert path.stat().st_mode & 0o777 == 0o600
    backups = list(path.parent.glob('.tingle-policy-backup-*'))
    assert len(backups) == 1 and backups[0].read_bytes() == raw


@pytest.mark.parametrize('answer', ['cancel', None, '', 'yes', True, 'unexpected'])
def test_enable_non_enable_choice_preserves_exact_snapshot(consent_env, answer):
    path, before, source, ctx = consent_env
    raw = path.read_bytes()
    ctx.answer = answer
    out = ctx.command('evaluation enable', source=source)
    assert len(ctx.questions) == 1
    assert 'nothing changed' in out.lower()
    assert path.read_bytes() == raw and not list(path.parent.glob('.tingle-policy-backup-*'))


@pytest.mark.parametrize('enabled', [False, True])
def test_status_reports_consent_only_without_chooser_or_route_lookup(consent_env, enabled):
    path, before, source, ctx = consent_env
    if enabled:
        path.write_text(json.dumps({**before, 'private_chat_evaluation': CONSENT}))
    raw = path.read_bytes()
    assert ctx.command('evaluation status', source=source) == 'Private discovery evaluation consent: ' + ('enabled.' if enabled else 'disabled.')
    assert path.read_bytes() == raw and not ctx.questions


@pytest.mark.parametrize('enabled', [False, True])
def test_disable_removes_only_nested_policy_and_mentions_inflight(consent_env, enabled):
    path, before, source, ctx = consent_env
    if enabled:
        path.write_text(json.dumps({**before, 'private_chat_evaluation': CONSENT}))
    out = ctx.command('evaluation disable', source=source)
    assert 'consent disabled' in out and 'already-dispatched request may finish' in out and 'no new ones start' in out
    assert json.loads(path.read_text()) == before and not ctx.questions


@pytest.mark.parametrize('command', ['status', 'enable', 'disable'])
@pytest.mark.parametrize('change', [
    {'chat_type': 'group'}, {'chat_type': 'supergroup'}, {'chat_type': 'unknown'},
    {'chat_type': ''}, {'chat_type': None}, {'chat_id': ''}, {'chat_id': None},
    {'is_bot': True}, {'user_id': ''}, {'platform': ''},
])
def test_unverified_dm_refuses_before_any_policy_read(consent_env, monkeypatch, command, change):
    path, before, source, ctx = consent_env
    # Even the generic load_settings/observation path must not run before the DM gate.
    def forbidden(*a, **k): pytest.fail('unverified sender must not read policy')
    monkeypatch.setattr(plugin, 'load_settings', forbidden)
    import hermes_tingle_policy as policy
    monkeypatch.setattr(policy, 'snapshot', forbidden)
    source = SimpleNamespace(**{**vars(source), **change})
    raw = path.read_bytes()
    out = ctx.command('evaluation ' + command, source=source)
    assert 'verified' in out and 'nothing changed' in out.lower()
    assert path.read_bytes() == raw and not ctx.questions


@pytest.mark.parametrize('command', ['status', 'enable', 'disable'])
@pytest.mark.parametrize('mode', ['stranger', 'no-owner', 'no-source', 'missing-type', 'missing-chat'])
def test_only_configured_owner_with_positive_dm_metadata_can_use_commands(consent_env, command, mode):
    path, before, source, ctx = consent_env
    if mode == 'stranger': source.user_id = '99'
    elif mode == 'no-owner': path.write_text(json.dumps({**before, 'owner_principals': []}))
    elif mode == 'no-source': source = None
    elif mode == 'missing-type': del source.chat_type
    else: del source.chat_id
    raw = path.read_bytes()
    out = ctx.command('evaluation ' + command, source=source)
    assert 'nothing changed' in out.lower()
    assert 'consent enabled' not in out and 'consent disabled' not in out
    assert path.read_bytes() == raw and not ctx.questions


@pytest.mark.parametrize('command', ['status', 'enable', 'disable'])
@pytest.mark.parametrize('mode', ['invalid-json', 'non-object', 'missing-policy', 'invalid-policy', 'invalid-consent', 'extra-runtime-key'])
def test_malformed_policy_refuses_without_disclosure_or_write(consent_env, command, mode):
    path, before, source, ctx = consent_env
    if mode == 'invalid-json': path.write_text('{broken synthetic')
    elif mode == 'non-object': path.write_text('[]')
    elif mode == 'missing-policy': path.unlink()
    elif mode == 'invalid-policy': path.write_text(json.dumps({'owner_principals': ['telegram:42']}))
    else:
        value = {**CONSENT, 'enabled': 'true'} if mode == 'invalid-consent' else {**CONSENT, 'model': 'unexpected'}
        path.write_text(json.dumps({**before, 'private_chat_evaluation': value}))
    raw = path.read_bytes() if path.exists() else None
    out = ctx.command('evaluation ' + command, source=source)
    assert 'stopped' in out.lower() and 'nothing changed' in out.lower()
    assert (path.read_bytes() if path.exists() else None) == raw and not ctx.questions


def test_enable_reconsent_preserves_existing_lower_cap(consent_env):
    path, before, source, ctx = consent_env
    lower = {**CONSENT, 'monthly_limit_microeur': 321_000}
    path.write_text(json.dumps({**before, 'private_chat_evaluation': lower}))
    ctx.answer = 'enable'
    assert 'consent enabled' in ctx.command('evaluation enable', source=source)
    assert json.loads(path.read_text()) == {**before, 'private_chat_evaluation': lower}


def test_enable_uses_shared_writer_with_exact_snapshot_and_no_lock_during_choice(consent_env, monkeypatch):
    import hermes_tingle_policy as policy
    import hermes_tingle_resources as resources
    path, before, source, ctx = consent_env
    raw = path.read_bytes()
    real_writer = policy.write_policy
    writes = []
    def checked(home, updated, expected):
        assert expected == raw and home == path.parent
        writes.append(updated)
        return real_writer(home, updated, expected)
    def choose(*args):
        assert path.parent not in getattr(resources._local, 'held', {})
        return 'enable'
    monkeypatch.setattr(policy, 'write_policy', checked)
    ctx.ask_choice = choose
    assert 'consent enabled' in ctx.command('evaluation enable', source=source)
    assert writes == [{**before, 'private_chat_evaluation': CONSENT}]


@pytest.mark.parametrize('change', ['policy', 'owner', 'profile', 'sender', 'dm', 'bot', 'maintenance'])
def test_enable_revalidates_after_preview_before_writing(consent_env, monkeypatch, change):
    import hermes_constants
    path, before, source, ctx = consent_env
    raw = path.read_bytes()
    other = path.parent / 'synthetic-other-profile'
    other.mkdir()
    other_policy = other / path.name
    other_policy.write_bytes(raw)
    def choose(*args):
        if change == 'policy': path.write_bytes(raw + b' ')
        elif change == 'owner': path.write_text(json.dumps({**before, 'owner_principals': ['telegram:99']}))
        elif change == 'profile': monkeypatch.setattr(hermes_constants, 'get_hermes_home', lambda: other)
        elif change == 'sender': source.user_id = '99'
        elif change == 'dm': source.chat_type = 'group'
        elif change == 'bot': source.is_bot = True
        else: (path.parent / '.tingle-removing').write_text('synthetic maintenance')
        return 'enable'
    ctx.ask_choice = choose
    out = ctx.command('evaluation enable', source=source)
    assert 'stopped' in out.lower() and 'nothing changed' in out.lower()
    assert 'private_chat_evaluation' not in json.loads(path.read_text())
    assert other_policy.read_bytes() == raw
    assert not list(path.parent.glob('.tingle-policy-backup-*'))


@pytest.mark.parametrize('action', ['status', 'enable', 'disable'])
def test_stale_snapshot_never_shows_status_or_overwrites_policy(consent_env, monkeypatch, action):
    import hermes_tingle_policy as policy
    path, before, source, ctx = consent_env
    raw = path.read_bytes()
    real_snapshot = policy.snapshot
    calls = []
    def changed(home):
        calls.append(home)
        if len(calls) == 2:
            path.write_bytes(raw + b' ')
        return real_snapshot(home)
    monkeypatch.setattr(policy, 'snapshot', changed)
    ctx.answer = 'enable'
    out = ctx.command('evaluation ' + action, source=source)
    assert 'stopped' in out.lower() and 'nothing changed' in out.lower()
    assert path.read_bytes() == raw + b' '
    assert not list(path.parent.glob('.tingle-policy-backup-*'))


@pytest.mark.parametrize('action', ['enable', 'disable'])
def test_shared_writer_last_moment_stale_check_is_used(consent_env, monkeypatch, action):
    import hermes_tingle_policy as policy
    path, before, source, ctx = consent_env
    raw = path.read_bytes()
    real_writer = policy.write_policy
    writes = []
    def raced(home, updated, expected):
        writes.append(expected)
        path.write_bytes(raw + b' ')
        return real_writer(home, updated, expected)
    monkeypatch.setattr(policy, 'write_policy', raced)
    ctx.answer = 'enable'
    out = ctx.command('evaluation ' + action, source=source)
    assert 'stopped' in out.lower() and 'consent enabled' not in out and 'consent disabled' not in out
    assert path.read_bytes() == raw + b' ' and writes == [raw]


@pytest.mark.parametrize('action', ['enable', 'disable'])
def test_failed_writer_readback_is_not_reported_as_success_or_retried(consent_env, monkeypatch, action):
    import hermes_tingle_policy as policy
    path, before, source, ctx = consent_env
    if action == 'disable':
        path.write_text(json.dumps({**before, 'private_chat_evaluation': CONSENT}))
    checks = []
    def failed(home):
        checks.append(home)
        raise ValueError('synthetic private path diagnostic')
    monkeypatch.setattr(policy, 'load_settings', failed)
    ctx.answer = 'enable'
    out = ctx.command('evaluation ' + action, source=source)
    assert 'may already be saved' in out and 'no automatic retry' in out
    assert 'synthetic private' not in out and 'consent enabled' not in out and 'consent disabled' not in out
    assert checks == [path.parent]
    assert json.loads(path.read_text()) == ({**before, 'private_chat_evaluation': CONSENT} if action == 'enable' else before)


def test_missing_raising_or_unexpected_chooser_never_writes(consent_env):
    path, before, source, ctx = consent_env
    raw = path.read_bytes()
    ctx.ask_choice = None
    assert 'unavailable' in ctx.command('evaluation enable', source=source)
    def fail(*args): raise RuntimeError('synthetic private exception')
    ctx.ask_choice = fail
    out = ctx.command('evaluation enable', source=source)
    assert 'unavailable' in out and 'synthetic private' not in out
    assert path.read_bytes() == raw


@pytest.mark.parametrize('action', ['status', 'enable', 'disable'])
def test_symlink_policy_is_refused(consent_env, action):
    path, before, source, ctx = consent_env
    outside = path.with_name('synthetic-outside.json')
    raw = path.read_bytes()
    path.rename(outside)
    path.symlink_to(outside)
    out = ctx.command('evaluation ' + action, source=source)
    assert 'stopped' in out.lower() and 'nothing changed' in out.lower()
    assert outside.read_bytes() == raw and path.is_symlink() and not ctx.questions


def test_help_documents_supported_consent_commands():
    out = plugin.handle_command(None, 'help')
    assert '/tingle evaluation status|enable|disable' in out
    assert 'default-off' in out and 'owner DM' in out


@pytest.mark.parametrize('action', ['status', 'enable', 'disable'])
@pytest.mark.parametrize('malformation', ['duplicate-owner', 'duplicate-consent', 'nonfinite'])
def test_ambiguous_or_non_json_snapshot_is_not_rewritten(consent_env, action, malformation):
    path, before, source, ctx = consent_env
    raw = json.dumps(before)
    if malformation == 'duplicate-owner':
        raw = raw[:-1] + ', "owner_principals": ["telegram:99"], "owner_principals": ["telegram:42"]}'
    elif malformation == 'duplicate-consent':
        raw = raw[:-1] + ', "private_chat_evaluation": null, "private_chat_evaluation": ' + json.dumps(CONSENT) + '}'
    else:
        raw = raw[:-1] + ', "unknown_extension": NaN}'
    path.write_text(raw)
    ctx.answer = 'enable'
    out = ctx.command('evaluation ' + action, source=source)
    assert 'stopped' in out.lower() and 'nothing changed' in out.lower()
    assert path.read_text() == raw and not ctx.questions


@pytest.mark.parametrize('action', ['status', 'disable'])
def test_profile_switch_before_final_boundary_refuses_even_without_chooser(consent_env, monkeypatch, action):
    import hermes_constants
    import hermes_tingle_policy as policy
    path, before, source, ctx = consent_env
    raw = path.read_bytes()
    other = path.parent / 'synthetic-other-profile'
    other.mkdir()
    real_snapshot = policy.snapshot
    def switch(home):
        result = real_snapshot(home)
        monkeypatch.setattr(hermes_constants, 'get_hermes_home', lambda: other)
        return result
    monkeypatch.setattr(policy, 'snapshot', switch)
    out = ctx.command('evaluation ' + action, source=source)
    assert 'stopped' in out.lower() and 'nothing changed' in out.lower()
    assert path.read_bytes() == raw and not (other / path.name).exists()


@pytest.mark.parametrize('action', ['status', 'enable', 'disable'])
def test_profile_switch_during_final_snapshot_cannot_disclose_or_write(consent_env, monkeypatch, action):
    import hermes_constants
    import hermes_tingle_policy as policy
    path, before, source, ctx = consent_env
    raw = path.read_bytes()
    other = path.parent / 'synthetic-other-profile'
    other.mkdir()
    real_snapshot = policy.snapshot
    calls = []
    def switch(home):
        result = real_snapshot(home)
        calls.append(home)
        if len(calls) == 2:
            monkeypatch.setattr(hermes_constants, 'get_hermes_home', lambda: other)
        return result
    monkeypatch.setattr(policy, 'snapshot', switch)
    ctx.answer = 'enable'
    out = ctx.command('evaluation ' + action, source=source)
    assert 'stopped' in out.lower() and 'nothing changed' in out.lower()
    assert path.read_bytes() == raw and not (other / path.name).exists()


def test_private_dm_alias_and_authenticated_principal_normalization(consent_env):
    path, before, source, ctx = consent_env
    source.platform = SimpleNamespace(value='TELEGRAM')
    source.chat_type = 'private'
    path.write_text(json.dumps({**before, 'owner_principals': ['Telegram:42']}))
    ctx.answer = 'enable'
    assert 'consent enabled' in ctx.command('evaluation enable', source=source)
    saved = json.loads(path.read_text())
    assert private_chat_evaluation_policy(saved) == CONSENT
    assert saved['owner_principals'] == ['Telegram:42']


@pytest.mark.parametrize('command', ['evaluation', 'evaluation enable extra', 'evaluation disable extra', 'evaluation status extra', 'evaluation nonsense'])
def test_exact_subcommand_arity_never_changes_policy(consent_env, command):
    path, before, source, ctx = consent_env
    raw = path.read_bytes()
    ctx.answer = 'enable'
    out = ctx.command(command, source=source)
    assert '/tingle evaluation status|enable|disable' in out
    assert path.read_bytes() == raw and not ctx.questions
