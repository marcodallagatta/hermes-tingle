"""Removal proceeds with a live gateway and tells the operator to restart it afterwards."""
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

import hermes_tingle_removal as removal
import hermes_tingle_resources as resources


def _profile(tmp_path, gateway_pid):
    home = tmp_path / 'profile'
    root = home / 'plugin-data' / 'hermes-tingle'
    root.mkdir(parents=True)
    (root / 'data.db').write_bytes(b'x')
    resources.record(root, home=home, directory=True)
    resources.record(root / 'data.db', home=home)
    install = home / 'plugins' / 'hermes-tingle'
    install.mkdir(parents=True)
    (install / 'plugin.yaml').write_text('name: hermes-tingle')
    api = {'list': lambda: [], 'get': lambda job_id: None,
           'gateway': lambda actual: gateway_pid}
    return home, api


def test_live_gateway_no_longer_blocks_cleanup(tmp_path):
    home, api = _profile(tmp_path, 999999)  # non-existent PID: gateway reported live
    plan = removal.inventory(home, api)
    assert not plan['jobs']
    result = removal.remove(home, plan, confirmation='REMOVE ' + plan['token'],
                            writers_stopped=True, adopt_legacy=True, api=api)
    assert result['cleanup_completed'] is True
    assert result['gateway_restart_required'] is True
    assert not (home / 'plugin-data' / 'hermes-tingle' / 'data.db').exists()


def test_no_live_gateway_means_no_restart_notice(tmp_path):
    home, api = _profile(tmp_path, None)
    plan = removal.inventory(home, api)
    result = removal.remove(home, plan, confirmation='REMOVE ' + plan['token'],
                            writers_stopped=True, adopt_legacy=True, api=api)
    assert result['gateway_restart_required'] is False


def test_cli_tells_operator_to_restart_from_separate_terminal(tmp_path, monkeypatch, capsys):
    import builtins
    home = tmp_path / 'profile'
    home.mkdir()
    profiles = types.ModuleType('hermes_cli.profiles')
    setattr(profiles, 'get_profile_dir', lambda name: home if name == 'named' else pytest.fail('wrong profile'))
    monkeypatch.setitem(sys.modules, 'hermes_cli.profiles', profiles)
    spec = importlib.util.spec_from_file_location(
        'tingle_remove_cli_restart', Path(__file__).resolve().parents[1] / 'tools' / 'tingle_remove.py')
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    plan = {'home': str(home), 'token': 'ABC123', 'jobs': [], 'resources': {},
            'plugin_installed': True, 'manager_record_present': True}
    monkeypatch.setattr(cli, 'inventory', lambda actual: plan)
    monkeypatch.setattr(cli, 'remove', lambda *args, **kwargs: {
        'cleanup_completed': True, 'plugin_uninstall_required': True,
        'kept_data': False, 'limits': 'synthetic', 'gateway_restart_required': True})
    answers = iter(['ALL WRITERS STOPPED', 'REMOVE ABC123'])
    monkeypatch.setattr(builtins, 'input', lambda _prompt: next(answers))
    assert cli.main(['-p', 'named']) == 0
    out = capsys.readouterr().out
    assert 'hermes gateway restart' in out
    assert 'separate terminal' in out
    assert 'new session' in out
