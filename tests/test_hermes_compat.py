"""Compatibility checks that only run when the real Hermes runtime is importable.

The main suite stubs the runtime so it runs anywhere; these checks skip there.
With Hermes on the path (PYTHONPATH=<hermes-agent checkout>), they run the
upstream admission validator against this plugin directory, so manifest,
registration, capability and security-scan drift against the real runtime
fails loudly instead of being discovered at install time.
"""
from pathlib import Path
import subprocess

import pytest

# Captured at import time, before conftest installs its no-external-access stubs.
# The validator's capability probe is a local, network-free subprocess; restore
# the real Popen for this check only.
_REAL_POPEN = subprocess.Popen


def test_plugin_passes_upstream_admission_validation(monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", _REAL_POPEN)
    validate = pytest.importorskip(
        "hermes_cli.plugin_validate",
        reason="requires the Hermes runtime validator",
    )
    report = validate.validate_plugin_dir(Path(__file__).resolve().parents[1])
    failures = [f"{name}: {detail}" for name, ok, detail in report.checks if not ok]
    assert not failures, failures
