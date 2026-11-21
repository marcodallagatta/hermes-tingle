"""Script-only deadline alerts; empty stdout means no alert is sent."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
import sys
from typing import Any

# Optional receipts can confirm a precise batch. Lack of receipt does not block
# automatic reminders. After this grace, reserve one diagnostic inclusion per
# batch. That attempt may also be lost; the owner recovery view remains usable.
UNCONFIRMED_ALERT_GRACE = timedelta(hours=6)


def _format_time(value: str | None) -> str:
    if not value:
        return ""
    from hermes_tingle_config import local_timezone
    local_zone = local_timezone()
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(local_zone).strftime("%a %d %b %Y, %H:%M %Z")
    except (TypeError, ValueError):
        return "date unavailable"


def alertable_due_items(items: list[dict[str, Any]], next_step=None, get_item=None) -> list[dict[str, Any]]:
    """Select distinct due items; verify every ancestor against full sibling ordering."""
    selected = []
    seen = set()
    for item in items:
        item_id = item.get("id")
        if not isinstance(item_id, str) or not re.fullmatch(r"(?:HT|OL)-[A-F0-9]{6}", item_id):
            continue
        if item_id in seen:
            continue
        seen.add(item_id)
        if (item.get("source_type") == "email"
                and item.get("completion_signal") in {"source_email_reply_received", "source_email_checkpoint"}):
            continue
        current = item
        ancestors = {item_id}
        while current.get("parent_id"):
            parent_id = current["parent_id"]
            if parent_id in ancestors or not next_step or not get_item:
                break
            first = next_step(parent_id)
            parent = get_item(parent_id)
            if not first or first.get("id") != current.get("id") or not parent or parent.get("id") != parent_id:
                break
            ancestors.add(parent_id)
            current = parent
        if current.get("parent_id") and not item.get("due_at"):
            continue
        selected.append(item)
    return selected


def render_due_alerts(items: list[dict[str, Any]], *, next_step=None, get_item=None) -> str:
    return _render_selected(alertable_due_items(items, next_step, get_item), next_step, get_item)


def _stored_step(item: dict[str, Any]) -> str:
    if item.get("status") != "open" or item.get("owner") != "me":
        return ""
    summary, title = item.get("summary"), item.get("title")
    step = ""
    if (not summary and isinstance(title, str) and len(title) <= 100
            and all(ch.isprintable() for ch in title)
            and re.match(r"^(?:review|draft|prepare|collect|check|compare|gather)\s+\S", title, re.I)):
        step = " ".join(title.split()).replace("`", "'")
    elif (isinstance(summary, str) and summary.startswith("Next action: ")
            and 0 < len(summary[13:]) <= 100 and all(ch.isprintable() for ch in summary)):
        step = summary[13:].strip().replace("`", "'")
    # Avoid truncating a step into a different instruction; reserve markdown escape space.
    return step if len(step) + step.count("\\") <= 80 else ""


def _not_snoozed(item: dict[str, Any]) -> bool:
    value = item.get("snoozed_until")
    if not value:
        return True
    try:
        until = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return until.tzinfo is not None and until <= datetime.now(timezone.utc)
    except (AttributeError, TypeError, ValueError):
        return False


def _render_selected(items: list[dict[str, Any]], next_step=None, get_item=None) -> str:
    if not items:
        return ""
    lines = ["Hermes Tingle reminders"]
    for item in items[:10]:
        due = _format_time(item.get("due_at"))
        suffix = f" — due {due}" if due else ""
        title = " ".join(str(item.get("title") or "(untitled)").split()).replace("`", "'")
        # shortcut: long titles are truncated so 10 escaped reminders stay within conservative chat message limits.
        if len(title) > 100:
            title = title[:99] + "…"
        lines.append(f"• `{item.get('id','')}`{suffix}")
        lines.append(f"  Item: `{title}`")
        if item.get("status") in {"open", "waiting", "blocked", "missed"}:
            lines.append(f"  Status: {item['status']}")
        details = get_item(item["id"]) if get_item else None
        steps = details.get("steps") if isinstance(details, dict) and details.get("id") == item["id"] else None
        first_v2 = next((entry for entry in steps if isinstance(entry, dict) and not (entry.get("complete") or entry.get("skipped"))), None) if isinstance(steps, list) else None
        step = ""
        waiting = ""
        if first_v2 and first_v2.get("wait_item_id") and get_item:
            linked = get_item(first_v2["wait_item_id"])
            title = first_v2.get("title")
            if (isinstance(linked, dict) and linked.get("id") == first_v2["wait_item_id"]
                    and linked.get("status") == "waiting" and _not_snoozed(linked)
                    and isinstance(title, str) and title and all(ch.isprintable() for ch in title)):
                title = " ".join(title.split()).replace("`", "'")
                if len(title) + title.count("\\") <= 80:
                    waiting = title
        if first_v2 and not first_v2.get("wait_item_id") and item.get("status") == "open" and item.get("owner") == "me":
            title = first_v2.get("title")
            if isinstance(title, str) and title and all(ch.isprintable() for ch in title):
                title = " ".join(title.split()).replace("`", "'")
                if len(title) + title.count("\\") <= 80:
                    step = title
        elif not steps:
            step = _stored_step(item)
        if (not step and not steps and item.get("status") == "open" and item.get("owner") == "me"
                and not item.get("summary") and next_step and get_item):
            first = next_step(item["id"])
            child_id = first.get("id") if isinstance(first, dict) else None
            if (isinstance(child_id, str) and re.fullmatch(r"(?:HT|OL)-[A-F0-9]{6}", child_id)
                    and child_id not in {candidate.get("id") for candidate in items}):
                child = get_item(child_id)
                if (child and child.get("id") == child_id and child.get("parent_id") == item["id"]
                        and _not_snoozed(child)
                        and child.get("completion_signal") != "source_email_reply_received"):
                    step = _stored_step(child)
        if waiting:
            lines.append(f"  Waiting: `{waiting}`")
        elif step:
            lines.append(f"  Stored next step (verify): `{step}`")
        else:
            lines.append("  Specific next action not determined; see item details.")
    if len(items) > 10:
        lines.append(f"…and {len(items) - 10} more due items; use /tingle list.")
    lines.append("Reply `/tingle done <ID>` or `/tingle snooze <ID> 2d` to update one.")
    return "\n".join(lines)


def _unconfirmed_delivery_note(store, *, now: datetime) -> tuple[str, list[int]]:
    """Summarize emitted-but-unconfirmed reminder batches: counts and times only.

    Never include item IDs or titles here; the note exists because delivery of
    earlier batches is uncertain, so it must not re-expose their content.
    Older store versions without the emission log simply skip the disclosure.
    """
    unsurfaced = getattr(store, "unsurfaced_emissions", None)
    if unsurfaced is None:
        return "", []
    pending = unsurfaced(older_than=now - UNCONFIRMED_ALERT_GRACE, now=now)
    if not pending:
        return "", []
    first = _format_time(pending[0].get("emitted_at"))
    latest = _format_time(pending[-1].get("emitted_at"))
    span = f"prepared at {first}" if first == latest else f"prepared between {first} and {latest}"
    note = (f"Note: {len(pending)} earlier reminder batch(es) ({span}) have unconfirmed delivery or an unresolved cycle budget; "
            "if they never arrived, the reminder channel may not be reaching you. "
            "Automatic repeats use the stored cadence, up to four attempts per cycle; uncertain attempts may duplicate. Use `/tingle delivery` to inspect or explicitly retry within that budget. No acknowledgment is required.")
    if any(row.get("budget_reserved") == 2 for row in pending):
        note += " Some legacy cycle budgets cannot be proved; affected automatic attempts are blocked. Inspect `/tingle delivery`, then explicitly snooze or set a new schedule for a new cycle. Retry/receipt cannot reset an unknown budget."
    return note, [int(row["id"]) for row in pending]


def main() -> int:
    # Installed helpers live outside the plugin tree. Bootstrap before importing
    # any plugin module, including the outbox lock used by this entry point.
    from hermes_constants import get_hermes_home
    plugin_root = get_hermes_home() / "plugins" / "hermes-tingle"
    if str(plugin_root) not in sys.path:
        sys.path.insert(0, str(plugin_root))
    import hermes_tingle_store as store
    if not hasattr(store, "resume_automatic_reminders"):
        raise RuntimeError("deadline helper/store version mismatch; install the coordinated build before resuming jobs")
    with store.serialized_operation("reminders"):
        return _main()


def _main() -> int:
    """Query only the profile's local Hermes Tingle DB; never call a model or network."""
    from hermes_constants import get_hermes_home

    plugin_root = get_hermes_home() / "plugins" / "hermes-tingle"
    if str(plugin_root) not in sys.path:
        sys.path.insert(0, str(plugin_root))
    import hermes_tingle_store as store
    from hermes_tingle_config import load_settings
    load_settings()  # Never consume a reminder without profile configuration.
    from hermes_tingle_sources import all_due_items

    now = datetime.now(timezone.utc)
    held = store.held_reminder_item_ids()
    candidates = [item for item in all_due_items(now=now) if item["id"] not in held]
    items = alertable_due_items(candidates, getattr(store, "next_step", None), store.get_item)
    message = _render_selected(items, getattr(store, "next_step", None), store.get_item)
    catchup = store.deadline_catchup_state(now=now)
    if not catchup["complete"]:
        message += "\nHard-deadline catch-up is incomplete; earlier missed windows are being recorded in bounded batches. Later checks continue without skipping windows."
    note, surfaced_ids = _unconfirmed_delivery_note(store, now=now)
    if note:
        message = (message + "\n" if message else "") + note
    # Extra one-offs reserve in their separate log before output and never
    # consume ordinary attempt budgets. Main holds the same reminder lock.
    import hermes_tingle_expiry as expiry
    # Reserve one bounded attempt before output. Cadence advances at reservation;
    # emission and optional positive owner receipt remain separate evidence.
    item_ids = [item["id"] for item in items[:10]]
    emission_id = None
    record_emission = getattr(store, "record_alert_emission", None)
    if record_emission is not None and item_ids:
        # Disclosure-only polls must not create their own never-ending outbox.
        try:
            emission_id = record_emission(item_ids, expected_versions={item["id"]: item.get("reminder_revision") for item in items[:10]})
        except store.ReminderRevisionChanged:
            # A committed owner edit won selection/reservation. No stale output;
            # the next scheduled check selects its new revision normally.
            return 0
        batch = store.get_alert_emission(emission_id)
        message += f"\nBatch B{emission_id}: automatic repeats, up to four attempts per cycle; no acknowledgment required. Optional receipt: `/tingle ack {batch['receipt_token']}` only if received. Unknown delivery may duplicate; inspect `/tingle delivery`."
    # Reserve one diagnostic attempt before output too: a failed/unknown flush
    # must not create an unbounded disclosure retry loop outside item budgets.
    mark_surfaced = getattr(store, "mark_emissions_surfaced", None)
    if surfaced_ids and mark_surfaced is not None:
        mark_surfaced(surfaced_ids)
    try:
        extra = expiry.render_nudges(expiry.reserve_due(now=now), now=now)
    except ValueError as exc:
        extra = str(exc)  # invalid preferences block expiry only, never ordinary reminders
    if extra:
        message = (message + "\n" if message else "") + extra
    if not message:
        return 0
    try:
        sys.stdout.write(message + "\n")
        sys.stdout.flush()
    except OSError:
        return 1
    if emission_id is not None:
        mark_flushed = getattr(store, "mark_emission_flushed", None)
        if mark_flushed is not None:
            mark_flushed(emission_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
