"""Hardening for automatic owner defaulting: real YAML, Telegram-only, genuinely fresh data."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

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


def test_admitted_chat_metadata_alone_still_allows_bootstrap(guided):  # noqa: F811
    home, data, source, ctx = _fresh(guided, TELEGRAM_HOME)
    _write_db(data / 'data.db', [
        'create table known_chats (chat_ref text)',
        "insert into known_chats values ('telegram:42')",
    ])
    out = ctx.command('setup', source=source)
    assert '/tingle setup destination' in out
    assert (data / 'setup-state.json').exists()
