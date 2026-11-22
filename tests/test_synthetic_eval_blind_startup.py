"""Subprocess startup scenarios for the eval runner's host-bootstrap warm-up.

Each scenario builds a minimal fake host in tmp_path (copied runner, fake
hermes_bootstrap, fake agent facade) and runs the runner as a real OS process,
so import-time os.execv re-exec and environment inheritance behave as they do
on the installed host. No model calls: the facade returns canned schema-valid
results and the fake bootstrap only re-execs. FAKE_BOOTSTRAP=once re-execs a
single time (startup settles); never re-execs on every import (never settles).
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys

# Capture before the repository autouse fixture blocks arbitrary subprocesses
# (same pattern as tests/test_admission_process_tree.py).
_REAL_POPEN = subprocess.Popen

REPO = pathlib.Path(__file__).resolve().parents[1]
FAKE_HOST = pathlib.Path(__file__).resolve().parent / 'fixtures' / 'fake_host'
HOPS_ENV = 'TINGLE_EVAL_BOOTSTRAP_HOPS'
SETTLE_MSG = 'did not settle after 3 warm-up attempts'
BAD_HOPS_MSG = 'hop marker TINGLE_EVAL_BOOTSTRAP_HOPS is malformed'
REFUSE_PREFIX = '--out already exists; refusing'


def build_host(tmp_path, with_bootstrap=True):
    host = tmp_path / 'host'
    (host / 'scripts').mkdir(parents=True)
    for name in ('synthetic_eval_blind.py', 'synthetic_eval_blind_input.json'):
        shutil.copy(REPO / 'scripts' / name, host / 'scripts' / name)
    for module in ('hermes_tingle_assess.py','hermes_tingle_topic.py','hermes_tingle_checkpoint.py'):
        shutil.copy(REPO / module, host / module)
    if with_bootstrap:
        shutil.copy(FAKE_HOST / 'hermes_bootstrap.py', host / 'hermes_bootstrap.py')
    shutil.copytree(FAKE_HOST / 'agent', host / 'agent')
    return host


def run_runner(host, *args, fake_mode='once', hops=None, facade_imports=True, no_site=False):
    env = dict(os.environ)
    env['PYTHONPATH'] = str(host)
    env['FAKE_BOOTSTRAP'] = fake_mode
    env['FAKE_FACADE_IMPORTS_BOOTSTRAP'] = '1' if facade_imports else '0'
    env.pop(HOPS_ENV, None)
    if hops is not None:
        env[HOPS_ENV] = hops
    cmd = [sys.executable]
    if no_site:
        # Exclude the installed Hermes venv's hermes_bootstrap so this fixture
        # really tests the missing-module case, not host-specific startup.
        cmd.append('-S')
    cmd.extend([str(host / 'scripts' / 'synthetic_eval_blind.py'), *args])
    proc = _REAL_POPEN(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    stdout, stderr = proc.communicate(timeout=120)
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def actual_args(out):
    return ('--run', '--i-approve-model-calls', '--out', str(out))


def test_settle_once_run_completes(tmp_path):
    host = build_host(tmp_path)
    out = tmp_path / 'evidence-settle'
    r = run_runner(host, *actual_args(out))
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.count('FAKE-BOOTSTRAP-FIRED') == 1  # one re-exec, absorbed before claim
    manifest = json.loads((out / 'manifest.json').read_text())
    assert manifest['status'] == 'done'
    assert [req['status'] for req in manifest['requests']] == ['returned', 'returned']


def test_never_settle_fails_closed_before_claim(tmp_path):
    host = build_host(tmp_path)
    out = tmp_path / 'evidence-never'
    r = run_runner(host, *actual_args(out), fake_mode='never')
    assert r.returncode == 1
    assert r.stdout.count('FAKE-BOOTSTRAP-FIRED') == 3  # bounded by the hop marker
    assert SETTLE_MSG in r.stderr
    assert not out.exists()  # failed before any evidence directory was created


def test_inherited_hop_marker_bounds_reexec(tmp_path):
    host = build_host(tmp_path)
    out = tmp_path / 'evidence-inherited'
    r = run_runner(host, *actual_args(out), fake_mode='never', hops='2')
    assert r.returncode == 1
    assert r.stdout.count('FAKE-BOOTSTRAP-FIRED') == 1  # hops 2 -> one more import, then stop
    assert SETTLE_MSG in r.stderr
    assert not out.exists()


def test_missing_bootstrap_fails_closed(tmp_path):
    host = build_host(tmp_path, with_bootstrap=False)
    out = tmp_path / 'evidence-missing'
    r = run_runner(host, *actual_args(out), no_site=True)
    assert r.returncode == 1
    assert "No module named 'hermes_bootstrap'" in r.stderr
    assert r.stdout.count('FAKE-BOOTSTRAP-FIRED') == 0
    assert not out.exists()  # failed before any evidence directory was created


def test_existing_out_refuses_before_startup(tmp_path):
    host = build_host(tmp_path)
    out = tmp_path / 'evidence-exists'
    out.mkdir()
    r = run_runner(host, *actual_args(out), fake_mode='never')
    assert r.returncode == 1
    assert REFUSE_PREFIX in r.stderr
    assert r.stdout.count('FAKE-BOOTSTRAP-FIRED') == 0  # no startup work for an obvious repeat


def test_dry_run_does_no_startup(tmp_path):
    host = build_host(tmp_path)
    r = run_runner(host, fake_mode='never')
    assert r.returncode == 0, r.stdout + r.stderr
    assert 'Dry run: no model was called.' in r.stdout
    assert r.stdout.count('FAKE-BOOTSTRAP-FIRED') == 0


def test_malformed_hop_marker_fails_closed(tmp_path):
    host = build_host(tmp_path)
    out = tmp_path / 'evidence-malformed'
    r = run_runner(host, *actual_args(out), fake_mode='never', hops='abc')
    assert r.returncode == 1
    assert BAD_HOPS_MSG in r.stderr
    assert r.stdout.count('FAKE-BOOTSTRAP-FIRED') == 0
    assert not out.exists()


def test_negative_hop_marker_fails_closed(tmp_path):
    host = build_host(tmp_path)
    out = tmp_path / 'evidence-negative'
    r = run_runner(host, *actual_args(out), fake_mode='never', hops='-2')
    assert r.returncode == 1
    assert BAD_HOPS_MSG in r.stderr
    assert r.stdout.count('FAKE-BOOTSTRAP-FIRED') == 0
    assert not out.exists()
