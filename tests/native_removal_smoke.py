"""Synthetic native-manager rehearsal for the Tingle local remover.

Run only with Hermes' managed Python and TMPDIR set to a private scratch path.
No real profile, Cron job, account, gateway or credential is used.
"""
import argparse
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def _terminate_group(pid):
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline=time.monotonic()+0.4
    while time.monotonic()<deadline:
        try:
            os.killpg(pid,0)
        except ProcessLookupError:
            return
        time.sleep(0.02)
    try:
        os.killpg(pid,signal.SIGKILL)
    except ProcessLookupError:
        pass


def _run_owned_command(command,*,cwd,env,timeout):
    """Capture logs without pipes and always reap the entire child process group."""
    with tempfile.TemporaryFile(mode='w+b') as log:
        process=subprocess.Popen(command,cwd=cwd,env=env,stdin=subprocess.DEVNULL,
                                 stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        try:
            code=process.wait(timeout=timeout)
        finally:
            _terminate_group(process.pid)
            process.wait(timeout=5)
        log.seek(0)
        output=log.read().decode('utf-8',errors='replace')
    return code,output


def _native_plugin_remove(home):
    """Simulate the operator's separate, explicit-profile Hermes removal."""
    env={**os.environ,'HOME':str(home),'HERMES_HOME':str(home),
         'HERMES_DISABLE_LAZY_INSTALLS':'1','HERMES_ENABLE_PROJECT_PLUGINS':'0'}
    command=[sys.executable,'-m','hermes_cli.main','-p','default','plugins','remove','hermes-tingle']
    code,output=_run_owned_command(command,cwd=home,env=env,timeout=120)
    if code:raise RuntimeError('Synthetic native plugin removal failed: '+output[-2000:])


def _cleanup_home(home):
    try:
        shutil.rmtree(home)
        return None
    except OSError:
        if not home.exists():
            # A vanishing scratch path is success for cleanup, but a real
            # permission error with the path still present must propagate.
            return None
        files={str(path.relative_to(home)) for path in home.rglob('*') if path.is_file()}
        paths={str(path.relative_to(home)) for path in home.rglob('*')}
        allowed={'cache','cache/plugin-catalog.json','cache/default-plugins.settled'}
        expected_files=allowed-{'cache'}
        if not files or not files<=expected_files or not paths<=allowed:raise
        return str(home/'cache')


def _run_smoke(keep):
    scratch=os.environ.get('TMPDIR')
    if not scratch:raise RuntimeError('Set TMPDIR to a private scratch directory before running this smoke test')
    home=Path(tempfile.mkdtemp(prefix='tingle-removal-native-',dir=scratch))
    result=None
    residue=None
    try:
        os.environ['HERMES_HOME']=str(home)
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
        import hermes_tingle_resources as r
        import hermes_tingle_removal as remove
        from cron import jobs
        from hermes_cli.plugins_cmd_git import _write_install_metadata,_read_install_metadata

        # Synthetic install, policy, helpers and SQLite stores only.
        plugin=home/'plugins'/'hermes-tingle';plugin.mkdir(parents=True)
        (plugin/'plugin.yaml').write_text('name: hermes-tingle\nversion: "0.9.0"\ndescription: Synthetic removal rehearsal\n')
        (plugin/'__init__.py').write_text('def register(ctx): pass\n')
        _write_install_metadata({'hermes-tingle':{'source':'synthetic-local-fixture'}})
        (home/'hermes-tingle.json').write_text('{"synthetic":true}')
        r.record(home/'hermes-tingle.json',home=home)
        scripts=home/'scripts';scripts.mkdir()
        helper=scripts/'hermes_tingle_due.py';helper.write_text('print(\'synthetic\')\n');r.record(helper,home=home)
        for namespace in r.NAMES:
            root=home/'plugin-data'/namespace;root.mkdir(parents=True)
            conn=sqlite3.connect(root/'data.db');conn.execute('create table synthetic(x)');conn.commit();conn.close()
            r.record(root,home=home,directory=True)
        with jobs.use_cron_store(home):
            job=jobs.create_job(prompt=None,schedule='30m',name='[S] Hermes Tingle deadline nudges',script='hermes_tingle_due.py',no_agent=True)
            other=jobs.create_job(prompt='unrelated synthetic',schedule='60m',name='other synthetic')
        r.record_job(home,job)

        # Tingle refuses before mutation; this is a separate native Hermes action.
        plan=remove.inventory(home)
        with jobs.use_cron_store(home):
            before_jobs={item['id']:item for item in jobs.list_jobs(include_disabled=True)}
        before_files={str(path.relative_to(home)):path.read_bytes() for path in home.rglob('*') if path.is_file()}
        try:
            remove.remove(home,plan,confirmation='REMOVE '+plan['token'],writers_stopped=True,
                          adopt_legacy=True,keep_data=keep)
        except ValueError as exc:
            if 'Cron jobs remain' not in str(exc):raise
        else:
            raise AssertionError('Tingle remover changed active Cron jobs instead of refusing')
        with jobs.use_cron_store(home):
            assert {item['id']:item for item in jobs.list_jobs(include_disabled=True)}==before_jobs
        assert {str(path.relative_to(home)):path.read_bytes() for path in home.rglob('*') if path.is_file()}==before_files
        assert not (home/r.MARKER).exists() and plugin.exists()

        with jobs.use_cron_store(home):
            assert jobs.remove_job(job['id']) is True  # Separate Hermes-native operator action.
            assert jobs.get_job(job['id']) is None and jobs.get_job(other['id']) is not None
        plan=remove.inventory(home)
        assert plan['jobs']==[]
        result=remove.remove(home,plan,confirmation='REMOVE '+plan['token'],writers_stopped=True,
                             adopt_legacy=True,keep_data=keep)
        assert result['cleanup_completed'] and result['plugin_uninstall_required']
        assert plugin.exists() and 'hermes-tingle' in _read_install_metadata()
        assert (home/'hermes-tingle.json').exists()==keep
        assert not (home/'.tingle-removal-recovery').exists() and not (home/r.MARKER).exists()
        for namespace in r.NAMES:assert (home/'plugin-data'/namespace).exists()==keep
        with jobs.use_cron_store(home):
            unrelated_job_retained=jobs.get_job(other['id']) is not None
        assert unrelated_job_retained
        # Separate explicit-profile Hermes action removes only the plugin install/ledger.
        _native_plugin_remove(home)
        assert not plugin.exists() and 'hermes-tingle' not in _read_install_metadata()
        with jobs.use_cron_store(home):
            unrelated_job_retained=jobs.get_job(other['id']) is not None
        assert unrelated_job_retained
        if not keep:
            # A clean reinstall has no inherited source grants, tracked items or budgets.
            sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
            from tingle_init import write_policy,DEFAULTS
            import hermes_tingle_store as store
            write_policy(home,{**DEFAULTS,'delivery':'telegram:42'},None)
            assert store.list_items()==[] and not any(row['enabled'] for row in store.list_source_optins())
        result={'separate_native_plugin_remove_verified':True,'plugin_preserved_until_manual_remove':True,
                'exact_owned_job_removed':True,'unrelated_job_retained':unrelated_job_retained,
                'default_namespace_delete_or_explicit_keep':True,
                'keep_data':keep,'clean_reinstall_checked':not keep}
    finally:
        residue=_cleanup_home(home)
    result={'result':'PASS',**result,'scratch_profile_cache_residue':residue}
    return result


def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument('--keep-data',action='store_true')
    args=parser.parse_args(argv)
    print(json.dumps(_run_smoke(args.keep_data),sort_keys=True))


if __name__=='__main__':
    main()
