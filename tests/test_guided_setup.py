"""Owner onboarding through the registered command; synthetic files/Cron only."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import hermes_tingle as plugin
import setup_jobs
from test_setup_jobs import FakeCron, fake_schedule


@pytest.fixture
def guided(tmp_path, monkeypatch):
    import hermes_tingle_setup as setup
    monkeypatch.setattr(setup, '_active_profiles', {})
    (tmp_path / 'hermes-tingle.json').unlink()
    data = tmp_path / 'plugin-data' / 'hermes-tingle'
    data.mkdir(parents=True)
    (data / 'setup-owner.json').write_text(json.dumps({'owner_principals': ['telegram:42'], 'fresh_profile': True}))
    source = SimpleNamespace(platform='telegram', user_id='42', chat_id='42', chat_type='dm', is_bot=False)

    class Context(FakeCron):
        def __init__(self):
            super().__init__()
            self.questions = []
            self.answer = 'cancel'
        def register_tool(self, **kwargs): pass
        def register_command(self, name, **kwargs): self.command = kwargs['handler']
        def register_skill(self, *args): pass
        def ask_choice(self, question, choices):
            self.questions.append((question, choices))
            return self.answer

    ctx = Context()
    monkeypatch.setattr(plugin, 'integrate', lambda _: None)
    spec = importlib.util.spec_from_file_location('synthetic_guided_plugin', Path(__file__).parents[1] / '__init__.py')
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    entry.register(ctx)
    monkeypatch.setattr(setup_jobs, '_cron_api', lambda: (fake_schedule, ctx.read_job))
    # Use real helper installation from isolated synthetic plugin resources.
    scripts = tmp_path / 'plugins' / 'hermes-tingle' / 'scripts'
    scripts.mkdir(parents=True)
    (scripts / 'source_snapshot.py').write_text('# synthetic source helper\n')
    (scripts / 'due_alerts.py').write_text('# synthetic due helper\n')
    return tmp_path, data, source, ctx


ANSWERS = [
    ('destination', 'telegram:123'), ('timezone', 'Europe/Berlin'),
    ('review', '0 0,8,16 * * *'), ('deadline', '30'), ('date-only', '09:00'),
    ('availability', '09:00 18:00'), ('sources', 'none'), ('evaluation', 'no'),
    ('exclusions', '{"calendar_exclude_substrings": [], "workstream_exclude_values": [], "source_exclude_substrings": [], "ticket_exclude_patterns": []}'),
]


def reply(ctx, source, field, value):
    # Values are typed text, not limited-choice ask_choice free text.
    return ctx.command('setup ' + field + ' ' + value, source=source)


def preview(guided, evaluation='no'):
    home, data, source, ctx = guided
    out = ctx.command('setup', source=source)
    assert '/tingle setup destination <value>' in out
    for index, (field, value) in enumerate(ANSWERS):
        out = reply(ctx, source, field, evaluation if field == 'evaluation' else value)
        assert not (home / 'hermes-tingle.json').exists()
        assert not ctx.jobs and not ctx.questions
        assert not (home / 'scripts').exists()
        if index + 1 < len(ANSWERS):
            assert '/tingle setup ' + ANSWERS[index + 1][0] + ' <value>' in out
    return out


def test_refresh_review_command_routes_only_with_existing_valid_policy(tmp_path, synthetic_settings, monkeypatch):
    import hermes_tingle_setup as setup
    source = SimpleNamespace(platform='telegram', user_id='42', chat_id='42', chat_type='dm', is_bot=False)
    monkeypatch.setattr(setup, '_active_profiles', {})
    monkeypatch.setattr(setup, 'snapshot', lambda home: {'existing': True})
    monkeypatch.setattr(setup, 'load_settings', lambda home: synthetic_settings)
    seen = []
    monkeypatch.setattr(setup_jobs, 'refresh_review_prompt_from_command',
                        lambda ctx, actor: seen.append((ctx, actor)) or 'prompt refresh preview')
    ctx = object()
    result = plugin.handle_command(ctx, 'setup refresh-review', source=source)
    assert result == 'prompt refresh preview'
    assert seen == [(ctx, source)]


def test_refresh_review_command_refuses_when_policy_is_invalid(tmp_path, monkeypatch):
    import hermes_tingle_setup as setup
    source = SimpleNamespace(platform='telegram', user_id='42', chat_id='42', chat_type='dm', is_bot=False)
    monkeypatch.setattr(setup, '_active_profiles', {})
    monkeypatch.setattr(setup, 'snapshot', lambda home: None)
    monkeypatch.setattr(setup_jobs, 'refresh_review_prompt_from_command',
                        lambda *args: pytest.fail('invalid policy cannot refresh an existing job'))
    result = plugin.handle_command(object(), 'setup refresh-review', source=source)
    assert 'No valid Tingle profile setup' in result and 'Nothing changed' in result


def test_refresh_review_command_preserves_an_active_setup_draft(guided):
    home, data, source, ctx = guided
    state = data / 'setup-state.json'
    original = b'{"draft":"keep me"}'
    state.write_bytes(original)
    result = ctx.command('setup refresh-review', source=source)
    assert 'setup draft is active' in result
    assert state.read_bytes() == original
    assert not ctx.jobs and not ctx.questions


def test_guided_evaluation_yes_is_exact_valid_staged_consent_until_write(guided, monkeypatch):
    import hermes_tingle_sources as sources
    from hermes_tingle_config import private_chat_evaluation_policy
    from hermes_tingle_store import list_source_optins
    home, data, source, ctx = guided
    before_sources = list_source_optins()
    monkeypatch.setattr(sources, 'probe_source', lambda _: pytest.fail('none cannot probe'))
    out = preview(guided, evaluation='yes')
    for text in ('bounded', 'eligible owner-DM', 'tool-free', 'default model/provider',
                 'verified Hermes pricing', '1.5 EUR/USD', 'local cues', 'cost nothing',
                 'not an invoice', '€5', 'fail', 'no retries or fallback', 'chosen provider'):
        assert text in out
    expected = {'enabled': True, 'consent': 'owner-dm-excerpts-v1', 'approved_by': 'telegram:42',
                'route': 'runtime-default', 'monthly_limit_microeur': 5_000_000}
    staged = json.loads((data / 'setup-state.json').read_text())['policy']
    assert staged['private_chat_evaluation'] == expected
    assert private_chat_evaluation_policy(staged) == expected
    assert not (home / 'hermes-tingle.json').exists()
    reply(ctx, source, 'confirm-destination', 'telegram:123')
    assert not (home / 'hermes-tingle.json').exists()
    assert 'policy saved' in reply(ctx, source, 'confirm', 'WRITE')
    saved = json.loads((home / 'hermes-tingle.json').read_text())
    assert saved == staged and private_chat_evaluation_policy(saved) == expected
    assert list_source_optins() == before_sources
    assert len(ctx.questions) == 1 and not ctx.jobs  # Only separate job consent.


def test_guided_evaluation_no_omits_consent(guided):
    home, data, source, ctx = guided
    preview(guided)
    assert 'private_chat_evaluation' not in json.loads((data / 'setup-state.json').read_text())['policy']
    reply(ctx, source, 'confirm-destination', 'telegram:123')
    reply(ctx, source, 'confirm', 'WRITE')
    assert 'private_chat_evaluation' not in json.loads((home / 'hermes-tingle.json').read_text())


@pytest.mark.parametrize('answer', ['YES', 'Yes', 'true', 'enable', 'yes please', ''])
def test_guided_evaluation_requires_exact_yes_no(guided, answer):
    home, data, source, ctx = guided
    ctx.command('setup', source=source)
    for field, value in ANSWERS:
        if field == 'evaluation':
            break
        out = reply(ctx, source, field, value)
    assert 'OFF' in out and 'yes' in out and 'no' in out
    assert 'discarded' in reply(ctx, source, 'evaluation', answer)
    assert not (data / 'setup-state.json').exists()
    assert not (home / 'hermes-tingle.json').exists() and not ctx.questions


@pytest.mark.parametrize('commands', [
    [('cancel', '')],
    [('confirm', 'WRITE')],
    [('confirm-destination', 'telegram:999')],
    [('confirm-destination', 'telegram:123'), ('confirm', 'write')],
])
def test_guided_evaluation_yes_does_not_bypass_cancel_destination_or_write(guided, commands):
    home, data, source, ctx = guided
    preview(guided, evaluation='yes')
    for field, value in commands:
        out = reply(ctx, source, field, value)
    assert 'discarded' in out
    assert not (home / 'hermes-tingle.json').exists() and not (data / 'setup-state.json').exists()
    assert not ctx.questions and not ctx.jobs


def test_guided_policy_write_then_separate_job_consent(guided):
    home, data, source, ctx = guided
    out = preview(guided)
    for value in ('telegram:123', 'Europe/Berlin', '0 0,8,16 * * *', '30', '09:00', '18:00', 'owner_principals', 'none'):
        assert value in out
    assert 'model' in out and 'history' in out
    assert 'confirm-destination' in out
    assert 'WRITE' in reply(ctx, source, 'confirm-destination', 'telegram:123')
    result = reply(ctx, source, 'confirm', 'WRITE')
    assert 'policy saved' in result.lower() and 'cancelled' in result
    policy = json.loads((home / 'hermes-tingle.json').read_text())
    assert policy['owner_principals'] == ['telegram:42']
    assert policy['delivery'] == 'telegram:123'
    assert (home / 'hermes-tingle.json').stat().st_mode & 0o777 == 0o600
    assert len(ctx.questions) == 1
    assert any(c['label'] == 'Create both jobs' for c in ctx.questions[0][1])
    assert not ctx.jobs and not (home / 'scripts').exists()
    ctx.answer = 'create'
    assert 'is scheduled' in ctx.command('setup', source=source)
    assert len(ctx.jobs) == 2
    assert (home / 'scripts' / 'hermes_tingle_due.py').exists()
    assert not (data / 'setup-state.json').exists()


@pytest.mark.parametrize('command', ['', 'setup'])
def test_missing_policy_points_to_operator_not_owner_terminal(guided, command):
    home, data, source, ctx = guided
    (data / 'setup-owner.json').unlink()
    out = ctx.command(command, source=source)
    assert 'installer/operator' in out and 'provision' in out
    assert 'docs/onboarding.md' in out and 'Nothing was changed' in out
    assert 'tingle_init.py' not in out and 'python3' not in out
    assert not (home / 'hermes-tingle.json').exists()
    assert not (data / 'setup-state.json').exists()


@pytest.mark.parametrize('change,expected', [
    ({'user_id': '99'}, 'not a configured owner'),
    ({'chat_type': 'group'}, 'direct message'),
    ({'chat_type': ''}, 'direct message'),
    ({'is_bot': True}, 'authenticated human'),
    ({'chat_id': ''}, 'authenticated human'),
])
def test_only_preprovisioned_owner_in_verified_dm_can_start(guided, change, expected):
    home, data, source, ctx = guided
    source = SimpleNamespace(**{**vars(source), **change})
    out = ctx.command('setup', source=source)
    assert expected in out
    assert not (data / 'setup-state.json').exists()
    assert not ctx.questions and not ctx.jobs


@pytest.mark.parametrize('commands', [
    [('confirm-destination', 'telegram:999')],
    [('confirm-destination', 'telegram:123'), ('confirm', 'write')],
    [('confirm-destination', 'telegram:123'), ('confirm', 'Write')],
    [('cancel', '')],
    [('owner', 'telegram:99')],
    [('review', "'unsupported")],
])
def test_bad_confirmation_cancel_or_unsupported_reply_discards_draft(guided, commands):
    home, data, source, ctx = guided
    preview(guided)
    for field, value in commands:
        out = reply(ctx, source, field, value)
    assert not (home / 'hermes-tingle.json').exists()
    assert not (data / 'setup-state.json').exists()
    assert not ctx.questions and not ctx.jobs
    assert 'discarded' in out
    reply(ctx, source, 'confirm', 'WRITE')
    assert not (home / 'hermes-tingle.json').exists()


def test_profile_switch_invalidates_old_draft(guided, monkeypatch):
    import hermes_constants
    home, data, source, ctx = guided
    preview(guided)
    other = home / 'another-profile'
    other.mkdir()
    monkeypatch.setattr(hermes_constants, 'get_hermes_home', lambda: other)
    assert 'profile changed' in reply(ctx, source, 'confirm-destination', 'telegram:123')
    assert not (other / 'hermes-tingle.json').exists()
    assert not (data / 'setup-state.json').exists()
    monkeypatch.setattr(hermes_constants, 'get_hermes_home', lambda: home)
    reply(ctx, source, 'confirm', 'WRITE')
    assert not (home / 'hermes-tingle.json').exists()


def test_existing_valid_policy_still_requires_known_dm_type(guided):
    home, data, source, ctx = guided
    preview(guided)
    reply(ctx, source, 'confirm-destination', 'telegram:123')
    reply(ctx, source, 'confirm', 'WRITE')
    source.chat_type = 'unknown'
    out = ctx.command('setup', source=source)
    assert 'direct message' in out
    assert len(ctx.questions) == 1


@pytest.mark.parametrize('change', ['authority', 'policy', 'timeout', 'path'])
def test_changed_authority_policy_timeout_or_profile_identity_cannot_commit(guided, monkeypatch, change):
    import hermes_tingle_setup as setup
    home, data, source, ctx = guided
    preview(guided)
    if change == 'authority':
        (data / 'setup-owner.json').write_text(json.dumps({'owner_principals': ['telegram:99']}))
    elif change == 'policy':
        (home / 'hermes-tingle.json').write_text('{broken')
    elif change == 'timeout':
        monkeypatch.setattr(setup.time, 'time', lambda: float('inf'))
    else:
        # Same spelling, different profile directory identity.
        moved = home.with_name(home.name + '-moved')
        home.rename(moved)
        home.mkdir()
        import shutil
        shutil.copytree(moved / 'plugin-data', home / 'plugin-data')
    out = reply(ctx, source, 'confirm-destination', 'telegram:123')
    assert 'discarded' in out
    assert not (data / 'setup-state.json').exists()
    assert not ctx.questions and not ctx.jobs
    if change == 'policy':
        assert (home / 'hermes-tingle.json').read_text() == '{broken'
    else:
        assert not (home / 'hermes-tingle.json').exists()


def test_cross_chat_reply_cannot_advance_or_cancel_owner_draft(guided):
    home, data, source, ctx = guided
    preview(guided)
    before = (data / 'setup-state.json').read_bytes()
    elsewhere = SimpleNamespace(**{**vars(source), 'chat_id': '999'})
    assert 'different owner or chat' in reply(ctx, elsewhere, 'confirm-destination', 'telegram:123')
    assert 'different owner or chat' in reply(ctx, elsewhere, 'cancel', '')
    assert (data / 'setup-state.json').read_bytes() == before
    assert not ctx.jobs and not ctx.questions


def test_tool_missing_policy_never_sends_owner_to_python(guided):
    out = plugin.handle_tool(None, {'action': 'add', 'title': 'Synthetic task'})
    assert 'installer/operator' in out and 'tingle_init.py' not in out


def test_no_source_probe_or_grant_until_write_even_with_all_selected(guided, monkeypatch):
    import hermes_tingle_sources as sources
    import hermes_tingle_store as store
    home, data, source, ctx = guided
    import sqlite3
    def connect(_):
        conn = sqlite3.connect(home / 'fresh-synthetic.db')
        conn.row_factory = sqlite3.Row
        return conn
    monkeypatch.setattr(store, 'plugin_db', connect)
    before = store.list_source_optins()
    probes = []
    def probe(kind):
        probes.append(kind)
        return 'a' * 64 if kind in ('email', 'calendar') else None
    monkeypatch.setattr(sources, 'probe_source', probe)
    ctx.command('setup', source=source)
    for field, value in ANSWERS:
        reply(ctx, source, field, 'all' if field == 'sources' else value)
    assert not probes and store.list_source_optins() == before
    reply(ctx, source, 'confirm-destination', 'telegram:123')
    out = reply(ctx, source, 'confirm', 'WRITE')
    assert probes == ['email', 'calendar', 'chat']
    assert all(store.source_enabled(kind) for kind in ('email', 'calendar', 'chat'))
    assert 'policy saved' in out and 'cancelled' in out
    assert not ctx.jobs
    assert len(ctx.questions) == 1


def test_job_confirmation_holds_no_policy_mutation_lock(guided):
    import hermes_tingle_resources as resources
    home, data, source, ctx = guided
    preview(guided)
    reply(ctx, source, 'confirm-destination', 'telegram:123')
    ask = ctx.ask_choice
    def unlocked(*args):
        assert home not in getattr(resources._local, 'held', {})
        return ask(*args)
    ctx.ask_choice = unlocked
    assert 'policy saved' in reply(ctx, source, 'confirm', 'WRITE')


@pytest.mark.parametrize('mode', ['maintenance', 'owner-symlink', 'state-symlink'])
def test_maintenance_or_symlink_refuses_cleanly_without_outside_writes(guided, mode):
    home, data, source, ctx = guided
    preview(guided)
    outside = home / 'synthetic-outside'
    outside.write_text('synthetic untouched')
    if mode == 'maintenance':
        (home / '.tingle-removing').write_text('maintenance')
    else:
        target = data / ('setup-owner.json' if mode == 'owner-symlink' else 'setup-state.json')
        target.unlink()
        target.symlink_to(outside)
    out = reply(ctx, source, 'confirm-destination', 'telegram:123')
    assert 'stopped' in out.lower() or 'operator' in out
    assert outside.read_text() == 'synthetic untouched'
    assert not (home / 'hermes-tingle.json').exists()
    assert not ctx.questions and not ctx.jobs


def test_profile_home_switch_while_waiting_for_jobs_cannot_create(guided, monkeypatch):
    import hermes_constants
    home, data, source, ctx = guided
    preview(guided)
    reply(ctx, source, 'confirm-destination', 'telegram:123')
    other = home / 'other'
    other.mkdir()
    def switch(*args):
        monkeypatch.setattr(hermes_constants, 'get_hermes_home', lambda: other)
        return 'create'
    ctx.ask_choice = switch
    out = reply(ctx, source, 'confirm', 'WRITE')
    assert 'policy saved' in out and 'stopped' in out
    assert not ctx.jobs and not (home / 'scripts').exists()
    assert not (other / 'hermes-tingle.json').exists()


def test_invalid_policy_fragment_is_not_bootstrap_authority(guided):
    home, data, source, ctx = guided
    (home / 'hermes-tingle.json').write_text(json.dumps({'owner_principals': ['telegram:42'], 'fresh_profile': True}))
    out = ctx.command('setup', source=source)
    assert 'installer/operator' in out and 'tingle_init.py' not in out
    assert not (data / 'setup-state.json').exists()


def test_expired_draft_cannot_be_resumed_after_host_reload(guided, monkeypatch):
    import hermes_tingle_setup as setup
    home, data, source, ctx = guided
    preview(guided)
    monkeypatch.setattr(setup, '_active_profiles', {})
    monkeypatch.setattr(setup.time, 'time', lambda: float('inf'))
    assert 'discarded' in reply(ctx, source, 'confirm-destination', 'telegram:123')
    assert not (home / 'hermes-tingle.json').exists()


def test_fresh_attestation_allows_admission_created_database(guided, monkeypatch):
    import sqlite3
    import hermes_tingle_store as store
    import hermes_tingle_wiring as wiring
    home, data, source, ctx = guided
    def connect(_):
        conn = sqlite3.connect(data / 'synthetic-storage.db')
        conn.row_factory = sqlite3.Row
        return conn
    monkeypatch.setattr(store, 'plugin_db', connect)
    wiring.observe_admitted_inbound(platform='telegram', source=vars(source))
    assert (data / 'synthetic-storage.db').exists()
    with connect(None) as conn:
        assert conn.execute('SELECT count(*) FROM known_chats').fetchone()[0] == 1
    assert '/tingle setup destination <value>' in ctx.command('setup', source=source)


def test_bootstrap_is_consumed_so_lost_policy_cannot_reconstruct_defaults(guided):
    home, data, source, ctx = guided
    preview(guided)
    reply(ctx, source, 'confirm-destination', 'telegram:123')
    reply(ctx, source, 'confirm', 'WRITE')
    assert (data / 'setup-consumed.json').exists()
    (home / 'hermes-tingle.json').unlink()
    out = ctx.command('setup', source=source)
    assert 'installer/operator' in out and not (data / 'setup-state.json').exists()
    assert not (home / 'hermes-tingle.json').exists()


def test_owner_only_fragment_without_fresh_install_attestation_refuses(guided):
    home, data, source, ctx = guided
    (data / 'setup-owner.json').write_text(json.dumps({'owner_principals': ['telegram:42']}))
    assert 'installer/operator' in ctx.command('setup', source=source)
    assert not (data / 'setup-state.json').exists()


@pytest.mark.parametrize('partial_failure', [False, True])
def test_guided_write_reuses_verified_creation_and_partial_failure_rollback(guided, partial_failure):
    home, data, source, ctx = guided
    preview(guided)
    reply(ctx, source, 'confirm-destination', 'telegram:123')
    ctx.answer = 'create'
    ctx.fail_create = partial_failure
    out = reply(ctx, source, 'confirm', 'WRITE')
    assert 'policy saved' in out
    if partial_failure:
        assert 'rolled back' in out and not ctx.jobs
    else:
        assert 'is scheduled' in out and len(ctx.jobs) == 2
        assert "hermes gateway restart" in out and "/new" in out and "separate terminal" in out
    assert (home / 'hermes-tingle.json').exists()
    assert (home / 'scripts' / 'hermes_tingle_due.py').exists()


def test_consumed_bootstrap_cannot_be_retried_after_failed_write(guided, monkeypatch):
    import hermes_tingle_setup as setup
    home, data, source, ctx = guided
    preview(guided)
    reply(ctx, source, 'confirm-destination', 'telegram:123')
    def fail(*args):
        raise OSError('synthetic private diagnostic')
    monkeypatch.setattr(setup, 'write_policy', fail)
    out = reply(ctx, source, 'confirm', 'WRITE')
    assert 'may already be saved' in out and 'synthetic private diagnostic' not in out
    assert (data / 'setup-consumed.json').exists()
    assert not (home / 'hermes-tingle.json').exists() and not ctx.jobs
    assert 'installer/operator' in reply(ctx, source, 'confirm', 'WRITE')


def test_none_source_choice_has_no_probe_or_grant(guided, monkeypatch):
    import sqlite3
    import hermes_tingle_sources as sources
    import hermes_tingle_store as store
    home, data, source, ctx = guided
    def connect(_):
        conn = sqlite3.connect(home / 'fresh-synthetic.db')
        conn.row_factory = sqlite3.Row
        return conn
    monkeypatch.setattr(store, 'plugin_db', connect)
    before = store.list_source_optins()
    monkeypatch.setattr(sources, 'probe_source', lambda _: pytest.fail('source opt-out cannot probe'))
    preview(guided)
    reply(ctx, source, 'confirm-destination', 'telegram:123')
    assert 'policy saved' in reply(ctx, source, 'confirm', 'WRITE')
    assert store.list_source_optins() == before


def test_draft_and_consumed_marker_renames_are_directory_durable(guided, monkeypatch):
    import os
    import stat
    import hermes_tingle_setup as setup
    home, data, source, ctx = guided
    original = os.fsync
    directory_syncs = []
    def synced(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            directory_syncs.append((data / 'setup-consumed.json').exists())
        return original(fd)
    monkeypatch.setattr(setup.os, 'fsync', synced)
    preview(guided)
    assert directory_syncs  # Draft renames survive a crash, not just file bytes.
    reply(ctx, source, 'confirm-destination', 'telegram:123')
    real_writer = setup.write_policy
    def checked_write(*args):
        assert any(directory_syncs)  # Consumption is durable before policy commit.
        return real_writer(*args)
    monkeypatch.setattr(setup, 'write_policy', checked_write)
    assert 'policy saved' in reply(ctx, source, 'confirm', 'WRITE')
