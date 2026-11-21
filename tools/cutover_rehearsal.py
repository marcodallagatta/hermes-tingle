#!/usr/bin/env python3
"""Synthetic qualification ONLY. There is no real Hermes/systemd backend.

A marker prevents accidental use, not hostile same-user access. All roots are
owned disposable fixtures. Neither a process scan nor this lock fences arbitrary
writers. This synthetic harness does not qualify a live deployment; live cutover remains a separate operator-reviewed gate.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import uuid

SERVICES = ('synthetic-backend', 'synthetic-gateway')
TARGET_JOBS = ('synthetic-daily', 'synthetic-due')
MARKER = b'tingle-disposable-cutover-v2\n'
FENCE = b'migration-may-have-committed-v2\n'
TERMINAL = ('committed', 'rolled-back', 'forward-repair', 'conflict')


class GateError(RuntimeError):
    pass


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def private_dir(path):
    info = Path(path).lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise GateError('owned private directory required')


def atomic(path, data, mode=0o600):
    path = Path(path)
    missing = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
    if path.is_symlink():
        raise GateError('symlink refused')
    temporary = path.with_name('.cutover-' + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_dir(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path, value):
    atomic(path, json.dumps(value, sort_keys=True, indent=2).encode())


def read_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        return json.load(stream)


def fingerprint(path):
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_gid != os.getgid() or info.st_nlink != 1):
        raise GateError('owned non-linked regular file required')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        data = stream.read()
    return {'sha256': hashlib.sha256(data).hexdigest(),
            'mode': stat.S_IMODE(info.st_mode), 'uid': info.st_uid, 'gid': info.st_gid}


def under(root, rel):
    if not isinstance(rel, str) or not rel or Path(rel).is_absolute() or '..' in Path(rel).parts:
        raise GateError('invalid relative path')
    path = root / rel
    if path.resolve() != path.absolute() or not path.resolve().is_relative_to(root):
        raise GateError('path escape or symlink')
    return path


def inventory(root):
    """All files, not just the supplied legacy partial runtime manifest."""
    result = {}
    for parent, directories, files in os.walk(root, followlinks=False):
        for name in directories:
            directory = Path(parent) / name
            if directory.is_symlink():
                raise GateError('directory symlink refused')
            private_dir(directory)
        for name in files:
            path = Path(parent) / name
            result[str(path.relative_to(root))] = fingerprint(path)
    return result


@contextlib.contextmanager
def database(path):
    connection = sqlite3.connect(path, timeout=0.2)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


class SyntheticBackend:
    """Durable fake services/Cron, synthetic files and real disposable SQLite.

    Hooks are test-only failure injection. No network or live service/Cron calls.
    The DB fixture is NOT the real plugin schema/migration.
    """
    def __init__(self, root):
        self.root = Path(root).absolute()
        private_dir(self.root)
        if under(self.root, 'SYNTHETIC_ONLY').read_bytes() != MARKER:
            raise GateError('disposable fixture required')
        self.spec = read_json(under(self.root, 'spec.json'))
        self.db = under(self.root, 'target/data.sqlite')
        self.fail_at = None
        self.hook = None

    @classmethod
    def create(cls, root):
        root = Path(root).absolute()
        # Never adopt or overwrite an existing directory/profile.
        root.mkdir(mode=0o700)
        atomic(root / 'SYNTHETIC_ONLY', MARKER)
        files = [
            ('plugin/runtime.py', 'runtime.py', 0o644),
            ('plugin/skills/usage/SKILL.md', 'SKILL.md', 0o644),
            ('plugin/scripts/due.py', 'due.py', 0o755),
            ('plugin/scripts/snapshot.py', 'snapshot.py', 0o755),
            ('helpers/due.py', 'due.py', 0o755),
            ('helpers/snapshot.py', 'snapshot.py', 0o755),
        ]
        specs = []
        for target, source, mode in files:
            atomic(root / 'target' / target, b'old synthetic file\n', mode)
            atomic(root / 'candidate' / source, b'new synthetic file\n', mode)
            specs.append({'target': target, 'source': source, 'mode': mode})
        atomic(root / 'target/config.json', b'{"sources":"off","owner":null}\n')
        write_json(root / 'spec.json', specs)
        write_json(root / 'services.json', dict.fromkeys(SERVICES, True))
        jobs = {job: {'id': job, 'enabled': True, 'prompt': 'old synthetic prompt',
                      'schedule': {'kind': 'interval', 'minutes': 30},
                      'timezone': 'Etc/UTC', 'delivery': 'synthetic-no-delivery',
                      'failure_delivery': 'synthetic-no-delivery', 'state': 'scheduled',
                      'claim': None, 'settings': {'exclude': ['synthetic-private-time']}}
                for job in (*TARGET_JOBS, 'synthetic-unrelated')}
        write_json(root / 'jobs.json', jobs)
        with database(root / 'target/data.sqlite') as connection:
            connection.execute('pragma journal_mode=WAL')
            connection.execute('create table original(value text)')
            connection.execute('insert into original values (?)', ('synthetic preserved',))
        backend = cls(root)
        write_json(root / 'anchors.json', backend.anchors())
        return backend

    def target(self, rel):
        return under(self.root / 'target', rel)

    def source(self, rel):
        return under(self.root / 'candidate', rel)

    def anchors(self):
        return {'spec': self.spec, 'candidate': inventory(self.root / 'candidate'),
                'target_files': {item['target']: fingerprint(self.target(item['target']))
                                 for item in self.spec},
                'plugin': inventory(self.root / 'target/plugin'),
                'config': fingerprint(self.target('config.json'))}

    def preflight(self):
        if self.anchors() != read_json(self.root / 'anchors.json'):
            raise GateError('unexpected files or candidate/target/config drift')
        jobs = self.jobs()
        if not all(job in jobs for job in TARGET_JOBS):
            raise GateError('missing target job')
        if any(jobs[job]['claim'] is not None for job in TARGET_JOBS):
            raise GateError('pending claim')

    def jobs(self):
        return read_json(self.root / 'jobs.json')

    def update_job(self, job, changes):
        if job not in TARGET_JOBS:
            raise GateError('non-target job write')
        jobs = self.jobs()
        jobs[job].update(changes)
        write_json(self.root / 'jobs.json', jobs)

    def states(self):
        return read_json(self.root / 'services.json')

    def service(self, unit, active):
        if unit not in SERVICES:
            raise GateError('non-target service')
        states = self.states()
        states[unit] = active
        write_json(self.root / 'services.json', states)

    def assert_quiet(self):
        if any(self.states().values()) or (self.root / 'independent-writer').exists():
            raise GateError('unfenced synthetic writer')
        if any(self.jobs()[job]['enabled'] or self.jobs()[job]['claim'] is not None
               for job in TARGET_JOBS):
            raise GateError('job not quiescent')

    def migrate(self):
        with database(self.db) as connection:
            connection.execute('create table if not exists new_schema(id integer)')

    def offline_health(self):
        with database(self.db) as connection:
            if connection.execute('pragma integrity_check').fetchone() != ('ok',):
                raise GateError('integrity failure')
            if connection.execute('pragma foreign_key_check').fetchall():
                raise GateError('foreign key failure')
            connection.execute('select * from new_schema').fetchall()
        if self.target('config.json').read_bytes() != b'{"sources":"off","owner":null}\n':
            raise GateError('source/config drift')

    def online_health(self):
        # Fake health only. Cannot prove real Hermes handlers or schemas.
        self.offline_health()

    def checkpoint(self, name):
        if self.hook:
            self.hook(name)
        if name == self.fail_at:
            self.fail_at = None
            raise RuntimeError('synthetic injected failure: ' + name)


class Runner:
    def __init__(self, backend):
        # Intentionally closed to production adapters: no accidental live entry.
        if type(backend) is not SyntheticBackend:
            raise GateError('only synthetic backend is implemented')
        self.b = backend
        self.work = under(backend.root, 'operation')
        self.work.mkdir(mode=0o700, exist_ok=True)
        private_dir(self.work)
        self.state = under(self.work, 'journal.json')

    @contextlib.contextmanager
    def locked(self):
        private_dir(self.work)
        lock = under(self.b.root, 'deployment.lock')
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)

    def journal(self):
        fingerprint(self.state)
        j = read_json(self.state)
        if (j.get('version') != 2 or type(j.get('migration_possible')) is not bool
                or not isinstance(j.get('files'), list) or j.get('services') is None):
            raise GateError('invalid journal; hold stopped for manual inspection')
        fence = under(self.work, 'migration-possible')
        if fence.exists():
            if fence.read_bytes() != FENCE:
                raise GateError('invalid migration fence')
            j['migration_possible'] = True
        if j['migration_possible'] is False and j.get('intent') == 'migrate':
            raise GateError('inconsistent migration boundary')
        if j['migration_possible'] and j.get('phase') == 'rolled-back':
            raise GateError('inconsistent rollback phase')
        if j.get('phase') not in ('prepared', 'installing', 'migration-possible',
                                  'recovering', *TERMINAL):
            raise GateError('unknown journal phase')
        # Paths come only from the fixture spec, never from journal-controlled destinations.
        if len(j['files']) != len(self.b.spec):
            raise GateError('invalid file journal')
        for recorded, spec in zip(j['files'], self.b.spec):
            if any(recorded.get(key) != spec[key] for key in ('source', 'target')):
                raise GateError('journal path mismatch')
        if set(j['services']) != set(SERVICES) or any(type(v) is not bool for v in j['services'].values()):
            raise GateError('invalid service journal')
        if any(job not in j.get('jobs', {}) for job in TARGET_JOBS):
            raise GateError('invalid job journal')
        return j

    def save(self, j, **values):
        if j.get('migration_possible') and values.get('migration_possible') is False:
            raise GateError('migration boundary is monotonic')
        j.update(values)
        write_json(self.state, j)  # fsync file + atomic replace + fsync directory

    def step(self, j, name, fn):
        self.save(j, intent=name)
        self.b.checkpoint('before:' + name)
        result = fn()
        self.b.checkpoint('after:' + name)
        self.save(j, intent=None)
        return result

    def job_step(self, j, job, want, prefix):
        before = j['expected_jobs'][job]
        if self.b.jobs().get(job) != before:
            raise GateError('job drift')
        self.save(j, pending_job={'id': job, 'before': before, 'after': want})
        def perform():
            self.b.update_job(job, {key: value for key, value in want.items()
                                    if before.get(key) != value})
            if self.b.jobs().get(job) != want:
                raise GateError('unexpected job API side effect')
        self.step(j, prefix + job, perform)
        j['expected_jobs'][job] = want
        self.save(j, pending_job=None)

    def verify_files(self, j, which):
        for item in j['files']:
            if fingerprint(self.b.target(item['target'])) != item[which]:
                raise GateError('file readback mismatch')
        expected = {key: value for key, value in j['plugin'].items()}
        for item in j['files']:
            if item['target'].startswith('plugin/'):
                expected[item['target'][len('plugin/'):]] = item[which]
        if inventory(self.b.target('plugin')) != expected:
            raise GateError('unexpected plugin files')
        if fingerprint(self.b.target('config.json')) != j['config']:
            raise GateError('config drift')

    def run(self):
        with self.locked():
            if self.state.exists():
                raise GateError('existing journal; recover rather than overwrite')
            self.b.preflight()
            jobs = self.b.jobs()
            j = {'version': 2, 'phase': 'prepared', 'intent': None,
                 'migration_possible': False, 'jobs': jobs, 'expected_jobs': jobs.copy(),
                 'pending_job': None, 'services': self.b.states(), 'files': [],
                 'plugin': inventory(self.b.target('plugin')),
                 'config': fingerprint(self.b.target('config.json'))}
            # Preparation has no effects on target files, services, DB or jobs.
            for index, spec in enumerate(self.b.spec):
                old = fingerprint(self.b.target(spec['target']))
                if old is None:
                    raise GateError('fixture target missing')
                data = self.b.source(spec['source']).read_bytes()
                atomic(self.work / f'candidate-{index}', data, old['mode'])
                j['files'].append({**spec, 'old': old,
                                   'new': fingerprint(self.work / f'candidate-{index}')})
            self.save(j)
            try:
                # Pause jobs first, then stop every synthetic writer.
                for job in TARGET_JOBS:
                    self.job_step(j, job, {**jobs[job], 'enabled': False}, 'pause:')
                for unit in SERVICES:
                    self.step(j, 'stop:' + unit, lambda unit=unit: self.b.service(unit, False))
                self.b.assert_quiet()
                self.b.preflight()  # file/claim checks after quiescence, before checkpoint
                if self.b.jobs() != j['expected_jobs']:
                    raise GateError('job table drift after quiescence')
                self.verify_files(j, 'old')
                # Archive exact affected files only AFTER writer quiescence.
                for index, item in enumerate(j['files']):
                    atomic(self.work / f'original-{index}', self.b.target(item['target']).read_bytes(),
                           item['old']['mode'])
                write_json(self.work / 'jobs-original.json', jobs)
                self.step(j, 'backup', self.backup)
                self.save(j, phase='installing')
                for index, item in enumerate(j['files']):
                    def install(index=index, item=item):
                        candidate = self.work / f'candidate-{index}'
                        if fingerprint(candidate) != item['new']:
                            raise GateError('candidate archive drift')
                        if fingerprint(self.b.target(item['target'])) != item['old']:
                            raise GateError('target changed during install')
                        atomic(self.b.target(item['target']), candidate.read_bytes(), item['old']['mode'])
                    self.step(j, 'file:' + str(index), install)
                self.verify_files(j, 'new')
                self.b.assert_quiet()
                # This durable write MUST precede even the call into migration.
                # A crash before/inside/after migrate is conservatively forward-only.
                # Independent never-cleared fence also survives a stale journal.
                atomic(self.work / 'migration-possible', FENCE)
                self.b.checkpoint('after:migration-fence')
                self.save(j, migration_possible=True, phase='migration-possible', intent='migrate')
                self.b.checkpoint('before:migrate')
                self.b.migrate()
                self.b.checkpoint('after:migrate')
                self.save(j, intent=None)
                self.job_step(j, TARGET_JOBS[0], {**j['expected_jobs'][TARGET_JOBS[0]],
                                                'prompt': 'reviewed synthetic prompt'}, 'prompt:')
                self.step(j, 'offline-health', self.b.offline_health)
                self.verify_files(j, 'new')
                for unit, active in j['services'].items():
                    self.step(j, 'start:' + unit, lambda unit=unit, active=active: self.b.service(unit, active))
                self.step(j, 'online-health', self.b.online_health)
                if self.b.jobs() != j['expected_jobs']:
                    raise GateError('paused job readback mismatch')
                for job in TARGET_JOBS:
                    self.job_step(j, job, {**j['expected_jobs'][job], 'enabled': jobs[job]['enabled']}, 'resume:')
                self.verify_files(j, 'new')
                if self.b.jobs() != j['expected_jobs'] or self.b.states() != j['services']:
                    raise GateError('final readback mismatch')
                self.save(j, phase='committed')
            except BaseException:
                # Read DURABLE state, not memory potentially ahead of a failed fsync.
                self._recover_durable()
                raise

    def backup(self):
        checkpoint = self.work / 'database.sqlite'
        # New private backup; never copy an active SQLite main file or restore it.
        atomic(checkpoint, b'')
        with database(self.b.db) as source, database(checkpoint) as target:
            source.backup(target)
            if target.execute('pragma integrity_check').fetchone() != ('ok',):
                raise GateError('backup integrity failure')
        with open(checkpoint, 'rb') as stream:
            os.fsync(stream.fileno())
        sync_dir(self.work)

    def hold(self):
        # Only stop known targets. A failure here is a blocker, never success.
        errors = []
        for job in TARGET_JOBS:
            try:
                self.b.update_job(job, {'enabled': False})
            except Exception as exc:
                errors.append(type(exc).__name__)
        for unit in SERVICES:
            try:
                self.b.service(unit, False)
            except Exception as exc:
                errors.append(type(exc).__name__)
        if errors or any(self.b.states().values()) or any(self.b.jobs()[job]['enabled'] for job in TARGET_JOBS):
            raise GateError('cannot establish stopped state; operator intervention required')

    def recover(self):
        with self.locked():
            self._recover_durable()

    def _recover_durable(self):
        try:
            j = self.journal()
        except Exception:
            self.hold()
            raise GateError('unreadable/invalid journal; targets held, manual inspection required') from None
        if j['phase'] in ('committed', 'rolled-back'):
            return
        # Preserve the pre-hold job table, including an ambiguous pending API write.
        actual = self.b.jobs()
        self.hold()
        self.b.assert_quiet()
        self.save(j, phase='recovering')
        conflicts = []
        for index, item in enumerate(j['files']):
            current = fingerprint(self.b.target(item['target']))
            if current not in (item['old'], item['new']):
                conflicts.append('file:' + str(index))
            # Archives are needed only for pre-boundary rollback. Forward repair
            # never uses old archives and must not depend on their availability.
            archive = self.work / f'original-{index}'
            if not j['migration_possible'] and current != item['old'] and fingerprint(archive) != item['old']:
                conflicts.append('archive:' + str(index))
        expected_plugin_paths = set(j['plugin'])
        if set(inventory(self.b.target('plugin'))) != expected_plugin_paths:
            conflicts.append('plugin-inventory')
        if fingerprint(self.b.target('config.json')) != j['config']:
            conflicts.append('config')
        pending = j.get('pending_job')
        # Recovery's own hold may have been persisted before a prior recovery crash.
        for job in TARGET_JOBS:
            acceptable = [j['expected_jobs'][job]]
            if pending and pending['id'] == job:
                acceptable.extend((pending['before'], pending['after']))
            acceptable += [{**value, 'enabled': False} for value in acceptable.copy()]
            if actual.get(job) not in acceptable:
                conflicts.append('job:' + job)
        if conflicts:
            # Keep conflicting bytes and definitions. No automatic overwrite.
            self.save(j, phase='conflict', conflicts=conflicts)
            return
        # Re-anchor the exact held table; preserve unrelated jobs, including changes.
        self.save(j, expected_jobs=self.b.jobs(), pending_job=None)
        if j['migration_possible']:
            # No automatic activation even if disk checks look good. Health/handlers
            # may be unknown and newly started processes may have written records.
            self.verify_files(j, 'new')
            self.save(j, phase='forward-repair')
            return
        for index, item in enumerate(j['files']):
            if fingerprint(self.b.target(item['target'])) != item['old']:
                self.step(j, 'restore-file:' + str(index), lambda index=index, item=item: atomic(
                    self.b.target(item['target']), (self.work / f'original-{index}').read_bytes(), item['old']['mode']))
        self.verify_files(j, 'old')
        # Keep jobs paused until coherent old files and initial service states verify.
        for job in TARGET_JOBS:
            self.job_step(j, job, {**j['jobs'][job], 'enabled': False}, 'restore-job:')
        for unit, active in j['services'].items():
            self.step(j, 'restore-service:' + unit, lambda unit=unit, active=active: self.b.service(unit, active))
        if self.b.states() != j['services']:
            raise GateError('pre-boundary service restoration failed')
        for job in TARGET_JOBS:
            self.job_step(j, job, j['jobs'][job], 'restore-enabled:')
        self.verify_files(j, 'old')
        self.save(j, phase='rolled-back')


def supervise(root, *, fault=None, timeout=5.0, recovery_timeout=5.0, recovery_fault=None):
    """Bounded separate-process synthetic watchdog. NOT systemd qualification.

    Child has its own session/process group. Supervisor remains outside it and
    waits for exit (including real SIGKILL) before recovery. A timed-out recovery
    is killed and reported blocked, not claimed completed. Re-run recovery only
    after the dead worker/recovery group has been reaped.
    """
    root = Path(root)
    for name in ('synthetic-home', 'synthetic-tmp'):
        directory = under(root, name)
        directory.mkdir(mode=0o700, exist_ok=True)
        private_dir(directory)
    environment = {**os.environ, 'HOME': str(root / 'synthetic-home'),
                   'TMPDIR': str(root / 'synthetic-tmp')}
    def invoke(mode, limit, fault=None):
        command = [sys.executable, str(Path(__file__).resolve()), mode, '--root', str(root)]
        if fault:
            command += ['--fault', fault]
        reached = under(root, 'fault-reached-' + mode)
        reached.unlink(missing_ok=True)
        process = subprocess.Popen(command, start_new_session=True, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env=environment)
        try:
            # Synthetic hangs must reach their intended phase before the short
            # phase watchdog starts. Interpreter startup has its own bounded
            # allowance; a slow start is not evidence of a hung migration.
            if fault and fault.startswith('hang:'):
                startup_deadline = time.monotonic() + 10.0
                while not reached.exists() and process.poll() is None:
                    if time.monotonic() >= startup_deadline:
                        raise subprocess.TimeoutExpired(command, 10.0)
                    time.sleep(0.01)
            out, err = process.communicate(timeout=limit)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            out, err = process.communicate()
            return {'returncode': process.returncode, 'timeout': True}
        return {'returncode': process.returncode, 'timeout': False}
    worker = invoke('worker', timeout, fault)
    recovery = None
    try:
        committed = Runner(SyntheticBackend(root)).journal()['phase'] == 'committed'
    except Exception:
        committed = False
    if worker['returncode'] != 0 or worker['timeout'] or not committed:
        recovery = invoke('recover', recovery_timeout, recovery_fault)
    if recovery and (recovery['returncode'] != 0 or recovery['timeout']):
        raise GateError('bounded synthetic recovery blocked; no successful recovery claim')
    return {'worker': worker, 'recovery': recovery, 'phase': Runner(SyntheticBackend(root)).journal()['phase']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('demo', 'worker', 'recover'))
    parser.add_argument('--root', type=Path)
    parser.add_argument('--fault')
    args = parser.parse_args()
    if args.mode == 'demo':
        if args.root or args.fault:
            parser.error('demo creates a new disposable fixture; no root/fault accepted')
        with tempfile.TemporaryDirectory(prefix='tingle-cutover-') as temp:
            backend = SyntheticBackend.create(Path(temp) / 'fixture')
            result = supervise(backend.root, fault='kill:after:migrate')
            with database(backend.db) as connection:
                result['migration_remains'] = bool(connection.execute(
                    "select 1 from sqlite_master where name='new_schema'").fetchone())
            result['new_code_kept'] = backend.target('plugin/runtime.py').read_bytes() == b'new synthetic file\n'
            result['services_stopped'] = not any(backend.states().values())
            result['jobs_paused'] = all(not backend.jobs()[job]['enabled'] for job in TARGET_JOBS)
            print(json.dumps(result, sort_keys=True))
        return
    if args.root is None:
        parser.error('synthetic worker/recover requires a disposable fixture')
    backend = SyntheticBackend(args.root)
    if args.fault:
        kind, name = args.fault.split(':', 1)
        def hook(point):
            if point == name:
                if kind == 'kill':
                    os.kill(os.getpid(), signal.SIGKILL)
                elif kind == 'hang':
                    atomic(under(backend.root, 'fault-reached-' + args.mode), point.encode())
                    time.sleep(3600)
                else:
                    raise GateError('unknown synthetic fault')
        backend.hook = hook
    runner = Runner(backend)
    if args.mode == 'worker':
        runner.run()
    else:
        runner.recover()


if __name__ == '__main__':
    main()
