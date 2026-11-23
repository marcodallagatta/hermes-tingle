import json
import sys
from pathlib import Path
from subprocess import CompletedProcess

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import hermes_tingle_resources as resources
import refresh_review_prompt as refresh
import setup_jobs


def _job(name=setup_jobs.DAILY_NAME, prompt="Owner-customized daily review"):
    return {
        "id": "review-1", "name": name, "prompt": prompt,
        "script": "hermes_tingle_snapshot.py",
        "enabled_toolsets": ["hermes_tingle", "web", "no_mcp"],
        "schedule": {"kind": "cron", "expr": "0 9 * * *", "display": "0 9 * * *"},
        "timezone": "Europe/Berlin", "deliver": "telegram:42", "enabled": True,
        "state": "scheduled", "next_run_at": "2026-11-24T09:00:00+01:00",
    }


def _write_jobs(home, jobs):
    path = home / "cron" / "jobs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
    return path


def test_prepare_prompt_accepts_current_and_legacy_review_jobs(tmp_path):
    for name in (setup_jobs.DAILY_NAME, setup_jobs.LEGACY_DAILY_NAME):
        path = _write_jobs(tmp_path, [_job(name)])
        original, updated = refresh.prepare_prompt(path, "review-1")
        assert original["name"] == name
        assert updated.startswith(original["prompt"])
        assert "Not added to Tingle in this review" in updated


@pytest.mark.parametrize("changes", [
    {"enabled": False, "state": "paused", "next_run_at": None},
    {"paused_at": "2026-11-23T14:00:00+01:00"},
    {"paused_reason": "maintenance"},
])
def test_prepare_prompt_refuses_paused_or_unscheduled_review_job(tmp_path, changes):
    paused = _job()
    paused.update(changes)
    path = _write_jobs(tmp_path, [paused])
    with pytest.raises(ValueError, match="must be enabled and scheduled"):
        refresh.prepare_prompt(path, "review-1")


def test_prepare_prompt_is_idempotent_and_rejects_partial_marker(tmp_path):
    current = _job(prompt="Saved" + setup_jobs.REVIEW_PROMPT_REFRESH)
    path = _write_jobs(tmp_path, [current])
    with pytest.raises(ValueError, match="already contains"):
        refresh.prepare_prompt(path, "review-1")

    partial = _job(prompt="Saved " + setup_jobs.REVIEW_PROMPT_REFRESH_MARKER)
    path = _write_jobs(tmp_path, [partial])
    with pytest.raises(ValueError, match="incomplete refresh marker"):
        refresh.prepare_prompt(path, "review-1")


def test_operator_apply_uses_hermes_cli_and_updates_only_prompt_and_matching_manifest(tmp_path):
    import stat
    home = tmp_path / "profile"
    home.mkdir()
    original = _job()
    path = _write_jobs(home, [original])
    resources.record_job(home, original)
    before, updated = refresh.prepare_prompt(path, "review-1")
    calls = []

    def fake_cli(command, **kwargs):
        calls.append((command, kwargs))
        assert command[:6] == ["/usr/bin/hermes", "-p", "default", "cron", "edit", "review-1"]
        assert command[6] == "--prompt" and command[7] == updated
        assert kwargs["env"]["HERMES_HOME"] == str(home)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["jobs"][0]["prompt"] = command[7]
        path.write_text(json.dumps(data), encoding="utf-8")
        return CompletedProcess(command, 0, "updated", "")

    result = refresh.apply_prompt_refresh(
        home, "default", "review-1", before, updated, "UPDATE review-1",
        run_cli=fake_cli, hermes_executable="/usr/bin/hermes",
    )
    after = refresh._job_for(path, "review-1")
    assert result.startswith("Updated only the daily-review prompt")
    backup = Path(result.split("Private backup: `", 1)[1].split("`", 1)[0])
    assert backup.read_text(encoding="utf-8") == original["prompt"]
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert stat.S_IMODE(backup.parent.stat().st_mode) == 0o700
    assert backup.parent.parent == home / "cache" / "scratch"
    assert [k for k in calls[0][0] if k == "--prompt"] == ["--prompt"]
    assert resources.job_definition(after) == resources.job_definition({**original, "prompt": updated})
    assert resources.read(home)["jobs"]["review-1"]["definition"] == resources.job_definition(after)


def test_operator_apply_refuses_profile_home_mismatch(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    original = _job()
    path = _write_jobs(home, [original])
    resources.record_job(home, original)
    _, updated = refresh.prepare_prompt(path, "review-1")
    called = []

    result = refresh.apply_prompt_refresh(
        home, "other-profile", "review-1", original, updated, "UPDATE review-1",
        run_cli=lambda *a, **k: called.append(a), hermes_executable="/usr/bin/hermes",
    )
    assert "profile name does not match" in result
    assert not called
    assert not (home / "cache" / "scratch").exists()
    assert refresh._job_for(path, "review-1")["prompt"] == original["prompt"]
    assert resources.read(home)["jobs"]["review-1"]["definition"] == resources.job_definition(original)


@pytest.mark.parametrize("profile", ["../work", "work/other", "Bad Name"])
def test_operator_apply_refuses_invalid_profile_ids(tmp_path, profile):
    home = tmp_path / "profile"
    home.mkdir()
    original = _job()
    path = _write_jobs(home, [original])
    _, updated = refresh.prepare_prompt(path, "review-1")
    result = refresh.apply_prompt_refresh(
        home, profile, "review-1", original, updated, "UPDATE review-1",
        run_cli=lambda *a, **k: pytest.fail("CLI must not run"),
        hermes_executable="/usr/bin/hermes",
    )
    assert "profile name does not match" in result
    assert refresh._job_for(path, "review-1")["prompt"] == original["prompt"]


def test_operator_apply_refuses_nested_profile_shaped_root(tmp_path):
    home = tmp_path / "root" / "profiles" / "a" / "profiles" / "work"
    home.mkdir(parents=True)
    original = _job()
    path = _write_jobs(home, [original])
    _, updated = refresh.prepare_prompt(path, "review-1")
    result = refresh.apply_prompt_refresh(
        home, "work", "review-1", original, updated, "UPDATE review-1",
        run_cli=lambda *a, **k: pytest.fail("CLI must not run"),
        hermes_executable="/usr/bin/hermes",
    )
    assert "profile name does not match" in result
    assert refresh._job_for(path, "review-1")["prompt"] == original["prompt"]


def test_operator_apply_accepts_named_profile_home(tmp_path):
    root = tmp_path / "root"
    home = root / "profiles" / "work"
    home.mkdir(parents=True)
    original = _job()
    path = _write_jobs(home, [original])
    resources.record_job(home, original)
    _, updated = refresh.prepare_prompt(path, "review-1")
    calls = []

    def fake_cli(command, **kwargs):
        calls.append((command, kwargs))
        assert command[:3] == ["/usr/bin/hermes", "-p", "work"]
        assert kwargs["env"]["HERMES_HOME"] == str(root)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["jobs"][0]["prompt"] = command[7]
        path.write_text(json.dumps(data), encoding="utf-8")
        return CompletedProcess(command, 0, "updated", "")

    result = refresh.apply_prompt_refresh(
        home, "work", "review-1", original, updated, "UPDATE review-1",
        run_cli=fake_cli, hermes_executable="/usr/bin/hermes",
    )
    assert result.startswith("Updated only the daily-review prompt")
    assert len(calls) == 1
    assert refresh._job_for(path, "review-1")["prompt"] == updated


def test_operator_interrupt_reports_uncertain_edit_and_backup(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    original = _job()
    path = _write_jobs(home, [original])
    resources.record_job(home, original)
    _, updated = refresh.prepare_prompt(path, "review-1")

    def interrupted_cli(command, **kwargs):
        data = json.loads(path.read_text(encoding="utf-8"))
        data["jobs"][0]["prompt"] = command[7]
        path.write_text(json.dumps(data), encoding="utf-8")
        raise KeyboardInterrupt

    result = refresh.apply_prompt_refresh(
        home, "default", "review-1", original, updated, "UPDATE review-1",
        run_cli=interrupted_cli, hermes_executable="/usr/bin/hermes",
    )
    assert "interrupted" in result and "may have applied" in result
    assert "Inspect Cron before retrying" in result and "Private prompt backup" in result
    assert refresh._job_for(path, "review-1")["prompt"] == updated


def test_operator_apply_requires_exact_confirmation(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    original = _job()
    path = _write_jobs(home, [original])
    _, updated = refresh.prepare_prompt(path, "review-1")
    called = []
    result = refresh.apply_prompt_refresh(
        home, "default", "review-1", original, updated, "yes",
        run_cli=lambda *a, **k: called.append(a), hermes_executable="/usr/bin/hermes",
    )
    assert "Confirmation did not match" in result
    assert not called
    assert refresh._job_for(path, "review-1")["prompt"] == original["prompt"]


def test_operator_apply_aborts_if_enabled_state_changes_during_confirmation(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    original = _job()
    path = _write_jobs(home, [original])
    _, updated = refresh.prepare_prompt(path, "review-1")
    paused = {**original, "enabled": False, "state": "paused"}
    _write_jobs(home, [paused])
    called = []
    result = refresh.apply_prompt_refresh(
        home, "default", "review-1", original, updated, "UPDATE review-1",
        run_cli=lambda *a, **k: called.append(a), hermes_executable="/usr/bin/hermes",
    )
    assert "paused or has no scheduled next run" in result
    assert not called
    assert refresh._job_for(path, "review-1")["enabled"] is False


def test_operator_apply_reports_scheduler_state_change_after_prompt_update(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    original = _job()
    path = _write_jobs(home, [original])
    resources.record_job(home, original)
    _, updated = refresh.prepare_prompt(path, "review-1")

    def fake_cli(command, **kwargs):
        data = json.loads(path.read_text(encoding="utf-8"))
        data["jobs"][0]["prompt"] = command[7]
        data["jobs"][0]["enabled"] = False
        data["jobs"][0]["state"] = "paused"
        path.write_text(json.dumps(data), encoding="utf-8")
        return CompletedProcess(command, 0, "updated", "")

    result = refresh.apply_prompt_refresh(
        home, "default", "review-1", original, updated, "UPDATE review-1",
        run_cli=fake_cli, hermes_executable="/usr/bin/hermes",
    )
    assert "enabled/paused state changed during the edit" in result
    assert refresh._job_for(path, "review-1")["enabled"] is False
    assert "Private prompt backup" in result


def test_operator_apply_reports_next_run_change_after_prompt_update(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    original = _job()
    path = _write_jobs(home, [original])
    resources.record_job(home, original)
    _, updated = refresh.prepare_prompt(path, "review-1")

    def fake_cli(command, **kwargs):
        data = json.loads(path.read_text(encoding="utf-8"))
        data["jobs"][0]["prompt"] = command[7]
        data["jobs"][0]["next_run_at"] = "2026-11-25T09:00:00+01:00"
        path.write_text(json.dumps(data), encoding="utf-8")
        return CompletedProcess(command, 0, "updated", "")

    result = refresh.apply_prompt_refresh(
        home, "default", "review-1", original, updated, "UPDATE review-1",
        run_cli=fake_cli, hermes_executable="/usr/bin/hermes",
    )
    assert "enabled/paused state changed during the edit" in result
    assert refresh._job_for(path, "review-1")["next_run_at"] == "2026-11-25T09:00:00+01:00"
    assert "Private prompt backup" in result


def test_operator_apply_detects_authored_field_changed_during_cli_edit(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    original = _job()
    path = _write_jobs(home, [original])
    _, updated = refresh.prepare_prompt(path, "review-1")

    def fake_cli(command, **kwargs):
        data = json.loads(path.read_text(encoding="utf-8"))
        data["jobs"][0]["prompt"] = command[7]
        data["jobs"][0]["wrap_response"] = "off"
        path.write_text(json.dumps(data), encoding="utf-8")
        return CompletedProcess(command, 0, "updated", "")

    result = refresh.apply_prompt_refresh(
        home, "default", "review-1", original, updated, "UPDATE review-1",
        run_cli=fake_cli, hermes_executable="/usr/bin/hermes",
    )
    assert "did not match the requested prompt-only update" in result
    assert refresh._job_for(path, "review-1")["wrap_response"] == "off"
    assert "Private prompt backup" in result


def test_operator_apply_aborts_if_saved_job_changed_after_preview(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    original = _job()
    path = _write_jobs(home, [original])
    _, updated = refresh.prepare_prompt(path, "review-1")
    changed = {**original, "prompt": "A concurrent operator edit"}
    _write_jobs(home, [changed])
    called = []
    result = refresh.apply_prompt_refresh(
        home, "default", "review-1", original, updated, "UPDATE review-1",
        run_cli=lambda *a, **k: called.append(a), hermes_executable="/usr/bin/hermes",
    )
    assert "changed during confirmation" in result
    assert not called
    assert refresh._job_for(path, "review-1")["prompt"] == "A concurrent operator edit"
