"""Owner onboarding through the registered command; synthetic files/Cron only."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

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
            self.answers = []
            self.answer = 'cancel'
        def register_tool(self, **kwargs): pass
        def register_command(self, name, **kwargs): self.command = kwargs['handler']
        def register_skill(self, *args): pass
        def ask_choice(self, question, choices) -> Any:
            self.questions.append((question, choices))
            return self.answers.pop(0) if self.answers else self.answer

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


def preview(guided, evaluation=None):
    del evaluation  # Kept as a call-site shim while consent is tested separately.
    home, data, source, ctx = guided
    ctx.answer = None
    out = ctx.command('setup', source=source)
    assert '`/tingle setup confirm WRITE`' in out
    return out


def test_successful_fresh_setup_offers_30_day_review_after_jobs(guided, monkeypatch):
    import hermes_tingle_onboarding as onboarding
    import hermes_tingle_setup as setup

    home, data, source, ctx = guided
    ctx.answer = "start"
    order = []
    monkeypatch.setattr(setup_jobs, "_activate_jobs", lambda *a, **k:
                        order.append("jobs") or "Hermes Tingle is scheduled with the configured destination and schedule.")
    monkeypatch.setattr(onboarding, "offer_after_setup", lambda context, actor:
                        order.append(("lookback", actor)) or "Look-back choice shown.", raising=False)

    out = plugin.handle_command(ctx, "", source=source)

    assert order == ["jobs", ("lookback", source)]
    assert "Look-back choice shown." in out
    assert "lookback start 30" not in out


def test_first_tingle_offers_one_approval_with_hermes_defaults(guided):
    home, data, source, ctx = guided
    (home / 'config.yaml').write_text(
        'timezone: Europe/Berlin\n'
        'platforms:\n'
        '  telegram:\n'
        '    home_channel:\n'
        '      platform: telegram\n'
        "      user_id: '42'\n"
        "      chat_id: '42'\n"
    )
    ctx.answer = None  # No choice is not consent; it leaves an approval route available.

    out = plugin.handle_command(ctx, '', source=source)

    assert len(ctx.questions) == 1
    question, choices = ctx.questions[0]
    assert [choice['value'] for choice in choices] == ['start', 'cancel']
    state = json.loads((data / 'setup-state.json').read_text())
    policy = state['policy']
    assert state['step'] == 'approval'
    assert state['sources'] == 'all'
    assert policy['delivery'] == 'telegram:42'
    assert policy['timezone'] == 'Europe/Berlin'
    assert policy['daily_schedule'] == '0 0,8,16 * * *'
    assert policy['deadline_poll_minutes'] == 30
    assert policy['date_only_time'] == '09:00'
    assert policy['availability_start'] == '09:00'
    assert policy['availability_end'] == '18:00'
    assert all(policy[key] == [] for key in (
        'calendar_exclude_substrings', 'workstream_exclude_values',
        'source_exclude_substrings', 'ticket_exclude_patterns'))
    assert 'private_chat_evaluation' not in policy
    assert all(emoji in question for emoji in ('📍', '🕰️', '⏰', '🤖'))
    assert 'optional extra AI pass' in question
    assert 'OFF' in question and 'separate consent' in question
    assert 'configured AI model' in question and 'possibly cloud' in question
    assert not (home / 'hermes-tingle.json').exists()
    assert not ctx.jobs
    assert 'Europe/Berlin' in question and 'Gmail' in question and 'read-only' in question
    assert 'Google may refresh its saved sign-in' in question
    assert 'chat checks access without reading messages' in question
    assert 'Turning a source off stops future reads' in question
    assert "can't pull back content already sent" in question
    assert 'Google Drive is not' in question
    assert 'evaluation' not in question and 'same-day availability window' not in question
    assert 'optional private discovery evaluation' not in question
    assert '/tingle setup confirm WRITE' in out
    assert '/tingle setup' in out
    import re
    for command in re.findall(r'/tingle(?: [A-Za-z_-]+)*', out):
        assert f'`{command}`' in out


def test_hermes_timezone_environment_override_wins_over_profile_yaml(guided, monkeypatch):
    home, data, source, ctx = guided
    (home / 'config.yaml').write_text('timezone: Europe/Berlin\n')
    monkeypatch.setenv('HERMES_TIMEZONE', 'Asia/Tokyo')
    ctx.answer = None

    out = plugin.handle_command(ctx, '', source=source)

    state = json.loads((data / 'setup-state.json').read_text())
    assert state['policy']['timezone'] == 'Asia/Tokyo'
    assert 'Asia/Tokyo' in out
    assert 'Europe/Berlin' not in out


def test_routed_profile_uses_hermes_timezone_not_default_process_timezone(guided, monkeypatch):
    import sys

    home, data, source, ctx = guided
    (home / 'config.yaml').write_text('timezone: Europe/Berlin\n')
    monkeypatch.setenv('HERMES_TIMEZONE', 'Asia/Tokyo')
    monkeypatch.setitem(
        sys.modules, 'hermes_time',
        SimpleNamespace(get_timezone_name=lambda: 'Europe/Berlin'),
    )
    ctx.answer = None

    plugin.handle_command(ctx, '', source=source)

    state = json.loads((data / 'setup-state.json').read_text())
    assert state['policy']['timezone'] == 'Europe/Berlin'


def test_fresh_setup_completes_jobs_then_offers_private_30_day_review(guided, monkeypatch):
    import sqlite3
    import hermes_tingle_sources as sources
    import hermes_tingle_store as store

    home, data, source, ctx = guided
    def connect(_):
        connection = sqlite3.connect(home / 'fresh-setup-sources.db')
        connection.row_factory = sqlite3.Row
        return connection
    monkeypatch.setattr(store, 'plugin_db', connect)
    checked = []
    monkeypatch.setattr(sources, 'probe_source', lambda kind: checked.append(kind) or ('a' * 64 if kind in ('email', 'calendar') else None))
    ctx.answers = ['start', 'later']

    out = plugin.handle_command(ctx, '', source=source)

    assert 'scheduled' in out
    assert checked == ['email', 'calendar', 'chat']
    assert all(store.source_enabled(kind) for kind in ('email', 'calendar', 'chat'))
    assert len(ctx.questions) == 2
    assert 'last 30 days' in ctx.questions[1][0]
    assert len(ctx.jobs) == 2
    assert 'Sit back and relax' in out and 'automatically' in out
    assert '`/tingle lookback start 30`' in out
    assert store.get_plugin_meta('setup_lookback_offer_v1') == 'offered'
    # The old empty-profile prompt must not appear on the next list command.
    next_list = plugin.handle_command(ctx, '', source=source)
    assert 'Welcome to Tingle' not in next_list and len(ctx.questions) == 2
    assert (home / 'hermes-tingle.json').exists()
    assert (home / 'scripts' / 'hermes_tingle_due.py').exists()


def test_invalid_fresh_setup_reply_keeps_approval_at_same_step(guided):
    home, data, source, ctx = guided
    ctx.answer = None
    plugin.handle_command(ctx, '', source=source)
    draft = data / 'setup-state.json'
    before = draft.read_bytes()

    out = ctx.command('setup evaluation yes', source=source)

    assert draft.read_bytes() == before
    assert 'Nothing changed' in out
    assert '`/tingle setup confirm WRITE`' in out
    assert not (home / 'hermes-tingle.json').exists()
    assert not ctx.jobs


def test_invalid_legacy_field_reply_keeps_current_step_and_policy(guided):
    home, data, source, ctx = guided
    preview(guided)
    draft = data / 'setup-state.json'
    state = json.loads(draft.read_text())
    state['step'] = 7  # An in-progress draft from the previous wizard version.
    state['sources'] = 'all'
    draft.write_text(json.dumps(state))
    before = draft.read_bytes()

    out = reply(ctx, source, 'evaluation', 'maybe')

    assert draft.read_bytes() == before
    assert 'Try this step again' in out
    assert '`/tingle setup evaluation <value>`' in out
    assert 'Private chat filtering is OFF' in out
    assert not (home / 'hermes-tingle.json').exists()

    next_prompt = reply(ctx, source, 'evaluation', 'no')
    resumed = json.loads(draft.read_text())
    assert resumed['step'] == 8
    assert 'private_chat_evaluation' not in resumed['policy']
    assert 'exclusions <value>' in next_prompt


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


def test_setup_keeps_private_chat_filter_off(guided):
    home, data, source, ctx = guided
    out = preview(guided)
    state = json.loads((data / 'setup-state.json').read_text())

    assert 'private_chat_evaluation' not in state['policy']
    assert 'The optional extra AI pass for private-chat suggestions is OFF' in out
    assert 'separate consent' in out and 'configured AI model' in out
    assert 'optional private discovery evaluation' not in out
    assert 'no Tingle spending ceiling' not in out

    result = reply(ctx, source, 'confirm', 'WRITE')
    saved = json.loads((home / 'hermes-tingle.json').read_text())
    assert 'private_chat_evaluation' not in saved
    assert 'scheduled' in result
    assert len(ctx.jobs) == 2
    assert len(ctx.questions) == 2


def test_guided_policy_write_and_job_creation_need_only_one_approval(guided):
    home, data, source, ctx = guided
    (home / 'config.yaml').write_text('timezone: Europe/Berlin\n')
    out = preview(guided)
    for value in ('telegram:42', 'Europe/Berlin', '00:00, 08:00 and 16:00', '30 minutes', '09:00', '09:00–18:00'):
        assert value in out
    assert 'Gmail' in out and 'read-only' in out and 'configured AI provider' in out
    assert 'confirm-destination' not in out
    assert '`/tingle setup confirm WRITE`' in out
    assert not (home / 'hermes-tingle.json').exists()
    assert not ctx.jobs

    result = reply(ctx, source, 'confirm', 'WRITE')
    policy = json.loads((home / 'hermes-tingle.json').read_text())
    assert 'scheduled' in result.lower()
    assert policy['owner_principals'] == ['telegram:42']
    assert policy['delivery'] == 'telegram:42'
    assert policy['timezone'] == 'Europe/Berlin'
    assert (home / 'hermes-tingle.json').stat().st_mode & 0o777 == 0o600
    assert len(ctx.questions) == 2  # Setup approval plus post-setup look-back choice.
    assert len(ctx.jobs) == 2
    assert (home / 'scripts' / 'hermes_tingle_due.py').exists()
    assert not (data / 'setup-state.json').exists()


@pytest.mark.parametrize('command', ['', 'setup'])
def test_missing_policy_points_to_operator_not_owner_terminal(guided, command):
    home, data, source, ctx = guided
    (data / 'setup-owner.json').unlink()
    out = ctx.command(command, source=source)
    assert 'operator' in out and 'setup-owner.json' in out
    assert '`setup-owner.json`' in out and 'Nothing changed' in out
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


@pytest.mark.parametrize(('field', 'value'), [
    ('evaluation', 'yes'),
    ('sources', 'none'),
    ('confirm', 'write'),
    ('confirm', 'Write'),
])
def test_bad_fresh_setup_reply_keeps_draft_at_approval(guided, field, value):
    home, data, source, ctx = guided
    preview(guided)
    draft = data / 'setup-state.json'
    before = draft.read_bytes()

    out = reply(ctx, source, field, value)

    assert draft.read_bytes() == before
    assert 'Nothing changed' in out
    assert '`/tingle setup confirm WRITE`' in out
    assert not (home / 'hermes-tingle.json').exists()
    assert not ctx.jobs
    result = reply(ctx, source, 'confirm', 'WRITE')
    assert 'scheduled' in result
    assert (home / 'hermes-tingle.json').exists()
    assert not draft.exists()


def test_setup_cancel_discards_pending_draft_without_side_effects(guided):
    home, data, source, ctx = guided
    preview(guided)

    out = reply(ctx, source, 'cancel', '')

    assert 'cancelled' in out.lower() or 'not now' in out.lower()
    assert not (data / 'setup-state.json').exists()
    assert not (home / 'hermes-tingle.json').exists()
    assert not ctx.jobs


def test_profile_switch_invalidates_old_draft(guided, monkeypatch):
    import hermes_constants
    home, data, source, ctx = guided
    preview(guided)
    other = home / 'another-profile'
    other.mkdir()
    monkeypatch.setattr(hermes_constants, 'get_hermes_home', lambda: other)
    assert 'profile changed' in reply(ctx, source, 'confirm', 'WRITE')
    assert not (other / 'hermes-tingle.json').exists()
    assert not (data / 'setup-state.json').exists()
    monkeypatch.setattr(hermes_constants, 'get_hermes_home', lambda: home)
    reply(ctx, source, 'confirm', 'WRITE')
    assert not (home / 'hermes-tingle.json').exists()


def test_existing_valid_policy_still_requires_known_dm_type(guided):
    home, data, source, ctx = guided
    preview(guided)
    reply(ctx, source, 'confirm', 'WRITE')
    source.chat_type = 'unknown'
    out = ctx.command('setup', source=source)
    assert 'direct message' in out
    assert len(ctx.questions) == 2


def test_valid_existing_policy_setup_activates_jobs_without_rewriting_policy(guided):
    import copy
    import hermes_tingle_store as store
    from hermes_tingle_policy import DEFAULTS, validate_settings

    home, data, source, ctx = guided
    policy = copy.deepcopy(DEFAULTS)
    policy.update(
        owner_principals=['telegram:42'], delivery='telegram:42',
        timezone='Pacific/Honolulu', daily_schedule='15 6 * * 1-5',
    )
    validate_settings(policy)
    policy_path = home / 'hermes-tingle.json'
    policy_path.write_text(json.dumps(policy))
    before_policy = policy_path.read_bytes()
    before_sources = store.list_source_optins()
    ctx.answer = 'create'

    out = plugin.handle_command(ctx, 'setup', source=source)

    assert 'scheduled' in out.lower()
    assert len(ctx.questions) == 1
    assert [choice['value'] for choice in ctx.questions[0][1]] == ['create', 'cancel']
    assert len(ctx.jobs) == 2
    assert policy_path.read_bytes() == before_policy
    assert store.list_source_optins() == before_sources
    assert not (data / 'setup-state.json').exists()


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
        moved = home.with_name(home.name + '-moved')
        home.rename(moved)
        home.mkdir()
        import shutil
        shutil.copytree(moved / 'plugin-data', home / 'plugin-data')
    out = reply(ctx, source, 'confirm', 'WRITE')
    assert 'discarded' in out
    assert not (data / 'setup-state.json').exists()
    assert len(ctx.questions) == 1 and not ctx.jobs
    if change == 'policy':
        assert (home / 'hermes-tingle.json').read_text() == '{broken'
    else:
        assert not (home / 'hermes-tingle.json').exists()


def test_cross_chat_reply_cannot_advance_or_cancel_owner_draft(guided):
    home, data, source, ctx = guided
    preview(guided)
    before = (data / 'setup-state.json').read_bytes()
    elsewhere = SimpleNamespace(**{**vars(source), 'chat_id': '999'})
    assert 'different owner or chat' in reply(ctx, elsewhere, 'confirm', 'WRITE')
    assert 'different owner or chat' in reply(ctx, elsewhere, 'cancel', '')
    assert (data / 'setup-state.json').read_bytes() == before
    assert not ctx.jobs and len(ctx.questions) == 1


def test_tool_missing_policy_never_sends_owner_to_python(guided):
    out = plugin.handle_tool(None, {'action': 'add', 'title': 'Synthetic task'})
    assert 'operator' in out and 'tingle_init.py' not in out


def test_no_source_probe_or_grant_until_single_start_approval(guided, monkeypatch):
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
    out = preview(guided)
    assert not probes and store.list_source_optins() == before
    assert not (home / 'hermes-tingle.json').exists() and not ctx.jobs

    out = reply(ctx, source, 'confirm', 'WRITE')
    assert probes == ['email', 'calendar', 'chat']
    assert all(store.source_enabled(kind) for kind in ('email', 'calendar', 'chat'))
    assert 'scheduled' in out and len(ctx.jobs) == 2
    assert len(ctx.questions) == 2


def test_job_confirmation_holds_no_policy_mutation_lock(guided):
    import hermes_tingle_resources as resources
    home, data, source, ctx = guided
    def unlocked(*args):
        assert home not in getattr(resources._local, 'held', {})
        return None
    ctx.ask_choice = unlocked
    out = plugin.handle_command(ctx, '', source=source)
    assert '`/tingle setup confirm WRITE`' in out
    assert 'scheduled' in reply(ctx, source, 'confirm', 'WRITE')
    assert len(ctx.jobs) == 2


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
    out = reply(ctx, source, 'confirm', 'WRITE')
    assert 'stopped' in out.lower() or 'operator' in out
    assert outside.read_text() == 'synthetic untouched'
    assert not (home / 'hermes-tingle.json').exists()
    assert len(ctx.questions) == 1 and not ctx.jobs


def test_profile_home_switch_while_waiting_for_start_cannot_create(guided, monkeypatch):
    import hermes_constants
    home, data, source, ctx = guided
    other = home / 'other'
    other.mkdir()
    def switch(*args):
        monkeypatch.setattr(hermes_constants, 'get_hermes_home', lambda: other)
        return 'start'
    ctx.ask_choice = switch
    out = plugin.handle_command(ctx, '', source=source)
    assert 'profile' in out.lower() and ('discarded' in out.lower() or 'changed' in out.lower())
    assert not ctx.jobs and not (home / 'scripts').exists()
    assert not (other / 'hermes-tingle.json').exists()


def test_invalid_policy_fragment_is_not_bootstrap_authority(guided):
    home, data, source, ctx = guided
    (home / 'hermes-tingle.json').write_text(json.dumps({'owner_principals': ['telegram:42'], 'fresh_profile': True}))
    out = ctx.command('setup', source=source)
    assert 'operator' in out and 'tingle_init.py' not in out
    assert not (data / 'setup-state.json').exists()


def test_expired_draft_cannot_be_resumed_after_host_reload(guided, monkeypatch):
    import hermes_tingle_setup as setup
    home, data, source, ctx = guided
    preview(guided)
    monkeypatch.setattr(setup, '_active_profiles', {})
    monkeypatch.setattr(setup.time, 'time', lambda: float('inf'))
    assert 'discarded' in reply(ctx, source, 'confirm', 'WRITE')
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
    assert '`/tingle setup confirm WRITE`' in preview(guided)


def test_bootstrap_is_consumed_so_lost_policy_cannot_reconstruct_defaults(guided):
    home, data, source, ctx = guided
    preview(guided)
    reply(ctx, source, 'confirm', 'WRITE')
    assert (data / 'setup-consumed.json').exists()
    (home / 'hermes-tingle.json').unlink()
    out = ctx.command('setup', source=source)
    assert 'operator' in out and not (data / 'setup-state.json').exists()
    assert not (home / 'hermes-tingle.json').exists()


def test_owner_only_fragment_without_fresh_install_attestation_refuses(guided):
    home, data, source, ctx = guided
    (data / 'setup-owner.json').write_text(json.dumps({'owner_principals': ['telegram:42']}))
    assert 'operator' in ctx.command('setup', source=source)
    assert not (data / 'setup-state.json').exists()


@pytest.mark.parametrize('partial_failure', [False, True])
def test_guided_write_reuses_verified_creation_and_partial_failure_rollback(guided, partial_failure):
    home, data, source, ctx = guided
    preview(guided)
    ctx.fail_create = partial_failure
    out = reply(ctx, source, 'confirm', 'WRITE')
    if partial_failure:
        assert 'rolled back' in out and not ctx.jobs
    else:
        assert 'is scheduled' in out and len(ctx.jobs) == 2
        assert "hermes gateway restart" in out and "/new" in out and "separate terminal" in out
    assert (home / 'hermes-tingle.json').exists()
    assert (home / 'scripts' / 'hermes_tingle_due.py').exists()
    assert not (data / 'setup-state.json').exists()


def test_consumed_bootstrap_cannot_be_retried_after_failed_write(guided, monkeypatch):
    import hermes_tingle_setup as setup
    home, data, source, ctx = guided
    preview(guided)
    def fail(*args):
        raise OSError('synthetic private diagnostic')
    monkeypatch.setattr(setup, 'write_policy', fail)
    out = reply(ctx, source, 'confirm', 'WRITE')
    assert 'may already be saved' in out and 'synthetic private diagnostic' not in out
    assert (data / 'setup-consumed.json').exists()
    assert not (home / 'hermes-tingle.json').exists() and not ctx.jobs
    assert 'operator' in reply(ctx, source, 'confirm', 'WRITE')


def test_not_now_does_not_probe_or_grant_sources(guided, monkeypatch):
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
    monkeypatch.setattr(sources, 'probe_source', lambda _: pytest.fail('cancel cannot probe'))
    ctx.answer = 'cancel'

    out = plugin.handle_command(ctx, '', source=source)

    assert 'not now' in out.lower()
    assert store.list_source_optins() == before
    assert not (home / 'hermes-tingle.json').exists()
    assert not (data / 'setup-state.json').exists()
    assert not ctx.jobs


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
    real_writer = setup.write_policy
    def checked_write(*args):
        assert any(directory_syncs)  # Consumption is durable before policy commit.
        return real_writer(*args)
    monkeypatch.setattr(setup, 'write_policy', checked_write)
    assert 'scheduled' in reply(ctx, source, 'confirm', 'WRITE')
