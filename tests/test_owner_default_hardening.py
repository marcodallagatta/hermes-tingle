"""Hardening for automatic owner defaulting: real YAML, Telegram-only, genuinely fresh data."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

import hermes_tingle_resources as resources
from test_guided_setup import guided  # noqa: F401 - shared synthetic command fixture


def _fresh(guided, config):
    home, data, source, ctx = guided
    (data / 'setup-owner.json').unlink()
    (home / 'config.yaml').write_text(config)
    return home, data, source, ctx


TELEGRAM_HOME = (
    'platforms:\n'
    '  telegram:\n'
    '    home_channel:\n'
    "      platform: telegram\n"
    "      user_id: '42'\n"
    "      chat_id: '42'\n"
)


@pytest.mark.parametrize('config', [
    # A telegram mapping buried under another platform's metadata.
    'platforms:\n'
    '  slack:\n'
    '    metadata:\n'
    '      telegram:\n'
    '        home_channel:\n'
    "          platform: telegram\n"
    "          user_id: '42'\n"
    "          chat_id: '42'\n",
    # Duplicate mapping: the last owner wins in a permissive parser.
    TELEGRAM_HOME + '    home_channel:\n      platform: telegram\n      user_id: \'99\'\n      chat_id: \'99\'\n',
    # Otherwise valid until an unclosed trailing flow sequence.
    TELEGRAM_HOME + 'unrelated: [unclosed\n',
])
def test_ambiguous_or_invalid_config_cannot_enroll(guided, config):  # noqa: F811
    home, data, source, ctx = _fresh(guided, config)
    out = ctx.command('setup', source=source)
    assert 'No Tingle owner is configured' in out
    assert not (data / 'setup-state.json').exists()


def test_non_telegram_home_channel_cannot_auto_enroll(guided):  # noqa: F811
    home, data, _source, ctx = _fresh(
        guided,
        'platforms:\n  discord:\n    home_channel:\n'
        "      platform: discord\n      user_id: '42'\n      chat_id: '42'\n")
    other = SimpleNamespace(platform='discord', user_id='42', chat_id='42', chat_type='dm', is_bot=False)
    out = ctx.command('setup', source=other)
    assert 'No Tingle owner is configured' in out
    assert not (data / 'setup-state.json').exists()


def _write_db(db, statements):
    conn = sqlite3.connect(db)
    for statement in statements:
        conn.execute(statement)
    conn.commit()
    conn.close()


def test_auto_owner_draft_survives_later_replies_and_restart(guided):  # noqa: F811
    home, data, source, ctx = _fresh(guided, TELEGRAM_HOME)
    first = ctx.command('setup', source=source)
    assert '/tingle setup destination' in first
    second = ctx.command('setup destination here', source=source)
    assert '/tingle setup timezone' in second
    state = json.loads((data / 'setup-state.json').read_text())
    assert state['policy']['delivery'] == 'telegram:42'
    # A discarded draft must not make fresh onboarding impossible.
    ctx.command('setup cancel', source=source)
    restarted = ctx.command('setup', source=source)
    assert '/tingle setup destination' in restarted


def test_auto_owner_setup_completes_policy_and_jobs(guided):  # noqa: F811
    from test_guided_setup import ANSWERS, reply
    home, data, source, ctx = _fresh(guided, TELEGRAM_HOME)
    ctx.command('setup', source=source)
    for field, value in ANSWERS:
        reply(ctx, source, field, value)
    reply(ctx, source, 'confirm-destination', 'telegram:123')
    written = reply(ctx, source, 'confirm', 'WRITE')
    assert 'policy saved' in written.lower()
    assert len(ctx.questions) == 1
    assert any(choice['label'] == 'Create both jobs' for choice in ctx.questions[0][1])
    ctx.answer = 'create'
    activated = ctx.command('setup', source=source)
    assert 'is scheduled' in activated
    assert len(ctx.jobs) == 2
    assert json.loads((home / 'hermes-tingle.json').read_text())['owner_principals'] == ['telegram:42']


def test_prior_tracked_item_blocks_automatic_bootstrap(guided):  # noqa: F811
    home, data, source, ctx = _fresh(guided, TELEGRAM_HOME)
    _write_db(data / 'data.db', [
        'create table hermes_tingle_items (id text)',
        "insert into hermes_tingle_items values ('HT-ABC123')",
    ])
    out = ctx.command('setup', source=source)
    assert 'No Tingle owner is configured' in out
    assert not (data / 'setup-state.json').exists()


def test_prior_source_grant_blocks_automatic_bootstrap(guided):  # noqa: F811
    home, data, source, ctx = _fresh(guided, TELEGRAM_HOME)
    _write_db(data / 'data.db', [
        'create table hermes_tingle_source_optins (kind text, enabled integer, generation integer, note text)',
        "insert into hermes_tingle_source_optins values ('email', 0, 3, 'prior grant')",
    ])
    out = ctx.command('setup', source=source)
    assert 'No Tingle owner is configured' in out
    assert not (data / 'setup-state.json').exists()


@pytest.mark.parametrize('state', [
    '{"state":"completed","items":[]}',
    '{"state":"declined"}',
])
def test_prior_onboarding_marker_blocks_automatic_bootstrap(guided, state):  # noqa: F811
    home, data, source, ctx = _fresh(guided, TELEGRAM_HOME)
    _write_db(data / 'data.db', [
        'create table plugin_meta (key text primary key, value text not null)',
        "insert into plugin_meta values ('onboarding_state', '" + state + "')",
    ])
    out = ctx.command('setup', source=source)
    assert 'No Tingle owner is configured' in out
    assert not (data / 'setup-state.json').exists()


def test_unmanifested_legacy_helper_blocks_automatic_bootstrap(guided):  # noqa: F811
    home, data, source, ctx = _fresh(guided, TELEGRAM_HOME)
    helper = home / 'scripts' / 'hermes_tingle_due.py'
    helper.parent.mkdir()
    helper.write_text('# legacy helper without provenance')
    out = ctx.command('setup', source=source)
    assert 'No Tingle owner is configured' in out
    assert not (data / 'setup-state.json').exists()


def test_unmanifested_tingle_cron_job_blocks_automatic_bootstrap(guided):  # noqa: F811
    home, data, source, ctx = _fresh(guided, TELEGRAM_HOME)
    cron = home / 'cron'
    cron.mkdir()
    (cron / 'jobs.json').write_text(json.dumps({'jobs': [{
        'id': 'legacy-tingle-review', 'name': 'Tingle daily review',
        'script': 'hermes_tingle_snapshot.py',
    }]}))
    out = ctx.command('setup', source=source)
    assert 'No Tingle owner is configured' in out
    assert not (data / 'setup-state.json').exists()


def test_unrelated_cron_job_does_not_block_automatic_bootstrap(guided):  # noqa: F811
    home, data, source, ctx = _fresh(guided, TELEGRAM_HOME)
    cron = home / 'cron'
    cron.mkdir()
    (cron / 'jobs.json').write_text(json.dumps({'jobs': [{
        'id': 'weekly-cleanup', 'name': 'Weekly cleanup', 'script': 'cleanup.py',
        'enabled_toolsets': ['web'],
    }]}))
    out = ctx.command('setup', source=source)
    assert '/tingle setup destination' in out
    assert (data / 'setup-state.json').exists()


@pytest.mark.parametrize('enabled_toolsets', [None, []])
def test_unrelated_cron_job_with_nullable_toolsets_allows_automatic_bootstrap(guided, enabled_toolsets):  # noqa: F811
    home, data, source, ctx = _fresh(guided, TELEGRAM_HOME)
    cron = home / 'cron'
    cron.mkdir()
    (cron / 'jobs.json').write_text(json.dumps({'jobs': [{
        'id': 'weekly-cleanup', 'name': 'Weekly cleanup', 'script': 'cleanup.py',
        'enabled_toolsets': enabled_toolsets,
    }]}))
    out = ctx.command('setup', source=source)
    assert '/tingle setup destination' in out
    assert (data / 'setup-state.json').exists()


def test_malformed_cron_inventory_blocks_automatic_bootstrap(guided):  # noqa: F811
    home, data, source, ctx = _fresh(guided, TELEGRAM_HOME)
    cron = home / 'cron'
    cron.mkdir()
    (cron / 'jobs.json').write_text('{"jobs": [')
    out = ctx.command('setup', source=source)
    assert 'No Tingle owner is configured' in out
    assert not (data / 'setup-state.json').exists()


def _write_manifest(home, text):
    (home / '.tingle-resources.json').write_text(text)


def test_duplicate_manifest_key_hiding_a_helper_blocks_bootstrap(guided):  # noqa: F811
    home, data, source, ctx = _fresh(guided, TELEGRAM_HOME)
    text = ('{"version":1,"home":' + json.dumps(str(home.resolve()))
            + ',"files":{"scripts/hermes_tingle_due.py":{"origin":"created","device":1,"inode":2}},'
            + '"files":{},"directories":{},"jobs":{}}')
    _write_manifest(home, text)
    out = ctx.command('setup', source=source)
    assert 'No Tingle owner is configured' in out
    assert not (data / 'setup-state.json').exists()


def test_duplicate_manifest_key_hiding_a_job_blocks_bootstrap(guided):  # noqa: F811
    home, data, source, ctx = _fresh(guided, TELEGRAM_HOME)
    text = ('{"version":1,"home":' + json.dumps(str(home.resolve()))
            + ',"files":{},"directories":{},'
            + '"jobs":{"review":{"origin":"created","definition":{}}},"jobs":{}}')
    _write_manifest(home, text)
    out = ctx.command('setup', source=source)
    assert 'No Tingle owner is configured' in out
    assert not (data / 'setup-state.json').exists()


def test_null_draft_entry_in_manifest_blocks_bootstrap(guided):  # noqa: F811
    home, data, source, ctx = _fresh(guided, TELEGRAM_HOME)
    text = ('{"version":1,"home":' + json.dumps(str(home.resolve()))
            + ',"files":{"plugin-data/hermes-tingle/setup-state.json":null},'
            + '"directories":{},"jobs":{}}')
    _write_manifest(home, text)
    out = ctx.command('setup', source=source)
    assert 'No Tingle owner is configured' in out
    assert not (data / 'setup-state.json').exists()


def test_prior_manifest_helper_blocks_automatic_bootstrap(guided):  # noqa: F811
    home, data, source, ctx = _fresh(guided, TELEGRAM_HOME)
    helper = home / 'scripts' / 'hermes_tingle_due.py'
    helper.parent.mkdir()
    helper.write_text('# existing copied helper')
    resources.record(helper, home=home)
    out = ctx.command('setup', source=source)
    assert 'No Tingle owner is configured' in out
    assert not (data / 'setup-state.json').exists()


def test_prior_manifest_job_blocks_automatic_bootstrap(guided):  # noqa: F811
    home, data, source, ctx = _fresh(guided, TELEGRAM_HOME)
    resources.record_job(home, {
        'id': 'tingle-review', 'name': 'Tingle daily review',
        'script': 'hermes_tingle_snapshot.py', 'schedule': '0 9 * * *',
    })
    out = ctx.command('setup', source=source)
    assert 'No Tingle owner is configured' in out
    assert not (data / 'setup-state.json').exists()


def test_admitted_chat_metadata_alone_still_allows_bootstrap(guided):  # noqa: F811
    home, data, source, ctx = _fresh(guided, TELEGRAM_HOME)
    _write_db(data / 'data.db', [
        'create table known_chats (chat_ref text)',
        "insert into known_chats values ('telegram:42')",
    ])
    out = ctx.command('setup', source=source)
    assert '/tingle setup destination' in out
    assert (data / 'setup-state.json').exists()
