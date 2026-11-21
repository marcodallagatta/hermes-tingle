#!/usr/bin/env python3
"""Run the official Hermes CLI checks in new synthetic homes, never a live profile.

Use only in disposable CI/test environments. Dependency validation may download
packages; HOME isolation and a minimal environment are not a security sandbox.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import signal
import time
import tempfile


def _outside(a: Path, b: Path) -> bool:
    return a != b and a not in b.parents and b not in a.parents


def _signal_group(group: int, sig: int) -> bool:
    try:
        os.killpg(group, sig)
        return True
    except ProcessLookupError:
        return False


def _run_owned(command: list[str], *, cwd: Path, env: dict[str, str], log,
               timeout: int) -> int:
    """Own a POSIX process group, not merely its immediate CLI process.

    Always clean remaining group members, even if the leader exited normally.
    A child that deliberately leaves its session is outside this boundary;
    these checks are for trusted code in disposable CI, not hostile isolation.
    Only our immediate child can be portably reaped here. Orphan descendants
    are reaped by the OS supervisor, not claimed as our wait() children.
    """
    process = subprocess.Popen(command, cwd=cwd, env=env,
        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
        start_new_session=True)
    try:
        return process.wait(timeout=timeout)
    finally:
        # The session leader PID is also its owned process-group ID. Never
        # signal our runner's own group. Give cooperative descendants a short
        # grace, then kill even if the leader has already exited/reaped.
        try:
            if _signal_group(process.pid, signal.SIGTERM):
                deadline = time.monotonic() + 0.3
                while time.monotonic() < deadline:
                    try:
                        os.killpg(process.pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.02)
                _signal_group(process.pid, signal.SIGKILL)
        except OSError as exc:
            raise RuntimeError("Admission group cleanup failed; refusing another stage") from exc
        finally:
            # Reap the direct child before deleting homes or starting another
            # check. A kernel-level uninterruptible process can still block;
            # treat failure to reap as fatal, never continue the next stage.
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("Unable to reap admission process; refusing another stage") from exc


def run_checks(*, python: Path, runtime: Path, plugin: Path, reports: Path,
               timeout: int = 600) -> int:
    if os.name != "posix":
        raise ValueError("Admission runner requires POSIX process groups; Windows is not supported")
    python = python.absolute()  # Do not resolve a venv symlink into system Python.
    runtime, plugin, reports = runtime.resolve(), plugin.resolve(), reports.resolve()
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError("An executable prepared-runtime Python is required")
    if not (runtime / "hermes_cli" / "main.py").is_file():
        raise ValueError("runtime-root must be the selected Hermes checkout")
    if not (plugin / "plugin.yaml").is_file() or not (plugin / "__init__.py").is_file():
        raise ValueError("plugin must be the reviewed native plugin directory")
    if not _outside(runtime, plugin) or not _outside(reports, plugin):
        raise ValueError("Runtime, report output and plugin must not nest in the plugin tree")
    if not 1 <= timeout <= 1800:
        raise ValueError("timeout must be between 1 and 1800 seconds")
    reports.mkdir(parents=True, exist_ok=False)  # Never overwrite previous evidence.
    results = []
    # No real HOME/HERMES_HOME, account variables or token-bearing CI environment
    # is inherited. Each check gets a fresh home, not another check's state.
    with tempfile.TemporaryDirectory(prefix="tingle-admission-") as scratch:
        scratch = Path(scratch)
        if not _outside(scratch, plugin):
            raise ValueError("TMPDIR must be outside the plugin tree")
        for name, flags in (
            ("doctor", ["doctor", str(plugin), "--ci"]),
            ("validate", ["validate", str(plugin), "--install-deps"]),
        ):
            work = scratch / name
            work.mkdir()
            home = work / "home"
            home.mkdir()
            hermes_home = work / "hermes-home"  # Must not pre-exist.
            env = {
                "HOME": str(home), "HERMES_HOME": str(hermes_home),
                "XDG_CONFIG_HOME": str(home / ".config"),
                "XDG_CACHE_HOME": str(home / ".cache"),
                "XDG_DATA_HOME": str(home / ".local" / "share"),
                "PATH": os.pathsep.join([str(python.parent), "/usr/bin", "/bin"]),
                "PYTHONPATH": str(runtime), "PYTHONNOUSERSITE": "1",
                "PYTHONDONTWRITEBYTECODE": "1", "HERMES_ENABLE_PROJECT_PLUGINS": "0",
                "TERM": "dumb", "NO_COLOR": "1", "LANG": "C.UTF-8",
            }
            command = [str(python), "-c", "from hermes_cli.main import main; main()",
                       "plugins", *flags]
            result = {"check": name, "status": "failed", "returncode": None}
            with (reports / (name + ".log")).open("w", encoding="utf-8") as log:
                try:
                    returncode = _run_owned(command, cwd=work, env=env,
                                            log=log, timeout=timeout)
                    result["returncode"] = returncode
                    result["status"] = "passed" if returncode == 0 else "failed"
                except subprocess.TimeoutExpired:
                    log.write("\nAdmission check timed out; not passed.\n")
                    result["status"] = "timed_out"
                except OSError as exc:
                    log.write("\nAdmission process could not start: " + type(exc).__name__ + "\n")
            results.append(result)
            # Keep diagnostics visible, including caution/private-import warnings.
            print("=== " + name + " ===", flush=True)
            print((reports / (name + ".log")).read_text(encoding="utf-8"), flush=True)
    (reports / "summary.json").write_text(json.dumps({
        "results": results,
        "limits": "CLI Doctor and validation only; not full install, live acceptance, security audit or catalog approval",
    }, indent=2) + "\n", encoding="utf-8")
    return 0 if all(r["status"] == "passed" for r in results) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", required=True, type=Path)
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--plugin", required=True, type=Path)
    parser.add_argument("--report-dir", required=True, type=Path)
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    try:
        return run_checks(python=args.python, runtime=args.runtime_root,
                          plugin=args.plugin, reports=args.report_dir, timeout=args.timeout)
    except (ValueError, FileExistsError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
