"""Fresh profiles default to the configured profile owner; setup has a one-word destination shortcut."""
import json
from types import SimpleNamespace

from test_guided_setup import guided  # noqa: F401 - shared synthetic command fixture


def _fresh_without_owner_file(guided, chat_id='42', user_id='42'):  # noqa: F811
    home, data, source, ctx = guided
    (data / 'setup-owner.json').unlink()
    (home / 'config.yaml').write_text(
        'platforms:\n'
        '  telegram:\n'
        '    home_channel:\n'
        '      platform: telegram\n'
        f"      user_id: '{user_id}'\n"
        f"      chat_id: '{chat_id}'\n"
        "      name: mae\n"
    )
    return home, data, source, ctx


def test_fresh_profile_defaults_owner_from_profile_home_channel(guided):  # noqa: F811
    home, data, source, ctx = _fresh_without_owner_file(guided)
    out = ctx.command('setup', source=source)
    state = json.loads((data / 'setup-state.json').read_text())
    assert state['policy']['owner_principals'] == ['telegram:42']
    assert '/tingle setup destination' in out


def test_another_sender_cannot_self_enroll_from_a_fresh_profile(guided):  # noqa: F811
    home, data, _source, ctx = _fresh_without_owner_file(guided)
    stranger = SimpleNamespace(platform='telegram', user_id='99', chat_id='99', chat_type='dm', is_bot=False)
    out = ctx.command('setup', source=stranger)
    assert 'No Tingle owner is configured' in out
    assert '`setup-owner.json`' in out
    assert '/tingle setup' in out
    assert not (data / 'setup-state.json').exists()


def test_group_home_channel_is_not_a_default_owner(guided):  # noqa: F811
    home, data, source, ctx = _fresh_without_owner_file(guided, chat_id='-1001234567')
    out = ctx.command('setup', source=source)
    assert 'No Tingle owner is configured' in out
    assert not (data / 'setup-state.json').exists()


def test_destination_here_uses_the_current_verified_chat(guided):  # noqa: F811
    home, data, source, ctx = guided
    ctx.command('setup', source=source)
    out = ctx.command('setup destination here', source=source)
    state = json.loads((data / 'setup-state.json').read_text())
    assert state['policy']['delivery'] == 'telegram:42'
    assert '/tingle setup timezone' in out


def test_setup_here_is_an_exact_shortcut_for_the_first_field(guided):  # noqa: F811
    home, data, source, ctx = guided
    ctx.command('setup', source=source)
    out = ctx.command('setup here', source=source)
    state = json.loads((data / 'setup-state.json').read_text())
    assert state['step'] == 1
    assert state['policy']['delivery'] == 'telegram:42'
    assert '/tingle setup timezone' in out


def test_setup_here_shortcut_does_not_fire_on_wrong_argument_count(guided):  # noqa: F811
    home, data, source, ctx = guided
    ctx.command('setup', source=source)
    out = ctx.command('setup here extra', source=source)
    assert 'Setup stopped' in out
    assert not (data / 'setup-state.json').exists()


def test_destination_prompt_shows_copyable_shortcut_and_chat_id(guided):  # noqa: F811
    home, data, source, ctx = guided
    out = ctx.command('setup', source=source)
    assert '/tingle setup here' in out
    assert '`telegram:42`' in out
