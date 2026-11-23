#!/usr/bin/env python3
"""Owner-run, confirmed refresh of a saved Tingle daily-review Cron prompt."""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import setup_jobs
import hermes_tingle_resources as resources


def _read_jobs(path: Path) -> list[dict]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Cron jobs file is missing or not a regular file")
    data = json.loads(path.read_text(encoding="utf-8"))
    jobs = data if isinstance(data, list) else data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list) or any(not isinstance(job, dict) for job in jobs):
        raise ValueError("Cron jobs file has an unsupported structure")
    return jobs


def _job_for(path: Path, job_id: str) -> dict:
    matches = [job for job in _read_jobs(path) if job.get("id") == job_id]
    if len(matches) != 1:
        raise ValueError("Expected exactly one Cron job with the requested ID")
    job = matches[0]
    if (job.get("name") not in {setup_jobs.DAILY_NAME, setup_jobs.LEGACY_DAILY_NAME}
            or Path(str(job.get("script") or "")).name != "hermes_tingle_snapshot.py"
            or "hermes_tingle" not in (job.get("enabled_toolsets") or [])
            or not isinstance(job.get("prompt"), str) or not job["prompt"].strip()):
        raise ValueError("The selected job is not a verifiable Tingle daily review")
    return job


def _authored_fields(job: dict) -> dict:
    """All authored non-prompt Cron settings, excluding scheduler-owned counters."""
    keys = (
        "id", "name", "script", "schedule", "schedule_display", "timezone", "deliver", "failure_deliver",
        "enabled_toolsets", "toolsets", "no_agent", "attach_to_session", "catch_up", "skills",
        "skill", "model", "provider", "base_url", "wrap_response", "workdir", "context_from",
        "continuity", "monitor_script", "monitor_url", "interpreter", "grace_minutes", "pinned",
        "reasoning_effort", "origin",
    )
    fields = {key: job.get(key) for key in keys}
    fields["repeat.times"] = (job.get("repeat") or {}).get("times")
    return fields


_PROFILE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _profile_matches_home(home: Path, profile: str) -> bool:
    """Check that `hermes -p <profile>` resolves to the explicit profile home."""
    name = profile.strip().lower()
    if name != "default" and not _PROFILE_ID.match(name):
        return False
    base_home = home.parent.parent if home.parent.name == "profiles" else home
    # Hermes re-homes again when HERMES_HOME itself is profile-shaped; a nested
    # root here would resolve `-p` to a different profile than --home.
    if base_home.parent.name == "profiles":
        return False
    expected = base_home if name == "default" else base_home / "profiles" / name
    return expected.resolve(strict=False) == home.resolve(strict=False)


def _runtime_fields(job: dict) -> dict:
    return {key: job.get(key) for key in ("enabled", "state", "paused_at", "paused_reason", "next_run_at")}


def _is_runnable(job: dict) -> bool:
    return (job.get("enabled") is True
            and job.get("state") in (None, "scheduled")
            and not job.get("paused_at")
            and not job.get("paused_reason")
            and bool(job.get("next_run_at")))


def _write_private_backup(prompt: str, scratch_root: Path | None = None) -> Path:
    root = Path(scratch_root or tempfile.gettempdir()).resolve(strict=True)
    folder = Path(tempfile.mkdtemp(prefix="tingle-review-prompt-", dir=root))
    path = folder / "before.txt"
    try:
        os.chmod(folder, 0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(prompt.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        info = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise OSError("Private prompt backup permissions could not be verified")
        return path
    except Exception:
        path.unlink(missing_ok=True)
        folder.rmdir()
        raise


def prepare_prompt(path: Path, job_id: str) -> tuple[dict, str]:
    job = _job_for(path, job_id)
    if not _is_runnable(job):
        raise ValueError("The daily review must be enabled and scheduled, without pause markers")
    prompt = job["prompt"]
    if setup_jobs.REVIEW_PROMPT_REFRESH in prompt:
        raise ValueError("The saved prompt already contains this wording")
    if setup_jobs.REVIEW_PROMPT_REFRESH_MARKER in prompt:
        raise ValueError("The saved prompt has an incomplete refresh marker; inspect it first")
    return job, prompt + setup_jobs.REVIEW_PROMPT_REFRESH


def apply_prompt_refresh(
    home: Path,
    profile: str,
    job_id: str,
    original: dict,
    updated_prompt: str,
    confirmation: str,
    *,
    run_cli: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    hermes_executable: str | None = None,
) -> str:
    """Run the operator's edit under Tingle's maintenance/resource fence."""
    if confirmation != f"UPDATE {job_id}":
        return "Confirmation did not match; no Cron job was changed."
    try:
        home = home.resolve(strict=True)
        with resources.mutation(home):
            return _apply_prompt_refresh_locked(
                home, profile, job_id, original, updated_prompt, confirmation,
                run_cli=run_cli, hermes_executable=hermes_executable)
    except (OSError, ValueError):
        return "Tingle maintenance is active or the profile is unavailable. Inspect Cron before retrying."


def _apply_prompt_refresh_locked(
    home: Path,
    profile: str,
    job_id: str,
    original: dict,
    updated_prompt: str,
    confirmation: str,
    *,
    run_cli: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    hermes_executable: str | None = None,
) -> str:
    """Apply via the public Hermes CLI only after exact local confirmation."""
    if confirmation != f"UPDATE {job_id}":
        return "Confirmation did not match; no Cron job was changed."
    home = home.resolve(strict=True)
    resources.active(home)
    if not _profile_matches_home(home, profile):
        return "The profile name does not match the explicit profile home. No Cron job was changed; prepare the command again."
    jobs_path = home / "cron" / "jobs.json"
    try:
        current = _job_for(jobs_path, job_id)
    except (OSError, ValueError, json.JSONDecodeError):
        return "Could not re-read the saved Cron job. Nothing was changed. Prepare the command again."
    if not _is_runnable(current):
        return "The daily review is paused or has no scheduled next run. No Cron job was changed."
    if (current.get("prompt") != original.get("prompt")
            or _authored_fields(current) != _authored_fields(original)
            or _runtime_fields(current) != _runtime_fields(original)):
        return "The saved Cron job or its enabled/paused state changed during confirmation. Nothing was changed; prepare the command again."

    executable = hermes_executable or shutil.which("hermes")
    if not executable:
        return "Hermes CLI was not found in PATH. No Cron job was changed. Run this as the Hermes OS user."
    try:
        scratch_root = resources.safe(home / "cache" / "scratch", home)
        scratch_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup = _write_private_backup(original["prompt"], scratch_root)
    except (OSError, ValueError):
        return "Could not create a private recovery backup. No Cron job was changed."
    command = [executable, "-p", profile, "cron", "edit", job_id, "--prompt", updated_prompt]
    base_home = home.parent.parent if home.parent.name == "profiles" else home
    environment = os.environ.copy()
    environment["HERMES_HOME"] = str(base_home)
    try:
        result = run_cli(command, text=True, capture_output=True, check=False, env=environment)
    except KeyboardInterrupt:
        return (f"Prompt refresh was interrupted during `hermes cron edit`; the update may have applied. "
                f"Inspect Cron before retrying. Private prompt backup: `{backup}`")
    except OSError:
        return f"Hermes CLI could not be started. Inspect Cron before retrying. Private prompt backup: `{backup}`"

    try:
        verified = _job_for(jobs_path, job_id)
    except (OSError, ValueError, json.JSONDecodeError):
        return f"The CLI ran, but the saved job could not be verified. Private prompt backup: `{backup}`. Inspect Cron before retrying."
    if (verified.get("prompt") != updated_prompt
            or _authored_fields(verified) != _authored_fields(current)):
        return f"The saved job did not match the requested prompt-only update. Private prompt backup: `{backup}`. Inspect Cron before retrying."
    scheduler_state_changed = _runtime_fields(verified) != _runtime_fields(current)
    try:
        provenance_updated = resources.refresh_recorded_job(home, original, verified)
    except Exception:
        return f"The prompt update was verified, but Tingle's ownership record could not be reconciled. Private prompt backup: `{backup}`. Inspect the removal plan before cleanup."
    if result.returncode != 0:
        return f"The prompt update is present and verified, but Hermes CLI returned an error. Private prompt backup: `{backup}`. Inspect its history before retrying."
    if scheduler_state_changed:
        return (f"The prompt update was verified, but the job's enabled/paused state changed during the edit. "
                f"The helper did not change that state. Private prompt backup: `{backup}`. Check Cron before the next run.")
    if provenance_updated:
        return f"Updated only the daily-review prompt and kept Tingle's ownership record in sync. Private backup: `{backup}`. Other job settings were unchanged."
    return f"Updated only the daily-review prompt. Its prior ownership record did not match and was left untouched. Private backup: `{backup}`. Inspect the removal plan before cleanup."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", required=True, type=Path, help="Exact Hermes profile home")
    parser.add_argument("--profile", required=True, help="Hermes profile name used by `hermes -p`")
    parser.add_argument("--job-id", required=True, help="Exact ID shown by Tingle")
    parser.add_argument("--apply", action="store_true", help="Show a preview and ask before updating")
    args = parser.parse_args(argv)
    if not args.apply:
        print("Dry run only. Add --apply to preview and explicitly confirm the prompt-only update.", file=sys.stderr)
        return 2
    if not sys.stdin.isatty():
        print("An interactive terminal is required; no Cron job was changed.", file=sys.stderr)
        return 2

    try:
        home = args.home.resolve(strict=True)
        resources.active(home)
        original, updated_prompt = prepare_prompt(home / "cron" / "jobs.json", args.job_id)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Cannot prepare the prompt refresh: {exc}", file=sys.stderr)
        return 2

    print("This updates only the saved daily-review prompt through `hermes cron edit`.")
    print("The schedule, destination, script, source settings and other jobs are unchanged; no review will run.")
    print("Privacy: the Hermes CLI receives the full prompt in its argument list. Run only on a trusted host where other local users cannot inspect process arguments.")
    print("After confirmation, a private mode-0600 backup is saved in this profile's scratch area before the edit.")
    print(f"Current prompt SHA-256: {resources.digest(original['prompt'].encode('utf-8'))}")
    print(f"Updated prompt SHA-256: {resources.digest(updated_prompt.encode('utf-8'))}")
    try:
        confirmation = input(f"Type UPDATE {args.job_id} to apply: ")
    except (EOFError, KeyboardInterrupt):
        print("Cancelled; no Cron job was changed.")
        return 1
    result = apply_prompt_refresh(home, args.profile, args.job_id, original, updated_prompt, confirmation)
    print(result)
    return 0 if result.startswith("Updated only the daily-review prompt") else 1


if __name__ == "__main__":
    raise SystemExit(main())
