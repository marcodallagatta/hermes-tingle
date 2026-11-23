import importlib.util
import os
import subprocess
import signal
import sys
import time
import types
from pathlib import Path

import pytest

_REAL_POPEN = subprocess.Popen


def _stub_profiles(monkeypatch,home,profile='test-profile'):
    profiles=types.ModuleType('hermes_cli.profiles')
    setattr(profiles,'get_profile_dir',lambda name: home if name==profile else pytest.fail('wrong profile selected'))
    monkeypatch.setitem(sys.modules,'hermes_cli.profiles',profiles)


def test_cli_reports_explicit_hermes_plugin_removal_after_tingle_cleanup(tmp_path,monkeypatch,capsys):
    import builtins
    home=tmp_path/'profile';home.mkdir()
    _stub_profiles(monkeypatch,home,'named-test')
    spec=importlib.util.spec_from_file_location('tingle_remove_cli_manual_plugin',Path(__file__).resolve().parent.parent/'tools'/'tingle_remove.py')
    assert spec is not None and spec.loader is not None
    cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
    plan={'home':str(home),'token':'ABC123','jobs':[],'resources':{},'plugin_installed':True,
          'manager_record_present':True}
    monkeypatch.setattr(cli,'inventory',lambda actual:plan if actual==home else pytest.fail('wrong home inventoried'))
    removed=[]
    monkeypatch.setattr(cli,'remove',lambda *args,**kwargs:removed.append((args,kwargs)) or
                        {'cleanup_completed':True,'plugin_uninstall_required':True,'kept_data':False,'limits':'synthetic'})
    answers=iter(['ALL WRITERS STOPPED','REMOVE ABC123'])
    monkeypatch.setattr(builtins,'input',lambda _prompt:next(answers))
    assert cli.main(['-p','named-test'])==0
    out=capsys.readouterr().out
    assert removed and 'hermes -p named-test plugins remove hermes-tingle' in out
    assert 'Tingle cleanup complete' in out


def test_keep_data_help_does_not_claim_job_removal(tmp_path, monkeypatch, capsys):
    import importlib.util
    home=tmp_path/'home';(home/'plugin-data').mkdir(parents=True)
    stub=types.ModuleType('hermes_constants');stub.get_hermes_home=lambda:str(home)
    monkeypatch.setitem(sys.modules,'hermes_constants',stub)
    spec=importlib.util.spec_from_file_location('tingle_remove_cli_help',Path(__file__).resolve().parent.parent/'tools'/'tingle_remove.py')
    cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
    with pytest.raises(SystemExit) as exc:
        cli.main(['--help'])
    assert exc.value.code==0
    out=' '.join(capsys.readouterr().out.split())
    assert 'jobs/helpers/install are still removed' not in out
    assert 'Cron jobs must be removed manually first' in out
    assert 'plugin uninstall is a separate Hermes command' in out
    assert 'explicit existing hermes profile name' in out.lower()


@pytest.mark.parametrize('args',[['-p','test-profile'],['-p','test-profile','--keep-data']])
def test_cli_refuses_real_run_with_jobs_before_prompt_or_removal(tmp_path,monkeypatch,capsys,args):
    import builtins
    import importlib.util
    home=tmp_path/'home';home.mkdir()
    _stub_profiles(monkeypatch,home)
    spec=importlib.util.spec_from_file_location('tingle_remove_cli_refusal',Path(__file__).resolve().parent.parent/'tools'/'tingle_remove.py')
    assert spec is not None and spec.loader is not None
    cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
    plan={'home':str(home),'token':'ABC123','jobs':[{'id':'TINGLE-1'}],'resources':{},
          'plugin_installed':True,'manager_record_present':False}
    monkeypatch.setattr(cli,'inventory',lambda _home:plan)
    removed=[]
    monkeypatch.setattr(cli,'remove',lambda *a,**k:removed.append((a,k)))
    monkeypatch.setattr(builtins,'input',lambda _prompt:pytest.fail('refusal must happen before prompting'))
    assert cli.main(args)==1
    out,err=capsys.readouterr()
    assert 'Exact IDs: TINGLE-1' in out and 'If an ID disappears' in out and 'No Tingle resources were changed' in err
    assert removed==[]


def _load_native_removal_smoke():
    path=Path(__file__).resolve().parent/'native_removal_smoke.py'
    spec=importlib.util.spec_from_file_location('native_removal_smoke_test',path)
    assert spec is not None and spec.loader is not None
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def test_native_smoke_cleanup_tolerates_only_known_profile_cache(tmp_path,monkeypatch):
    smoke=_load_native_removal_smoke()
    home=tmp_path/'home';cache=home/'cache';cache.mkdir(parents=True)
    (cache/'plugin-catalog.json').write_text('{}')
    (cache/'default-plugins.settled').write_text('')
    def denied(_path):raise OSError('synthetic rmtree failure')
    monkeypatch.setattr(smoke.shutil,'rmtree',denied)
    assert smoke._cleanup_home(home)==str(cache)


def test_native_smoke_cleanup_propagates_permission_error_without_expected_residue(tmp_path,monkeypatch):
    smoke=_load_native_removal_smoke()
    home=tmp_path/'home';home.mkdir()
    def denied(_path):raise PermissionError('synthetic permission error')
    monkeypatch.setattr(smoke.shutil,'rmtree',denied)
    with pytest.raises(PermissionError,match='synthetic permission error'):
        smoke._cleanup_home(home)


def test_native_smoke_cleanup_propagates_unexpected_residue(tmp_path,monkeypatch):
    smoke=_load_native_removal_smoke()
    home=tmp_path/'home';home.mkdir()
    (home/'unexpected.txt').write_text('not expected cache residue')
    def denied(_path):raise PermissionError('synthetic permission error')
    monkeypatch.setattr(smoke.shutil,'rmtree',denied)
    with pytest.raises(PermissionError,match='synthetic permission error'):
        smoke._cleanup_home(home)


@pytest.mark.parametrize('exit_code',[0,7])
def test_native_smoke_owned_process_group_reaps_descendants(tmp_path,monkeypatch,exit_code):
    smoke=_load_native_removal_smoke()
    monkeypatch.setattr(smoke.subprocess,'Popen',_REAL_POPEN)
    pid_file=tmp_path/'group-pids.txt'
    child=("import os,subprocess,sys; "
           "desc=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']); "
           f"open({str(pid_file)!r},'w').write(str(os.getpid())+' '+str(desc.pid)); "
           f"sys.exit({exit_code})")
    env={'HOME':str(tmp_path),'HERMES_HOME':str(tmp_path),'TMPDIR':os.environ.get('TMPDIR',str(tmp_path))}
    parent_pid=None
    try:
        started=time.monotonic()
        result,output=smoke._run_owned_command([sys.executable,'-c',child],cwd=tmp_path,env=env,timeout=3)
        assert result==exit_code and time.monotonic()-started<2
        parent_pid,descendant_pid=map(int,pid_file.read_text().split())
        for _ in range(40):
            try:
                state=(Path('/proc')/str(descendant_pid)/'stat').read_text().rsplit(')',1)[1].split()[0]
                if state=='Z':break
            except FileNotFoundError:
                break
            time.sleep(0.05)
        else:pytest.fail('native-manager descendant survived process-group cleanup')
    finally:
        if parent_pid is None and pid_file.exists():
            parent_pid,_=map(int,pid_file.read_text().split())
        if parent_pid is not None:
            try:os.killpg(parent_pid,signal.SIGKILL)
            except ProcessLookupError:pass


def _assert_pinned_smoke_contract(text):
    marker='\n  hermes-compat:\n'
    assert marker in text
    compat=text.split(marker,1)[1]
    step_name='      - name: Run native removal smoke (DELETE and KEEP)\n'
    assert 'matrix:' in compat and 'hermes_commit:' in compat
    assert step_name in compat and compat.index('hermes_commit:')<compat.index(step_name)
    tail=compat.split(step_name,1)[1]
    env_block=tail.split('        run: |\n',1)[0]
    env_lines=[line[10:] for line in env_block.splitlines() if line.startswith('          ')]
    assert env_lines==[
        'HOME: ${{ runner.temp }}/tingle-smoke-home',
        'HERMES_HOME: ${{ runner.temp }}/tingle-smoke-home',
        'TMPDIR: ${{ runner.temp }}/tingle-smoke-tmp',
        'PYTHONPATH: ${{ github.workspace }}:${{ runner.temp }}/hermes-agent',
    ]
    run_block=tail.split('        run: |\n',1)[1]
    run_lines=[]
    for line in run_block.splitlines():
        if line.startswith('          '):run_lines.append(line[10:])
        elif line.strip():break
    assert run_lines==[
        'mkdir -m 700 -p "$HOME" "$TMPDIR"',
        '"$RUNNER_TEMP/hermes-agent/.venv/bin/python" tests/native_removal_smoke.py',
        '"$RUNNER_TEMP/hermes-agent/.venv/bin/python" tests/native_removal_smoke.py --keep-data',
    ]
    assert 'continue-on-error: true' not in tail
    assert all('||' not in line and 'set +e' not in line for line in run_lines)


def test_pinned_hermes_ci_runs_native_removal_smoke_in_both_modes():
    workflow=Path(__file__).resolve().parent.parent/'.github'/'workflows'/'test.yml'
    _assert_pinned_smoke_contract(workflow.read_text())


def test_pinned_smoke_ci_contract_rejects_bypassed_commands_and_unsafe_home():
    workflow=Path(__file__).resolve().parent.parent/'.github'/'workflows'/'test.yml'
    text=workflow.read_text()
    mutations=[
        text.replace('          "$RUNNER_TEMP/hermes-agent/.venv/bin/python" tests/native_removal_smoke.py\n',
                    '          # "$RUNNER_TEMP/hermes-agent/.venv/bin/python" tests/native_removal_smoke.py\n',1),
        text.replace('tests/native_removal_smoke.py --keep-data\n',
                    'tests/native_removal_smoke.py --keep-data || true\n',1),
        text.replace('          HOME: ${{ runner.temp }}/tingle-smoke-home\n',
                    '          HOME: /home/marco/.hermes\n',1),
    ]
    for mutated in mutations:
        with pytest.raises(AssertionError):_assert_pinned_smoke_contract(mutated)


def test_native_smoke_failure_cleans_profile_and_never_prints_pass(tmp_path,monkeypatch,capsys):
    from runtime_support import hermes_import
    hermes_import('cron.jobs')
    import hermes_tingle_resources as resources
    smoke=_load_native_removal_smoke()
    created=[]
    real_mkdtemp=smoke.tempfile.mkdtemp
    def record_mkdtemp(*args,**kwargs):
        path=real_mkdtemp(*args,**kwargs);created.append(Path(path));return path
    monkeypatch.setattr(smoke.tempfile,'mkdtemp',record_mkdtemp)
    monkeypatch.setenv('TMPDIR',str(tmp_path))
    outer=tmp_path/'outer';outer.mkdir()
    monkeypatch.setenv('HOME',str(outer));monkeypatch.setenv('HERMES_HOME',str(outer))
    def fail_record_job(*_args,**_kwargs):raise RuntimeError('synthetic smoke failure')
    monkeypatch.setattr(resources,'record_job',fail_record_job)
    with pytest.raises(RuntimeError,match='synthetic smoke failure'):
        smoke.main([])
    assert len(created)==1
    if created[0].exists():
        paths={str(path.relative_to(created[0])) for path in created[0].rglob('*')}
        files={str(path.relative_to(created[0])) for path in created[0].rglob('*') if path.is_file()}
        allowed={'cache','cache/plugin-catalog.json','cache/default-plugins.settled'}
        assert paths<=allowed and files<=(allowed-{'cache'}) and files
    assert capsys.readouterr().out==''


def test_uninstall_examples_use_same_named_profile_for_every_step():
    doc=Path(__file__).resolve().parent.parent/'docs'/'uninstall.md'
    text=doc.read_text()
    blocks=text.split('```text\n')[1:]
    assert len(blocks)>=2
    main=blocks[0].split('\n```',1)[0]
    main_commands=[line.strip() for line in main.splitlines() if line.strip()]
    assert len(main_commands)==5
    assert all('-p <profile-name>' in command for command in main_commands)
    assert main_commands[-1]=='hermes -p <profile-name> plugins remove hermes-tingle'
    assert '`-p default`' in text
    assert 'do not retry a stale ID' in text
    recovery=blocks[1].split('\n```',1)[0]
    commands=[line.strip() for line in recovery.splitlines() if line.strip()]
    assert len(commands)==2
    assert all('-p <profile-name>' in command for command in commands)
    assert 'python <profile-home>/.tingle-removal-recovery/tingle_remove.py' in '\n'.join(commands)


def test_plan_prints_readable_summary_before_exact_inventory(tmp_path, monkeypatch, capsys):
    home=tmp_path/'home';(home/'plugin-data').mkdir(parents=True)
    _stub_profiles(monkeypatch,home)
    spec = importlib.util.spec_from_file_location('tingle_remove_cli', Path(__file__).resolve().parent.parent / 'tools' / 'tingle_remove.py')
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    fake={'home':str(home),'token':'ABC123','jobs':[{'id':'job1','ownership':'needs explicit adoption'}],
          'resources':{'a':{'kind':'data','ownership':'created'},'b':{'kind':'data','ownership':'needs explicit adoption'},
                       'c':{'kind':'helper','ownership':'created'}},'plugin_installed':True,'manager_record_present':False}
    monkeypatch.setattr(cli,'inventory',lambda _home:fake)
    assert cli.main(['-p','test-profile','--plan'])==0
    out = capsys.readouterr().out
    print('\n--- removal plan (synthetic inventory)\n' + out)
    assert out.startswith('🧹 Tingle removal check: 2 data, 1 helper.') and '1 item(s) need your ownership decision' in out
    assert out.index('Exact inventory') < out.index('"token"')
    assert 'Cron jobs are never changed' in out and 'Exact IDs: job1' in out
    assert 'manual Hermes cleanup' in out
    assert 'hermes -p test-profile plugins remove hermes-tingle' in out
