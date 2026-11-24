"""Fresh profiles default to the configured profile owner; setup has a one-word destination shortcut."""
import json
from types import SimpleNamespace

import hermes_tingle as plugin
from test_guided_setup import guided  # noqa: F401 - shared synthetic command fixture


def _fresh_without_owner_file(guided, chat_id='42', user_id='42'):  # noqa: F811
    home, data, source, ctx = guided
    (data / 'setup-owner.json').unlink()
    (home / 'config.yaml').write_text(
        'timezone: Etc/UTC\n'
        'platforms:\n'
        '  telegram:\n'
        '    home_channel:\n'
        '      platform: telegram\n'
        f"      user_id: '{user_id}'\n"
        f"      chat_id: '{chat_id}'\n"
        "      name: mae\n"
    )
    return home, data, source, ctx


def test_fresh_profile_defaults_owner_from_profile_home_channel(guided, monkeypatch):  # noqa: F811
    home, data, source, ctx = _fresh_without_owner_file(guided)
    monkeypatch.delenv('HERMES_TIMEZONE', raising=False)
    ctx.answer = None
    out = plugin.handle_command(ctx, '', source=source)
    state = json.loads((data / 'setup-state.json').read_text())
    assert state['policy']['owner_principals'] == ['telegram:42']
    assert state['policy']['delivery'] == 'telegram:42'
    assert state['policy']['timezone'] == 'Etc/UTC'
    assert state['step'] == 'approval'
    assert state['sources'] == 'all'
    assert '`/tingle setup confirm WRITE`' in out
    assert len(ctx.questions) == 1


def test_another_sender_cannot_self_enroll_from_a_fresh_profile(guided):  # noqa: F811
    home, data, _source, ctx = _fresh_without_owner_file(guided)
    stranger = SimpleNamespace(platform='telegram', user_id='99', chat_id='99', chat_type='dm', is_bot=False)
    out = ctx.command('setup', source=stranger)
    assert 'No Tingle owner is configured' in out
    assert '`setup-owner.json`' in out
    assert '`/tingle`' in out
    assert not (data / 'setup-state.json').exists()


def test_group_home_channel_is_not_a_default_owner(guided):  # noqa: F811
    home, data, source, ctx = _fresh_without_owner_file(guided, chat_id='-1001234567')
    out = ctx.command('setup', source=source)
    assert 'No Tingle owner is configured' in out
    assert not (data / 'setup-state.json').exists()


def test_destination_is_defaulted_without_an_extra_step(guided):  # noqa: F811
    home, data, source, ctx = guided
    ctx.answer = None

    out = plugin.handle_command(ctx, '', source=source)

    state = json.loads((data / 'setup-state.json').read_text())
    assert state['policy']['delivery'] == 'telegram:42'
    assert state['step'] == 'approval'
    assert '`/tingle setup confirm WRITE`' in out
    assert 'telegram:42' in out
    assert 'destination <value>' not in out
    assert len(ctx.questions) == 1
