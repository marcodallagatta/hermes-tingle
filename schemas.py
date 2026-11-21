"""Model-facing schemas for the Hermes Tingle plugin."""

HERMES_TINGLE = {
    "name": "hermes_tingle",
    "description": (
        "Manage the user's Hermes Tingle commitments across chats and /new. "
        "Use this when the user makes a clear commitment, asks for a reminder, "
        "or updates a tracked item. Auto-add only clear commitments; ask before "
        "adding ambiguous inferences. Discovery is extracted locally from bounded owner-DM history, staged privately, and never included in shared review-model input; only explicit owner approval in a verified DM via /tingle discover can create a discovery item. This tool cannot stage or approve discovery candidates. Respect configured work exclusions. "
        "For a multi-step goal, use add_step with goal item_id, title and why in order; link an existing reply-wait by wait_item_id rather than duplicating its source. "
        "Use complete_step for an ordinary step and complete for a linked reply-wait; neither closes the goal. Verify and complete the goal separately once all steps are done. "
        "Use list offset and has_more to page through all items. "
        "Recognize same-case/same-current-outcome items from available context, not shared keywords/provider/title alone. "
        "Distinct real obligations stay separate. Use propose_case_merge with existing item_ids, canonical_id and reason; "
        "it changes no items. Proactively ask the owner in plain language whether to review a possible duplicate case "
        "privately; never put item titles, IDs, reasons or the review command into shared-topic output. A natural-language yes "
        "calls open_case_merge_review, which opens the authenticated paginated preview only in a verified owner DM and "
        "fails closed elsewhere. If the interactive chooser is unavailable on that turn, the call changes nothing, keeps "
        "the proposal pending and names the typed /tingle merge command instead - never say the preview was shown when it "
        "was not. The merge "
        "is confirmed or declined inside the authenticated paginated preview, never by the yes. A clear natural-language no "
        "is recorded with decline_case_merge so the group is not proposed again; an ambiguous answer is not a decline. "
        "Only that paginated preview and confirmation "
        "can merge, preserving all source records and distinct historical steps with undo. Never dismiss/complete/update "
        "siblings to simulate a merge. Same topic alone is insufficient. Recognition is bounded to supplied context. "
        "Use mark_notified only for due items in the final reminder. Copy expected_versions (item ID to reminder_revision) from the exact snapshot reviewed; missing or stale revisions are refused. It reserves one of at most four attempts per cycle before output and advances the stored cadence, never proving emission or delivery. No DM acknowledgment required; include the receipt command as optional only. Unknown delivery can duplicate on a later scheduled attempt. Honor snooze/edits and reverify no_reply each time; /tingle delivery shows recovery state. "
        "Recent Gmail metadata uses a durable history cursor with bounded bootstrap/reconciliation scans; group new messages by thread before judging unanswered requests. "
        "For a clear sent request with no later reply, create a waiting email item with owner=other, status=waiting, completion_signal=source_email_reply_received, and source_ref=gmail:<verified_account_hash>/<thread_id>#<sent_message_id>. "
        "For broader incoming processing updates use source_email_checkpoint plus review_plan, not a fabricated sent-request anchor. Fresh email_checkpoint_checks receipt is required before reserving a chase. Interpret changed context, never auto-complete the goal. Set next_check_at from that email's urgency, expected turnaround, and any explicit reply timing; never use one fixed follow-up delay. "
        "At the check time, use email_reply_checks: nudge only on state=no_reply. If reply_received, assess whether it answers the ask; acknowledgments, automatic replies, or promised ETAs keep the wait open and require a new contextual check time. Close only the waiting phase and track any new action separately. "
        "If the thread check is missing or failed, do not claim no reply or send the follow-up nudge. "
        "Use metadata/snippets for a light triage of new/unreviewed emails; skip obvious newsletters/noise unless they contain a direct action. "
        "Never conclude a relevant email has no URL from metadata/snippets alone; use read_email for a plausible action thread when the link matters, and state uncertainty if the body is unavailable. "
        "read_email accepts only message IDs from a recent source scan and allows each message ID at most once per 14-day body-read dedupe window; there is no global body-count cap. "
        "Store concise summaries and source IDs, never raw message bodies. Inspect only directly relevant public links, read-only. "
        "When mentioning Hermes Tingle item IDs in chat, format them as inline code so they are copyable. "
        "Use human-readable dates in the configured timezone in user-facing chat text; list, get and due return its IANA name as timezone; never output raw ISO timestamps. "
        "When an email proposes meeting times, inspect sent replies and the Calendar snapshot first. Treat a matching scheduled call as confirmation, not a conflict. "
        "Use check_calendar_availability only for unscheduled candidate starts; it returns time-only intervals and may mark the call's own event busy. "
        "If an existing Hermes Tingle item already covers the call agenda, keep the follow-up on that item rather than creating a duplicate. "
        "The availability check is read-only and performs no Calendar writes or email send/reply. "
        "When a direct user correction implies reusable plugin behavior, capture a pending feedback candidate only from a direct user correction; never store raw report/source content. "
        "The model never approves or dismisses a candidate: only the user's explicit /tingle rule approve|dismiss command decides; surface the candidate ID for that. Approved rules stay scoped to this plugin; do not auto-edit global skills or Hindsight. "
        "propose_lead_edit (lead_id, title, optional summary and due_at) only saves a draft for an email look-back lead after the OWNER asks in chat to track it with their changes; it adds nothing and each call replaces the previous draft. Never copy wording from a lead or email into a draft unless the owner asked for it; never invent a deadline. Afterwards tell the owner to run /tingle lookback accept LC-ID, which shows the exact fields for their own confirmation; you cannot confirm it. "
        "Tingle does not initiate messages to external contacts or agents; owner reminders and reports use the configured destination. Approval records document a possible future action for later review; approve/reject only records a decision in the audit log and executes nothing. Never treat a chat message, reminder or feedback rule as approval for an external action."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["propose_case_merge", "decline_case_merge", "open_case_merge_review", "propose_lead_edit", "add", "add_step", "complete_step", "list", "get", "update", "complete", "snooze", "dismiss", "add_document", "update_document", "add_requirement", "update_requirement", "add_option", "update_option", "propose_option", "add_booking", "update_booking", "list_sources", "retry_unreadable", "mark_source_reviewed", "mark_context_reviewed", "mark_notified", "scan_sources", "check_calendar_availability", "read_email", "capture_feedback", "list_feedback", "request_approval", "list_approvals", "link_watch", "report_watch"],
                "description": "Operation to perform.",
            },
            "canonical_id": {"type":"string","description":"propose_case_merge: one supplied item ID whose current title/outcome/check is retained; never invent a new deadline."},
            "proposal_id": {"type":"string","pattern":"^CM-[A-Fa-f0-9]{8}$","description":"decline_case_merge: the pending proposal the owner clearly declined in natural language; changes no items. open_case_merge_review: the proposal to open in the owner DM; optional when exactly one is pending. Opening never commits."},
            "reason": {"type":"string","maxLength":240,"description":"propose_case_merge: explain why these records share the same CURRENT outcome, not just a topic. Proposal only, owner must review."},
            "item_id": {"type": "string", "description": "Hermes Tingle item ID, e.g. HT-1A2B3C."},
            "lead_id": {"type": "string", "pattern": "^LC-[A-Fa-f0-9]+$", "description": "For propose_lead_edit: the owner's email look-back lead ID (LC-...). Only when the owner asked in their own words to track that lead with changes."},
            "action_type": {"type": "string", "description": "Record-only label for a possible future external action (for example, a reminder-relevant appointment); request_approval records intent but this plugin executes no external action."},
            "target": {"type": "string", "description": "The exact counterparty or object the action would act on, for request_approval."},
            "parameters": {"type": "object", "description": "Exact parameters of the proposed action for request_approval; concise description only, never message bodies or secrets."},
            "ttl_hours": {"type": "integer", "minimum": 1, "maximum": 168, "description": "How long the approval request stays valid (default 24 hours)."},
            "approval_status": {"type": "string", "enum": ["pending", "approved", "rejected", "expired", "consumed"], "description": "Status filter for list_approvals."},
            "step_position": {"type": "integer", "minimum": 1, "description": "Ordered step position for complete_step."},
            "why": {"type": "string", "description": "Concise reason for this step."},
            "wait_item_id": {"type": "string", "description": "Optional ID of an existing separate active reply-wait; do not duplicate its source."},
            "title": {"type": "string", "description": "Descriptive concrete action or awaited outcome, recognizable without opening the email. Never just a vague subject; do not invent an action. Keep source subject in summary."},
            "summary": {"type": "string", "description": "Concise context or next step; do not paste source text."},
            "review_plan": {"type": "object", "additionalProperties": False,
                "required": ["actor", "outcome", "basis", "review_at", "review_needed"],
                "properties": {"actor": {"type": "string", "enum": ["me", "other", "unknown"]},
                    "outcome": {"type": "string", "maxLength": 120},
                    "basis": {"type": "string", "enum": ["stated", "inferred", "unknown"]},
                    "review_at": {"type": ["string", "null"]},
                    "review_needed": {"type": "boolean"}},
                "description": "Required for new source-derived email tracking: proposed next actor/outcome and context-specific review time, never a deadline. Respect explicit owner schedules and source-relative processing estimates. Unknown/partial context: actor/basis=unknown, review_at=null, review_needed=true and ask, no generic default."},
            "expected_email_checks": {"type": "object", "additionalProperties": {"type": "string"},
                "description": "mark_notified email checkpoints: copy each receipt from the exact email_checkpoint_checks snapshot interpreted. Fresh source match required."},
            "checkpoint_next_checks": {"type": "object", "additionalProperties": {"type": ["string", "null"]},
                "description": "mark_notified email checkpoints: next contextual review time per item, or null if genuinely unknown. Never a fixed generic repeat. Null pauses further reminders and must be disclosed."},
            "expected_versions": {"type": "object", "additionalProperties": {"type": "integer"}, "description": "Required by mark_notified: exact item ID to reminder_revision from the reviewed due snapshot, never a guessed/current re-read value."},
            "item_ids": {"type": "array", "items": {"type": "string"}, "description": "IDs actually included in a reminder; used by mark_notified."},
            "source_type": {
                "type": "string",
                "enum": ["conversation", "email", "calendar", "manual"],
                "description": "Where this item originated.",
            },
            "source_ref": {"type": "string", "description": "Minimal source identifier. For a waiting Gmail reply, use gmail:<verified_account_hash>/<thread_id>#<sent_message_id> so the exact ask can be checked. Copy the verified account-qualified prefix from the source snapshot; never infer an account for legacy refs."},
            "source_url": {"type": "string", "description": "Optional direct URL to the source object."},
            "completion_signal": {"type": "string", "enum": ["", "source_email_trashed", "source_email_reply_received", "source_email_checkpoint"], "description": "Use source_email_trashed only for an explicitly agreed Trash=done rule. Use source_email_reply_received for a waiting email reply item with a sent-message anchor."},
            "workstream": {"type": "string", "description": "Pass the configured workstream from the source when known; configured exclusions are skipped."},
            "review_receipt": {"type":"string","description":"For mark_context_reviewed: exact opaque receipt from a document/chat lead. Use only after successful interpretation and required item changes; stale receipts require a fresh scan."},
            "source_id": {"type": "string", "description": "Source message/event ID for mark_source_reviewed or read_email."},
            "source_generation": {"type": "integer", "description": "Required for mark_source_reviewed: copy the exact grant epoch from the source candidate/snapshot; never infer from a new grant."},
            "source_version": {"type": "string", "description": "Source message ID or event updated timestamp; used for dedupe."},
            "owner": {"type": "string", "enum": ["me", "other", "joint", "unknown"], "description": "Who owns the next action."},
            "priority": {"type": "string", "enum": ["low", "normal", "high"], "description": "Urgency, based on evidence rather than guesswork."},
            "status": {"type": "string", "enum": ["open", "waiting", "blocked"], "description": "Current state for an update."},
            "due_at": {"type": "string", "description": "Explicit deadline in ISO-8601 with offset, or YYYY-MM-DD (configured date-only time in the configured timezone). Do not invent a deadline."},
            "next_check_at": {"type": "string", "description": "Optional next check in ISO-8601 with offset or YYYY-MM-DD. Required for a waiting email reply item; choose per-thread timing from context, not a fixed delay."},
            "recur_interval": {"type": "integer", "description": "Optional positive count for recurring obligations (bills, renewals, cancellation windows); pair with recur_unit. Requires due_at; completing the item records the occurrence and advances to the next one instead of closing. Set only for genuinely repeating obligations."},
            "recur_text": {"type": "string", "maxLength": 120, "description": "The user's own recurrence words, passed through unchanged (e.g. 'every Monday', 'every 2 weeks', 'quarterly', 'yearly'). Parsed deterministically by the plugin; never invent a mapping yourself. Conflicts with explicit recur_interval/recur_unit are refused."},
            "recur_unit": {"type": "string", "enum": ["day", "month", "year"], "description": "Recurrence unit for recur_interval: fixed days, or calendar months/years with month-end clamping (Jan 31 monthly lands Feb 28)."},
            "recur_lead_days": {"type": "integer", "description": "Optional days before each recurring deadline to start reminding; defaults to the item's initial due_at/next_check_at gap and persists across occurrences."},
            "watch_ref": {"type": "string", "maxLength": 200, "description": "For link_watch: which external monitor serves this item's goal, by convention cron:<job-name> or url:<watch>. The monitor lives outside Tingle; linking gives its reports somewhere to land."},
            "watch_max_age_hours": {"type": "integer", "minimum": 1, "maximum": 720, "description": "For link_watch: the monitor's expected reporting cadence in hours. Reports older than this are flagged stale in reviews, so a silent monitor stops looking current. Omit when the cadence is unknown; the report age is then surfaced without a verdict."},
            "state": {"type": "string", "maxLength": 500, "description": "For report_watch: the monitor's latest finding as a short summary (e.g. 'price now 189, was 229'). Never paste raw output."},
            "hard_deadline": {"type": "boolean", "description": "Optional: mark due_at as a cancellation window that CLOSES when it passes unmet. A missed non-recurring window becomes terminal (missed) instead of nagging forever; a missed recurring window records the miss and advances to the next occurrence. Requires due_at. Use for cancel-by dates, renewal opt-out windows and booking cutoffs."},
            "until": {"type": "string", "description": "Snooze-until ISO-8601 with offset or YYYY-MM-DD."},
            "document_name": {"type": "string", "description": "For add_document: name of the paperwork document (form, certificate, proof)."},
            "document_position": {"type": "integer", "minimum": 1, "description": "For update_document: the document's position on the item, from get/list output."},
            "document_state": {"type": "string", "enum": ["needed", "collected", "submitted", "accepted", "rejected"], "description": "For update_document: lifecycle state. 'expired' is set only by the validity sweep, never by hand."},
            "valid_until": {"type": "string", "description": "ISO date/datetime after which the document is no longer accepted (e.g. a certificate only valid when recent). Past this, the sweep marks it expired; the item stays open."},
            "requirement_text": {"type": "string", "description": "For add/update_requirement: one shopping requirement, e.g. 'fits a 15-inch laptop'."},
            "requirement_position": {"type": "integer", "minimum": 1, "description": "For update_requirement: the requirement's position on the item, from get/list output."},
            "must": {"type": "boolean", "description": "For add/update_requirement: true marks a must-have requirement; must-haves still unmet show on /tingle list."},
            "met": {"type": "boolean", "description": "For update_requirement: whether the current direction satisfies the requirement."},
            "option_name": {"type": "string", "description": "For add_option: name of a candidate purchase option."},
            "option_position": {"type": "integer", "minimum": 1, "description": "For update_option/propose_option: the option's position on the item, from get/list output."},
            "option_state": {"type": "string", "enum": ["candidate", "shortlisted", "rejected"], "description": "For update_option: comparison state. 'proposed' is set only by propose_option, which also creates the purchase approval request."},
            "price_text": {"type": "string", "description": "For add/update_option: recorded price as display text ('EUR 189 at example-shop'). Recorded by the assistant, never verified or fetched live; price watches use link_watch monitors."},
            "detail": {"type": "string", "description": "For add/update_option: short comparison detail (key specs, tradeoffs). Never paste full pages."},
            "booking_kind": {"type": "string", "enum": ["flight", "train", "lodging", "rental", "event", "other"], "description": "For add_booking: the kind of reservation."},
            "booking_name": {"type": "string", "description": "For add_booking: short name of the reservation (e.g. 'outbound flight')."},
            "booking_position": {"type": "integer", "minimum": 1, "description": "For update_booking: the booking's position on the item, from get/list output."},
            "booking_state": {"type": "string", "enum": ["researched", "booked", "confirmed", "cancelled"], "description": "For update_booking: lifecycle state. Recording a booking never books or cancels anything with a provider."},
            "cancel_by": {"type": "string", "description": "ISO date/datetime the free-cancellation window closes. Creates (or moves) a linked hard-deadline item so the alert engine guards the window; a missed window becomes a missed item, it never closes the booking."},
            "confirmation_ref": {"type": "string", "description": "For add/update_booking: the provider's confirmation reference as recorded text - stored for the user's convenience, never verified against a provider."},
            "note": {"type": "string", "description": "Optional completion note."},
            "feedback_text": {"type": "string", "maxLength": 500, "description": "Concise reusable rule distilled from a direct user correction. Never paste full report/email/source text."},
            "feedback_category": {"type": "string", "enum": ["general", "email", "calendar", "links", "triage", "formatting", "reminders", "other"], "description": "Plugin behavior area for the pending feedback candidate."},
            "feedback_id": {"type": "string", "pattern": "^FB-[A-Fa-f0-9]{6}$", "description": "Feedback candidate ID."},
            "feedback_status": {"type": "string", "enum": ["pending", "approved", "dismissed", "all"], "description": "Feedback candidate filter; defaults to pending."},
            "feedback_decision": {"type": "string", "enum": ["approved", "dismissed"], "description": "Resolve a candidate only after explicit user approval or rejection."},
            "feedback_source_ref": {"type": "string", "maxLength": 256, "description": "Provenance lead for a candidate: the item ID, conversation reference, or correction that prompted it. Never message content."},
            "feedback_evidence": {"type": "string", "maxLength": 240, "description": "How the resolve decision arrived (e.g. the user's chat reply or /tingle command)."},
            "limit": {"type": "integer", "description": "Maximum number of rows (1-100)."},
            "offset": {"type": "integer", "minimum": 0, "description": "List offset; use has_more to retrieve the next page."},
            "lookback_days": {"type": "integer", "description": "Bounded initial/reconciliation Gmail scan window (1-30 days; default 14). Routine checks use Gmail history deltas."},
            "upcoming_days": {"type": "integer", "description": "Calendar event window (1-30 days; default 7)."},
            "date": {"type": "string", "description": "Local calendar date YYYY-MM-DD in the configured timezone for check_calendar_availability; the action is limited to the next 7 days."},
            "candidate_starts": {"type": "array", "items": {"type": "string", "pattern": "^(?:[01][0-9]|2[0-3]):[0-5][0-9]$"}, "maxItems": 10, "description": "Candidate local start times HH:MM in the configured timezone; use the times explicitly offered. Ambiguous or nonexistent DST times are rejected."},
            "duration_minutes": {"type": "integer", "minimum": 5, "maximum": 180, "default": 30, "description": "Call duration for check_calendar_availability; use 30 if the thread does not specify."},
            "list_status": {"type": "string", "enum": ["active", "all", "open", "waiting", "blocked", "done", "dismissed", "missed"], "description": "List filter; defaults to active."},
        },
        "required": ["action"],
    },
}
