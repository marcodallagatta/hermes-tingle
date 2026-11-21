"""Gateway-neutral delivery: reminders must not depend on Telegram.

Config accepts any explicit Hermes cron delivery destination (strict Telegram
form, generic platform:target, or a bare platform home-channel token) and
refuses broadcast/routing tokens; setup fails loudly when the platform is not
a gateway Hermes ships.
"""
import json
import sys
import types

import pytest

import hermes_tingle_config as config
import setup_jobs


@pytest.mark.parametrize("delivery", [
    "telegram:123456789",
    "telegram:-1001234567890:42",
    "telegram",                 # bare platform: its configured home channel
    "whatsapp:1234567890",
    "slack:C0123456789",
    "discord:123456789012345678",
    "signal:12345",
    "matrix:!room:example.org",
    "whatsapp", "slack", "signal",
])
def test_explicit_destinations_for_any_gateway_are_accepted(delivery):
    assert config.validate_delivery(delivery) == delivery


@pytest.mark.parametrize("delivery", [
    "", None, 123,
    "all", "origin", "local",   # routing tokens are not explicit destinations
    "all:123",
    "telegram:invalid", "telegram:",
    "slack:", "whatsapp:",
    "bad platform:123", "whatsapp:chat id",
    "Telegram:123",             # platform names are lowercase upstream
    "x" * 40 + ":123",
    "telegram:1:2:3:4",
])
def test_non_explicit_or_malformed_destinations_are_rejected(delivery):
    with pytest.raises(ValueError):
        config.validate_delivery(delivery)


def test_load_settings_accepts_a_non_telegram_gateway(tmp_path, synthetic_settings):
    (tmp_path / "hermes-tingle.json").write_text(
        json.dumps({**synthetic_settings, "delivery": "whatsapp:1234567890"}))
    assert config.load_settings(tmp_path)["delivery"] == "whatsapp:1234567890"


def _fake_registry(monkeypatch, known):
    module = types.ModuleType("gateway.platform_registry")
    module.core_ships_platform = lambda name: name in known
    gateway = types.ModuleType("gateway")
    gateway.platform_registry = module
    monkeypatch.setitem(sys.modules, "gateway", gateway)
    monkeypatch.setitem(sys.modules, "gateway.platform_registry", module)


def test_setup_fails_for_an_unknown_gateway(tmp_path, synthetic_settings, monkeypatch):
    _fake_registry(monkeypatch, known={"telegram"})
    (tmp_path / "hermes-tingle.json").write_text(
        json.dumps({**synthetic_settings, "delivery": "pagerduty:12345"}))
    with pytest.raises(ValueError, match="not a Hermes gateway"):
        setup_jobs.job_specs(tmp_path)


def test_setup_accepts_a_known_non_telegram_gateway(tmp_path, synthetic_settings, monkeypatch):
    _fake_registry(monkeypatch, known={"telegram", "whatsapp"})
    (tmp_path / "hermes-tingle.json").write_text(
        json.dumps({**synthetic_settings, "delivery": "whatsapp:1234567890"}))
    specs = setup_jobs.job_specs(tmp_path)
    assert all(spec["deliver"] == "whatsapp:1234567890" for spec in specs)
    assert all(spec["failure_deliver"] == "whatsapp:1234567890" for spec in specs)


def test_setup_without_the_gateway_module_falls_back_to_pattern_checks(tmp_path, synthetic_settings, monkeypatch):
    monkeypatch.delitem(sys.modules, "gateway", raising=False)
    monkeypatch.delitem(sys.modules, "gateway.platform_registry", raising=False)
    monkeypatch.setattr(setup_jobs, "_check_delivery_platform", lambda delivery: None)
    (tmp_path / "hermes-tingle.json").write_text(
        json.dumps({**synthetic_settings, "delivery": "whatsapp:1234567890"}))
    assert setup_jobs.job_specs(tmp_path)


def test_agent_prompt_is_gateway_neutral(tmp_path, synthetic_settings):
    prompt = setup_jobs._job_specs(synthetic_settings)[0]["prompt"]
    assert "Telegram" not in prompt
    assert "chat" in prompt
