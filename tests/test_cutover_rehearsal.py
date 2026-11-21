"""Source-off disposable-only cutover qualification, never live deployment.

The inherited unsafe-runner reproducer was run separately. These assertions
have the opposite polarity: no old-code activation after migration may begin.
"""
import copy
import importlib.util
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys

import pytest

pytest.importorskip('fcntl', reason='synthetic process recovery qualification is POSIX-only')
# Hermes itself owns a regular `tools` package. Load this repository-local
# qualification script by exact path, never through the runtime's namespace.
_spec = importlib.util.spec_from_file_location(
    'tingle_cutover_rehearsal', Path(__file__).resolve().parents[1] / 'tools/cutover_rehearsal.py')
cut = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cut)


@pytest.fixture
def backend(tmp_path):
    return cut.SyntheticBackend.create(tmp_path / 'fixture')


def assert_held(backend):
    assert not any(backend.states().values())
    assert all(not backend.jobs()[job]['enabled'] for job in cut.TARGET_JOBS)


def assert_new(backend):
    assert all(backend.target(item['target']).read_bytes() == b'new synthetic file\n'
               for item in backend.spec)


def migrated(backend):
    with cut.database(backend.db) as connection:
        return bool(connection.execute("select 1 from sqlite_master where name='new_schema'").fetchone())


def baseline(backend):
    return {'jobs': backend.jobs(), 'services': backend.states(),
            'files': {item['target']: backend.target(item['target']).read_bytes() for item in backend.spec},
            'config': backend.target('config.json').read_bytes()}


def fail(backend, point):
    backend.fail_at = point
    with pytest.raises(RuntimeError, match='synthetic injected'):
        cut.Runner(backend).run()


def test_commits_coherent_synthetic_install_preserving_fields(backend):
    original = baseline(backend)
    # Previously disabled jobs and services stay disabled.
    backend.update_job(cut.TARGET_JOBS[1], {'enabled': False})
    backend.service(cut.SERVICES[1], False)
    original = baseline(backend)
    cut.Runner(backend).run()
    assert cut.Runner(backend).journal()['phase'] == 'committed'
    assert_new(backend)
    assert migrated(backend)
    expected = copy.deepcopy(original['jobs'])
    expected[cut.TARGET_JOBS[0]]['prompt'] = 'reviewed synthetic prompt'
    assert backend.jobs() == expected
    assert backend.states() == original['services']
    assert backend.target('config.json').read_bytes() == original['config']
    assert (backend.root / 'operation/database.sqlite').stat().st_mode & 0o777 == 0o600
    with cut.database(backend.db) as connection:
        assert connection.execute('select * from original').fetchall() == [('synthetic preserved',)]


PRE_BOUNDARY = [
    'before:pause:synthetic-daily', 'after:pause:synthetic-daily',
    'after:pause:synthetic-due', 'before:stop:synthetic-backend',
    'after:stop:synthetic-backend', 'after:stop:synthetic-gateway',
    'before:backup', 'after:backup',
    *[point + ':file:' + str(index) for index in range(6) for point in ('before', 'after')],
]


@pytest.mark.parametrize('point', PRE_BOUNDARY)
def test_each_pre_migration_interruption_restores_exact_old_state(backend, point):
    old = baseline(backend)
    fail(backend, point)
    assert cut.Runner(backend).journal()['phase'] == 'rolled-back'
    assert baseline(backend) == old
    assert not migrated(backend)
    cut.Runner(backend).recover()  # idempotent
    assert baseline(backend) == old


POST_BOUNDARY = [
    'before:migrate', 'after:migrate',
    'before:prompt:synthetic-daily', 'after:prompt:synthetic-daily',
    'before:offline-health', 'after:offline-health',
    'before:start:synthetic-backend', 'after:start:synthetic-backend',
    'before:start:synthetic-gateway', 'after:start:synthetic-gateway',
    'before:online-health', 'after:online-health',
    'before:resume:synthetic-daily', 'after:resume:synthetic-daily',
    'before:resume:synthetic-due', 'after:resume:synthetic-due',
]


@pytest.mark.parametrize('point', POST_BOUNDARY)
def test_every_ambiguous_or_post_migration_failure_is_forward_only(backend, point):
    unrelated = backend.jobs()['synthetic-unrelated']
    fail(backend, point)
    journal = cut.Runner(backend).journal()
    assert journal['migration_possible'] is True
    assert journal['phase'] == 'forward-repair'
    assert_held(backend)
    assert_new(backend)
    assert backend.jobs()['synthetic-unrelated'] == unrelated
    assert migrated(backend) is (point != 'before:migrate')
    cut.Runner(backend).recover()
    assert_held(backend)
    assert_new(backend)


def test_boundary_is_durable_before_migration_call(backend):
    original = backend.migrate
    def migrate():
        j = cut.Runner(backend).journal()
        assert j['migration_possible'] is True and j['intent'] == 'migrate'
        original()
        raise TimeoutError('outcome unknown')
    backend.migrate = migrate
    with pytest.raises(TimeoutError):
        cut.Runner(backend).run()
    assert migrated(backend)
    assert_held(backend)
    assert_new(backend)
    with pytest.raises(cut.GateError, match='monotonic'):
        cut.Runner(backend).save(cut.Runner(backend).journal(), migration_possible=False)


def test_migration_exception_without_commit_still_forward_only(backend):
    def migrate():
        raise sqlite3.OperationalError('synthetic busy')
    backend.migrate = migrate
    with pytest.raises(sqlite3.OperationalError):
        cut.Runner(backend).run()
    assert not migrated(backend)
    assert_new(backend)
    assert_held(backend)
    assert cut.Runner(backend).journal()['phase'] == 'forward-repair'


@pytest.mark.parametrize('health', ['offline_health', 'online_health'])
def test_schema_handler_health_failures_never_restart_old(backend, health):
    def bad():
        raise cut.GateError('synthetic registered handler/schema mismatch')
    setattr(backend, health, bad)
    with pytest.raises(cut.GateError, match='handler/schema'):
        cut.Runner(backend).run()
    assert migrated(backend)
    assert_held(backend)
    assert_new(backend)


def test_later_records_never_replaced_by_checkpoint(backend):
    def hook(point):
        if point == 'after:start:synthetic-backend':
            with cut.database(backend.db) as connection:
                connection.execute('insert into original values (?)', ('later synthetic edit',))
            raise RuntimeError('activation uncertain')
    backend.hook = hook
    with pytest.raises(RuntimeError):
        cut.Runner(backend).run()
    assert_held(backend)
    assert_new(backend)
    with cut.database(backend.db) as connection:
        assert connection.execute('select * from original').fetchall() == [
            ('synthetic preserved',), ('later synthetic edit',)]
    with cut.database(backend.root / 'operation/database.sqlite') as connection:
        assert connection.execute('select count(*) from original').fetchone() == (1,)


@pytest.mark.parametrize('point', ['after:file:1', 'after:migrate'])
def test_file_conflict_preserved_not_overwritten_or_activated(backend, point):
    path = backend.target('plugin/runtime.py')
    def hook(name):
        if name == point:
            cut.atomic(path, b'competing synthetic edit\n', 0o644)
            raise RuntimeError('competing edit')
    backend.hook = hook
    with pytest.raises(RuntimeError):
        cut.Runner(backend).run()
    assert path.read_bytes() == b'competing synthetic edit\n'
    assert cut.Runner(backend).journal()['phase'] == 'conflict'
    assert_held(backend)


@pytest.mark.parametrize('point', ['after:file:1', 'after:migrate'])
def test_job_conflict_is_preserved_except_safety_pause(backend, point):
    def hook(name):
        if name == point:
            backend.update_job(cut.TARGET_JOBS[0], {'prompt': 'competing synthetic prompt'})
            raise RuntimeError('competing job edit')
    backend.hook = hook
    with pytest.raises(RuntimeError):
        cut.Runner(backend).run()
    assert backend.jobs()[cut.TARGET_JOBS[0]]['prompt'] == 'competing synthetic prompt'
    assert cut.Runner(backend).journal()['phase'] == 'conflict'
    assert_held(backend)


def test_unrelated_job_change_preserved_through_recovery(backend):
    def hook(name):
        if name == 'after:migrate':
            jobs = backend.jobs()
            jobs['synthetic-unrelated']['prompt'] = 'new unrelated synthetic prompt'
            cut.write_json(backend.root / 'jobs.json', jobs)
            raise RuntimeError('injected')
    backend.hook = hook
    with pytest.raises(RuntimeError):
        cut.Runner(backend).run()
    assert backend.jobs()['synthetic-unrelated']['prompt'] == 'new unrelated synthetic prompt'
    assert_held(backend)


@pytest.mark.parametrize('point', ['after:pause:synthetic-daily', 'after:prompt:synthetic-daily',
                                   'after:resume:synthetic-daily'])
def test_ambiguous_job_api_persistence_is_recoverable(backend, point):
    fail(backend, point)
    assert cut.Runner(backend).journal()['phase'] == (
        'rolled-back' if 'pause' in point else 'forward-repair')


RECOVERY_POINTS = ['after:restore-file:0', 'after:restore-file:2',
                   'after:restore-job:synthetic-daily', 'after:restore-job:synthetic-due',
                   'after:restore-service:synthetic-backend', 'after:restore-service:synthetic-gateway',
                   'after:restore-enabled:synthetic-daily', 'after:restore-enabled:synthetic-due']


@pytest.mark.parametrize('point', RECOVERY_POINTS)
def test_pre_boundary_recovery_itself_can_be_interrupted_and_restarted(backend, point):
    old = baseline(backend)
    def hook(name):
        if name in ('after:file:2', point):
            raise RuntimeError('interruption')
    backend.hook = hook
    with pytest.raises(RuntimeError):
        cut.Runner(backend).run()
    backend.hook = None
    cut.Runner(backend).recover()
    assert cut.Runner(backend).journal()['phase'] == 'rolled-back'
    assert baseline(backend) == old
    assert not migrated(backend)


def test_unknown_journal_outcome_fails_stopped_never_old_restart(backend):
    def hook(point):
        if point == 'after:migrate':
            cut.atomic(backend.root / 'operation/journal.json', b'broken synthetic journal')
            raise RuntimeError('journal corrupt')
    backend.hook = hook
    with pytest.raises(cut.GateError, match='journal'):
        cut.Runner(backend).run()
    assert_new(backend)
    assert_held(backend)
    assert migrated(backend)


@pytest.mark.parametrize('kind', ['candidate', 'extra', 'symlink', 'mode', 'hardlink', 'config', 'claim'])
def test_preflight_rejects_drift_unknown_files_unsafe_paths_and_claims(backend, kind):
    old_services = backend.states()
    if kind == 'candidate':
        cut.atomic(backend.source('runtime.py'), b'changed', 0o644)
    elif kind == 'extra':
        cut.atomic(backend.target('plugin/unknown.py'), b'unexpected')
    elif kind == 'symlink':
        backend.target('plugin/runtime.py').unlink()
        backend.target('plugin/runtime.py').symlink_to(backend.source('runtime.py'))
    elif kind == 'mode':
        backend.root.chmod(0o755)
    elif kind == 'hardlink':
        os.link(backend.target('plugin/runtime.py'), backend.root / 'linked')
    elif kind == 'config':
        cut.atomic(backend.target('config.json'), b'changed private config')
    elif kind == 'claim':
        backend.update_job(cut.TARGET_JOBS[0], {'claim': 'synthetic-pending'})
    with pytest.raises(cut.GateError):
        cut.Runner(cut.SyntheticBackend(backend.root)).run()
    assert backend.states() == old_services
    assert not (backend.root / 'operation/journal.json').exists()


def test_racing_unknown_file_after_preflight_is_not_activated(backend):
    def hook(point):
        if point == 'after:file:5':
            cut.atomic(backend.target('plugin/unexpected.py'), b'late unrelated file')
    backend.hook = hook
    with pytest.raises(cut.GateError, match='unexpected plugin files'):
        cut.Runner(backend).run()
    assert cut.Runner(backend).journal()['phase'] == 'conflict'
    assert_held(backend)
    assert not migrated(backend)


def test_independent_writer_blocks_checkpoint_and_activation(backend):
    def hook(point):
        if point == 'after:stop:synthetic-gateway':
            cut.atomic(backend.root / 'independent-writer', b'synthetic writer')
    backend.hook = hook
    with pytest.raises(cut.GateError, match='unfenced'):
        cut.Runner(backend).run()
    assert not migrated(backend)
    assert_held(backend)
    assert not (backend.root / 'operation/database.sqlite').exists()


@pytest.mark.parametrize('point', ['after:file:2', 'before:migrate', 'after:migrate',
                                   'after:start:synthetic-backend', 'after:resume:synthetic-daily'])
def test_real_sigkill_supervisor_recovers_dead_worker(backend, point, monkeypatch):
    # Global suite blocks all subprocesses. Permit ONLY this offline executable.
    # The runtime's autouse safety fixture replaced Popen; use its original via
    # monkeypatch context undo only this single binding from the known saved class.
    monkeypatch.setattr(subprocess, 'Popen', ORIGINAL_POPEN)
    old = baseline(backend)
    result = cut.supervise(backend.root, fault='kill:' + point, timeout=5, recovery_timeout=5)
    assert result['worker']['returncode'] == -signal.SIGKILL
    assert result['recovery']['returncode'] == 0
    if point == 'after:file:2':
        assert result['phase'] == 'rolled-back'
        assert baseline(backend) == old
    else:
        assert result['phase'] == 'forward-repair'
        assert_held(backend)
        assert_new(backend)


def test_watchdog_bounds_hung_migration_and_keeps_candidate(backend, monkeypatch):
    monkeypatch.setattr(subprocess, 'Popen', ORIGINAL_POPEN)
    result = cut.supervise(backend.root, fault='hang:before:migrate', timeout=0.3, recovery_timeout=5)
    assert result['worker']['timeout'] is True
    assert (backend.root / 'fault-reached-worker').read_text() == 'before:migrate'
    assert (backend.root / 'operation/migration-possible').exists()
    assert result['phase'] == 'forward-repair'
    assert not migrated(backend)
    assert_new(backend)
    assert_held(backend)


# Captured at module import before the autouse per-test external-state fixture.
ORIGINAL_POPEN = subprocess.Popen


def test_crash_after_independent_fence_before_journal_is_forward_only(backend):
    fail(backend, 'after:migration-fence')
    assert cut.Runner(backend).journal()['phase'] == 'forward-repair'
    assert not migrated(backend)
    assert_new(backend)
    assert_held(backend)


def test_stale_pre_boundary_journal_cannot_reverse_durable_fence(backend):
    stale = None
    def hook(point):
        nonlocal stale
        if point == 'after:file:5':
            stale = (backend.root / 'operation/journal.json').read_bytes()
        if point == 'after:migrate':
            cut.atomic(backend.root / 'operation/journal.json', stale)
            raise RuntimeError('synthetic stale journal')
    backend.hook = hook
    with pytest.raises(RuntimeError):
        cut.Runner(backend).run()
    assert migrated(backend)
    assert cut.Runner(backend).journal()['phase'] == 'forward-repair'
    assert_new(backend)
    assert_held(backend)


def test_fsync_exception_after_boundary_replace_uses_durable_state(backend, monkeypatch):
    original = cut.sync_dir
    fired = False
    def uncertain(path):
        nonlocal fired
        if Path(path) == backend.root / 'operation' and (Path(path) / 'migration-possible').exists() and not fired:
            fired = True
            raise OSError('synthetic directory fsync failed after fence replacement')
        original(path)
    monkeypatch.setattr(cut, 'sync_dir', uncertain)
    with pytest.raises(OSError):
        cut.Runner(backend).run()
    assert fired
    assert_new(backend)
    assert_held(backend)
    assert cut.Runner(backend).journal()['phase'] == 'forward-repair'
    assert not migrated(backend)


def test_recovery_hold_interrupted_after_partial_pause_can_retry(backend):
    original_update = backend.update_job
    failures = 0
    def update(job, changes):
        nonlocal failures
        original_update(job, changes)
        if cut.Runner(backend).state.exists() and (backend.root / 'operation/migration-possible').exists() and failures == 0:
            failures += 1
            raise RuntimeError('synthetic pause API persisted but ack lost')
    def hook(point):
        if point == 'after:migrate':
            backend.update_job = update
            raise RuntimeError('migration unknown')
    backend.hook = hook
    with pytest.raises(cut.GateError, match='cannot establish stopped'):
        cut.Runner(backend).run()
    backend.hook = None
    cut.Runner(backend).recover()
    assert_held(backend)
    assert_new(backend)
    assert cut.Runner(backend).journal()['phase'] == 'forward-repair'


def test_actual_plugin_schema_reopen_preserves_synthetic_records_on_recovery(backend):
    # The runner's default migration is a tiny synthetic table, not product proof.
    # This extra probe invokes the actual plugin initializer on a disposable DB.
    import hermes_tingle_store as store
    backend.migrate = lambda: _real_schema(backend.db, store)
    backend.fail_at = 'after:migrate'
    with pytest.raises(RuntimeError):
        cut.Runner(backend).run()
    assert_new(backend)
    assert_held(backend)
    with cut.database(backend.db) as connection:
        assert connection.execute('select * from original').fetchall() == [('synthetic preserved',)]
        store.ensure_schema(connection)
        assert connection.execute('pragma integrity_check').fetchone() == ('ok',)
        assert connection.execute('pragma foreign_key_check').fetchall() == []
        assert connection.execute('select count(*) from hermes_tingle_items').fetchone() == (0,)
        assert connection.execute('select count(*) from hermes_tingle_source_optins where enabled=1').fetchone() == (0,)


def _real_schema(path, store):
    with cut.database(path) as connection:
        connection.row_factory = sqlite3.Row
        store.ensure_schema(connection)


@pytest.mark.parametrize('recovery_fault', ['kill:after:restore-file:0', 'hang:before:restore-file:0'])
def test_real_recovery_death_or_timeout_is_blocked_then_restartable(backend, monkeypatch, recovery_fault):
    monkeypatch.setattr(subprocess, 'Popen', ORIGINAL_POPEN)
    old = baseline(backend)
    with pytest.raises(cut.GateError, match='bounded synthetic recovery blocked'):
        cut.supervise(backend.root, fault='kill:after:file:2', timeout=5,
                      recovery_timeout=0.4, recovery_fault=recovery_fault)
    assert not migrated(backend)
    assert_held(backend)
    if recovery_fault.startswith('hang:'):
        assert (backend.root / 'fault-reached-recover').read_text() == 'before:restore-file:0'
    cut.Runner(backend).recover()
    assert baseline(backend) == old
    assert cut.Runner(backend).journal()['phase'] == 'rolled-back'


def test_supervisor_and_worker_have_distinct_real_process_groups(backend, monkeypatch):
    seen = []
    def popen(command, **kwargs):
        assert command[:2] == [sys.executable, str(Path(cut.__file__).resolve())]
        assert str(backend.root) in command
        assert kwargs['start_new_session'] is True
        child = ORIGINAL_POPEN(command, **kwargs)
        # Child starts in a fresh session before Popen returns.
        seen.append((child.pid, os.getpgid(child.pid), os.getpgrp()))
        return child
    monkeypatch.setattr(subprocess, 'Popen', popen)
    cut.supervise(backend.root, fault='hang:before:migrate', timeout=0.3)
    assert len(seen) == 2
    assert all(pid == pgid and pgid != parent for pid, pgid, parent in seen)
    for pid, _, _ in seen:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_phase_watchdog_does_not_confuse_slow_startup_with_stalled_migration(backend, monkeypatch):
    # Deliberately delay child execution longer than the 0.1s phase timeout.
    # The explicit phase-ready marker must arrive before that timeout starts.
    def slow_start(command, **kwargs):
        wrapper = [sys.executable, '-c',
                   'import os,sys,time; time.sleep(0.4); os.execv(sys.argv[1], sys.argv[1:])',
                   *command]
        return ORIGINAL_POPEN(wrapper, **kwargs)
    monkeypatch.setattr(subprocess, 'Popen', slow_start)
    result = cut.supervise(backend.root, fault='hang:before:migrate', timeout=0.1)
    assert (backend.root / 'fault-reached-worker').read_text() == 'before:migrate'
    assert result['worker']['timeout'] is True
    assert result['phase'] == 'forward-repair'
    assert_new(backend)
    assert_held(backend)
