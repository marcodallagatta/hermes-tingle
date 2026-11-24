"""Real Hermes compatibility gates, skipped only in the stub-only suite.

The pinned compatibility job sets HERMES_REQUIRE_RUNTIME=1 so missing runtime
imports fail rather than silently skip. Isolation checks use disposable homes,
synthetic settings, and the actual Hermes-managed Python environment.
"""
from pathlib import Path
import importlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

import pytest
from runtime_support import hermes_import

# conftest replaces Popen with a no-external-process guard; preserve the real
# constructor before that guard for these explicitly synthetic runtime checks.
_REAL_POPEN = subprocess.Popen
_REQUIRE_RUNTIME = os.environ.get("HERMES_REQUIRE_RUNTIME") == "1"


def _runtime_python():
    configured = os.environ.get("HERMES_RUNTIME_PYTHON")
    if configured:
        return configured
    try:
        yaml = importlib.import_module("ruamel.yaml")
    except ModuleNotFoundError:
        if _REQUIRE_RUNTIME:
            raise
        pytest.skip("requires Hermes runtime dependencies")
        return sys.executable
    yaml_file = getattr(yaml, "__file__", None)
    if not isinstance(yaml_file, str) or not Path(yaml_file).resolve().is_relative_to(Path(sys.prefix).resolve()):
        if _REQUIRE_RUNTIME:
            pytest.fail("Hermes dependencies are only injected via PYTHONPATH; set HERMES_RUNTIME_PYTHON")
        pytest.skip("set HERMES_RUNTIME_PYTHON to the Hermes-managed interpreter")
    return sys.executable


def _run_runtime_child(command, *, cwd, env, timeout):
    """Run an owned process group; always terminate descendants and reap leader."""
    with tempfile.TemporaryFile(mode="w+b") as log:
        process = _REAL_POPEN(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                              stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            returncode = process.wait(timeout=timeout)
        finally:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            else:
                deadline = time.monotonic() + 0.3
                while time.monotonic() < deadline:
                    try:
                        os.killpg(process.pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.02)
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait(timeout=3)
        log.seek(0)
        stdout = log.read().decode("utf-8", errors="replace")
    return subprocess.CompletedProcess(command, returncode, stdout, "")


def test_required_runtime_import_fails_instead_of_skipping(monkeypatch):
    import runtime_support

    monkeypatch.setattr(runtime_support, "_REQUIRED", True)
    monkeypatch.setattr(runtime_support.importlib, "import_module",
                        lambda name: (_ for _ in ()).throw(ModuleNotFoundError(name)))
    monkeypatch.setattr(runtime_support.pytest, "importorskip",
                        lambda *args, **kwargs: pytest.fail("required runtime import must not skip"))
    with pytest.raises(ModuleNotFoundError):
        runtime_support.hermes_import("missing.required_module")


def test_plugin_passes_upstream_admission_validation(monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", _REAL_POPEN)
    validate = hermes_import("hermes_cli.plugin_validate", reason="requires the Hermes runtime validator")
    report = validate.validate_plugin_dir(Path(__file__).resolve().parents[1])
    failures = [f"{name}: {detail}" for name, ok, detail in report.checks if not ok]
    assert not failures, failures
    assert report.isolation is not None and report.isolation.get("host_ready") is True, report.isolation


def test_plugin_is_host_ready_under_real_runtime_audit():
    audit = hermes_import("hermes_cli.plugin_isolation_audit")
    report = audit.audit_plugin_dir(
        Path(__file__).resolve().parents[1],
        manifest={"name": "hermes-tingle", "kind": "standalone"},
    )
    assert report.host_ready, report.to_dict()


def test_routed_profile_setup_uses_hermes_profile_timezone(tmp_path, monkeypatch):
    hermes_time = hermes_import("hermes_time")
    constants = hermes_import("hermes_constants")
    secret_scope = hermes_import("agent.secret_scope")
    import hermes_tingle_setup as setup

    default_home, routed_home = tmp_path / "default-profile", tmp_path / "routed-profile"
    default_home.mkdir()
    routed_home.mkdir()
    (default_home / "config.yaml").write_text("timezone: Asia/Tokyo\n", encoding="utf-8")
    (routed_home / "config.yaml").write_text("timezone: Europe/Berlin\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    monkeypatch.setenv("HERMES_TIMEZONE", "Asia/Tokyo")  # Default profile's bridged value.
    active_home = [default_home]
    monkeypatch.setattr(constants, "get_hermes_home", lambda: active_home[0])
    was_multiplexed = secret_scope.is_multiplex_active()
    secret_scope.set_multiplex_active(False)
    try:
        hermes_time.reset_cache()
        assert hermes_time.get_timezone_name() == "Asia/Tokyo"
        secret_scope.set_multiplex_active(True)
        active_home[0] = routed_home
        hermes_time.reset_cache()
        assert hermes_time.get_timezone_name() == "Europe/Berlin"
        assert setup._profile_timezone(routed_home) == "Europe/Berlin"
    finally:
        hermes_time.reset_cache()
        secret_scope.set_multiplex_active(was_multiplexed)


@pytest.mark.parametrize("isolation", ["host", "in_process"])
def test_plugin_registers_and_invokes_in_real_isolation_runtime(tmp_path, isolation):
    audit = hermes_import("hermes_cli.plugin_isolation_audit")
    hermes_import("hermes_cli.plugins")
    python = _runtime_python()
    if not isinstance(python, str):
        pytest.fail("Hermes runtime interpreter path is unavailable")
    assert Path(python).is_file(), python

    audit_file = getattr(audit, "__file__", None)
    assert isinstance(audit_file, str), "Hermes runtime source path is unavailable"
    source_root = Path(audit_file).resolve().parents[1]
    repo_root = Path(__file__).resolve().parents[1]
    home = tmp_path / "hermes-home"
    os_home = tmp_path / "os-home"
    os_home.mkdir()
    empty_bundled = tmp_path / "bundled"
    empty_bundled.mkdir()
    plugin_dir = home / "plugins" / "hermes-tingle"
    plugin_dir.parent.mkdir(parents=True)
    shutil.copytree(
        repo_root,
        plugin_dir,
        ignore=shutil.ignore_patterns(".git", ".planning", "__pycache__", ".pytest_cache",
                                      ".venv", "venv", "node_modules", "tests", "docs"),
    )
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        json.dumps({"plugins": {"enabled": ["hermes-tingle"], "isolation": isolation}}),
        encoding="utf-8",
    )
    (home / "hermes-tingle.json").write_text(json.dumps({
        "calendar_exclude_substrings": [],
        "workstream_exclude_values": [],
        "calendar_workstream_aliases": {},
        "source_exclude_substrings": [],
        "ticket_exclude_patterns": [],
        "owner_principals": [],
        "delivery": "telegram:123",
        "daily_schedule": "0 8 * * *",
        "timezone": "UTC",
        "date_only_time": "09:00",
        "availability_start": "09:00",
        "availability_end": "18:00",
        "deadline_poll_minutes": 30,
    }), encoding="utf-8")

    child = r'''
import json, logging, os, sys
from hermes_cli.plugins import PluginManager
from tools.registry import registry
logging.basicConfig(level=logging.INFO)
mode = sys.argv[1]
manager = PluginManager()
try:
    manager.discover_and_load()
    loaded = manager._plugins.get("hermes-tingle")
    assert loaded is not None and loaded.enabled and loaded.error is None, (
        None if loaded is None else loaded.error
    )
    assert "hermes_tingle" in loaded.tools_registered, loaded.tools_registered
    result = registry.dispatch("hermes_tingle", {"action": "list"}, scope=manager.scope_key)
    payload = json.loads(result)
    assert payload.get("success") is True, payload
    host_pid = None
    if mode == "host":
        host = manager._running_plugin_host()
        assert host is not None and host.alive
        assert host._proc is not None and host._proc.pid != os.getpid()
        host_pid = host._proc.pid
    else:
        assert manager._running_plugin_host() is None
    print(json.dumps({"mode": mode, "tools": sorted(loaded.tools_registered),
                      "hooks": sorted(loaded.hooks_registered), "tool_success": True,
                      "host_pid": host_pid}))
finally:
    manager.unload()
    if mode == "host":
        host = getattr(manager, "_plugin_host_instance", None)
        if host is not None:
            host.shutdown()
'''
    scratch_tmp = tmp_path / "tmp"
    scratch_pycache = tmp_path / "pycache"
    scratch_tmp.mkdir()
    scratch_pycache.mkdir()
    env = {
        "HOME": str(os_home),
        "HERMES_HOME": str(home),
        "HERMES_BUNDLED_PLUGINS": str(empty_bundled),
        "HERMES_ENABLE_PROJECT_PLUGINS": "0",
        "HERMES_DISABLE_LAZY_INSTALLS": "1",
        "PATH": os.environ.get("PATH", os.defpath),
        "PYTHONPATH": str(source_root),
        "PYTHONPYCACHEPREFIX": str(scratch_pycache),
        "TMPDIR": str(scratch_tmp),
    }
    result = _run_runtime_child([python, "-c", child, isolation], cwd=tmp_path, env=env, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["mode"] == isolation and report["tool_success"] is True
    assert report["tools"] and report["hooks"]
    assert (report["host_pid"] is not None) is (isolation == "host")
