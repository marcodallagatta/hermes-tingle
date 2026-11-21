import pytest

import hermes_tingle_sources as sources


def test_run_json_reports_redacted_helper_diagnostic(monkeypatch):
    class Result:
        returncode = 1
        stdout = ""
        stderr = "Traceback ...\nModuleNotFoundError: No module named 'x' from /tmp/me@example.com/x"

    monkeypatch.setattr(sources.subprocess, "run", lambda *a, **k: Result())
    with pytest.raises(sources.SourceReadError) as exc:
        sources._run_json(["helper"])
    assert "ModuleNotFoundError" in str(exc.value)
    assert "example.com" not in str(exc.value)
