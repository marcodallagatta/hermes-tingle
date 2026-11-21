"""Synthetic checks for admission orchestration; never launch Hermes or a provider."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import json
import subprocess
from types import SimpleNamespace

import pytest

spec = spec_from_file_location("tingle_admission_runner", Path(__file__).parents[1] / "tools" / "run_admission_checks.py")
runner = module_from_spec(spec)
spec.loader.exec_module(runner)


def paths(tmp_path):
    runtime, plugin = tmp_path / "runtime", tmp_path / "plugin"
    (runtime / "hermes_cli").mkdir(parents=True)
    (runtime / "hermes_cli" / "main.py").write_text("")
    plugin.mkdir()
    (plugin / "plugin.yaml").write_text("name: synthetic\n")
    (plugin / "__init__.py").write_text("")
    python = tmp_path / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("synthetic executable, never run")
    python.chmod(0o700)
    return dict(python=python, runtime=runtime, plugin=plugin, reports=tmp_path / "reports")


def test_checks_use_real_cli_shapes_and_fresh_secret_free_homes(tmp_path, monkeypatch):
    args = paths(tmp_path)
    calls = []
    monkeypatch.setenv("SYNTHETIC_SECRET", "must not pass to child")
    monkeypatch.setenv("HERMES_HOME", "/do-not-use-existing-profile")

    def run(command, **kw):
        env = kw["env"]
        assert "SYNTHETIC_SECRET" not in env
        assert not Path(env["HERMES_HOME"]).exists()
        assert Path(env["HOME"]).is_dir()
        assert str(args["python"]) == command[0]
        assert command[1:4] == ["-c", "from hermes_cli.main import main; main()", "plugins"]
        assert env["HERMES_ENABLE_PROJECT_PLUGINS"] == "0"
        assert env["PYTHONPATH"] == str(args["runtime"])
        kw["log"].write("synthetic warning retained\n")
        calls.append((command, env.copy()))
        return 0

    monkeypatch.setattr(runner, "_run_owned", run)
    assert runner.run_checks(**args) == 0
    assert calls[0][0][-3:] == ["doctor", str(args["plugin"]), "--ci"]
    assert calls[1][0][-3:] == ["validate", str(args["plugin"]), "--install-deps"]
    assert calls[0][1]["HERMES_HOME"] != calls[1][1]["HERMES_HOME"]
    assert not Path(calls[0][1]["HOME"]).exists()
    assert "synthetic warning retained" in (args["reports"] / "doctor.log").read_text()
    summary = json.loads((args["reports"] / "summary.json").read_text())
    assert [r["status"] for r in summary["results"]] == ["passed", "passed"]
    assert "not full install" in summary["limits"]


@pytest.mark.parametrize("mode", ["failed", "timeout", "start_error"])
def test_failures_keep_evidence_and_still_check_other_stage(tmp_path, monkeypatch, mode):
    args = paths(tmp_path)
    calls = []

    def run(command, **kw):
        calls.append(command)
        if len(calls) == 1:
            if mode == "timeout":
                raise subprocess.TimeoutExpired(command, 1)
            if mode == "start_error":
                raise OSError("synthetic")
            kw["log"].write("synthetic refusal\n")
            return 2
        return 0

    monkeypatch.setattr(runner, "_run_owned", run)
    assert runner.run_checks(**args) == 1
    assert len(calls) == 2
    summary = json.loads((args["reports"] / "summary.json").read_text())
    assert summary["results"][0]["status"] != "passed"
    assert summary["results"][1]["status"] == "passed"
    assert (args["reports"] / "doctor.log").is_file()


def test_refuses_reusing_evidence_or_nesting_trees(tmp_path):
    args = paths(tmp_path)
    args["reports"].mkdir()
    with pytest.raises(FileExistsError):
        runner.run_checks(**args)
    args["reports"] = args["plugin"] / "reports"
    with pytest.raises(ValueError, match="nest"):
        runner.run_checks(**args)
    args["reports"] = tmp_path / "fresh-reports"
    args["runtime"] = args["plugin"] / "runtime"
    (args["runtime"] / "hermes_cli").mkdir(parents=True)
    (args["runtime"] / "hermes_cli" / "main.py").write_text("")
    with pytest.raises(ValueError, match="nest"):
        runner.run_checks(**args)


def test_rejects_invalid_timeout_before_creating_output(tmp_path):
    args = paths(tmp_path)
    with pytest.raises(ValueError, match="timeout"):
        runner.run_checks(**args, timeout=0)
    assert not args["reports"].exists()


def test_non_posix_refuses_before_any_output(tmp_path, monkeypatch):
    args = paths(tmp_path)
    monkeypatch.setattr(runner.os, "name", "nt")
    with pytest.raises(ValueError, match="POSIX"):
        runner.run_checks(**args)
    assert not args["reports"].exists()
