"""Agent-facing Hermes Tingle tools and `/tingle` command."""
from __future__ import annotations

from pathlib import Path

import json
import re
import shlex
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

try:  # Native plugin import (package-relative); fallback supports the isolated test harness.
    from . import hermes_tingle_store as store
    from . import hermes_tingle_sources as sources
    from .hermes_tingle_config import load_settings, local_timezone
except ImportError:  # pragma: no cover - exercised by the test harness
    import hermes_tingle_store as store
    import hermes_tingle_sources as sources
    from hermes_tingle_config import load_settings, local_timezone

_ID_RE = re.compile(r"^(?:HT|OL)-[A-F0-9]{6}$", re.IGNORECASE)
_RELATIVE_RE = re.compile(r"^(\d{1,3})(m|h|d|w)$", re.IGNORECASE)

# Version of the code actually loaded in this process. Chat replies show THIS
# constant, never a value re-read from plugin.yaml at reply time: a stale host
# child still serving old code must keep reporting its old version, so the
# owner can see from chat whether a restart is still needed. Bump it together
# with plugin.yaml on every meaningful batch (a test guards the match).
VERSION = "0.9.0"


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _approval_parameters_display(raw: Any) -> str:
    """Keep approval JSON inside Telegram's inline-code display boundary.

    Backticks terminate Markdown code spans, so encode them as equivalent JSON
    Unicode escapes. The displayed JSON remains losslessly decodable while the
    parameter value cannot break out into formatted chat text.
    """
    try:
        parameters = json.loads(str(raw))
    except (TypeError, ValueError):
        return "{\"error\":\"parameters unavailable\"}"
    return json.dumps(parameters, ensure_ascii=False, separators=(",", ":")).replace("`", r"\u0060")


def _format_local_time(value: str | None) -> str:
    if not value:
        return ""
    local_zone = local_timezone()
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=local_zone)
        return dt.astimezone(local_zone).strftime("%a, %-d %B %Y at %H:%M")
    except (TypeError, ValueError):
        return "date unavailable"


def _excluded_work(args: dict[str, Any]) -> bool:
    settings = load_settings()
    workstream = str(args.get("workstream") or "").strip().casefold()
    if workstream in {v.strip().casefold() for v in settings["workstream_exclude_values"]}:
        return True
    combined = " ".join(str(args.get(k) or "") for k in ("source_ref", "source_url")).casefold()
    if any(v.casefold() in combined for v in settings["source_exclude_substrings"]):
        return True
    content = " ".join(str(args.get(k) or "") for k in ("title", "summary", "source_ref", "source_url"))
    return any(re.search(pattern, content, re.IGNORECASE) for pattern in settings["ticket_exclude_patterns"])


def handle_tool(ctx: Any, args: dict[str, Any], **kwargs: Any) -> str:
    """Handle the `hermes_tingle` model tool. All writes stay in the local plugin DB."""
    del kwargs
    action = str(args.get("action", "")).strip()
    try:
        if action == "propose_case_merge":
            try:
                from . import hermes_tingle_case_merge as case_merge
            except ImportError:
                import hermes_tingle_case_merge as case_merge
            return _json({'success':True, **case_merge.propose(args.get('item_ids'), args.get('canonical_id'), args.get('reason'))})
        if action == "decline_case_merge":
            try:
                from . import hermes_tingle_case_merge as case_merge
            except ImportError:
                import hermes_tingle_case_merge as case_merge
            return _json({'success':True, **case_merge.decline(str(args.get('proposal_id','')).upper())})
        if action == "open_case_merge_review":
            try:
                from . import hermes_tingle_case_merge as case_merge
            except ImportError:
                import hermes_tingle_case_merge as case_merge
            source = _tool_turn_source()
            refusal = _source_sender_error(source, noun='Case merge previews')
            if refusal:
                # Leak-free refusal: no titles, IDs, reasons or counts leave this
                # branch, so a group-topic or unverifiable turn learns nothing.
                return (refusal + " Say 'review the merge' in your private DM with the bot; "
                        "nothing was shown or changed.")
            mid = str(args.get('proposal_id', '') or '').upper() or None
            try:
                resolved = case_merge.resolve_pending(mid)
            except (ValueError, KeyError) as exc:
                return str(exc)
            if isinstance(resolved, dict):
                return _json({'success': True, 'choose_one': resolved['proposals'],
                              'note': 'Ask the owner which proposal to review; nothing opened or changed.'})
            try:
                # The shared preview path: same first page, paging and final-choice
                # commit rechecks as /tingle merge. Opening never commits.
                return case_merge.handle(ctx, source, resolved)
            except (AttributeError, TypeError):
                return ("This chat cannot open the interactive review here. "
                        "Run `/tingle merge " + resolved + "` in your DM; nothing was shown or changed.")
            except (ValueError, KeyError) as exc:
                return str(exc)

        if action == "add":
            if str(args.get('source_ref', '')).casefold().startswith('chat-discovery:'):
                raise ValueError('Discovery suggestions require explicit owner approval in the private preview')
            try:
                load_settings()
            except ValueError:
                try:
                    from .hermes_tingle_setup import PROVISIONING
                except ImportError:
                    from hermes_tingle_setup import PROVISIONING
                raise ValueError(PROVISIONING + " Tracking refused; no item captured.")
            if _excluded_work(args):
                return _json({
                    "success": True,
                    "skipped": True,
                    "reason": "Work is excluded by the profile configuration.",
                })
            if args.get('source_type') == 'email' and not args.get('completion_signal'):
                args = dict(args, completion_signal='source_email_checkpoint')
                if not isinstance(args.get('review_plan'), dict):
                    raise ValueError('Email tracking needs a contextual review_plan; use unknown/review-needed rather than inventing a default interval')
            recur_interval, recur_unit = store.resolve_recurrence_args(
                args.get("recur_text"), args.get("recur_interval"), args.get("recur_unit"))
            item = store.add_item(
                title=args.get("title", ""),
                review_plan=args.get("review_plan"),
                summary=args.get("summary", ""),
                status=args.get("status", "open"),
                owner=args.get("owner", "me"),
                priority=args.get("priority", "normal"),
                source_type=args.get("source_type", "conversation"),
                source_ref=args.get("source_ref", ""),
                source_url=args.get("source_url", ""),
                completion_signal=args.get("completion_signal", ""),
                due_at=args.get("due_at"),
                next_check_at=args.get("next_check_at"),
                recur_interval=recur_interval,
                recur_unit=recur_unit,
                recur_lead_days=args.get("recur_lead_days"),
                origin=_reaction_context(),
                hard_deadline=args.get("hard_deadline"),
            )
            if item.get("origin_message_id"):
                _react(item["origin_platform"], item["origin_chat_id"], item["origin_message_id"], "👀")
            return _json({"success": True, "item": item,
                          "review_note": ('Contextual check time unknown; ask the owner and disclose that no reminder is scheduled.'
                              if item.get('completion_signal') == 'source_email_checkpoint' and not item.get('next_check_at') else '')})

        if action == "add_step":
            if _excluded_work({"title": args.get("title", ""), "summary": args.get("why", "")}):
                raise ValueError("Step matches an exclusion in the profile configuration.")
            return _json({"success": True, "step": store.add_step(args.get("item_id", ""), title=args.get("title", ""), why=args.get("why", ""), wait_item_id=args.get("wait_item_id", ""))})

        if action == "add_document":
            if _excluded_work({"title": args.get("document_name", ""), "summary": args.get("note", "")}):
                raise ValueError("Document matches an exclusion in the profile configuration.")
            return _json({"success": True, "document": store.add_document(args.get("item_id", ""), name=args.get("document_name", ""), valid_until=args.get("valid_until", ""), note=args.get("note", ""))})

        if action == "update_document":
            return _json({"success": True, "document": store.update_document(args.get("item_id", ""), position=int(args.get("document_position") or 0), state=args.get("document_state", ""), valid_until=args.get("valid_until", ""), note=args.get("note", ""))})

        if action == "add_requirement":
            if _excluded_work({"title": args.get("requirement_text", ""), "summary": ""}):
                raise ValueError("Requirement matches an exclusion in the profile configuration.")
            return _json({"success": True, "requirement": store.add_requirement(args.get("item_id", ""), text=args.get("requirement_text", ""), must=bool(args.get("must") or False))})

        if action == "update_requirement":
            met = args.get("met")
            return _json({"success": True, "requirement": store.update_requirement(args.get("item_id", ""), position=int(args.get("requirement_position") or 0), text=args.get("requirement_text", ""), must=args.get("must") if "must" in args else None, met=bool(met) if met is not None else None)})

        if action == "add_option":
            if _excluded_work({"title": args.get("option_name", ""), "summary": args.get("detail", "")}):
                raise ValueError("Option matches an exclusion in the profile configuration.")
            return _json({"success": True, "option": store.add_option(args.get("item_id", ""), name=args.get("option_name", ""), detail=args.get("detail", ""), price_text=args.get("price_text", ""), note=args.get("note", ""))})

        if action == "update_option":
            return _json({"success": True, "option": store.update_option(args.get("item_id", ""), position=int(args.get("option_position") or 0), state=args.get("option_state", ""), detail=args.get("detail", ""), price_text=args.get("price_text", ""), note=args.get("note", ""))})

        if action == "add_booking":
            if _excluded_work({"title": args.get("booking_name", ""), "summary": args.get("detail", "")}):
                raise ValueError("Booking matches an exclusion in the profile configuration.")
            return _json({"success": True, "booking": store.add_booking(args.get("item_id", ""), kind=args.get("booking_kind", "other") or "other", name=args.get("booking_name", ""), detail=args.get("detail", ""), cancel_by=args.get("cancel_by", ""), confirmation_ref=args.get("confirmation_ref", ""), note=args.get("note", ""))})

        if action == "update_booking":
            return _json({"success": True, "booking": store.update_booking(args.get("item_id", ""), position=int(args.get("booking_position") or 0), state=args.get("booking_state", ""), detail=args.get("detail", ""), cancel_by=args.get("cancel_by", ""), confirmation_ref=args.get("confirmation_ref", ""), note=args.get("note", ""))})

        if action == "propose_option":
            proposal = store.propose_option(
                args.get("item_id", ""),
                position=int(args.get("option_position") or 0),
                approver=_current_principal(),
                requested_by="hermes_tingle tool",
            )
            return _json({"success": True, **proposal, "note": "Proposal recorded only - nothing was bought, booked or paid. The purchase approval is decided with /tingle approve."})

        if action == "complete_step":
            return _json({"success": True, "step": store.complete_step(args.get("item_id", ""), args.get("step_position"))})

        if action == "list":
            status = str(args.get("list_status", "active"))
            limit = max(1, min(int(args.get("limit", 20)), 100))
            offset = int(args.get("offset", 0))
            items = store.list_items(status=status, limit=limit, offset=offset)
            more = bool(store.list_items(status=status, limit=1, offset=offset + len(items)))
            return _json({"success": True, "items": items, "count": len(items), "offset": offset, "has_more": more, "timezone": local_timezone().key})

        if action == "get":
            item_id = str(args.get("item_id", "")).upper()
            if not _ID_RE.fullmatch(item_id):
                raise ValueError("item_id must look like HT-1A2B3C")
            item = store.get_item(item_id)
            if item is None:
                raise KeyError("Hermes Tingle item not found")
            return _json({"success": True, "item": item, "timezone": local_timezone().key})

        if action == "update":
            item_id = str(args.get("item_id", "")).upper()
            if not _ID_RE.fullmatch(item_id):
                raise ValueError("item_id must look like HT-1A2B3C")
            fields = {k: args[k] for k in ("title", "summary", "status", "owner", "priority", "due_at", "next_check_at", "source_url", "completion_signal", "recur_interval", "recur_unit", "recur_lead_days", "hard_deadline", "review_plan") if k in args}
            if "recur_text" in args:
                fields["recur_interval"], fields["recur_unit"] = store.resolve_recurrence_args(
                    args.get("recur_text"), fields.get("recur_interval"), fields.get("recur_unit"))
            if any(key in fields for key in ("title", "summary", "source_url")) and _excluded_work(fields):
                raise ValueError("Update matches an exclusion in the profile configuration.")
            return _json({"success": True, "item": store.update_item(item_id, **fields)})

        if action == "complete":
            item_id = str(args.get("item_id", "")).upper()
            if not _ID_RE.fullmatch(item_id):
                raise ValueError("item_id must look like HT-1A2B3C")
            return _json({"success": True, "item": store.complete_item(item_id, note=args.get("note", ""))})

        if action == "dismiss":
            item_id = str(args.get("item_id", "")).upper()
            if not _ID_RE.fullmatch(item_id):
                raise ValueError("item_id must look like HT-1A2B3C")
            return _json({"success": True, "item": store.dismiss_item(item_id)})

        if action == "snooze":
            item_id = str(args.get("item_id", "")).upper()
            if not _ID_RE.fullmatch(item_id):
                raise ValueError("item_id must look like HT-1A2B3C")
            if not args.get("until"):
                raise ValueError("until is required")
            return _json({"success": True, "item": store.snooze_item(item_id, until=args["until"])})

        if action == "due":
            items = store.due_items(limit=args.get("limit", 20))
            return _json({"success": True, "items": items, "count": len(items), "timezone": local_timezone().key})

        if action == "mark_notified":
            item_ids = args.get("item_ids")
            if not isinstance(item_ids, list) or not item_ids:
                raise ValueError("item_ids must be a non-empty list")
            if any(not _ID_RE.fullmatch(str(item_id).upper()) for item_id in item_ids):
                raise ValueError("every item_id must look like HT-1A2B3C")
            return _json({"success": True, "items": sources.prepare_reminder_batch(item_ids, expected_versions=args.get("expected_versions"), expected_email_checks=args.get("expected_email_checks"), checkpoint_next_checks=args.get("checkpoint_next_checks")),
                          "delivery": "attempt reserved, not proof of emission or delivery; include the batch receipt as optional only. Automatic repeats follow stored cadence, at most four attempts per cycle including uncertainty. No DM acknowledgment required. Honor edits/snooze and reverify no_reply for each reply-wait nudge."})

        if action == "capture_feedback":
            feedback = store.capture_feedback_candidate(
                args.get("feedback_text", ""),
                category=args.get("feedback_category", "general"),
                source_ref=args.get("feedback_source_ref", ""),
            )
            return _json({"success": True, "feedback": feedback})

        if action == "list_feedback":
            feedback = store.list_feedback_candidates(
                status=args.get("feedback_status", "pending"),
                limit=args.get("limit", 20),
            )
            return _json({"success": True, "feedback": feedback, "count": len(feedback)})

        if action == "resolve_feedback":
            # Authorization boundary: a rule becomes a trusted preference only
            # through the user's explicit /tingle rule command. The model turns
            # that surface this tool run inside an authenticated user session, so
            # a model call here is never evidence of the user's decision.
            return _json({"success": False,
                          "error": "Feedback candidates are approved or dismissed only by the user's explicit /tingle rule approve|dismiss command, never by the model. List the candidate ID for the user instead."})

        if action == "mark_context_reviewed":
            try:
                from .hermes_tingle_review import acknowledge
            except ImportError:
                from hermes_tingle_review import acknowledge
            acknowledge(args.get('review_receipt'))
            return _json({'success':True,'delivery_confirmed':False,'recovery':'If the final report is lost, an owner can use /tingle source review-again document or chat to requeue current leads. This marker records model review, not delivery.'})

        if action == "mark_source_reviewed":
            with store.serialized_operation("sources"):
                kind = str(args.get("source_type", ""))
                if not store.source_enabled(kind):
                    raise ValueError("source is disabled; nothing was marked reviewed")
                created = store.mark_source_reviewed(
                    kind,
                    str(args.get("source_id", "")),
                    str(args.get("source_version", "")),
                    source_generation=args.get("source_generation"),
                )
            return _json({"success": True, "already_reviewed": not created})

        if action == "propose_lead_edit":
            principal = _current_principal()
            try:
                from . import hermes_tingle_lookback as lookback
            except ImportError:
                import hermes_tingle_lookback as lookback
            result = lookback.propose_edit(
                f"{principal[0]}:{principal[1]}" if principal else None,
                args.get("lead_id"), args.get("title"), args.get("summary"), args.get("due_at"),
            )
            return _json({"success": True, **result})

        if action == "scan_sources":
            snapshot = sources.collect_snapshot(
                lookback_days=args.get("lookback_days", 14),
                upcoming_days=args.get("upcoming_days", 7),
                discovery_enabled=False,
            )
            return _json({"success": True, "snapshot": snapshot})

        if action == "check_calendar_availability":
            availability = sources.check_calendar_availability(
                date_value=args.get("date", ""),
                candidate_starts=args.get("candidate_starts", []),
                duration_minutes=args.get("duration_minutes", 30),
            )
            return _json({"success": True, "availability": availability})

        if action == "read_email":
            load_settings()  # Do not consume a read allowance without a valid source policy.
            message_id = str(args.get("source_id", "")).strip()
            return _json({"success": True, "email": sources.read_email(message_id)})

        if action == "request_approval":
            approval = store.request_approval(
                args.get("action_type"),
                args.get("target"),
                args.get("parameters") or {},
                ttl_hours=args.get("ttl_hours", 24),
                requested_by="hermes_tingle tool",
                approver=_current_principal(),
            )
            return _json({"success": True, "approval": approval})

        if action == "list_approvals":
            source = _tool_turn_source()
            refusal = _source_sender_error(source, noun="Approval listings")
            if refusal is not None:
                return _json({"success": False, "error": refusal})
            approvals = store.list_approvals(
                args.get("approval_status"), limit=args.get("limit", 20),
                approver=_deciding_principal(source))
            return _json({"success": True, "approvals": approvals, "count": len(approvals)})

        if action == "retry_unreadable":
            ids = args.get("item_ids")
            if ids is not None and not isinstance(ids, list):
                raise ValueError("item_ids must be a list when given")
            return _json({"success": True, "requeued": store.retry_quarantined_gmail(ids)})

        if action == "list_sources":
            return _json({"success": True, "sources": store.list_source_optins()})

        if action == "link_watch":
            item_id = str(args.get("item_id", "")).upper()
            if not _ID_RE.fullmatch(item_id):
                raise ValueError("item_id must look like HT-1A2B3C")
            return _json({"success": True, "item": store.link_watch(
                item_id, args.get("watch_ref", ""), max_age_hours=args.get("watch_max_age_hours"))})

        if action == "report_watch":
            item_id = str(args.get("item_id", "")).upper()
            if not _ID_RE.fullmatch(item_id):
                raise ValueError("item_id must look like HT-1A2B3C")
            return _json({"success": True, "item": store.report_watch(item_id, args.get("state", ""))})

        raise ValueError("unsupported action")
    except (ValueError, KeyError) as exc:
        return _json({"success": False, "error": str(exc)})
    except Exception as exc:
        return _json({"success": False, "error": f"Hermes Tingle operation failed: {type(exc).__name__}"})


def _parse_snooze(value: str) -> str:
    raw = value.strip()
    match = _RELATIVE_RE.fullmatch(raw)
    if match:
        amount = int(match.group(1))
        unit = match.group(2).lower()
        delta = {
            "m": timedelta(minutes=amount),
            "h": timedelta(hours=amount),
            "d": timedelta(days=amount),
            "w": timedelta(weeks=amount),
        }[unit]
        return (datetime.now(timezone.utc) + delta).isoformat(timespec="seconds")
    # Store parser accepts ISO-8601 and date-only values; validation happens there.
    return raw


def _record_command_interaction() -> None:
    """Note that a /tingle command succeeded. This is NOT a delivery receipt:
    a user-initiated reply does not prove any alert batch arrived. Bookkeeping
    must never break the command itself."""
    try:
        store.note_command_interaction()
    except Exception:
        pass


# Chat types where other participants can watch or type: approval decisions are
# refused there unless the sender is in a direct conversation.
_GROUP_CHAT_TYPES = frozenset({"group", "channel", "thread"})


def _current_principal() -> tuple[str, str, str] | None:
    """(platform, user_id, chat_id) of the principal this agent turn serves, when the
    Hermes session context exposes it. Absent context (CLI, cron without origin,
    tests) yields None, and the request is then registered unbound: undecidable
    by anyone, expiring on its own - never bound to a guessed owner.

    Reads ONLY the task-local context bindings (_bound_session_value, Hermes's own
    approval-identity reader): never os.environ, which is process-global and could
    leak another session's identity into this one. The values are bound by the
    gateway from the authenticated inbound source and inherited into tool worker
    threads via copy_context; the model can neither supply nor overwrite them.
    """
    try:
        from gateway.session_context import _UNSET, _bound_session_value
    except ImportError:
        return None

    def _read(name: str) -> str:
        try:
            value = _bound_session_value(name)
        except Exception:
            return ""
        return "" if value is _UNSET or value is None else str(value).strip()

    platform = (_read("HERMES_SESSION_PLATFORM") or _read("HERMES_SESSION_SOURCE")).casefold()
    # USER_ID is the conversation's route, not necessarily this turn's sender.
    # Both supported pins expose ACTOR_ID and can dynamically clear it when
    # multiple senders merge into a turn. Empty/unbound actors fail closed.
    user_id = _read("HERMES_SESSION_ACTOR_ID")
    chat_id = _read("HERMES_SESSION_CHAT_ID")
    if not platform or not user_id:
        return None
    return (platform, user_id, chat_id)


def _session_chat_type(platform: str, user_id: str, chat_id: str) -> str | None:
    """Best-effort host-derived chat shape for the current turn: 'dm', 'group' or None.

    Prefers the gateway's own HERMES_SESSION_CHAT_TYPE binding. When the gateway does
    not bind one, only the Telegram direct-chat convention (chat ID equal to the user
    ID) proves a DM; anything else is unknown and callers fail closed.
    """
    try:
        from gateway.session_context import _UNSET, _bound_session_value
        try:
            value = _bound_session_value("HERMES_SESSION_CHAT_TYPE")
        except Exception:
            value = _UNSET
        if value is not _UNSET and value is not None:
            shape = str(value).strip().casefold()
            if shape in ("dm", "private"):
                return "dm"
            if shape in _GROUP_CHAT_TYPES or shape == "supergroup":
                return "group"
    except ImportError:
        pass
    if platform == "telegram" and user_id and chat_id and str(chat_id) == str(user_id):
        return "dm"
    return None


def _tool_turn_source() -> Any | None:
    """Host-derived sender/chat for the current model-tool turn, or None.

    Built ONLY from the task-local gateway session bindings (never from model-supplied
    arguments), so it carries the same authenticated identity the command dispatch
    would. None when the turn has no verifiable principal or chat shape; callers fail
    closed and show nothing.
    """
    principal = _current_principal()
    if principal is None:
        return None
    platform, user_id, chat_id = principal
    chat_type = _session_chat_type(platform, user_id, chat_id)
    if chat_type is None:
        return None
    return SimpleNamespace(platform=platform, user_id=user_id, chat_id=chat_id,
                           chat_type=chat_type, is_bot=False)


def _deciding_principal(source: Any) -> tuple[str, str, str]:
    """The dispatch-authenticated principal tuple for a present, identified source."""
    platform = getattr(source, "platform", None)
    platform = getattr(platform, "value", platform) or ""
    return (str(platform).casefold(), str(getattr(source, "user_id", "") or ""),
            str(getattr(source, "chat_id", "") or ""))


def _reaction_context() -> tuple[str, str, str] | None:
    """(platform, chat_id, message_id) of the message this turn answers, when the
    session context binds all three. Task-local only (_bound_session_value); the
    model can neither supply nor forge these."""
    try:
        from gateway.session_context import _UNSET, _bound_session_value
    except ImportError:
        return None

    def _read(name: str) -> str:
        try:
            value = _bound_session_value(name)
        except Exception:
            return ""
        return "" if value is _UNSET or value is None else str(value).strip()

    platform = (_read("HERMES_SESSION_PLATFORM") or _read("HERMES_SESSION_SOURCE")).casefold()
    chat_id = _read("HERMES_SESSION_CHAT_ID")
    message_id = _read("HERMES_SESSION_MESSAGE_ID")
    if not (platform and chat_id and message_id):
        return None
    return (platform, chat_id, message_id)


def _platform_actions() -> Any:
    """Hermes's capability-gated platform-actions facade for this plugin, or None
    outside a real runtime. Reactions send only when the operator explicitly
    enabled platform actions for the plugin (Hermes default: OFF)."""
    try:
        from hermes_cli.platform_actions import PlatformActions
    except ImportError:
        return None
    try:
        return PlatformActions("hermes-tingle")
    except Exception:
        return None


def _react(platform: Any, chat_id: Any, message_id: Any, emoji: str) -> None:
    """Best-effort task-relevant reaction. Decoration only: the state change it
    marks has already happened, so any failure (no gateway, capability off,
    unsupported platform) is a silent no-op and never breaks the flow."""
    platform_s, chat_id_s, message_id_s = (str(platform or "").strip(), str(chat_id or "").strip(),
                                           str(message_id or "").strip())
    if not (platform_s and chat_id_s and message_id_s):
        return
    actions = _platform_actions()
    if actions is None:
        return
    try:
        import asyncio
        coro = actions.add_reaction(platform_s, chat_id_s, message_id_s, emoji)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            loop.create_task(coro)
        else:
            asyncio.run(coro)
    except Exception:
        return


def _react_on_command_message(source: Any, emoji: str) -> None:
    """React to the /tingle command message itself, when dispatch bound one."""
    if source is None:
        return
    platform = getattr(source, "platform", None)
    platform = getattr(platform, "value", platform)
    _react(platform, getattr(source, "chat_id", None), getattr(source, "message_id", None), emoji)


def _react_on_origin(item: Any, emoji: str, *, skip_message_id: Any = None) -> None:
    """React to the message an item was captured from, unless already reacted."""
    if not isinstance(item, dict):
        return
    message_id = item.get("origin_message_id")
    if not message_id or str(message_id) == str(skip_message_id or ""):
        return
    _react(item.get("origin_platform"), item.get("origin_chat_id"), message_id, emoji)


def _verified_direct_message(source: Any) -> bool:
    """Require explicit private-chat metadata; never infer a DM from missing evidence."""
    return (str(getattr(source, "chat_type", "")).casefold() in ("dm", "private")
            and bool(str(getattr(source, "chat_id", "") or "").strip()))


def _approval_sender_error(source: Any) -> str | None:
    """Require an identified, configured owner in a positively verified DM."""
    return _source_sender_error(source, noun="Approval decisions")


def _source_sender_error(source: Any, *, noun: str = "Source opt-in changes") -> str | None:
    """Fail-closed sender check for global-authority changes; None when acceptable.

    Same boundary as approval decisions: grant, widening and revocation need an
    identified human in a direct conversation - never the model tool, never a bot,
    never a group participant, and locality is never inferred from absent evidence.
    """
    if source is None:
        return (f"Couldn't verify who sent this command, so nothing changed. "
                f"{noun} need an identified sender in a direct message.")
    if getattr(source, "is_bot", False):
        return f"{noun} must come from a person, not a bot or webhook."
    if not _verified_direct_message(source):
        return (f"{noun} require a positively verified direct message. "
                "Run this command in your DM with the bot; nothing changed.")
    if not getattr(source, "user_id", None):
        return (f"Couldn't verify who sent this command, so nothing changed. "
                f"{noun} need an identified sender.")
    # A DM shape is not ownership: global source grants need a principal the operator
    # explicitly configured. Without configured owners, every decision refuses.
    platform = getattr(source, "platform", None)
    platform = getattr(platform, "value", platform) or ""
    sender = f"{platform}:{getattr(source, 'user_id', '')}".strip().casefold()
    try:
        owners = {str(v).strip().casefold() for v in (load_settings().get("owner_principals") or [])}
    except ValueError:
        owners = set()
    if not owners:
        return (f"{noun} are refused: no owner principal is configured, so no "
                "sender can be verified as the owner.")
    if sender not in owners:
        return f"{noun} are refused: this sender is not a configured owner principal."
    return None


def _approval_evidence(command: str, source: Any) -> str:
    """Recorded evidence of who decided: the command plus the dispatch-bound sender."""
    platform = getattr(source, "platform", None)
    platform = getattr(platform, "value", platform) or "unknown"
    user_id = getattr(source, "user_id", None) or "unknown"
    chat_type = str(getattr(source, "chat_type", "dm") or "dm")
    return f"/tingle {command} command from {platform} user {user_id} in {chat_type}"


def _active_overview() -> list[str]:
    """FT-12: one glance at what needs attention across all active items.

    Counts only nonzero states so a quiet tracker stays quiet, and names the
    soonest upcoming deadline so the next action is visible without scanning.
    """
    items = store.list_items(status="active", limit=100)
    if not items:
        return []
    now = datetime.now(timezone.utc)
    soon = now + timedelta(hours=48)

    def _due(item):
        value = item.get("due_at")
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return None

    missed = len(store.list_items(status="missed", limit=100))
    overdue = sum(1 for item in items if (due := _due(item)) and due < now)
    due_soon = sum(1 for item in items if (due := _due(item)) and now <= due <= soon)
    waiting = sum(1 for item in items if item.get("status") == "waiting")
    blocked = sum(1 for item in items if item.get("status") == "blocked")
    parts = []
    if missed:
        parts.append(f"🕳 {missed} missed window{'s' if missed != 1 else ''}")
    if overdue:
        parts.append(f"⚠️ {overdue} overdue")
    if due_soon:
        parts.append(f"📅 {due_soon} due within 48h")
    if waiting:
        parts.append(f"⏳ {waiting} waiting on others")
    if blocked:
        parts.append(f"🚧 {blocked} blocked")
    lines = []
    if parts:
        lines.append(" · ".join(parts))
    upcoming = [(due, item) for item in items if (due := _due(item)) and due >= now]
    if upcoming:
        due, item = min(upcoming, key=lambda pair: (pair[0], str(pair[1].get("id", ""))))
        lines.append(f"Next deadline: {item.get('title', '(untitled)')} — {_format_local_time(item['due_at'])}")
    return lines


def integrate(ctx: Any) -> dict[str, Any]:
    """Runtime integration, called by the plugin's register(ctx) entry point.

    Wires, through verified pinned-runtime surfaces only:
    - chat history: the public hermes_state session store (only inside a real
      Hermes runtime; stays refused otherwise);
    - source discovery: a post_gateway_admission observer at the admitted,
      post-auth lifecycle point that never alters dispatch.
    Returns a report of the available runtime interfaces."""
    try:
        from . import hermes_tingle_wiring as wiring
    except ImportError:  # pragma: no cover - top-level pytest import
        import hermes_tingle_wiring as wiring
    report = wiring.wire_from_runtime(ctx)
    register_hook = getattr(ctx, "register_hook", None)
    if callable(register_hook):
        register_hook("post_gateway_admission", wiring.observe_admitted_inbound)
        report["inbound_hook"] = True
    else:
        report["inbound_hook"] = False
    return report


def _optional_line(heading=False):
    """Truthful about source state: say what is on, not 'off until you opt in' when sources are enabled."""
    try:
        on = [r["kind"] for r in store.list_source_optins() if r["enabled"]]
    except Exception:
        return ("Source status unavailable right now: check `/tingle sources`." + ("\n" if heading else " "))
    if on:
        text = "Sources on: " + ", ".join(on) + (". Also:" if heading else ". Optional: ")
    else:
        text = "Optional, off until you opt in: "
    return text.rstrip() + ("\n" if heading else " ")


def handle_command(ctx: Any, raw_args: str, source: Any = None, **kwargs: Any) -> str:
    """Human-readable `/tingle` command for any active profile session."""
    del kwargs  # session_id/session_key/platform are unused; source carries the sender.
    head = (raw_args or "").strip().split(None, 1)
    if head and head[0].casefold() == "setup":
        try:
            from .hermes_tingle_setup import handle
        except ImportError:
            from hermes_tingle_setup import handle
        return handle(ctx, head[1] if len(head) == 2 else "", source)
    try:
        parts = shlex.split(raw_args or "")
    except ValueError:
        return "Those quotes didn't parse - `/tingle help` shows the everyday commands."
    command = parts[0].casefold() if parts else "list"
    if command == 'evaluation':
        # Consent/status must authenticate a positively verified owner DM before
        # ordinary policy reads or command observers can run.
        try:
            from .hermes_tingle_evaluation_consent import handle
        except ImportError:
            from hermes_tingle_evaluation_consent import handle
        return handle(ctx, source, parts[1:])
    if command == "identity":
        if source is None or getattr(source, "is_bot", False) or not getattr(source, "user_id", None):
            return "No authenticated human sender is available. No owner was enrolled."
        platform, user_id, chat_id = _deciding_principal(source)
        return (f"Authenticated sender: {platform}:{user_id}; chat ID: {chat_id or 'unavailable'}. "
                "Display only, no owner enrollment. Choose and confirm the reminder destination in /tingle setup; "
                "sender ID is not always the chat destination.")
    try:
        load_settings()
    except ValueError:
        try:
            from .hermes_tingle_setup import PROVISIONING
        except ImportError:
            from hermes_tingle_setup import PROVISIONING
        return PROVISIONING
    if source is not None:
        try:
            # A configured owner's command registers the chat it came from;
            # anyone else's command never makes a chat readable. Chats also
            # enter the registry from admitted inbound traffic (see wiring).
            if _source_sender_error(source) is None:
                _platform, _user_id, _chat_id = _deciding_principal(source)
                if _platform and _chat_id:
                    store.note_known_chat(f"{_platform}:{_chat_id}", origin="command")
        except Exception:
            pass
    # Refused owner commands must not mutate an existing owner-DM binding either.
    if source is not None and _source_sender_error(source) is None:
        try:
            from .hermes_tingle_discovery import observe_source
        except ImportError:
            from hermes_tingle_discovery import observe_source
        observe_source(source)
    if command == 'discover':
        try:
            from .hermes_tingle_discovery import handle
        except ImportError:
            from hermes_tingle_discovery import handle
        try:
            return handle(ctx, source, parts[1:])
        except (ValueError, KeyError):
            return 'Discovery preview is stale or unavailable; nothing changed. Review again privately.'
    if command == 'merge' or (command == 'undo' and len(parts)>1 and parts[1]=='merge'):
        try:
            from . import hermes_tingle_case_merge as case_merge
        except ImportError:
            import hermes_tingle_case_merge as case_merge
        undo=command=='undo'
        if not undo and len(parts)==1:
            refusal=_source_sender_error(source,noun='Case merge')
            if refusal: return refusal
            try: resolved=case_merge.resolve_pending(None)
            except (ValueError,KeyError) as exc: return str(exc)
            if isinstance(resolved,dict):
                return ('🤝 More than one merge review is waiting. Reply with the one you mean: '
                        + ', '.join('`'+x['proposal_id']+'`' for x in resolved['proposals'])+'.')
            mid=resolved
        elif len(parts)==(3 if undo else 2):
            mid=parts[-1].upper()
        else:
            return 'Review a proposal with `/tingle merge CM-ABCDEF12`; undo with `/tingle undo merge CM-ABCDEF12`.'
        try: return case_merge.handle(ctx,source,mid,undo=undo)
        except (ValueError,KeyError) as exc: return str(exc)
    if command == 'case' and len(parts)==2:
        refusal=_source_sender_error(source,noun='Case details')
        if refusal: return refusal
        try:
            item=store.get_item(parts[1])
            if not item or not item.get('case_merge'): return 'That item is not part of a merged case.'
            canonical=store.get_item(item['case_merge']['canonical_id'])
            members=canonical.get('case_members',[]) if canonical else []
            text=['Case records, all sources retained. Historical merged members are not separate active reminders.']
            for member in members:
                text.append('`'+member['id']+'` · '+member['title']+'\n'+member['summary']+'\nState: '+member['status']+'\nSource: '+(member['source_url'] or member['source_ref'] or member['source_type']))
            try:
                from .hermes_tingle_lookback import _preview_pages
            except ImportError:
                from hermes_tingle_lookback import _preview_pages
            pages=_preview_pages('\n\n'.join(text))
            for page in pages[:-1]:
                if ctx.ask_choice(page,[{'value':'next','label':'Next page'},{'value':'stop','label':'Stop'}])!='next': return 'Stopped viewing; nothing changed.'
            return pages[-1]
        except (ValueError,KeyError) as exc: return str(exc)
    if command == "undo" and parts[1:] == ["onboarding"]:
        refusal = _source_sender_error(source, noun="Onboarding undo")
        if refusal is not None:
            return refusal
        try:
            from . import hermes_tingle_onboarding as onboarding
        except ImportError:  # pragma: no cover - test harness import
            import hermes_tingle_onboarding as onboarding
        _record_command_interaction()
        return onboarding.undo(source)
    if command == "list" and len(parts) <= 1:
        try:
            from . import hermes_tingle_onboarding as onboarding
        except ImportError:  # pragma: no cover - test harness import
            import hermes_tingle_onboarding as onboarding
        reply = onboarding.maybe_handle(ctx, source)
        if reply is not None:
            _record_command_interaction()
            return reply
    if command == "replan":
        try:
            from . import hermes_tingle_lookback as lookback
        except ImportError:
            import hermes_tingle_lookback as lookback
        try:
            if len(parts) != 2:
                return "Choose one item: /tingle replan <ID>."
            return lookback.replan(ctx, source, parts[1])
        except (ValueError, sources.SourceReadError, lookback.assess.AssessmentError) as exc:
            return str(exc)
    if command == "lookback":
        try:
            from . import hermes_tingle_lookback as lookback
        except ImportError:
            import hermes_tingle_lookback as lookback
        try:
            return lookback.handle(ctx, parts[1:], source)
        except (ValueError, sources.SourceReadError) as exc:
            return str(exc)
    if command in {"remove", "uninstall"}:
        return ("🧹 Removal is a local operator step, not a chat command, so nothing was removed or changed.\n\n"
                "On the machine that runs Hermes, `tools/tingle_remove.py --plan` shows exactly what would be removed and changes nothing. "
                "Applying it asks for typed confirmations there. It does not restart the gateway. Steps and what is kept: `docs/uninstall.md`.")
    if command in {"help", "?"}:
        return ("🧝 *Hermes Tingle* - your follow-through helper\n\n"
                "Every day:\n"
                "• `/tingle` - what needs you now\n"
                "• `/tingle done HT-ABC123` · `/tingle snooze HT-ABC123 2d` · `/tingle dismiss HT-ABC123`\n"
                "• `/tingle undo merge CM-ABCDEF12` - undo a merge in your DM\n\n"
                "• `/tingle skip HT-ABC123 2` - skip one goal step\n"
                "• `/tingle ack <receipt>` - confirm a reminder batch you received\n"
                "• `/tingle timing HT-ID` - private timing details; `/tingle debug` - opt-in local counts\n"
                "• `/tingle setup` - review and schedule automation\n"
                "• `/tingle evaluation status|enable|disable` - default-off private discovery consent, owner DM only\n\n"
                "Approval requests for future actions:\n"
                "• `/tingle approvals` lists pending requests; `/tingle approve AR-1A2B3C` or `/tingle reject AR-1A2B3C` decides one\n\n"
                + _optional_line(True) + "\n"
                "• `/tingle sources` - source grants\n"
                "• `/tingle lookback` - owner-only one-off email review (full disclosure: `/tingle lookback help`)")
    if command == "debug":
        refusal = _source_sender_error(source, noun="Private diagnostic access")
        if refusal is not None:
            return refusal
        try:
            from . import hermes_tingle_debug as debug
        except ImportError:
            import hermes_tingle_debug as debug
        mode = parts[1].casefold() if len(parts) == 2 else "status" if len(parts) == 1 else "invalid"
        if mode == "spend":
            if (str(getattr(source, 'chat_type', '') or '').casefold() not in ('dm', 'private')
                    or not getattr(source, 'chat_id', None)):
                return "Private spend accounting requires a verified owner direct message; nothing shown."
            try:
                from .hermes_tingle_discovery_assess import format_spend
            except ImportError:
                from hermes_tingle_discovery_assess import format_spend
            return format_spend()
        if mode == "on":
            debug.set_enabled(True)
            return ("Private debug is on for this profile. Closed-set counts only, at most 200 records; "
                    "records expire from reads after 7 days and are deleted on the next debug write/purge. "
                    "No new source/model calls or jobs. Local operators can read them; shared topics cannot. "
                    "Off deletes retained records: `/tingle debug off`.")
        if mode == "off":
            debug.set_enabled(False)
            return "Private debug is off; retained diagnostic records deleted. Backups are not erased."
        if mode == "purge":
            debug.purge()
            return "Retained diagnostic records deleted; debug mode unchanged. Backups are not erased."
        if mode == "status":
            return "Private debug is " + ("on" if debug.enabled() else "off") + ". Read: `/tingle debug show`; off/purge deletes records."
        if mode == "show":
            rows = debug.read()
            if not rows:
                return "No unexpired diagnostic records. This is not proof that a run succeeded."
            return "Private observed counters, not model-quality proof:\n" + "\n".join(
                r['event'] + ": " + ", ".join(k + "=" + str(v) for k, v in r['counts'].items()) for r in rows)
        return "Usage: /tingle debug on|off|status|show|purge|spend (owner DM only)."
    if command == "timing" and len(parts) == 2:
        refusal = _source_sender_error(source, noun="Timing details")
        if refusal is not None:
            return refusal
        item = store.get_item(parts[1])
        if not item:
            return "That item is unavailable."
        if item.get('completion_signal') != 'source_email_checkpoint':
            return "This item does not use contextual email timing."
        if item.get('next_check_at'):
            return "Thread check: " + _format_local_time(item['next_check_at'])
        return ("No reminder scheduled. Contextual timing is unknown or paused; "
                "no date was invented. Review this item with `/tingle replan " + item['id'] + "`.")
    if command == "list":
        if len(parts) > 2 or (len(parts) == 2 and (not parts[1].isdigit() or int(parts[1]) < 1)):
            return "Usage: /tingle list [page number]"
        page = int(parts[1]) if len(parts) == 2 else 1
        items = store.list_items(status="unresolved", limit=21, offset=(page - 1) * 20)
        more = len(items) > 20
        items = items[:20]
        if not items:
            if page == 1:
                return (f"📌 *Hermes Tingle* · v{VERSION}\n\nAll clear - nothing needs you right now.\n\n"
                        "Tell Hermes one real item to track, with its due date or next check. "
                        "This profile's items are shared profile context, not private per sender.\n\n"
                        + _optional_line() + "`/tingle sources` · `/tingle lookback` (owner-only email review).")
            return "Nothing on this page - `/tingle list` returns to the first."
        lines = [f"📌 *Hermes Tingle* · v{VERSION} — Unresolved commitments"]
        if page == 1:
            overview = _active_overview()
            if overview:
                lines.extend(overview)
        status_styles = {
            "open": ("🔵", "Open"),
            "waiting": ("⏳", "Waiting"),
            "blocked": ("🚧", "Blocked"),
            "missed": ("🕳", "Missed window"),
        }
        for item in items:
            icon, label = status_styles.get(str(item.get("status", "")).casefold(), ("📌", "Active"))
            if item.get("watch_ref"):
                staleness = store.watch_staleness(item)
                if staleness["state"] == "stale":
                    note = (f" ⚠️ stale - last report {int(staleness['age_hours'])}h ago, "
                            f"expected within {staleness['max_age_hours']}h")
                elif staleness["state"] == "no_reports":
                    note = " ⚠️ no reports yet"
                else:
                    note = ""
                state = item.get("watch_state") or "(no state reported)"
                watch_line = f"   🔭 {item['watch_ref']}: {state}{note}"
            else:
                watch_line = ""
            try:
                from .hermes_tingle_topic import safe_icon
            except ImportError:
                from hermes_tingle_topic import safe_icon
            lines.extend(("", f"{safe_icon(item.get('topic_emoji'))} *{item['title']}*", f"`{item['id']}` · {label}"))
            if item.get('case_merge'):
                lines.append('🧩 One case · details: `/tingle case '+item['id']+'`')
            if item.get('completion_signal') == 'source_email_checkpoint':
                if item.get('next_check_at'):
                    lines.append(f"🔎 Check: {_format_local_time(item['next_check_at'])}")
            if item.get("due_at"):
                lines.append(f"📅 Due: {_format_local_time(item['due_at'])}")
            if item.get("recur_interval"):
                count, unit = item["recur_interval"], item.get("recur_unit") or "day"
                label = f"every {unit}" if count == 1 else f"every {count} {unit}s"
                lines.append(f"🔁 Repeats {label}")
            if watch_line:
                lines.append(watch_line)
            steps = item.get('steps', [])
            next_step = next((step for step in steps if not (step['complete'] or step.get('skipped'))), None)
            if next_step:
                lines.append(f"➡️ Next: {next_step['title']} — {next_step['why']}")
            elif steps:
                lines.append("➡️ Steps complete; verify the goal before closing.")
            documents = item.get("documents", [])
            if documents:
                submitted = sum(1 for d in documents if d["state"] in ("submitted", "accepted"))
                expired = sum(1 for d in documents if d["state"] == "expired")
                suffix = f" - {expired} expired, re-collect" if expired else ""
                lines.append(f"📄 Documents: {submitted}/{len(documents)} submitted{suffix}")
            options = item.get("options", [])
            if options:
                shortlisted = sum(1 for o in options if o["state"] == "shortlisted")
                proposed = sum(1 for o in options if o["state"] == "proposed")
                unmet = sum(1 for r in item.get("requirements", []) if r["must"] and not r["met"])
                suffix = f" - {unmet} must-have(s) unmet" if unmet else ""
                lines.append(f"🛒 Options: {shortlisted} shortlisted, {proposed} proposed of {len(options)}{suffix}")
            bookings = item.get("bookings", [])
            if bookings:
                active = [b for b in bookings if b["state"] != "cancelled"]
                confirmed = sum(1 for b in active if b["state"] in ("booked", "confirmed"))
                upcoming = sorted(b["cancel_by"] for b in active if b.get("cancel_by"))
                suffix = f" - next cancel-by {_format_local_time(upcoming[0])}" if upcoming else ""
                lines.append(f"🧳 Bookings: {confirmed}/{len(active)} booked or confirmed{suffix}")
        if more:
            lines.append(f"\nMore: page {page + 1} — `/tingle list {page + 1}`")
        return "\n".join(lines)
    if command == "sources":
        rows = store.list_source_optins()
        what = {
            "calendar": "Google Calendar - upcoming event details, organizer and attendee count included on enable (never recordings/transcripts)",
            "chat": "Hermes chat history - chats this profile has seen are included by default once chat is on; exclude any with `/tingle chat exclude`; days here caps how far back it looks",
            "document": "Google Drive - removed as a Tingle source; an old grant shown here can be cleared with `/tingle source disable document`",
            "email": "Gmail - recent mail within the window",
        }
        lines = ["🔎 *Hermes Tingle* — Source opt-ins", "Today Tingle's email and calendar sources use Google services.", ""]
        for row in rows:
            state = "on" if row["enabled"] else "off"
            detail = f" - history capped at {row['lookback_days']} days" if row.get("lookback_days") else ""
            if row['kind'] == 'calendar' and row['enabled']:
                detail += ' - organizer/count context ' + ('on' if row.get('calendar_context') else 'off (existing scope preserved)')
            lines.append(f"`{row['kind']}`: {state}{detail}")
            lines.append(f"  {what.get(row['kind'], '')}".rstrip())
        lines.append("")
        lines.append(sources.source_privacy_notice())
        lines.append("")
        lines.append("Change with `/tingle source enable <kind> [days]` or `/tingle source disable <kind>` in a DM. "
                     "The optional days (1-30) limits how far back a source looks, on every scan; leave it out for up to 30 days. "
                     "Calendar only looks ahead.")
        return "\n".join(lines)
    if command == "source" and len(parts) == 3 and parts[1] == "review-again" and parts[2] == "chat":
        refusal = _source_sender_error(source)
        if refusal is not None:
            return refusal
        count = store.purge_source_retained(parts[2])
        return f"Reset {count} local review markers for {parts[2]}; consent and tracked items unchanged. The next authorized review can rediscover current matching leads. Prior markers never proved delivery; unavailable or changed source content cannot be reconstructed."
    if command == "source" and parts[1:] == ["gap-reviewed", "email"]:
        refusal = _source_sender_error(source)
        if refusal is not None:
            return refusal
        try:
            store.acknowledge_email_coverage_gap()
        except ValueError as exc:
            return str(exc)
        return "Recorded your confirmation that you reviewed the historical email gap yourself. This is not provider evidence and does not widen lookback. Future gaps will be reported separately."
    if command == "source" and len(parts) <= 2 and (len(parts) == 1 or parts[1].casefold() in {"enable", "disable"}):
        what = parts[1].casefold() if len(parts) == 2 else "enable"
        return ("🔎 Which source?\n\n"
                f"`/tingle source {what} email` · `calendar` · `chat`\n"
                "See what each one reads first: `/tingle sources`.\n"
                "Optional days (1-30) limits how far back it looks, for example `/tingle source enable email 14`.")
    if command == "source" and len(parts) >= 3 and parts[1].casefold() in {"enable", "disable"}:
        refusal = _source_sender_error(source)
        if refusal is not None:
            return refusal
        sub = parts[1].casefold()
        kind = parts[2]
        if kind == 'meeting':
            if sub == 'disable':
                store.disable_calendar_context(note=_approval_evidence('source disable meeting', source))
                return 'Organizer/count context turned off within Calendar. The existing event permission is unchanged.'
            return 'Meeting context is part of Calendar now. Use `/tingle source enable calendar` to enable it, including organizer/count context. Nothing changed.'
        lookback = None
        if sub == "enable" and len(parts) >= 4:
            try:
                lookback = int(parts[3])
            except ValueError:
                return "⚠️ Days should be a number from 1 to 30, for example `/tingle source enable email 14`. Nothing changed."
        if sub == "enable" and kind not in store.SOURCE_KINDS or (lookback is not None and not 1 <= lookback <= 30):
            if kind not in store.SOURCE_KINDS:
                return f"⚠️ I don't know a source called `{kind}`. Choose one of: " + ", ".join(f"`{k}`" for k in sorted(store.SOURCE_KINDS)) + ". Nothing changed."
            return "⚠️ Days should be a number from 1 to 30, for example `/tingle source enable email 14`. Nothing changed."
        evidence = _approval_evidence(f"source {sub} {kind}", source)
        try:
            if sub == "enable":
                if kind not in store.SOURCE_KINDS or (lookback is not None and not 1 <= lookback <= 30):
                    raise ValueError("Choose a supported source and lookback days 1-30")
                import hermes_constants
                before_home = Path(hermes_constants.get_hermes_home()).resolve()
                before = load_settings()
                prior_grant = next((r for r in store.list_source_optins() if r["kind"] == kind), None)
                generation = int(prior_grant["generation"]) if prior_grant else -1
                calendar_context = True if kind == 'calendar' else None

                @store.serialized("sources")
                def probe_and_enable():
                    current_grant = next((r for r in store.list_source_optins() if r["kind"] == kind), None)
                    current_generation = int(current_grant["generation"]) if current_grant else -1
                    if (Path(hermes_constants.get_hermes_home()).resolve() != before_home or load_settings() != before
                            or _source_sender_error(source) is not None or current_generation != generation):
                        raise ValueError("Policy or consent changed while waiting; no grant changed and no probe performed")
                    account_key = sources.probe_source(kind)
                    if (Path(hermes_constants.get_hermes_home()).resolve() != before_home or load_settings() != before
                            or _source_sender_error(source) is not None):
                        raise ValueError("Profile policy changed during the probe; no grant was written")
                    return store.enable_source(kind, lookback_days=lookback, enabled_by=evidence,
                                               expected_generation=generation, account_key=account_key if kind in ("email", "calendar") else None,
                                               calendar_context=calendar_context)
                row = probe_and_enable()
            else:
                row = store.disable_source(kind, note=evidence)
        except (ValueError, sources.SourceReadError) as exc:
            return str(exc)
        _record_command_interaction()
        if sub == "enable":
            cap = f" with history capped at {row['lookback_days']} days" if row.get("lookback_days") else ""
            return (f"Source `{row['kind']}` opted in{cap}. Recorded under your authenticated command; "
                    "data retained under any earlier scope was cleared. "
                    "Review or change grants any time with `/tingle sources`.")
        return f"Source `{row['kind']}` opted out. Collection stops and its retained data was purged."
    if command == "approvals":
        refusal = _source_sender_error(source, noun="Approval listings")
        if refusal is not None:
            return refusal
        pending = store.list_approvals("pending", limit=20, approver=_deciding_principal(source))
        if not pending:
            return "No pending approval requests - nothing is waiting for your decision."
        lines = ["🔐 *Hermes Tingle* — Pending approval records",
                 f"{len(pending)} waiting for your decision. Approve/reject only records the decision; nothing is executed or sent."]
        for req in pending:
            lines.extend(("", f"`{req['id']}` · {req['action_type']}", f"Target: {req['target']}",
                           f"Parameters: `{_approval_parameters_display(req['parameters_json'])}`",
                           f"Expires: {_format_local_time(req['expires_at'])}"))
        lines.append("\nDecide with `/tingle approve <ID>` or `/tingle reject <ID>`.")
        return "\n".join(lines)
    if command in {"approve", "reject"}:
        if len(parts) != 2 or not store.APPROVAL_ID_RE.fullmatch(parts[1].upper()):
            if len(parts) == 2:
                return f"⚠️ That doesn't look like an approval ID; they look like `AR-1A2B3C`. See `/tingle approvals`. Nothing changed."
            return f"Which request? `/tingle {command} AR-1A2B3C` (see `/tingle approvals`). Nothing changed."
        refusal = _approval_sender_error(source)
        if refusal is not None:
            return refusal
        decision = "approved" if command == "approve" else "rejected"
        try:
            req = store.decide_approval(parts[1], decision, evidence=_approval_evidence(command, source),
                                        decided_by=_deciding_principal(source))
        except KeyError:
            return f"No approval request `{parts[1].upper()}`."
        except ValueError as exc:
            return f"Couldn't {command} that request: {exc}"
        _record_command_interaction()
        if decision == "approved":
            return (f"Approved `{req['id']}`: {req['action_type']} on {req['target']}. "
                    "Recorded only - no external action is executed or sent; this is one-shot and binds the exact parameters shown.")
        return f"Rejected `{req['id']}`: {req['action_type']} on {req['target']}."
    if command == "expiry":
        refusal = _source_sender_error(source, noun="Expiry suggestion changes")
        if refusal is not None:
            return refusal
        try:
            from . import hermes_tingle_expiry as expiry
        except ImportError:
            import hermes_tingle_expiry as expiry
        if len(parts) == 3 and parts[1] == 'cancel':
            changed = expiry.cancel(parts[2].upper())
            return 'Extra expiry suggestion cancelled; ordinary reminders unchanged.' if changed else 'No pending extra expiry suggestion.'
        if len(parts) == 5 and parts[1] == 'at':
            # Owner gives both exact instant with offset and the unverified source date.
            proposal = {'tier': 'explicit', 'date': parts[4], 'fire_at': parts[3], 'category': 'other'}
            made = expiry.create_nudge(parts[2].upper(), proposal, explicit_time=parts[3])
            return ('Extra one-off recorded with 45-minute eligibility grace; source date unverified, not a delivery guarantee. '
                    'Private derived timing metadata lasts at most 45 days or until emission/drop/completion; '
                    'source text is not retained. Ordinary reminders unchanged.' if made else
                    'No extra one-off created: check the active item, explicit offset, future instant within 45 days, '
                    'source date and active item state. No ordinary reminder changed.')
        return 'Usage: /tingle expiry cancel HT-ID or /tingle expiry at HT-ID ISO-instant-with-offset YYYY-MM-DD-source-date'
    if command == 'rules' and parts[1:] == ['reset', 'learned']:
        refusal = _source_sender_error(source, noun='Expiry preference reset')
        if refusal is not None:
            return refusal
        try:
            from . import hermes_tingle_expiry_preferences as preferences
        except ImportError:
            import hermes_tingle_expiry_preferences as preferences
        try:
            count = preferences.reset_learned(decided_by=_deciding_principal(source),
                                              evidence=_approval_evidence('rules reset learned', source))
            return (f'Removed {count} learned expiry rules and reset decline streaks. '
                    'Your preferences file and owner-command rules were not touched.')
        except ValueError as exc:
            return str(exc)
    if command == "rule" and len(parts) == 5 and parts[1:3] == ['add', 'expiry']:
        refusal = _source_sender_error(source, noun='Expiry preference changes')
        if refusal is not None:
            return refusal
        try:
            from . import hermes_tingle_expiry_preferences as preferences
        except ImportError:
            import hermes_tingle_expiry_preferences as preferences
        try:
            row = preferences.explicit_rule(parts[3], parts[4], decided_by=_deciding_principal(source),
                                            evidence=_approval_evidence('rule add expiry', source))
            note = ''
            if preferences.file_rule_for(parts[3]):
                note = (' Your own preferences file already has a rule for this category and still wins; '
                        'edit the file to change it. This command rule is recorded but overridden.')
            return (f'Saved owner-command expiry rule `{row["id"]}` ({parts[4]} {parts[3]}). '
                    'It never writes your preferences file; your file rules always outrank it. '
                    'Ordinary items and reminders unchanged.' + note)
        except ValueError as exc:
            return str(exc)
    if command == "rules":
        pending = store.list_feedback_candidates(status="pending", limit=20)
        approved = store.list_feedback_candidates(status="approved", limit=20)
        lines = ["🧠 *Hermes Tingle* — Preference rules"]
        if approved:
            lines.append("\nApproved (trusted preferences):")
            for row in approved:
                lines.append(f"`{row['id']}` · {row['rule_text']}" + (' (expiry provenance log only; applied file rules below)' if row['source_ref'].startswith('expiry-preference:') else ''))
        if pending:
            lines.append("\nPending your decision:")
            for row in pending:
                lines.append(f"`{row['id']}` · {row['rule_text']}" + (' (expiry provenance log only; applied file rules below)' if row['source_ref'].startswith('expiry-preference:') else ''))
            lines.append("\nDecide with `/tingle rule approve <ID>` or `/tingle rule dismiss <ID>` in a DM.")
        if not approved and not pending:
            lines.append("\nNo preference rules yet.")
        try:
            from . import hermes_tingle_expiry_preferences as preferences
        except ImportError:
            import hermes_tingle_expiry_preferences as preferences
        try:
            rules = preferences.effective_rules()
            if rules:
                lines.append("\nApplied expiry-only rules (your file outranks owner commands, which outrank learned):")
                labels = {'file': 'your file', 'explicit': 'owner command', 'learned': 'learned, approved'}
                lines.extend(f"`{r['id']}` · {labels[r['tier']]} · {r['category']}: {r['action']}" for r in rules)
                lines.append("The plugin never writes plugin-data/hermes-tingle/hermes-tingle-preferences.json - it is yours. "
                             "/tingle rules reset learned removes only learned rules.")
        except ValueError as exc:
            lines.append(str(exc))
        return "\n".join(lines)
    if command == "rule" and len(parts) >= 2 and parts[1].casefold() == "add":
        # The owner's own words are both the rule and the approval, so it takes effect at once.
        refusal = _source_sender_error(source, noun="Preference rule decisions")
        if refusal is not None:
            return refusal
        text = " ".join(parts[2:]).strip()
        if not text:
            return "Usage: /tingle rule add <what to track or never track, in your words>"
        if len(text) > store.TRIAGE_RULE_CHARS:
            return f"Keep the rule under {store.TRIAGE_RULE_CHARS} characters. Nothing was saved."
        if len(store.approved_triage_rules()) >= store.TRIAGE_RULES_MAX:
            return (f"{store.TRIAGE_RULES_MAX} preference rules are already active, the most a scan applies. "
                    "Nothing was saved; remove one with `/tingle rule dismiss <ID>` (see `/tingle rules`).")
        try:
            row = store.capture_feedback_candidate(text, category="triage", source_ref="owner command")
            row = store.resolve_feedback_candidate(
                row["id"], "approved", decided_by=_deciding_principal(source),
                evidence=_approval_evidence("rule add " + row["id"], source))
        except ValueError as exc:
            return f"Couldn't save that rule: {exc}"
        _record_command_interaction()
        try:
            from . import hermes_tingle_assess as _assess
        except ImportError:  # pragma: no cover - top-level pytest import
            import hermes_tingle_assess as _assess
        terms = _assess.skip_terms([row["rule_text"]])
        if terms:
            how = ("Picks whose subject, quote or action contain " + ", ".join(f"`{t}`" for t in terms)
                   + " are always left out, and the count is shown. The rest of the rule is applied by your "
                   "configured Hermes model, which sees it with the excerpts.")
        else:
            how = ("Your configured Hermes model sees it with the excerpts and applies it by judgment, so a "
                   "rare miss is possible. For an exact skip, add words like `[skip: voucher, coupon]` to the rule.")
        return (f"Saved `{row['id']}` as your standing preference: {row['rule_text']}\n{how} "
                "See all with `/tingle rules`; remove one with `/tingle rule dismiss " + row["id"] + "`.")
    if command == "rule" and len(parts) == 3 and parts[1].casefold() in {"approve", "dismiss"}:
        sub = parts[1].casefold()
        feedback_id = parts[2].upper()
        if not store._FEEDBACK_ID_RE.fullmatch(feedback_id):
            return f"Usage: /tingle rule {sub} FB-1A2B3C"
        refusal = _source_sender_error(source, noun="Preference rule decisions")
        if refusal is not None:
            return refusal
        try:
            from . import hermes_tingle_expiry_preferences as preferences
        except ImportError:
            import hermes_tingle_expiry_preferences as preferences
        try:
            row = store.resolve_feedback_candidate(
                feedback_id, "approved" if sub == "approve" else "dismissed",
                decided_by=_deciding_principal(source),
                evidence=_approval_evidence(f"rule {sub} {feedback_id}", source))
        except KeyError:
            return f"No feedback candidate `{feedback_id}`."
        except ValueError as exc:
            return f"Couldn't {sub} that rule: {exc}"
        _record_command_interaction()
        if sub == "approve":
            return (f"Approved `{row['id']}` as a trusted preference: {row['rule_text']} "
                    "It now guides this plugin only; revoke any time with `/tingle rule dismiss`.")
        return f"Dismissed `{row['id']}`: {row['rule_text']}"
    if command == "chats":
        known = store.list_known_chats()
        exclusions = {row["chat_ref"] for row in store.list_chat_exclusions()}
        legacy = {row["chat_ref"] for row in store.list_chat_consents()}
        lines = ["💬 *Hermes Tingle* — Chat history"]
        if not store.source_enabled("chat"):
            lines.append("\nChat history is off. Fresh installs turn it on in first-run setup; `/tingle source enable chat` turns it on here.")
        in_scope = sorted(({row['chat_ref'] for row in known} | legacy) - exclusions)
        if in_scope:
            lines.append("\nKnown chats not excluded:")
            for ref in in_scope:
                lines.append(f"`{ref}`")
        else:
            lines.append("\nNo chats are known yet. A chat becomes known once this profile sees it.")
        if exclusions:
            lines.append("\nExcluded:")
            for ref in sorted(exclusions):
                lines.append(f"`{ref}`")
        lines.append("\nExclude a chat with `/tingle chat exclude <platform:chat-id>` and bring it back with `/tingle chat include <platform:chat-id>` in a DM. "
                     "When enabled, bounded recent history checks tracked-item updates and local commitment cues in owner DMs. Separately consented model evaluation may send bounded excerpts to the configured model. Only explicit owner-DM acceptance creates a shared item; Tingle does not message other people from chat history.")
        return "\n".join(lines)
    if command == "chat" and len(parts) == 3 and parts[1].casefold() in {"exclude", "include"}:
        refusal = _source_sender_error(source, noun="Chat exclusion changes")
        if refusal is not None:
            return refusal
        sub = parts[1].casefold()
        if sub == "exclude":
            try:
                row = store.exclude_chat(parts[2], excluded_by=_approval_evidence(f"chat exclude {parts[2]}", source))
            except ValueError as exc:
                return str(exc)
            _record_command_interaction()
            return (f"Chat `{row['chat_ref']}` excluded. The collector never reads it and its retained chat leads were purged. "
                    "Bring it back any time with `/tingle chat include`.")
        try:
            row = store.remove_chat_exclusion(parts[2])
        except KeyError:
            return f"Chat `{parts[2]}` is not excluded. Nothing changed."
        _record_command_interaction()
        return f"Chat `{row['chat_ref']}` is included again by default."
    if command in {"chatconsent", "chatconsents"}:
        return ("Per-chat grants are gone: once chat history is on, chats this profile has seen are included by default. "
                "See `/tingle chats`; exclude one with `/tingle chat exclude <platform:chat-id>`. Nothing changed.")
    if command == "delivery":
        refusal = _source_sender_error(source, noun="Reminder delivery decisions")
        if refusal is not None:
            return refusal
        if len(parts) > 2 or (len(parts) == 2 and not re.fullmatch(r"B[0-9]+", parts[1], re.I)):
            return "Usage: /tingle delivery [B<last batch number>] (shows batches after that ID)."
        after = int(parts[1][1:]) if len(parts) == 2 else 0
        rows = store.pending_alert_emissions(after_id=after, limit=31)
        if not rows:
            return "No unconfirmed reminder batches."
        lines = [f"Unconfirmed reminder batches after B{after} (not proof of receipt; {'more than 30' if len(rows) > 30 else len(rows)} remaining):"]
        for row in rows[:30]:
            state = "emitted, unconfirmed" if row["emitted_to_gateway_at"] else "prepared, delivery unknown"
            if row["budget_reserved"] == 2:
                state = ("batch confirmed; " if row["acked_at"] else "") + "legacy cycle budget unknown; automatic attempts blocked until an explicit owner new cycle (snooze or schedule update); retry/receipt cannot reset budget"
            lines.append(f"B{row['id']}: {state}; items {', '.join(json.loads(row['item_ids']))}")
        if len(rows) > 30:
            lines.append(f"More batches: `/tingle delivery B{rows[29]['id']}` (stable ID cursor; refresh without a cursor for earlier batches).")
        lines.append("No acknowledgment required. Automatic repeats reserve at most four attempts per cycle, including unknown delivery; some attempts may never arrive and later attempts may duplicate.\n"
                     "Historical batches remain visible after edits, retry, completion and new cycles. Optional receipts confirm only their exact original batch, not a later revision.\n"
                     "`/tingle retry B<id>` uses the remaining budget, never resets it or overrides snooze. No email or third-party contact.")
        return "\n".join(lines)
    if command == "retry" and len(parts) == 2:
        refusal = _source_sender_error(source, noun="Reminder recovery")
        if refusal is not None:
            return refusal
        if not re.fullmatch(r"B[1-9][0-9]*", parts[1], re.I):
            return "Usage: /tingle retry B<batch number> (may repeat an already received reminder)."
        try:
            changed = store.release_alert_emission(int(parts[1][1:]))
        except ValueError as exc:
            return str(exc)
        return "Batch released for retry; a fresh check is eligible only for its unchanged revision within the remaining cycle budget and snooze. Prior delivery remains unknown and stays in recovery." if changed else "No pending batch with that ID."
    if command == "ack":
        refusal = _source_sender_error(source, noun="Reminder delivery decisions")
        if refusal is not None:
            return refusal
        if len(parts) != 2:
            return "Use the exact `/tingle ack <receipt>` command from the reminder you received. A generic acknowledgment cannot confirm unseen batches. Use `/tingle delivery` for recovery."
        try:
            count = store.acknowledge_alert_emissions(parts[1])
        except Exception:
            return "Couldn't record the acknowledgement; the reminder log is unavailable."
        _record_command_interaction()
        return "Acknowledged that reminder batch; receipt recorded once." if count else "No eligible unconfirmed batch matches that receipt. Nothing changed."
    if command == "skip" and len(parts) == 3:
        if not _ID_RE.fullmatch(parts[1].upper()):
            return "Hermes Tingle IDs look like `HT-1A2B3C`."
        try:
            position = int(parts[2])
        except ValueError:
            return "Usage: /tingle skip <goal ID> <step number>"
        try:
            step = store.skip_step(parts[1], position)
        except (ValueError, KeyError) as exc:
            return str(exc)
        _record_command_interaction()
        return f"Skipped step {position} of `{parts[1].upper()}`: {step['title']}. The goal stays open until its other steps are done."
    if command in {"done", "complete"} and len(parts) == 2:
        if not _ID_RE.fullmatch(parts[1].upper()):
            return "Hermes Tingle IDs look like `HT-1A2B3C`."
        try:
            item = store.complete_item(parts[1])
        except (ValueError, KeyError) as exc:
            return str(exc)
        _record_command_interaction()
        _react_on_command_message(source, "✅")
        _react_on_origin(item, "✅", skip_message_id=getattr(source, "message_id", None))
        return f"Marked `{item['id']}` done: {item['title']} - one less thing on the hook."
    if command == "dismiss" and len(parts) == 2:
        if not _ID_RE.fullmatch(parts[1].upper()):
            return "Hermes Tingle IDs look like `HT-1A2B3C`."
        try:
            item = store.dismiss_item(parts[1])
        except (ValueError, KeyError) as exc:
            return str(exc)
        _record_command_interaction()
        return f"Dismissed `{item['id']}`: {item['title']} - it won't nudge you again."
    if command == "snooze" and len(parts) == 3:
        if not _ID_RE.fullmatch(parts[1].upper()):
            return "Hermes Tingle IDs look like `HT-1A2B3C`."
        try:
            item = store.snooze_item(parts[1], until=_parse_snooze(parts[2]))
        except (ValueError, KeyError) as exc:
            return str(exc)
        _record_command_interaction()
        _react_on_command_message(source, "😴")
        return f"Snoozed `{item['id']}` until {_format_local_time(item['snoozed_until'])}: {item['title']} - it'll be back on your list then."
    hints = {
        "done": "Which item is finished? `/tingle done HT-1A2B3C`",
        "snooze": "Which item, and for how long? `/tingle snooze HT-1A2B3C 2d`",
        "dismiss": "Which item should stop reminding you? `/tingle dismiss HT-1A2B3C`",
        "skip": "Which goal step? `/tingle skip HT-1A2B3C 2`",
        "rule": "Say what to track or ignore with `/tingle rule add <your words>`; approve a suggested one by ID with `/tingle rule approve FB-1A2B3C`; remove with `/tingle rule dismiss <ID>` (see `/tingle rules`)",
        "chat": "Chats are included by default: `/tingle chat exclude <platform:chat-id>` or `include <platform:chat-id>`. `/tingle chats` shows what is in scope",
    }
    if command in hints:
        return f"⚠️ {hints[command]}. Nothing changed."
    if _ID_RE.fullmatch(command.upper()):
        return f"That's an item ID. Pair it with what you want to do: `/tingle done {parts[0].upper()}`, `snooze {parts[0].upper()} 2d` or `dismiss {parts[0].upper()}`."
    return "I didn't catch that command - `/tingle help` shows the everyday ones."
