"""One-time hands-off onboarding: a single yes/no question, then one bounded import.

On the first `/tingle` for an empty profile, an authenticated owner in a DM is
asked once whether to import the last 30 days of Gmail and let Tingle pick the
useful items. The yes covers exactly one bounded look-back scan (existing caps
and consent machinery) plus one model pick pass; picks backed by a quote from the email,
clearly action-bearing picks are added to the shared profile, everything else
stays private, and the marker means the question never repeats. No source
grants, jobs or settings are created or changed by this flow.
"""
from __future__ import annotations

import html
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any

try:
    from . import hermes_tingle_store as store
    from . import hermes_tingle_sources as sources
    from . import hermes_tingle_lookback as lookback
except ImportError:  # pragma: no cover - exercised by the test harness
    import hermes_tingle_store as store
    import hermes_tingle_sources as sources
    import hermes_tingle_lookback as lookback

DAYS = 30
SETUP_LOOKBACK_OFFER_META = "setup_lookback_offer_v1"


def _version() -> str:
    """The running plugin version, from the single VERSION constant (lazy: the plugin imports this module)."""
    try:
        from . import hermes_tingle as plugin
    except ImportError:  # pragma: no cover - exercised by the test harness
        import hermes_tingle as plugin
    return str(plugin.VERSION)


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _stale_marker(state: dict[str, Any]) -> bool:
    """True when a stored marker records an import that never actually ran.

    Covers a failed scan, and a completed/partial marker that added nothing and
    points at a scan with no checked dates or partial coverage (a
    false all-clear). Declined, unavailable, undone and any marker that added
    items are never stale.
    """
    kind = state.get("state")
    if kind == "failed":
        return True
    if kind not in ("completed", "partial") or state.get("items") or not state.get("scan_id"):
        return False
    try:
        conn = lookback.connect()
        try:
            row = conn.execute("SELECT checked, coverage FROM scans WHERE id=?",
                               (state["scan_id"],)).fetchone()
        finally:
            conn.close()
    except Exception:
        return False
    if row is None:
        return False
    return not row["checked"] or str(row["coverage"] or "").startswith("Partial")


def due() -> bool:
    """Onboarding is due once, unless fresh setup already offered the look-back."""
    try:
        if store.get_plugin_meta(SETUP_LOOKBACK_OFFER_META):
            return False
    except Exception:
        return False
    state = store.onboarding_state()
    if state is not None and not _stale_marker(state):
        return False
    return store.profile_item_count() == 0


def offer_after_setup(ctx: Any, source: Any) -> str:
    """Offer a separate, bounded private Gmail review after fresh setup succeeds."""
    ready = "All set. Sit back and relax; Tingle will send follow-ups automatically when needed."
    retry = ready + " Start a one-off Gmail look-back later with `/tingle lookback start 30`."
    try:
        expected = lookback.binding(source)
    except Exception:
        return retry
    try:
        email_available = "email" in sources.source_capabilities()
    except Exception:
        email_available = False
    if not email_available:
        # This is not a usable consent choice without the Google bridge. Record
        # the fresh-setup path as unavailable to avoid a duplicate legacy prompt.
        try:
            store.set_plugin_meta(SETUP_LOOKBACK_OFFER_META, "unavailable")
        except Exception:
            pass
        return ("Setup is complete, but the one-off Gmail review isn't available: the Google Workspace email bridge "
                "isn't wired into this Hermes runtime, so no review was started. Ask your operator to wire it; "
                "once available, run `/tingle lookback start 30`. "
                "Tingle will send follow-ups automatically when needed.")
    try:
        # Suppress the older empty-profile onboarding prompt on the next `/tingle`;
        # this offer is the fresh-setup look-back path, with manual review instead.
        store.set_plugin_meta(SETUP_LOOKBACK_OFFER_META, "offered")
    except Exception:
        return retry
    choices = [
        {"value": "assess", "label": "Review with model"},
        {"value": "scan", "label": "Review without model"},
        {"value": "later", "label": "Not now"},
    ]
    try:
        pick = ctx.ask_choice(lookback._start_disclosure(DAYS), choices)
    except Exception:
        pick = None
    if pick == "later":
        return ready + " If you want the one-off review later, run `/tingle lookback start 30`."
    if pick not in ("assess", "scan"):
        return retry
    try:
        scan_id = lookback._run_scan(ctx, source, expected, DAYS, grant_assess=(pick == "assess"))
        result = lookback.review_after_scan(ctx, source, expected, scan_id)
    except (lookback.AssessmentCancelled, ValueError, sources.SourceReadError):
        return ("Setup is complete, but the one-off Gmail review didn't finish. "
                "Retry it with `/tingle lookback start 30`. Tingle will send follow-ups automatically when needed.")
    if result.startswith("⏸ Review paused:"):
        suffix = "Once you finish, Tingle will send follow-ups automatically when needed."
    else:
        suffix = ready
    return result if "send follow-ups automatically when needed" in result else result + "\n" + suffix


@store.serialized("onboarding")
def maybe_handle(ctx: Any, source: Any) -> str | None:
    """Run the onboarding flow when it is due and this is an owner DM.

    Returns the flow's reply, or None when onboarding is not due or this
    context must not see the prompt (non-owner, group chat): the caller then
    shows the normal reply and nothing is asked or recorded.
    """
    if not due():
        return None
    try:
        expected = lookback.binding(source)
    except Exception:
        # Onboarding must never break the normal reply; a profile-store or
        # policy read failure is handled by the caller's own degradation path.
        return None
    if "email" not in sources.source_capabilities():
        store.set_onboarding_state({"state": "unavailable", "at": _stamp(),
                                    "reason": "google-workspace-bridge-missing"})
        return (f"👋🧝 Welcome to Tingle v{_version()}. Gmail is not connected in this profile, so the one-time "
                "history import can't run. Nothing was read or changed - tell Hermes one real "
                "item to track and Tingle starts from there.")
    pick = ctx.ask_choice(
        f"👋🧝 Welcome to Tingle v{_version()}. Import likely follow-ups from the last 30 days of Gmail once?\n\n"
        "A lookback scan reads headers, snippets and selected bodies, then your configured Hermes model "
        "(possibly cloud and billed) picks likely follow-ups from short excerpts. Tingle cannot cap or see the cost; "
        "host retries can add requests. Coverage may be incomplete.\n\n"
        "Before you choose: picks enter the shared profile and may add an unverified one-off the day before a claimed expiry. "
        "You can dismiss them. Other leads stay private for 7 days or until discard; private timing metadata "
        "can stay up to 45 days. Model/gateway history and backups may keep copies. "
        "Accepted contextual review plans stay with items until closure; unknown timing needs review. No standing source settings change.\n\n"
        "It can take a while; I'll report as soon as it finishes.",
        [{"value": "yes", "label": "Yes, import once"},
         {"value": "no", "label": "No thanks"}])
    if pick != "yes":
        store.set_onboarding_state({"state": "declined", "at": _stamp()})
        return ("No import run - nothing was read and nothing changed. Tell Hermes one real "
                "item to track and Tingle starts from there.")
    try:
        scan_id = lookback._run_scan(ctx, source, expected, DAYS, grant_assess=True)
    except lookback.AssessmentCancelled as exc:
        store.set_onboarding_state({"state": "cancelled", "at": _stamp()})
        return f"{exc} Start again any time: `/tingle lookback start 30`."
    except (ValueError, sources.SourceReadError) as exc:
        store.set_onboarding_state({"state": "failed", "at": _stamp(), "error": str(exc)[:200]})
        return (f"⚠️ The onboarding import did not complete: {lookback.failure_reason(exc)}. "
                "Nothing was added. You can retry the same review any time with "
                "`/tingle lookback start 30`.")
    conn = lookback.connect()
    try:
        scan = dict(conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone())
        assessment = lookback._latest_assessment(conn, scan_id)
        total = len(lookback._groups(conn.execute("SELECT * FROM candidates WHERE scan_id=?", (scan_id,)).fetchall()))
        excluded = int(assessment['excluded']) if assessment else 0
        incomplete = (scan['checked'] == 0 or scan['coverage'].startswith('Partial')
                      or scan['assess_state'] != 'done' or assessment is None
                      or assessment['assessed'] < assessment['considered'])
    finally:
        conn.close()
    # A disclosed acquisition cap is not an operational failure. Keep the scan's
    # coverage/state evidence intact, but do not turn a successful bounded import
    # into a failure headline. Actual read/model/acceptance faults still warn.
    operational_failure = (not scan['checked'] or bool(scan['error_detail'])
                           or scan['coverage'].startswith('Partial/failed')
                           or scan['assess_state'] != 'done' or assessment is None
                           or (assessment is not None and
                               (assessment['assessed'] < assessment['considered']
                                or assessment['failed_calls'] or assessment['plan_faults'])))
    acceptance = {}
    added, considered, _skipped, uncertain = lookback.auto_accept_qualified(source, expected, scan_id, stats=acceptance)
    incomplete = incomplete or bool(acceptance.get('rejected'))
    store.set_onboarding_state({"state": "partial" if incomplete else "completed",
                                "at": _stamp(), "scan_id": scan_id,
                                "items": added, "considered": considered})
    if uncertain:
        lines_note = (f"Possible time limit (unverified): {_titles(uncertain, limit=2)}. "
                      "No extra reminder set; see timing help below.")
    if operational_failure or acceptance.get('rejected') or acceptance.get('pending_confirmation'):
        if acceptance.get('pending_confirmation'):
            reason = (f"{acceptance['pending_confirmation']} partial-history timing proposals need individual review and confirmation; "
                      f"nothing was added for those picks; use /tingle lookback review {scan_id}")
        elif acceptance.get('rejected'):
            reason = f"{acceptance['rejected']} selected follow-up(s) could not be accepted; review the pending picks with /tingle lookback results {scan_id}"
        elif assessment is not None and assessment['plan_faults']:
            reason = f"{int(assessment['plan_faults'])} model timing plans were missing or malformed; affected picks were not qualified"
        elif assessment is not None and (assessment['failed_calls'] or scan['assess_state'] == 'failed'):
            # The model step is the reason when it failed, even if the scan also hit a size cap.
            missing = max(0, assessment['considered'] - assessment['assessed'])
            reason = (f"the model review failed or timed out on {assessment['failed_calls']} "
                      f"request{'s' if assessment['failed_calls'] != 1 else ''}, so {missing} of "
                      f"{assessment['considered']} conversations were not assessed")
        elif scan['coverage'].startswith('Partial/failed'):
            reason = lookback.failure_reason(scan['error_detail'])
        elif scan['error_detail']:
            reason = "some Gmail date checks could not be verified"
        elif scan['coverage'].startswith('Partial'):
            reason = "the bounded scan reached its size limit"
        elif not scan['checked']:
            reason = "no message dates were checked"
        else:
            reason = "the model review stopped early"
        if added:
            result = (f"Partial import added {len(added)} item{'s' if len(added) != 1 else ''}. "
                      f"Coverage is incomplete ({reason}), so more may be missed.\n"
                      f"{_titles(added, limit=5, decorated=True)}\n"
                      "Review: `/tingle list`\nRetry: `/tingle lookback start 30`.")
        else:
            result = (f"⚠️ The onboarding import did not complete: {reason}. Nothing was added. "
                      "Try a fresh scan: `/tingle lookback start 30`.")
        if excluded:
            result += f"\n{excluded} matched your skip rules and were left out."
        if uncertain:
            result += '\n' + lines_note
        result += "\nTiming, review trail and undo: `/tingle lookback help`."
        return result
    if not total:
        lines = [f"✅ Onboarding import done - checked {scan['checked']} message dates from the last {DAYS} days; "
                 "no conversations were staged for review, so nothing was added."]
    else:
        finding = (f"✅ Onboarding import done - checked {total} "
                   f"conversation{'s' if total != 1 else ''} from the last {DAYS} days, ")
        if added:
            finding += (f"added {len(added)} item{'s' if len(added) != 1 else ''}")
        else:
            finding += "nothing clearly needed you, so nothing was added"
        kept_out = total - len(added)
        if kept_out:
            finding += (f"; {kept_out} other "
                        f"conversation{'s' if kept_out != 1 else ''} stayed private")
        lines = [finding + "."]
        if added:
            lines.append(_titles(added, limit=5, decorated=True))
            lines.append("Review: `/tingle list`")
        if uncertain:
            lines.append(lines_note)
    if excluded:
        lines.append(f"{excluded} matched your skip rules and were left out.")
    lines.append("Timing, review trail and undo: `/tingle lookback help`.")
    return "\n".join(lines)


def _titles(item_ids: list, *, limit: int = 8, decorated: bool = False) -> str:
    """Short human-readable titles for added items (email-derived text: flattened, no markup)."""
    names = []
    for item_id in item_ids[:limit]:
        item = store.get_item(str(item_id)) or {}
        name = str(item.get("title") or "")
        name = html.unescape(name)
        name = re.sub(r"<[^>]*>", " ", name)                          # html tags
        name = "".join(" " if unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp") else ch for ch in name)
        name = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", name)      # [text](url) -> text
        name = re.sub(r"https?://\S+|www\.\S+", "link", name)
        name = re.sub(r"[`*_~#>|\[\]<]+", "", name)                 # formatting characters
        name = " ".join(name.split())[:60]
        name = name or "untitled item"
        icon = lookback.assess.safe_icon(item.get('topic_emoji'))
        names.append((icon + ' ' if decorated else '') + name)
    more = len(item_ids) - len(names)
    separator = "\n" if decorated else ", "
    return separator.join(names) + ((f"\nPlus {more} more." if decorated else f", plus {more} more") if more > 0 else "")


def undo(source: Any) -> str:
    """Remove only the items the onboarding import added and record that the owner undid it.

    The undone marker is kept on purpose: an owner who undid the import is not asked again.
    """
    result = store.undo_onboarding(expected_origin=lookback.ONBOARDING_NOTE)
    if result is None:
        return "There is no completed onboarding import to undo. Nothing changed."
    ids = list(result.get('removed', [])) + list(result.get('preserved', []))
    removed = result['removed']
    if not ids:
        return ("The onboarding import added nothing, so there was nothing to remove. "
                "Onboarding will not offer the import again; "
                "`/tingle lookback start 30` runs a fresh review.")
    skipped = len(ids) - len(removed)
    text = (f"Removed {len(removed)} item{'s' if len(removed) != 1 else ''} added by the onboarding import; "
            "every other item is untouched. Onboarding will not offer the import again; "
            "`/tingle lookback start 30` runs a fresh review.")
    if skipped:
        text += (f" {skipped} listed item{'s' if skipped != 1 else ''} were changed or lack a safe original fingerprint "
                 "(changed since the import) and were left alone.")
    return text
