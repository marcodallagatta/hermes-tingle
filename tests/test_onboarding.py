"""Synthetic onboarding checks. No accounts, real jobs or profiles are touched."""
import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import hermes_tingle as plugin
import hermes_tingle_sources as sources
import hermes_tingle_store as store
import setup_jobs

spec = importlib.util.spec_from_file_location("tingle_init", Path(__file__).resolve().parents[1] / "tools" / "tingle_init.py")
wizard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wizard)


def owner(tmp_path, settings):
    data = {**settings, "owner_principals": ["telegram:42"]}
    (tmp_path / "hermes-tingle.json").write_text(json.dumps(data))
    return SimpleNamespace(platform="telegram", user_id="42", chat_id="-123", chat_type="dm", is_bot=False)


def parser(monkeypatch):
    monkeypatch.setattr(setup_jobs, "_cron_api", lambda: (lambda s: {"kind": "cron"}, lambda _: None))


def test_pure_validation_does_not_write(tmp_path, synthetic_settings):
    before = (tmp_path / wizard.NAME).read_bytes()
    assert wizard.validate_settings(synthetic_settings) == synthetic_settings
    with pytest.raises(ValueError):
        wizard.validate_settings({**synthetic_settings, "delivery": "all"})
    assert (tmp_path / wizard.NAME).read_bytes() == before


def test_fresh_wizard_confirmed_defaults(tmp_path, monkeypatch):
    (tmp_path / wizard.NAME).unlink()
    parser(monkeypatch)
    answers = iter(["", "telegram:123", "", "", "", "", "", "telegram:42", "telegram:123", "WRITE"])
    output = []
    assert wizard.wizard(tmp_path, ask=lambda _: next(answers), emit=output.append)
    data = wizard.load_settings(tmp_path)
    assert data["daily_schedule"] == "0 0,8,16 * * *"
    assert data["deadline_poll_minutes"] == 30
    assert data["owner_principals"] == ["telegram:42"]
    assert not data["calendar_exclude_substrings"]
    assert "not separate human authentication" in " ".join(output)
    assert (tmp_path / wizard.NAME).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("repair", [False, True])
def test_valid_policy_never_overwritten(tmp_path, repair):
    before = (tmp_path / wizard.NAME).read_bytes()
    with pytest.raises(ValueError, match="valid"):
        wizard.wizard(tmp_path, repair=repair, ask=lambda _: pytest.fail("no prompts"))
    assert (tmp_path / wizard.NAME).read_bytes() == before


def test_repair_preserves_policy_and_unknown_fields(tmp_path, monkeypatch, synthetic_settings):
    data = {**synthetic_settings, "timezone": "bad", "custom": {"preserve": True}, "owner_principals": ["telegram:1", "slack:2"]}
    original = json.dumps(data).encode()
    (tmp_path / wizard.NAME).write_bytes(original)
    parser(monkeypatch)
    answers = iter(["Europe/Berlin"] + [""] * 6 + [""] * 5 + [data["delivery"], "WRITE"])
    assert wizard.wizard(tmp_path, repair=True, ask=lambda _: next(answers), emit=lambda _: None)
    assert wizard.load_settings(tmp_path) == {**data, "timezone": "Europe/Berlin"}
    assert next(tmp_path.glob(".tingle-policy-backup-*")).read_bytes() == original


def test_corrupt_policy_refuses_reset(tmp_path):
    (tmp_path / wizard.NAME).write_text("{broken")
    with pytest.raises(ValueError, match="parseable"):
        wizard.wizard(tmp_path, repair=True)
    assert (tmp_path / wizard.NAME).read_text() == "{broken"


def test_missing_policy_with_existing_plugin_data_refuses_defaults(tmp_path):
    (tmp_path / wizard.NAME).unlink()
    (tmp_path / "plugin-data" / "hermes-tingle").mkdir(parents=True)
    with pytest.raises(ValueError, match="database"):
        wizard.wizard(tmp_path)


def test_write_policy_rechecks_bytes_and_refuses_symlink(tmp_path, synthetic_settings):
    with pytest.raises(ValueError, match="changed"):
        wizard.write_policy(tmp_path, synthetic_settings, b"older")
    (tmp_path / wizard.NAME).unlink()
    (tmp_path / "outside").write_text("untouched")
    (tmp_path / wizard.NAME).symlink_to(tmp_path / "outside")
    with pytest.raises(OSError):
        wizard.write_policy(tmp_path, synthetic_settings, None)
    assert (tmp_path / "outside").read_text() == "untouched"


def test_cancel_and_changed_during_confirmation(tmp_path, monkeypatch, synthetic_settings):
    (tmp_path / wizard.NAME).unlink()
    parser(monkeypatch)
    answers = iter(["", "telegram:123", "", "", "", "", "", "", "wrong"])
    assert not wizard.wizard(tmp_path, ask=lambda _: next(answers), emit=lambda _: None)
    assert not (tmp_path / wizard.NAME).exists()
    answers = iter(["", "telegram:123", "", "", "", "", "", "", "telegram:123", "WRITE"])
    def race(question):
        answer = next(answers)
        if "WRITE" in question:
            (tmp_path / wizard.NAME).write_text(json.dumps(synthetic_settings))
        return answer
    with pytest.raises(ValueError, match="changed"):
        wizard.wizard(tmp_path, ask=race, emit=lambda _: None)
    assert wizard.load_settings(tmp_path) == synthetic_settings


def test_missing_config_orientation_and_identity_no_enrollment(tmp_path):
    (tmp_path / wizard.NAME).unlink()
    sender = SimpleNamespace(platform="telegram", user_id="42", chat_id="-123", chat_type="dm", is_bot=False)
    assert "operator" in plugin.handle_command(None, "", source=sender)
    assert "tingle_init.py" not in plugin.handle_command(None, "", source=sender)
    assert "no item captured" in plugin.handle_tool(None, {"action": "add", "title": "x"})
    result = plugin.handle_command(None, "identity", source=sender)
    assert "telegram:42" in result and "-123" in result and "no owner enrollment" in result
    assert not (tmp_path / wizard.NAME).exists()


@pytest.mark.parametrize("change", [{"user_id": "43"}, {"chat_type": "group"}, {"is_bot": True}, {"platform": "slack"}])
def test_setup_rejects_nonowner_before_preview(tmp_path, synthetic_settings, monkeypatch, change):
    sender = owner(tmp_path, synthetic_settings)
    for key, value in change.items():
        setattr(sender, key, value)
    monkeypatch.setattr(setup_jobs, "_activate_jobs", lambda *a, **k: pytest.fail("not authorized"))
    assert any(word in plugin.handle_command(None, "setup", source=sender) for word in ("refused", "direct message"))


def test_setup_owner_allowed_ownerless_refused(tmp_path, synthetic_settings, monkeypatch):
    sender = owner(tmp_path, synthetic_settings)
    monkeypatch.setattr(setup_jobs, "_activate_jobs", lambda *a, **k: "preview reached")
    assert plugin.handle_command(None, "setup", source=sender) == "preview reached"
    (tmp_path / wizard.NAME).write_text(json.dumps(synthetic_settings))
    assert "no owner principal" in plugin.handle_command(None, "setup", source=sender)


def test_corrupt_owner_fragment_cannot_authorize(tmp_path, synthetic_settings, monkeypatch):
    sender = owner(tmp_path, synthetic_settings)
    data = json.loads((tmp_path / wizard.NAME).read_text())
    data.pop("delivery")
    (tmp_path / wizard.NAME).write_text(json.dumps(data))
    monkeypatch.setattr(setup_jobs, "_activate_jobs", lambda *a, **k: pytest.fail("not authorized"))
    assert "repair" in plugin.handle_command(None, "setup", source=sender)


def test_offers_only_local_presence_and_chat_interfaces(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "_workspace_scripts", lambda: tmp_path)
    monkeypatch.setattr(sources, "_chat_session_resolver", None)
    monkeypatch.setattr(sources, "_chat_message_reader", None)
    assert sources.source_capabilities() == set()
    (tmp_path / "gws_bridge.py").touch()
    assert sources.source_capabilities() == {"email", "calendar"}
    assert "account connection not checked" in sources.source_offers()


@pytest.mark.parametrize("kind", ["email", "calendar", "chat"])
def test_minimal_post_consent_probes(monkeypatch, kind):
    use_real_identity(monkeypatch)
    monkeypatch.setattr(sources, "source_capabilities", lambda: set(store.SOURCE_KINDS))
    calls = []
    def read(*args):
        calls.append(args)
        return {"emailAddress":"owner@example.invalid", "id":"owner@example.invalid", "historyId": "123", "items": [], "files": []}
    monkeypatch.setattr(sources, "_gws_bridge", read)
    sources.probe_source(kind)
    assert len(calls) == (0 if kind == "chat" else 1)
    if calls:
        params = json.loads(calls[0][-1])
        if kind == "email":
            assert params == {"userId": "me", "fields": "emailAddress"}
        else:
            assert params == {"calendarId":"primary","fields":"id"}


def test_probe_failure_changes_no_grant(tmp_path, synthetic_settings, monkeypatch):
    sender = owner(tmp_path, synthetic_settings)
    before = store.list_source_optins()
    def fail(kind):
        raise sources.SourceReadError("unreachable")
    monkeypatch.setattr(sources, "probe_source", fail)
    assert plugin.handle_command(None, "source enable email 7", source=sender) == "unreachable"
    assert store.list_source_optins() == before


def test_probe_revocation_race_does_not_restore_grant(tmp_path, synthetic_settings, monkeypatch):
    sender = owner(tmp_path, synthetic_settings)
    monkeypatch.setattr(sources, "probe_source", lambda _: store.disable_source("email", note="later owner revocation"))
    reply = plugin.handle_command(None, "source enable email 7", source=sender)
    assert "consent changed" in reply
    assert not store.source_enabled("email")


def test_nonowner_never_probes(tmp_path, synthetic_settings, monkeypatch):
    sender = owner(tmp_path, synthetic_settings)
    sender.user_id = "visitor"
    monkeypatch.setattr(sources, "probe_source", lambda _: pytest.fail("no consent"))
    assert "not a configured owner" in plugin.handle_command(None, "source enable email", source=sender)


def test_owner_revoked_during_confirmation_stops_jobs(tmp_path, synthetic_settings):
    sender = owner(tmp_path, synthetic_settings)
    class Context:
        def ask_choice(self, *args):
            (tmp_path / wizard.NAME).write_text(json.dumps(synthetic_settings))
            return "create"
        def dispatch_tool(self, *args):
            pytest.fail("revoked owner must not reach cron")
    assert "stopped before changes" in setup_jobs.setup_from_command(Context(), source=sender)


def test_activation_profile_change_stops_before_writes(tmp_path, synthetic_settings, monkeypatch):
    import hermes_constants
    next_home = tmp_path / "other"
    next_home.mkdir()
    (next_home / wizard.NAME).write_text(json.dumps(synthetic_settings))
    class Context:
        def ask_choice(self, *args):
            monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: next_home)
            return "create"
        def dispatch_tool(self, *args):
            pytest.fail("changed profile must not reach cron")
    assert "active profile changed" in setup_jobs._activate_jobs(Context())


def test_local_activation_adapter_calls_only_cron(monkeypatch):
    import sys
    module = SimpleNamespace(cronjob=lambda **kwargs: kwargs)
    monkeypatch.setitem(sys.modules, "tools.cronjob_tools", module)
    ctx = wizard.LocalActivation()
    assert ctx.dispatch_tool("cronjob_manage", {"action": "list"}) == {"action": "list"}
    with pytest.raises(ValueError):
        ctx.dispatch_tool("other", {})


def test_failed_probe_malformed_responses_redacted(monkeypatch):
    use_real_identity(monkeypatch)
    monkeypatch.setattr(sources, "source_capabilities", lambda: set(store.SOURCE_KINDS))
    monkeypatch.setattr(sources, "_gws_bridge", lambda *args: {"error": "sensitive provider details"})
    for kind in ("email", "calendar"):
        with pytest.raises(sources.SourceReadError, match="[Nn]o grant changed") as exc:
            sources.probe_source(kind)
        assert "sensitive" not in str(exc.value)
    with pytest.raises(sources.SourceReadError):
        sources.probe_source("document")


@pytest.mark.parametrize("missing", ["calendar_exclude_substrings", "workstream_exclude_values",
                                     "source_exclude_substrings", "ticket_exclude_patterns"])
def test_repair_missing_required_list_demands_explicit_json(tmp_path, monkeypatch, synthetic_settings, missing):
    data = copy.deepcopy(synthetic_settings)
    data.pop(missing)
    original = json.dumps(data).encode()
    (tmp_path / wizard.NAME).write_bytes(original)
    parser(monkeypatch)
    def answer(question):
        if question.startswith(missing):
            assert "missing requires explicit JSON" in question
            return '["Explicit repair exclusion"]'
        if question.startswith("Type destination"):
            return data["delivery"]
        if "Type WRITE" in question:
            return "WRITE"
        return ""
    assert wizard.wizard(tmp_path, repair=True, ask=answer, emit=lambda _: None)
    expected = {**data, missing: ["Explicit repair exclusion"]}
    assert wizard.load_settings(tmp_path) == expected
    assert next(tmp_path.glob(".tingle-policy-backup-*")).read_bytes() == original


@pytest.mark.parametrize("missing", ["calendar_exclude_substrings", "workstream_exclude_values",
                                     "source_exclude_substrings", "ticket_exclude_patterns"])
def test_repair_missing_required_list_blank_never_invents_default(tmp_path, monkeypatch, synthetic_settings, missing):
    data = copy.deepcopy(synthetic_settings)
    data.pop(missing)
    original = json.dumps(data).encode()
    (tmp_path / wizard.NAME).write_bytes(original)
    parser(monkeypatch)
    with pytest.raises(ValueError, match="explicit string list"):
        wizard.wizard(tmp_path, repair=True, ask=lambda _: "", emit=lambda _: None)
    assert (tmp_path / wizard.NAME).read_bytes() == original


@pytest.mark.parametrize("change", ["owner", "profile", "grant"])
def test_source_prelock_changes_never_probe_or_restore(tmp_path, synthetic_settings, monkeypatch, change):
    import contextlib
    import hermes_constants
    sender = owner(tmp_path, synthetic_settings)
    original = store.serialized_operation
    other = tmp_path / "other"
    other.mkdir()
    (other / wizard.NAME).write_text((tmp_path / wizard.NAME).read_text())
    @contextlib.contextmanager
    def waiting(category):
        with original(category):
            monkeypatch.setattr(store, "serialized_operation", original)
            if change == "owner":
                (tmp_path / wizard.NAME).write_text(json.dumps(synthetic_settings))
            elif change == "profile":
                monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: other)
            else:
                store.disable_source("email", note="newer revocation while stale enable waits")
            yield
    monkeypatch.setattr(store, "serialized_operation", waiting)
    monkeypatch.setattr(sources, "probe_source", lambda _: pytest.fail("stale authority must never probe"))
    reply = plugin.handle_command(None, "source enable email 7", source=sender)
    assert "no grant changed and no probe" in reply
    if change == "grant":
        assert not store.source_enabled("email")


def test_wizard_privacy_disclosed_before_policy_prompts(tmp_path, monkeypatch):
    (tmp_path / wizard.NAME).unlink()
    output = []
    def stop(question):
        joined = " ".join(output)
        assert "Hermes host" in joined and "configured model provider" in joined
        assert "does not cover the rest" in joined and "does not erase" in joined
        raise EOFError()
    with pytest.raises(EOFError):
        wizard.wizard(tmp_path, ask=stop, emit=output.append)
    assert not (tmp_path / wizard.NAME).exists()


def test_activation_privacy_in_preview_before_choice():
    class Context:
        def ask_choice(self, question, choices):
            assert "configured model provider" in question
            assert "without an agent/model" in question
            assert "before activating" in question
            return "cancel"
        def dispatch_tool(self, *args):
            pytest.fail("cancel before effects")
    assert "cancelled" in setup_jobs._activate_jobs(Context())


def test_source_privacy_before_enable_direction(monkeypatch):
    monkeypatch.setattr(sources, "probe_source", lambda _: pytest.fail("orientation cannot probe"))
    result = plugin.handle_command(None, "sources")
    assert result.index("configured AI provider") < result.index("Change with")
    assert "can't pull back content already sent" in result
    assert "Google may refresh its saved sign-in on this profile" in result
    assert "chat checks access without reading messages" in result
    assert "active session records across this platform" in result
    assert "stored prompt/config fields" in result
    assert "does not persist or send the other fields" in result


def use_real_identity(monkeypatch):
    import importlib.util
    spec=importlib.util.spec_from_file_location('onboarding_real_identity',sources.__file__)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    module.SourceReadError=sources.SourceReadError
    monkeypatch.setattr(module,'_gws_bridge',lambda *a,**k:sources._gws_bridge(*a,**k))
    monkeypatch.setattr(sources,'body_account_identity',module.body_account_identity)
    monkeypatch.setattr(sources,'calendar_account_identity',module.calendar_account_identity)


# Calendar probe is restored explicitly in probe-transport tests, not other unit tests.


@pytest.mark.parametrize("zone", ["Etc/UTC", "Europe/Berlin", "Asia/Tokyo"])
def test_new_cadence_creates_one_review_and_one_script_nudge(tmp_path, monkeypatch, zone):
    from test_setup_jobs import FakeCron, fake_cron_setup

    (tmp_path / wizard.NAME).unlink()
    parser(monkeypatch)
    prompts = []
    answers = iter([zone, "telegram:123", "", "", "", "", "", "telegram:42", "telegram:123", "WRITE"])

    def answer(prompt):
        prompts.append(prompt)
        return next(answers)

    assert wizard.wizard(tmp_path, ask=answer, emit=lambda _: None)
    assert any("00:00, 08:00, 16:00 in configured timezone" in p for p in prompts)
    ctx = FakeCron()
    fake_cron_setup(monkeypatch, ctx)
    setup_jobs._activate_jobs(ctx)
    assert len(ctx.jobs) == 2
    review = next(j for j in ctx.jobs.values() if j["name"] == setup_jobs.DAILY_NAME)
    nudge = next(j for j in ctx.jobs.values() if j["name"] == setup_jobs.DUE_NAME)
    assert review["schedule"]["expr"] == "0 0,8,16 * * *"
    assert review["timezone"] == zone
    assert review["no_agent"] is False
    assert nudge["schedule"]["minutes"] == 30
    assert nudge["no_agent"] is True
    before = copy.deepcopy(ctx.jobs)
    assert "duplicate" in setup_jobs._activate_jobs(ctx)
    assert ctx.jobs == before


def test_upgrade_defaults_leave_existing_policy_and_jobs_unchanged(tmp_path, monkeypatch, synthetic_settings):
    from test_setup_jobs import FakeCron, fake_cron_setup

    before_policy = (tmp_path / wizard.NAME).read_bytes()
    assert wizard.DEFAULTS["daily_schedule"] == "0 0,8,16 * * *"
    ctx = FakeCron()
    fake_cron_setup(monkeypatch, ctx)
    setup_jobs._activate_jobs(ctx)
    review = next(j for j in ctx.jobs.values() if j["name"] == setup_jobs.DAILY_NAME)
    assert review["schedule"]["expr"] == synthetic_settings["daily_schedule"]
    assert (tmp_path / wizard.NAME).read_bytes() == before_policy
    before_jobs = copy.deepcopy(ctx.jobs)
    assert "duplicate" in setup_jobs._activate_jobs(ctx)
    assert ctx.jobs == before_jobs
    assert (tmp_path / wizard.NAME).read_bytes() == before_policy
