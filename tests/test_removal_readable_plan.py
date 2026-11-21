import importlib.util
import sys
import types
from pathlib import Path


def test_plan_prints_readable_summary_before_exact_inventory(tmp_path, monkeypatch, capsys):
    home = tmp_path / 'home'
    (home / 'plugin-data').mkdir(parents=True)
    stub = types.ModuleType('hermes_constants')
    stub.get_hermes_home = lambda: str(home)
    monkeypatch.setitem(sys.modules, 'hermes_constants', stub)
    spec = importlib.util.spec_from_file_location('tingle_remove_cli', Path(__file__).resolve().parent.parent / 'tools' / 'tingle_remove.py')
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    fake = {'token': 'ABC123', 'jobs': [{'ownership': 'needs explicit adoption'}],
            'resources': {'a': {'kind': 'data', 'ownership': 'created'}, 'b': {'kind': 'data', 'ownership': 'needs explicit adoption'},
                          'c': {'kind': 'helper', 'ownership': 'created'}}}
    monkeypatch.setattr(cli, 'inventory', lambda _home: fake)
    assert cli.main(['--plan']) == 0
    out = capsys.readouterr().out
    print('\n--- removal plan (synthetic inventory)\n' + out)
    assert out.startswith('🧹 Tingle removal check: 2 data, 1 helper.') and '2 item(s) need your ownership decision' in out
    assert out.index('Exact inventory') < out.index('"token"')
    assert 'Native Hermes remove has no Tingle cleanup hook' in out
