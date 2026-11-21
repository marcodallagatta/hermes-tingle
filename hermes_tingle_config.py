"""Explicit profile settings for Hermes Tingle; missing policy fails closed.

owner_principals is the only ground for global authority decisions (source
opt-in grants). It is optional: absent or empty means NO principal can make
those decisions - first-caller enrollment is never inferred.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_TELEGRAM_DELIVERY = re.compile(r"telegram:-?\d{1,20}(?::\d{1,20})?")
_PLATFORM_TOKEN = re.compile(r"[a-z][a-z0-9_]{0,30}")
_GENERIC_DELIVERY = re.compile(r"[a-z][a-z0-9_]{0,30}:[^\s,:]{1,120}(?::[^\s,:]{1,120}){0,2}")
# Hermes cron routing tokens that are not explicit single destinations.
_RESERVED_DELIVERY_TOKENS = {"all", "origin", "local"}


def validate_delivery(delivery: object) -> str:
    """Explicit Hermes cron delivery target for reminder jobs.

    Telegram keeps the strict polished form (telegram:<chat-id>[:<topic-id>]);
    every other gateway uses a generic platform:target[:thread] destination or
    a bare platform token (that platform's configured home channel), matching
    the shapes Hermes cron delivery resolves. Broadcast/routing tokens
    (all/origin/local) are refused: reminders must go to one explicit place.
    """
    if not isinstance(delivery, str) or not delivery.strip():
        raise ValueError("Tingle config delivery must be an explicit delivery destination")
    delivery = delivery.strip()
    if delivery.split(":", 1)[0].lower() in _RESERVED_DELIVERY_TOKENS:
        raise ValueError("Tingle config delivery must name one explicit destination, not a routing token")
    if delivery.startswith("telegram:"):
        # The polished path stays strict: numeric chat and optional topic IDs only.
        if _TELEGRAM_DELIVERY.fullmatch(delivery):
            return delivery
        raise ValueError(
            "Tingle config delivery must be an explicit Hermes delivery destination, "
            "for example telegram:<chat-id>[:<topic-id>], whatsapp:<chat-id>, or a bare platform name"
        )
    if (_PLATFORM_TOKEN.fullmatch(delivery) or _GENERIC_DELIVERY.fullmatch(delivery)):
        return delivery
    raise ValueError(
        "Tingle config delivery must be an explicit Hermes delivery destination, "
        "for example telegram:<chat-id>[:<topic-id>], whatsapp:<chat-id>, or a bare platform name"
    )


def calendar_workstream_aliases(settings: dict) -> dict[str, str]:
    aliases = settings.get("calendar_workstream_aliases", {})
    workstreams = {v.strip().casefold(): v.strip() for v in settings["workstream_exclude_values"]}
    if not isinstance(aliases, dict) or any(
        not isinstance(k, str) or not k.strip() or not isinstance(v, str)
        or v.strip().casefold() not in workstreams for k, v in aliases.items()
    ):
        raise ValueError("Tingle config calendar aliases must map nonempty substrings to excluded workstreams")
    normalized = {}
    for key, value in aliases.items():
        normalized.setdefault(key.strip().casefold(), workstreams[value.strip().casefold()])
    return normalized


def load_settings(home: Path | None = None) -> dict:
    if home is None:
        from hermes_constants import get_hermes_home
        home = Path(get_hermes_home())
    try:
        data = json.loads((Path(home) / "hermes-tingle.json").read_text())
    except (OSError, ValueError) as exc:
        raise ValueError("Tingle config missing or invalid") from exc
    return validate_settings(data)


def validate_settings(data: object) -> dict:
    """Validate a candidate without touching the profile or normalizing policy."""
    if not isinstance(data, dict):
        raise ValueError("Tingle config must be an object")
    for key in ("calendar_exclude_substrings", "workstream_exclude_values", "source_exclude_substrings", "ticket_exclude_patterns"):
        value = data.get(key)
        if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
            raise ValueError(f"Tingle config requires an explicit string list: {key}")
    calendar_workstream_aliases(data)
    try:
        for pattern in data["ticket_exclude_patterns"]:
            re.compile(pattern)
    except re.error as exc:
        raise ValueError("Tingle config has an invalid ticket exclusion pattern") from exc
    owners = data.get("owner_principals", [])
    if not isinstance(owners, list) or any(
        not isinstance(v, str) or not re.fullmatch(r"[a-z0-9_]+:[^\s:]+", v.strip(), re.IGNORECASE)
        for v in owners
    ):
        raise ValueError("Tingle config owner_principals must be a list of 'platform:user_id' strings")
    validate_delivery(data.get("delivery"))
    if not isinstance(data.get("daily_schedule"), str) or not data["daily_schedule"].strip():
        raise ValueError("Tingle config requires daily_schedule")
    try:
        ZoneInfo(data.get("timezone", ""))
    except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("Tingle config requires a valid timezone") from exc
    for key in ("date_only_time", "availability_start", "availability_end"):
        if not isinstance(data.get(key), str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", data[key]):
            raise ValueError(f"Tingle config requires {key} in HH:MM format")
    if data["availability_start"] >= data["availability_end"]:
        raise ValueError("Tingle config availability_start must be before availability_end on the same day")
    cadence = data.get("deadline_poll_minutes")
    if isinstance(cadence, bool) or not isinstance(cadence, int) or not 1 <= cadence <= 1440:
        raise ValueError("Tingle config deadline_poll_minutes must be an integer from 1 to 1440")
    verbose = data.get("verbose_diagnostics", False)
    if not isinstance(verbose, bool):
        raise ValueError("Tingle config verbose_diagnostics must be a boolean when given")
    return data


def private_chat_evaluation_policy(settings: dict) -> dict | None:
    """Separate default-OFF consent. Unknown route/pricing is never a grant."""
    value = settings.get('private_chat_evaluation')
    required = {'enabled', 'consent', 'approved_by', 'route', 'monthly_limit_microeur'}
    if isinstance(value, dict) and value.get('route') != 'runtime-default':
        required |= {'model', 'pricing_revision'}  # Reviewed fixed adapters (including synthetic tests).
    if not isinstance(value, dict) or set(value) != required or value.get('enabled') is not True:
        return None
    if (value.get('consent') not in ('owner-dm-excerpts-v1', 'owner-dm-excerpts-v2') or
            not isinstance(value.get('approved_by'), str) or
            value['approved_by'] not in {v.strip().casefold() for v in settings.get('owner_principals', [])} or
            type(value.get('monthly_limit_microeur')) is not int or
            not ((value['consent'] == 'owner-dm-excerpts-v1' and 1 <= value['monthly_limit_microeur'] <= 5_000_000) or
                 (value['consent'] == 'owner-dm-excerpts-v2' and value['monthly_limit_microeur'] == 0 and value['route'] == 'runtime-default')) or
            any(not isinstance(value.get(k), str) or not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,120}', value[k])
                for k in ({'route', 'model', 'pricing_revision'} & required))):
        return None
    return dict(value)


def local_timezone() -> ZoneInfo:
    return ZoneInfo(load_settings()["timezone"])
