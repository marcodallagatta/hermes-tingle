#!/usr/bin/env python3
"""Operator-local profile configuration. No account reads, jobs or sources by default."""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hermes_tingle_config import validate_settings, load_settings, validate_delivery

NAME = "hermes-tingle.json"
TRUST = ("This writes global plugin policy as the local operator. Anyone with file access "
         "as this OS user can do the same. This is not separate human authentication.")
from hermes_tingle_policy import DEFAULTS, snapshot, write_policy


def prompt_value(label: str, prior: object, ask=input, *, as_json=False):
    shown = json.dumps(prior, ensure_ascii=False) if as_json else str(prior if prior is not None else "")
    value = ask(f"{label} [{shown}]: ").strip()
    if not value:
        return prior
    return json.loads(value) if as_json else value


def wizard(home: Path, *, repair=False, ask=input, emit=print) -> bool:
    requested_home = Path(home).absolute()
    home = requested_home.resolve(strict=True)
    original = snapshot(home)
    prior = None
    if original is not None:
        try:
            prior = json.loads(original)
        except (ValueError, UnicodeDecodeError):
            pass
        try:
            validate_settings(prior)
        except ValueError as exc:
            emit(f"Existing policy is invalid: {exc}")
        else:
            raise ValueError("Existing policy is valid; wizard refuses to overwrite it, even in repair mode")
        if not repair:
            raise ValueError("Existing policy requires explicit --repair; nothing overwritten")
        if not isinstance(prior, dict):
            raise ValueError("Policy is not a parseable object. Preserve it and restore a reviewed policy locally; no reset offered")
    elif repair:
        raise ValueError("Policy is absent. Restore your reviewed policy locally if this is an existing install; repair cannot invent prior values")
    # Runtime plugin_storage owns this exact profile-scoped state location.
    if original is None and (home / "plugin-data" / "hermes-tingle").exists():
        raise ValueError("Profile database exists without policy; restore a reviewed config locally, no fresh defaults written")
    emit(TRUST)
    emit("Tingle stores tracker records on this Hermes host. Authorized source content and tracked details may go "
         "to your configured model provider, Hermes history/logs and chosen messaging gateway. This version has no "
         "built-in Tingle-operated telemetry or content-upload service; that does not cover the rest of your Hermes setup. "
         "Review model, accounts, destination and source scope before enabling them. Revocation/removal does not erase "
         "past history or outside copies. See README: Privacy and data.")
    emit(f"Profile: {home}. No sources, jobs, account reads or test messages are created by this wizard.")
    data = copy.deepcopy(prior if prior is not None else DEFAULTS)
    for key, label in (
        ("timezone", "IANA timezone"), ("delivery", "Required reminder destination (platform:target or configured home platform)"),
        ("daily_schedule", "Review cron (new profiles: 00:00, 08:00, 16:00 in configured timezone)"),
        ("deadline_poll_minutes", "Deadline checks in minutes"), ("date_only_time", "Date-only due time HH:MM"),
        ("availability_start", "Availability start HH:MM"), ("availability_end", "Availability end HH:MM"),
    ):
        emit("Destination help: use /tingle identity in the intended chat. Telegram uses telegram:<chat-id>[:topic-id]. "
             "Other gateways: copy the documented destination from your gateway config; sender ID need not be the chat ID.") if key == "delivery" else None
        value = prompt_value(label, data.get(key), ask)
        if key == "deadline_poll_minutes":
            value = int(value)
        data[key] = value
    # Existing owner lists and every exclusion are edited as explicit JSON, never collapsed/reset.
    if prior is None:
        owner = ask("Owner platform:user_id (blank keeps ownerless; chat cannot activate jobs): ").strip()
        data["owner_principals"] = [owner] if owner else []
    else:
        for key in ("owner_principals", "calendar_exclude_substrings", "workstream_exclude_values",
                    "source_exclude_substrings", "ticket_exclude_patterns", "calendar_workstream_aliases",
                    "verbose_diagnostics"):
            if key in data or key in {"calendar_exclude_substrings", "workstream_exclude_values", "source_exclude_substrings", "ticket_exclude_patterns"}:
                data[key] = prompt_value(key + " (JSON; blank preserves, missing requires explicit JSON)", data.get(key), ask, as_json=True)
    validate_settings(data)
    from setup_jobs import _check_delivery_platform, _cron_api
    _check_delivery_platform(data["delivery"])
    # Require the real runtime parser before writing a schedule it cannot activate.
    parse_schedule, _ = _cron_api()
    if parse_schedule(data["daily_schedule"]).get("kind") != "cron":
        raise ValueError("Daily review must use a valid recurring cron expression")
    validate_delivery(data["delivery"])
    emit("Proposed policy (unknown existing fields preserved):\n" + json.dumps(data, indent=2, ensure_ascii=False))
    if ask(f"Type destination exactly to confirm [{data['delivery']}]: ").strip() != data["delivery"]:
        emit("Cancelled; no policy written.")
        return False
    if ask("Write this global profile policy? Type WRITE: ").strip() != "WRITE":
        emit("Cancelled; no policy written.")
        return False
    if requested_home.resolve(strict=True) != home:
        raise ValueError("Profile path changed during confirmation; restart")
    backup = write_policy(home, data, original)
    emit("Validated policy saved. " + TRUST)
    if backup:
        emit(f"Exact prior bytes backed up at {backup}; treat this as private configuration.")
    emit("Tell Hermes one real commitment to track. Reminders are not active yet: use /tingle setup as an owner, "
         "or run this script --activate locally. On a fresh install, setup presents email, calendar and chat "
         "as on by default in plain language, with a clear opt-out; Google Drive is not a Tingle source.")
    return True


class LocalActivation:
    def ask_choice(self, question, choices):
        print(question)
        values = {c.get("value") for c in choices if isinstance(c, dict)}
        if "all" in values:
            return "all" if input("Type KEEP ALL ON to keep the default sources, anything else keeps all off: ").strip() == "KEEP ALL ON" else "none"
        return "create" if input("Type CREATE BOTH JOBS to activate, anything else cancels: ").strip() == "CREATE BOTH JOBS" else "cancel"

    def dispatch_tool(self, name, args):
        if name != "cronjob_manage":
            raise ValueError("Local activation only supports Cron")
        from tools.cronjob_tools import cronjob
        return cronjob(**args)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-p", "--profile", help="Existing Hermes profile name (otherwise HERMES_HOME or default home)")
    parser.add_argument("--repair", action="store_true")
    parser.add_argument("--activate", action="store_true", help="Preview and explicitly activate existing valid policy locally")
    args = parser.parse_args(argv)
    if args.activate and args.repair:
        parser.error("Choose repair or activation, not both")
    try:
        if args.profile:
            from hermes_cli.profiles import get_profile_dir
            home = get_profile_dir(args.profile).resolve(strict=True)
            os.environ["HERMES_HOME"] = str(home)
        else:
            from hermes_constants import get_hermes_home
            home = Path(get_hermes_home()).resolve(strict=True)
        if args.activate:
            from setup_jobs import _activate_jobs
            print(TRUST)
            print(f"Local activation for profile: {home}")
            print(_activate_jobs(LocalActivation()))
        else:
            wizard(home, repair=args.repair)
    except (ValueError, OSError, ImportError, EOFError, KeyboardInterrupt) as exc:
        print(f"Tingle local setup stopped: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
