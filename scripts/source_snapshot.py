"""Pre-run collection and isolated private discovery evaluation, then sanitized output."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from datetime import datetime, timezone
from typing import Any


def _plugin_root() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "plugins" / "hermes-tingle"


def build_agent_input(snapshot: dict[str, Any], *, profile_timezone: str | None = None) -> str:
    fenced = dict(snapshot)
    fenced.pop("review_context_id", None)
    chat_review = fenced.get("chat_review")
    if isinstance(chat_review, dict):
        chat_review = dict(chat_review)
        chat_review.pop("discovery", None)
        fenced["chat_review"] = chat_review
    actionable = any(fenced.get(key) for key in (
        "due_items", "emails", "tracked_email_context", "email_completion_signals", "calendar_events", "errors",
    ))
    review = fenced.get("chat_review")
    if isinstance(review, dict) and review.get("matches"):
        actionable = True
    if not actionable:
        return '{"wakeAgent": false}'
    approved_rules = fenced.get("approved_feedback_rules", [])
    fenced.pop("approved_feedback_rules", None)
    # Diagnostics and local discovery are operator detail, never agent input.
    fenced.pop("diagnostics", None)
    fenced["untrusted_source_data"] = True
    payload: dict[str, Any] = {"source_snapshot": fenced}
    try:
        from hermes_tingle_case_merge import INSTRUCTIONS as case_instructions
        payload['case_merge_guidance']=case_instructions
    except ImportError:
        pass  # Frozen isolated helper tests may not expose the installed plugin root.
    if profile_timezone is not None:
        payload["profile_timezone"] = profile_timezone
    if approved_rules:
        payload["user_approved_feedback_rules"] = approved_rules
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def gate_metrics(snapshot: dict[str, Any], output: str) -> dict[str, Any]:
    """Content-free measured bytes/counts, never claimed model tokens or savings."""
    keys=("due_items","emails","tracked_email_context","email_completion_signals","calendar_events","errors")
    counts={key:len(snapshot.get(key) or []) for key in keys}
    for key in ('chat_review',):
        review=snapshot.get(key) or {}
        counts[key]=len(review.get('matches') or [])
        counts['chat_discovery']=len((review.get('discovery') or {}).get('matches') or [])
    return {'wake_agent':output!='{"wakeAgent": false}',
            'reasons':[key for key,value in counts.items() if value],
            'counts':counts,'payload_utf8_bytes':len(output.encode('utf-8')),
            'measurement':'script output bytes/counts, not tokens or provider usage'}


def main(*, metrics: bool = False) -> int:
    root = _plugin_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from hermes_tingle_sources import collect_snapshot
    from hermes_tingle_discovery import stage_local_candidates
    from hermes_tingle_wiring import wire_from_runtime
    from hermes_tingle_config import local_timezone

    wire_from_runtime()
    try:
        profile_timezone = local_timezone().key
    except ValueError:
        # Invalid profile settings block source reads; still emit the bounded
        # configuration error rather than failing before the collector reports it.
        profile_timezone = None
    snapshot = collect_snapshot(now=datetime.now(timezone.utc), discovery_enabled=True)
    try:
        from hermes_tingle_discovery_assess import evaluate
        evaluate(snapshot)
        stage_local_candidates(snapshot)
    except Exception:
        snapshot.setdefault("diagnostics", []).append(
            "Chat discovery staging failed; no candidate details were sent to the shared review.")
    for line in snapshot.get("diagnostics", []):
        print(f"hermes-tingle diagnostics: {line}", file=sys.stderr)
    output=build_agent_input(snapshot, profile_timezone=profile_timezone)
    if metrics:
        print('hermes-tingle gate: '+json.dumps(gate_metrics(snapshot,output),sort_keys=True),file=sys.stderr)
    print(output)
    return 0


if __name__ == "__main__":
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--metrics',action='store_true',help='Print content-free gate counts and UTF-8 bytes to stderr')
    raise SystemExit(main(metrics=parser.parse_args().metrics))
