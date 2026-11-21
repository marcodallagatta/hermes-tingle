"""Harmless real process-tree probes. No Hermes, credentials or network."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

# Capture before the repository autouse fixture blocks arbitrary subprocesses.
_REAL_POPEN = subprocess.Popen
spec = importlib.util.spec_from_file_location("admission_process_tree", Path(__file__).parents[1] / "tools" / "run_admission_checks.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def _live(pid):
    path = Path("/proc") / str(pid) / "stat"
    try:
        state = path.read_text()
    except (FileNotFoundError, ProcessLookupError):
        # A process can exit between any two /proc operations.
        return False
    return state.split(") ", 1)[1].split()[0] != "Z"


@pytest.mark.skipif(sys.platform != "linux", reason="Actual descendant-state probe uses Linux /proc")
@pytest.mark.parametrize("leader_exits", [False, True])
def test_real_group_cleanup_kills_term_resistant_child_and_grandchild(tmp_path, monkeypatch, leader_exits):
    monkeypatch.setattr(runner.subprocess, "Popen", _REAL_POPEN)
    sentinel = tmp_path / "survivor"
    pids = tmp_path / "pids.json"
    grandchild = "import signal,time,pathlib;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(3);pathlib.Path(" + repr(str(sentinel)) + ").write_text('survived')"
    child = "import subprocess,signal,time,os,json,pathlib;signal.signal(signal.SIGTERM,signal.SIG_IGN);p=subprocess.Popen([" + repr(sys.executable) + ",'-c'," + repr(grandchild) + "]);pathlib.Path(" + repr(str(pids)) + ").write_text(json.dumps([os.getpid(),p.pid]));time.sleep(10)"
    parent = "import subprocess,time,pathlib;p=subprocess.Popen([" + repr(sys.executable) + ",'-c'," + repr(child) + "]);\nwhile not pathlib.Path(" + repr(str(pids)) + ").exists():time.sleep(.01)\n" + ("" if leader_exits else "time.sleep(10)")
    with (tmp_path / "output.log").open("w") as log:
        try:
            if leader_exits:
                assert runner._run_owned([sys.executable, "-c", parent], cwd=tmp_path,
                    env={"PATH": "/usr/bin:/bin"}, log=log, timeout=1) == 0
            else:
                with pytest.raises(subprocess.TimeoutExpired):
                    runner._run_owned([sys.executable, "-c", parent], cwd=tmp_path,
                        env={"PATH": "/usr/bin:/bin"}, log=log, timeout=1)
            descendants = json.loads(pids.read_text())
            deadline = time.monotonic() + 1
            while any(_live(pid) for pid in descendants) and time.monotonic() < deadline:
                time.sleep(.02)
            assert not any(_live(pid) for pid in descendants)
            time.sleep(3.1)
            assert not sentinel.exists()
        finally:
            if pids.exists():
                for pid in json.loads(pids.read_text()):
                    if _live(pid):
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass


@pytest.mark.skipif(os.name != "posix", reason="POSIX process session")
def test_interruption_cleans_group_and_reaps_direct_child(tmp_path, monkeypatch):
    calls = []

    class Process:
        pid = 424242
        def wait(self, timeout):
            calls.append(("wait", timeout))
            if len([c for c in calls if c[0] == "wait"]) == 1:
                raise KeyboardInterrupt
            return -signal.SIGKILL

    monkeypatch.setattr(runner.subprocess, "Popen", lambda *a, **kw: Process())
    monkeypatch.setattr(runner.os, "killpg", lambda pid, sig: calls.append(("signal", sig)))
    monkeypatch.setattr(runner.time, "sleep", lambda value: None)
    with (tmp_path / "output.log").open("w") as log:
        with pytest.raises(KeyboardInterrupt):
            runner._run_owned(["synthetic"], cwd=tmp_path, env={}, log=log, timeout=1)
    assert ("signal", signal.SIGTERM) in calls
    assert ("signal", signal.SIGKILL) in calls
    assert calls[-1] == ("wait", 3)


def test_process_starts_owned_session_and_cleanup_failure_is_fatal(tmp_path, monkeypatch):
    seen = {}
    class Process:
        pid = 424242
        def wait(self, timeout):
            return 0
    def launch(*args, **kwargs):
        seen.update(kwargs)
        return Process()
    def refused(*args):
        raise PermissionError("synthetic cleanup refusal")
    monkeypatch.setattr(runner.subprocess, "Popen", launch)
    monkeypatch.setattr(runner.os, "killpg", refused)
    with (tmp_path / "output.log").open("w") as log:
        with pytest.raises(RuntimeError, match="cleanup failed"):
            runner._run_owned(["synthetic"], cwd=tmp_path, env={}, log=log, timeout=1)
    assert seen["start_new_session"] is True
    assert seen["stdin"] == subprocess.DEVNULL


@pytest.mark.parametrize("error", [FileNotFoundError, ProcessLookupError])
def test_live_process_disappears_during_stat_read(monkeypatch, error):
    def vanished(path, *args, **kwargs):
        raise error("synthetic process exit before stat read")
    monkeypatch.setattr(Path, "read_text", vanished)
    assert _live(424242) is False


def test_live_process_permission_error_is_not_hidden(monkeypatch):
    def denied(path, *args, **kwargs):
        raise PermissionError("synthetic denied stat")
    monkeypatch.setattr(Path, "read_text", denied)
    with pytest.raises(PermissionError):
        _live(424242)
