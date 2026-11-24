import sys
import copy
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import setup_jobs


def test_gitignore_retains_sensitive_artifact_patterns():
    patterns = (Path(__file__).resolve().parents[1] / '.gitignore').read_text().splitlines()
    required = {'.env', 'credentials*.json', '*.key', 'hermes-tingle.json', '*.db.*', '*.sql', '*snapshot*.json', '*.log.*', '.test-harness/'}
    assert required <= set(patterns)
    assert not any(line.startswith('!') for line in patterns)


def test_readme_sample_is_valid_and_excludes_synthetic_ticket(tmp_path):
    import json
    import re
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    sample = json.loads(readme.split("```json\n", 1)[1].split("\n```", 1)[0])
    (tmp_path / "hermes-tingle.json").write_text(json.dumps(sample))
    loaded = setup_jobs.load_settings(tmp_path)
    assert re.search(loaded["ticket_exclude_patterns"][0], "TASK-123")


def test_fresh_setup_docs_use_one_approval_and_plain_defaults():
    root = Path(__file__).resolve().parents[1]
    for relative in ("README.md", "docs/installation.md", "docs/beta-setup.md",
                     "docs/onboarding.md"):
        text = (root / relative).read_text(encoding="utf-8")
        assert "/tingle setup sources all" not in text
        assert "/tingle setup sources none" not in text
        assert "00:00" in text and "08:00" in text and "16:00" in text
        assert "guided first-run policy/source/evaluator consent" not in text
        assert "separate two-job consent" not in text
        assert "Source opt-ins and windows require separate permission" not in text
        assert "keep-all-off opt-out" not in text
        assert "private-chat suggestions" in text and "separate consent" in text
        assert "30-day Gmail review" in text
        assert "/tingle lookback review LB-ID" in text and "/tingle lookback start 30" in text
    for relative in ("README.md", "docs/installation.md", "docs/beta-setup.md",
                     "docs/onboarding.md"):
        assert "/tingle source enable" in (root / relative).read_text(encoding="utf-8")
    installation = (root / "docs/installation.md").read_text(encoding="utf-8")
    assert "hermes -p <profile> tools enable --platform telegram hermes_tingle" in installation
    assert "fresh session or after `/reset`" in installation
    assert "A saved platform list may intentionally omit a known plugin toolset" in installation
    onboarding = (root / "docs/onboarding.md").read_text(encoding="utf-8")
    assert "one plain-language approval screen" in onboarding
    assert "does not send the user back through questions" in onboarding
    assert "profile's Hermes `timezone`" in onboarding
    assert "On an existing configured profile with an empty tracker" in onboarding
    assert "fresh setup instead offers the separately confirmed private 30-day Gmail look-back" in onboarding


def test_chat_history_docs_match_multi_session_read_limits():
    root = Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text(encoding="utf-8")
    usage = (root / "skills/usage/SKILL.md").read_text(encoding="utf-8")
    assert "Up to 5 active session routes per chat (including separate threads/topics)" in readme
    assert "200 message reads per chat" in readme
    assert "only the current segment of the most recent live session" not in readme
    assert "up to five active session routes per chat" in usage
    assert "200 message reads total per chat, current segments only" in usage
    assert "ended sessions, earlier lineage segments and compaction-archived history are not covered" in usage
    assert "Hitting a bound is disclosed as an incomplete review" in usage


def test_gmail_lookback_missing_bridge_fallback_is_documented():
    root = Path(__file__).resolve().parents[1]
    expected = ("If the email bridge is unavailable, Tingle says no review ran; ask your operator to wire it, "
                "then run `/tingle lookback start 30`.")
    for relative in ("README.md", "docs/onboarding.md", "docs/beta-setup.md"):
        assert expected in (root / relative).read_text(encoding="utf-8")


def test_existing_installation_prompt_refresh_is_documented():
    root = Path(__file__).resolve().parents[1]
    doc = (root / "docs" / "installation.md").read_text(encoding="utf-8")
    usage = (root / "skills" / "usage" / "SKILL.md").read_text(encoding="utf-8")
    assert "/tingle setup refresh-review" in doc
    assert "does not run the review" in doc
    assert "/tingle setup refresh-review" in usage


def test_docs_state_approvals_are_record_only_and_no_external_messaging():
    root = Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text(encoding="utf-8")
    schema_text = (root / "schemas.py").read_text(encoding="utf-8")
    assert "does not send messages to external counterparties" in readme
    assert "Item approvals only record your decision" in readme
    assert "Tingle does not perform that action" in readme
    assert "The separate Start Tingle setup approval applies defaults, checks sources and creates the two jobs." in readme
    assert "Start Tingle is a separate setup approval" in readme
    assert "Nothing is executed or sent by an approval" not in readme
    assert "does not initiate messages to external contacts or agents" in schema_text
    assert "owner reminders and reports use the configured destination" in schema_text
    bug_template = (root / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml").read_text(encoding="utf-8")
    assert "coordination attempt" not in bug_template


def test_confirmation_includes_calendar_aliases(tmp_path, synthetic_settings):
    import json
    aliases = {"lab": "Work Alpha"}
    (tmp_path / "hermes-tingle.json").write_text(json.dumps({**synthetic_settings, "calendar_workstream_aliases": aliases}))

    class CancelContext:
        def ask_choice(self, question, choices):
            assert '"calendar_workstream_aliases"' in question
            assert json.dumps(aliases) in question
            return "cancel"

    assert "cancelled" in setup_jobs._activate_jobs(CancelContext())


def test_confirmation_includes_explicit_source_exclusions(synthetic_settings):
    class CancelContext:
        def ask_choice(self, question, choices):
            for key in ("calendar_exclude_substrings", "workstream_exclude_values", "source_exclude_substrings", "ticket_exclude_patterns"):
                assert key in question
                import json
                for value in synthetic_settings[key]:
                    assert json.dumps(value, ensure_ascii=False) in question
            return "cancel"
    assert "cancelled" in setup_jobs._activate_jobs(CancelContext())


def test_deadline_cadence_and_setup_confirmation_use_profile_values(tmp_path, synthetic_settings):
    import json
    settings = {**synthetic_settings, "timezone": "Asia/Tokyo", "date_only_time": "16:20", "availability_start": "13:30", "availability_end": "17:15", "deadline_poll_minutes": 45}
    (tmp_path / "hermes-tingle.json").write_text(json.dumps(settings))
    assert setup_jobs.job_specs(tmp_path)[1]["schedule"] == "every 45m"

    class CancelContext:
        def ask_choice(self, question, choices):
            for value in ("Asia/Tokyo", "16:20", "13:30", "17:15", "45", settings["daily_schedule"], settings["delivery"]):
                assert value in question
            return "cancel"

        def dispatch_tool(self, *args, **kwargs):
            raise AssertionError("cancelled setup must not touch cron")

    assert "cancelled" in setup_jobs._activate_jobs(CancelContext())


def test_setup_stops_if_configuration_changes_during_confirmation(tmp_path, synthetic_settings):
    import json
    class ChangeContext:
        def ask_choice(self, question, choices):
            (tmp_path / "hermes-tingle.json").write_text(json.dumps({**synthetic_settings, "deadline_poll_minutes": 45}))
            return "create"

        def dispatch_tool(self, *args, **kwargs):
            raise AssertionError("changed configuration must not touch cron")

    assert "changed after confirmation" in setup_jobs._activate_jobs(ChangeContext())


def fake_schedule(value):
    if value.startswith("every "):
        return {"kind": "interval", "minutes": int(value[6:-1]), "display": value}
    return {"kind": "cron", "expr": value, "display": value}


class FakeCron:
    def __init__(self):
        self.jobs = {}
        self.corrupt = None
        self.fail_create = False
        self.fail_remove = False
        self.fail_read = False

    def ask_choice(self, question, choices):
        return "create"

    def read_job(self, job_id):
        if self.fail_read:
            raise RuntimeError("unavailable")
        record = copy.deepcopy(self.jobs.get(job_id))
        if record and self.corrupt and job_id == "id-1":
            record.update(self.corrupt)
        return record

    def dispatch_tool(self, name, payload):
        assert name == "cronjob_manage"
        action = payload["action"]
        if action == "list":
            return {"success": True, "count": len(self.jobs), "jobs": [
                {"job_id": job_id, "name": job["name"]} for job_id, job in self.jobs.items()]}
        if action == "create":
            job_id = f"id-{len(self.jobs) + 1}"
            record = {k: copy.deepcopy(v) for k, v in payload.items() if k != "action"}
            record.update(id=job_id, schedule=fake_schedule(payload["schedule"]), enabled=True,
                          no_agent=payload.get("no_agent", False))
            self.jobs[job_id] = record
            if self.fail_create and job_id == "id-2":
                return {"success": False, "job_saved": True, "job_id": job_id}
            return {"success": True, "job_id": job_id, "job": {"job_id": job_id, "name": record["name"]}}
        assert action == "remove"
        job_id = payload["job_id"]
        if not self.fail_remove:
            self.jobs.pop(job_id, None)
        return {"success": not self.fail_remove, "removed_job": {"id": job_id}}


def fake_cron_setup(monkeypatch, ctx):
    monkeypatch.setattr(setup_jobs, "install_scripts", lambda *_: [])
    monkeypatch.setattr(setup_jobs, "_cron_api", lambda: (fake_schedule, ctx.read_job), raising=False)


@pytest.mark.parametrize("corrupt", [
    {"id": "other-id"}, {"name": "different"}, {"timezone": "Asia/Tokyo"},
    {"schedule": {"kind": "interval", "minutes": 1, "display": "every 1m"}},
    {"deliver": "telegram:999"}, {"failure_deliver": "telegram:999"},
    {"script": "different.py"}, {"enabled_toolsets": []}, {"no_agent": True},
    {"prompt": "different"}, {"attach_to_session": False}, {"catch_up": False},
])
def test_setup_rejects_corrupt_readback_and_verifies_rollback(monkeypatch, corrupt):
    ctx = FakeCron()
    ctx.corrupt = corrupt
    fake_cron_setup(monkeypatch, ctx)
    result = setup_jobs._activate_jobs(ctx)
    assert "is scheduled" not in result
    assert "rolled back" in result
    assert ctx.jobs == {}


def test_setup_rolls_back_saved_job_from_partial_create(monkeypatch):
    ctx = FakeCron()
    ctx.fail_create = True
    fake_cron_setup(monkeypatch, ctx)
    result = setup_jobs._activate_jobs(ctx)
    assert "rolled back" in result
    assert ctx.jobs == {}


def test_setup_reports_ids_when_rollback_cannot_be_confirmed(monkeypatch):
    ctx = FakeCron()
    ctx.fail_create = True
    ctx.fail_remove = True
    fake_cron_setup(monkeypatch, ctx)
    result = setup_jobs._activate_jobs(ctx)
    assert "id-1" in result and "id-2" in result
    assert "rolled back" not in result
    assert set(ctx.jobs) == {"id-1", "id-2"}


def test_rollback_never_resolves_missing_id_as_an_unrelated_name(monkeypatch):
    class MissingBeforeRollback(FakeCron):
        removed = []
        def dispatch_tool(self, name, payload):
            if payload["action"] == "remove":
                self.removed.append(payload["job_id"])
            if payload["action"] == "create" and len(self.jobs) == 1:
                result = super().dispatch_tool(name, payload)
                self.jobs.pop("id-1")
                self.jobs["id-9"] = {"id": "id-9", "name": "id-1"}
                return result
            return super().dispatch_tool(name, payload)
    ctx = MissingBeforeRollback()
    ctx.fail_create = True
    fake_cron_setup(monkeypatch, ctx)
    setup_jobs._activate_jobs(ctx)
    assert "id-1" not in ctx.removed
    assert "id-9" in ctx.jobs


def test_readback_unavailable_stops_before_any_writes(monkeypatch):
    ctx = FakeCron()
    ctx.fail_read = True
    fake_cron_setup(monkeypatch, ctx)
    installs = []
    monkeypatch.setattr(setup_jobs, "install_scripts", lambda *_: installs.append(True))
    result = setup_jobs._activate_jobs(ctx)
    assert "no Hermes Tingle scripts or jobs were changed" in result
    assert not installs and not ctx.jobs


def test_failed_create_without_saved_marker_does_not_remove_ambiguous_id(monkeypatch):
    class FailedUnknown(FakeCron):
        removed = []
        def dispatch_tool(self, name, payload):
            if payload["action"] == "remove":
                self.removed.append(payload["job_id"])
            result = super().dispatch_tool(name, payload)
            if payload["action"] == "create":
                return {"success": False, "job_id": result["job_id"]}
            return result
    ctx = FailedUnknown()
    fake_cron_setup(monkeypatch, ctx)
    result = setup_jobs._activate_jobs(ctx)
    assert "unconfirmed" in result and "id-1" in result
    assert not ctx.removed


def test_conflicting_create_ids_are_not_blindly_removed(monkeypatch):
    class ConflictingIds(FakeCron):
        removed = []
        def dispatch_tool(self, name, payload):
            if payload["action"] == "remove":
                self.removed.append(payload["job_id"])
            result = super().dispatch_tool(name, payload)
            if payload["action"] == "create":
                result["job_id"] = "id-untrusted"
            return result
    ctx = ConflictingIds()
    fake_cron_setup(monkeypatch, ctx)
    result = setup_jobs._activate_jobs(ctx)
    assert "unconfirmed" in result and "id-1" in result
    assert "id-1" in ctx.jobs
    assert not ctx.removed


def test_setup_unknown_create_result_does_not_delete_unowned_candidate(monkeypatch):
    class UnknownCreate(FakeCron):
        def dispatch_tool(self, name, payload):
            result = super().dispatch_tool(name, payload)
            if payload["action"] == "create":
                raise RuntimeError("lost acknowledgement")
            return result
    ctx = UnknownCreate()
    fake_cron_setup(monkeypatch, ctx)
    result = setup_jobs._activate_jobs(ctx)
    assert "unconfirmed" in result and "id-1" in result
    assert "id-1" in ctx.jobs


def test_setup_reports_unverified_rollback_after_list_failure(monkeypatch):
    class FailList(FakeCron):
        lists = 0
        def dispatch_tool(self, name, payload):
            if payload["action"] == "list":
                self.lists += 1
                if self.lists > 1:
                    return {"success": False}
            return super().dispatch_tool(name, payload)
    ctx = FailList()
    ctx.fail_create = True
    fake_cron_setup(monkeypatch, ctx)
    result = setup_jobs._activate_jobs(ctx)
    assert "unconfirmed" in result and "id-1" in result and "id-2" in result
    assert set(ctx.jobs) == {"id-1", "id-2"}  # No removal without an exact-ID inventory.


def test_setup_rolls_back_if_configuration_changes_during_creation(tmp_path, synthetic_settings, monkeypatch):
    import json
    class Changing(FakeCron):
        def dispatch_tool(self, name, payload):
            result = super().dispatch_tool(name, payload)
            if payload["action"] == "create":
                (tmp_path / "hermes-tingle.json").write_text(json.dumps({**synthetic_settings, "delivery": "telegram:123"}))
            return result
    ctx = Changing()
    fake_cron_setup(monkeypatch, ctx)
    result = setup_jobs._activate_jobs(ctx)
    assert "rolled back" in result and not ctx.jobs


def test_fake_setup_dispatches_configured_cadence_for_two_profiles(tmp_path, synthetic_settings, monkeypatch):
    import json
    for minutes, zone in [(12, "America/New_York"), (75, "Asia/Tokyo")]:
        settings = {**synthetic_settings, "deadline_poll_minutes": minutes, "timezone": zone}
        (tmp_path / "hermes-tingle.json").write_text(json.dumps(settings))
        ctx = FakeCron()
        fake_cron_setup(monkeypatch, ctx)
        assert "is scheduled" in setup_jobs._activate_jobs(ctx)
        assert ctx.jobs["id-2"]["schedule"] == fake_schedule(f"every {minutes}m")
        assert ctx.jobs["id-2"]["timezone"] == zone
        assert ctx.jobs["id-2"]["no_agent"] is True


def test_setup_completion_tells_owner_to_restart_for_new_tools(tmp_path, synthetic_settings, monkeypatch):
    import json
    (tmp_path / "hermes-tingle.json").write_text(json.dumps(synthetic_settings))
    ctx = FakeCron()
    fake_cron_setup(monkeypatch, ctx)
    result = setup_jobs._activate_jobs(ctx)
    assert "is scheduled" in result
    assert "hermes gateway restart" in result and "/new" in result and "separate terminal" in result
    assert "tool" in result or "action" in result


def test_each_new_setting_is_required_before_setup(tmp_path, synthetic_settings):
    import json
    for key in ("date_only_time", "availability_start", "availability_end", "deadline_poll_minutes"):
        values = {k: v for k, v in synthetic_settings.items() if k != key}
        (tmp_path / "hermes-tingle.json").write_text(json.dumps(values))
        class NoChanges:
            def ask_choice(self, *args):
                raise AssertionError("invalid configuration must stop before confirmation")
            def dispatch_tool(self, *args):
                raise AssertionError("invalid configuration must not touch cron")
        assert "stopped before changes" in setup_jobs._activate_jobs(NoChanges())


def test_job_specs_pin_topic_timezone_and_minimal_toolset(tmp_path, synthetic_settings):
    specs = setup_jobs.job_specs(tmp_path)
    daily = specs[0]
    due = specs[1]

    assert daily["name"] == "Tingle daily review"
    assert due["name"] == "Tingle nudges"
    assert daily["schedule"] == synthetic_settings["daily_schedule"]
    assert daily["timezone"] == synthetic_settings["timezone"]
    assert daily["deliver"] == synthetic_settings["delivery"]
    assert daily["failure_deliver"] == synthetic_settings["delivery"]
    assert daily["script"] == "hermes_tingle_snapshot.py"
    assert daily["enabled_toolsets"] == ["hermes_tingle", "web", "no_mcp"]
    assert "open_loops" not in daily["prompt"]
    assert "/openloops" not in daily["prompt"]
    assert daily["attach_to_session"] is True
    assert "wrap each Hermes Tingle item ID in inline backticks" in daily["prompt"]
    assert "human-readable dates in the configured timezone" in daily["prompt"]
    assert "profile_timezone" in daily["prompt"]
    assert "never output raw ISO timestamps" in daily["prompt"]
    assert "in:anywhere" in daily["prompt"]
    assert "all messages in the thread are in Trash" in daily["prompt"]
    assert "metadata/snippets" in daily["prompt"]
    assert "light first-pass triage" in daily["prompt"]
    assert "Each message ID can be read once per 14-day body-read dedupe window" in daily["prompt"]
    assert "Gmail sync uses a persisted history cursor" in daily["prompt"]
    assert "Never use a fixed follow-up delay" in daily["prompt"]
    assert "email_reply_checks shows state='no_reply'" in daily["prompt"]
    assert "auto-reply, acknowledgment, or promised future response keeps it waiting" in daily["prompt"]
    assert "stored per-thread interval" in daily["prompt"]
    assert "never nudge on received reply or missing/failed checks" in daily["prompt"]
    assert "no global body-count cap" in daily["prompt"]
    assert "Never claim a relevant email has no URL based only on metadata/snippets" in daily["prompt"]
    assert "available under the recent-scan and per-message dedupe rules" in daily["prompt"]
    assert "read cap" not in daily["prompt"]
    assert "directly relevant public link" in daily["prompt"]
    assert "never claim full coverage" in daily["prompt"]
    assert "When the user directly corrects report behavior or states a reusable feature expectation, use hermes_tingle(action='capture_feedback')" in daily["prompt"]
    assert "one-off factual corrections without a reusable rule affect only the current item/report" in daily["prompt"]
    assert "ask before promoting it to an approved rule" in daily["prompt"]
    assert "user_approved_feedback_rules" in daily["prompt"]
    assert "check_calendar_availability" in daily["prompt"]
    assert "without event titles" in daily["prompt"]
    assert "7-day Calendar availability check" in daily["prompt"]
    assert "never write Calendar" in daily["prompt"]
    assert "If a sent reply already confirms a call time and a matching Calendar event is present, treat it as scheduled, not a conflict" in daily["prompt"]
    assert "availability action only for unscheduled candidate starts" in daily["prompt"]
    assert "do not create a duplicate item" in daily["prompt"]
    assert "send/reply to email" in daily["prompt"]
    assert due["name"] == "Tingle nudges"
    assert due["script"] == "hermes_tingle_due.py"
    assert due["no_agent"] is True
    assert due["enabled_toolsets"] == []
    assert due["deliver"] == daily["deliver"]
    assert due["failure_deliver"] == synthetic_settings["delivery"]
    assert due["timezone"] == synthetic_settings["timezone"]


def test_review_prompt_preserves_independent_reply_waits(tmp_path):
    prompt = setup_jobs.job_specs(tmp_path)[0]['prompt']
    assert 'A linked reply-wait remains independently due' in prompt
    assert 'never complete the broader goal based on a reply alone' in prompt


def test_review_prompt_labels_suggestions_not_added_this_run_without_claiming_global_absence(tmp_path):
    prompt = setup_jobs.job_specs(tmp_path)[0]['prompt']
    assert 'Not added to Tingle in this review' in prompt
    assert 'Reply if you want me to track it' in prompt
    assert 'Never use this label for a tracked item you are reminding or updating' in prompt
    assert 'Do not claim the suggestion is absent from Tingle based only on its absence from bounded context' in prompt
    assert 'Only call it tracked when a matching active item ID is in supplied context' in prompt
    assert 'reuse a matching active item instead of creating a duplicate' in prompt


@pytest.mark.parametrize('job_name', [setup_jobs.DAILY_NAME, setup_jobs.LEGACY_DAILY_NAME])
def test_refresh_review_command_prepares_operator_only_update(job_name, tmp_path, synthetic_settings, monkeypatch):
    import json
    from types import SimpleNamespace
    config = tmp_path / 'hermes-tingle.json'
    config.write_text(json.dumps({**synthetic_settings, 'owner_principals':['telegram:42']}))
    source = SimpleNamespace(platform='telegram', user_id='42', chat_id='42', chat_type='dm', is_bot=False)

    class Context:
        def __init__(self):
            self.calls = []
            self.job = {'id':'review-1','name':job_name,'script':'hermes_tingle_snapshot.py',
                        'prompt':'Existing customized prompt.', 'enabled':True,
                        'next_run_at':'2026-11-24T09:00:00+01:00',
                        'enabled_toolsets':['hermes_tingle','web','no_mcp']}
        def dispatch_tool(self, name, payload):
            self.calls.append(copy.deepcopy(payload))
            assert payload['action']=='list'
            return {'success':True,'count':1,'jobs':[{'job_id':'review-1','name':job_name}]}
        def read_job(self, job_id):
            return copy.deepcopy(self.job) if job_id=='review-1' else None
        def ask_choice(self, *args):
            raise AssertionError('The DM command must not edit or confirm the Cron job')

    ctx = Context()
    monkeypatch.setattr(setup_jobs, '_cron_api', lambda: (fake_schedule, ctx.read_job))
    result = setup_jobs.refresh_review_prompt_from_command(ctx, source)
    assert 'No changes were made here' in result
    assert 'refresh_review_prompt.py' in result and '--job-id' in result and 'review-1' in result
    assert '--profile' in result and 'default' in result and '--apply' in result
    assert 'hermes cron edit' in result
    assert 'Hermes OS user' in result and '`marco`' not in result
    assert 'cannot inspect process arguments' in result
    assert [call['action'] for call in ctx.calls] == ['list']


def test_saved_review_prompt_refresh_is_idempotent(tmp_path, synthetic_settings, monkeypatch):
    import hermes_tingle
    monkeypatch.setattr(hermes_tingle, '_source_sender_error', lambda *a, **k: None)
    class Context:
        def __init__(self):
            self.jobs={'review-1': {'id':'review-1','name':setup_jobs.DAILY_NAME,
                'script':'hermes_tingle_snapshot.py','prompt':'Saved prompt'+setup_jobs.REVIEW_PROMPT_REFRESH,
                'enabled':True,'next_run_at':'2026-11-24T09:00:00+01:00',
                'enabled_toolsets':['hermes_tingle','web','no_mcp']}}
            self.asked=False
        def dispatch_tool(self, name, payload):
            assert payload['action']=='list'
            return {'success':True,'count':1,'jobs':[{'job_id':'review-1','name':setup_jobs.DAILY_NAME}]}
        def read_job(self, jid): return copy.deepcopy(self.jobs.get(jid))
        def ask_choice(self, *_): self.asked=True; return 'update'
    ctx=Context()
    monkeypatch.setattr(setup_jobs, '_cron_api', lambda: (fake_schedule, ctx.read_job))
    result=setup_jobs.refresh_review_prompt_from_command(ctx, object())
    assert 'already includes this wording' in result
    assert not ctx.asked


@pytest.mark.parametrize('source', [
    type('Sender', (), {'platform':'telegram','user_id':'42','chat_id':'-10077','chat_type':'supergroup','is_bot':False})(),
    type('Sender', (), {'platform':'telegram','user_id':'99','chat_id':'99','chat_type':'dm','is_bot':False})(),
])
def test_saved_review_prompt_refresh_rejects_unverified_sender_before_cron_read(monkeypatch, tmp_path, synthetic_settings, source):
    import json
    config = tmp_path / 'hermes-tingle.json'
    config.write_text(json.dumps({**synthetic_settings, 'owner_principals':['telegram:42']}))
    class NoAccess:
        def dispatch_tool(self, *args): raise AssertionError('Cron must not be read')
        def ask_choice(self, *args): raise AssertionError('No choice may be shown')
    result=setup_jobs.refresh_review_prompt_from_command(NoAccess(), source)
    assert ('verified direct message' in result or 'not a configured owner principal' in result)
    assert 'Nothing changed' in result or 'nothing changed' in result


def test_daily_prompt_refresh_guidance_is_separated_from_coverage_guidance(tmp_path):
    prompt = setup_jobs.job_specs(tmp_path)[0]["prompt"]
    marker = setup_jobs.REVIEW_PROMPT_REFRESH_MARKER
    assert marker in prompt
    marker_end = prompt.index(marker) + len(marker)
    coverage_start = prompt.index("Report material source errors or coverage gaps")
    assert prompt[marker_end:coverage_start].endswith("\n\n")


def test_review_prompt_applies_owner_nudge_calibration(tmp_path, synthetic_settings):
    prompt = setup_jobs.job_specs(tmp_path)[0]["prompt"]
    assert "Calendar conflict deduplication is receipt-driven from the supplied transition data" in prompt
    assert "never infer prior alerts from attached-session history" in prompt
    assert "For low-urgency service or account notices already delivered by email, do not repeat the notice in the report" in prompt
    assert "If such a notice has a real deadline requiring action, add one quiet reminder before it and omit the notice from the report" in prompt
    assert "or duplicate an existing reminder" in prompt
    assert "Do not present retry-loop or delivery-status bookkeeping as a follow-up item" in prompt
    assert "if a failure needs an owner decision, state it plainly as a failure rather than a follow-up" in prompt


def test_verified_email_reply_nudges_offer_a_grounded_draft_without_sending(tmp_path):
    prompt = setup_jobs.job_specs(tmp_path)[0]["prompt"]

    assert "For each verified no_reply nudge, briefly explain what is still awaited using only the tracked summary or available thread context" in prompt
    assert "offer to draft a follow-up" in prompt
    assert "If the context is insufficient, say what is missing rather than inventing details" in prompt
    assert "Do not draft or send the email automatically" in prompt
    assert "If the check is missing or failed, do not claim no reply or issue the nudge" in prompt
    assert "Repeat nudges use the item's stored per-thread interval and stop after four reserved attempts" in prompt


def test_daily_review_requests_context_grounded_next_actions_for_due_items(tmp_path):
    prompt = setup_jobs.job_specs(tmp_path)[0]["prompt"]

    assert "For each due item you remind" in prompt
    assert "suggest one concrete next action" in prompt
    assert "stored title, summary and available source context" in prompt
    assert "do not invent people, facts, missing inputs or deadlines" in prompt
    assert "ask for the missing context" in prompt
    assert "Do not draft or send the email automatically" in prompt


def test_usage_skill_matches_email_nudge_draft_offer_policy():
    usage = (Path(__file__).resolve().parents[1] / "skills" / "usage" / "SKILL.md").read_text()
    assert "For verified no-reply nudges, briefly state what is awaited using available tracked or thread context and offer to draft a follow-up" in usage
    assert "Do not draft or send automatically" in usage


def test_install_scripts_refuses_to_overwrite_user_file(tmp_path):
    source = tmp_path / "plugin" / "scripts"
    target = tmp_path / "home" / "scripts"
    source.mkdir(parents=True)
    snapshot_source = source / "source_snapshot.py"
    due_source = source / "due_alerts.py"
    snapshot_source.write_text("snapshot helper\n")
    due_source.write_text("due helper\n")
    target.mkdir(parents=True)
    target_file = target / "hermes_tingle_due.py"
    target_file.write_text("user-owned content\n")

    try:
        setup_jobs.install_scripts(tmp_path / "plugin", target)
    except FileExistsError:
        pass
    else:
        raise AssertionError("unexpected overwrite of an existing user script")

    assert target_file.read_text() == "user-owned content\n"
    assert not (target / "hermes_tingle_snapshot.py").exists()


def test_setup_with_missing_config_stops_before_cron_or_install(tmp_path, monkeypatch):
    (tmp_path / "hermes-tingle.json").unlink()
    monkeypatch.setattr(setup_jobs, "install_scripts", lambda *args: (_ for _ in ()).throw(AssertionError("must not install")))

    class ConfirmContext:
        def ask_choice(self, question, choices):
            return "create"

        def dispatch_tool(self, *args, **kwargs):
            raise AssertionError("invalid configuration must not touch cron")

    assert "stopped before changes" in setup_jobs._activate_jobs(ConfirmContext())


def test_setup_requires_explicit_confirmation_before_any_tool_call():
    class CancelContext:
        def ask_choice(self, question, choices):
            assert "Set up Hermes Tingle automation" in question
            assert "explicit profile configuration" in question
            assert any(choice.get("value") == "cancel" for choice in choices)
            for heading in ("When\n", "Where reminders go\n", "What it reads\n", "Who sees it\n"):
                assert heading in question
            assert "four reserved attempts" in question and "without an agent/model" in question
            print("\n--- setup preview\n" + question)
            return "cancel"

        def dispatch_tool(self, *args, **kwargs):
            raise AssertionError("cancelled setup must not touch cron")

    assert setup_jobs._activate_jobs(CancelContext()).endswith("no scripts or cron jobs were changed.")


@pytest.mark.parametrize("name", [
    "Tingle daily review", "Tingle nudges",
    "[SA] Hermes Tingle daily review", "[S] Hermes Tingle deadline nudges",
])
@pytest.mark.parametrize("enabled", [False, True])
def test_old_or_new_display_name_blocks_duplicate_without_mutating_existing_id(monkeypatch, name, enabled):
    ctx = FakeCron()
    ctx.jobs["stable-existing-id"] = {
        "id": "stable-existing-id", "name": name, "enabled": enabled,
        "schedule": {"kind": "interval", "minutes": 47},
        "prompt": "preserve synthetic user instructions", "deliver": "synthetic-no-delivery",
        "failure_deliver": "synthetic-failure-route", "custom": {"keep": True},
    }
    before = copy.deepcopy(ctx.jobs)
    fake_cron_setup(monkeypatch, ctx)
    def forbidden(*args):
        raise AssertionError("existing legacy/current job must stop before helper install")
    monkeypatch.setattr(setup_jobs, "install_scripts", forbidden)
    result = setup_jobs._activate_jobs(ctx)
    assert "no duplicate jobs" in result
    assert ctx.jobs == before


def test_new_display_names_create_once_with_original_scripts_and_ids(monkeypatch):
    ctx = FakeCron()
    fake_cron_setup(monkeypatch, ctx)
    assert "scheduled" in setup_jobs._activate_jobs(ctx)
    before = copy.deepcopy(ctx.jobs)
    assert [job["name"] for job in before.values()] == ["Tingle daily review", "Tingle nudges"]
    assert [job["script"] for job in before.values()] == ["hermes_tingle_snapshot.py", "hermes_tingle_due.py"]
    assert "no duplicate jobs" in setup_jobs._activate_jobs(ctx)
    assert ctx.jobs == before
