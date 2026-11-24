<p align="center">
  <img src="docs/images/tingle-banner-1200x600.png" alt="Tingle - the follow-through layer for Hermes. Remembers commitments. Nudges at the right time.">
</p>

# Hermes Tingle

A Hermes plugin that tracks your commitments and follow-ups and reminds you at the right time, over your normal messaging gateway.

Requires a Hermes runtime supported by the plugin manifest and Python 3.11–3.14. Linux is the tested platform; see the [installation requirements](docs/installation.md).

| | |
| --- | --- |
| Plugin ID | `hermes-tingle` |
| Model tool | `hermes_tingle` |
| Chat command | `/tingle` |
| Data | SQLite in the profile's `plugin-data/hermes-tingle` directory; survives `/new`, session resets and gateway restarts |

## What it does

- Captures requested ideas, tasks and user-owned commitments from conversation, email and calendar, including high-level ones - and asks before assuming an unclear commitment. A schedule-like capture without a clear date stays undated rather than getting a guessed one.
- Reminds you on a schedule you configure, through any Hermes messaging gateway (Telegram is the best-polished path). Reminders stop after four reserved attempts in a cycle - snoozing, an explicit schedule update or a recurrence starts a fresh cycle - and the item stays tracked. No DM acknowledgment is required. Each attempt advances the stored cadence even if delivery is uncertain. A failed attempt may never arrive and a later attempt may duplicate; prepared output and stdout flush are not confirmed delivery.
- Tracks multi-step goals, recurring obligations, cancellation deadlines, paperwork with validity windows, shopping comparisons, trip bookings and waits on other people's replies.
- Works quietly: routine recoverable errors stay out of your chat, persistent problems get one plain-language notice per 24 hours, and technical detail is opt-in.
- Shopping options can be proposed for your review; Tingle never buys or books anything itself. It does not message external counterparties or agents.

## How it works

1. **Capture.** Tell Tingle what to track. It adds requested ideas, tasks and commitments, including high-level ones. If a commitment is unclear it asks first, and a schedule-like capture without a clear date stays undated.
2. **Track.** Items live in your profile's SQLite database with their steps, deadlines and recurrence.
3. **Review.** Tingle checks on the schedules you configure and reviews enabled sources within their configured scope.
4. **Remind.** Nudges arrive in your chat, up to four reserved attempts per cycle. Snoozing, an explicit schedule update or a recurrence starts a fresh cycle. The item stays tracked.

Tingle does not send messages to external counterparties; reminders and reports use your configured destination.

- **Item approvals only record your decision.** They are tied to one exact action and expire; Tingle does not perform that action.
- **The separate Start Tingle setup approval applies defaults, checks sources and creates the two jobs.** Successful checks enable those sources; no test message is sent.
- Revoking a source clears what that source retained (review markers, and for email also staged message IDs and sync cursors).
- Your tracked items and Hermes session history are not touched, and Tingle never buys or books anything.

## Quick start

1. Install [Hermes Agent](https://hermes-agent.nousresearch.com/docs/getting-started/installation) and configure a messaging gateway, for example [Telegram](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/telegram).
2. Install the plugin in the same profile (see the [Hermes plugin guide](https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins)):

   ```sh
   hermes -p <profile> plugins install https://github.com/marcodallagatta/hermes-tingle.git
   ```

   For the default profile, omit `-p <profile>`. This installs the latest published revision and enables it; pass `--ref <full-commit>` for a reproducible pin, as described in [installation](docs/installation.md). Enabling alone schedules no jobs, reads no sources and grants no scopes. Only use `--no-enable` when you deliberately want to inspect an unreviewed candidate before enabling it; install-then-enable is not the normal path. If the `hermes_tingle` model tool is missing from a platform, see [installation](docs/installation.md#if-the-model-cannot-use-tingle-tools) for the explicit opt-in.
3. In the owner DM, send **`/tingle`**. A single approval screen replaces the setup questionnaire; `/tingle setup` is still available if you prefer it. Tingle defaults reminders to this DM, uses the profile's Hermes `timezone`, checks for follow-ups at 00:00, 08:00 and 16:00, checks due reminders every 30 minutes, uses 09:00 for date-only deadlines, and limits today's calendar suggestions to 09:00–18:00. Exclusions start empty. The screen explains that Gmail is used for follow-ups, Google Calendar is read-only but may include organizer/attendee context, and chats already seen by this profile are in scope, including groups; relevant source text and tracked details may reach your configured AI provider and Hermes history. Google Drive is not included. The optional extra AI pass for private-chat suggestions is off. It would send additional chat excerpts to your configured model (possibly cloud/billed), so it needs separate consent. Choose **Start Tingle** to save the defaults, run minimal connection checks and create the two jobs, or **Not now** to change nothing. No email or event details are read during setup and no test message is sent. If the choice panel is unavailable, send **`/tingle setup confirm WRITE`**; invalid replies leave the approval at the same step. Fresh Telegram profiles derive the owner from the configured home channel; older or broken profiles still need [operator repair](docs/onboarding.md#operator-provisioning).
4. For source reviews, set up [Google Workspace](https://hermes-agent.nousresearch.com/docs/user-guide/skills/google-workspace) in the same profile. After the approval, failed source checks stay off. Enable or disable sources individually with `/tingle source enable <kind>`, `/tingle source disable <kind>`, or `/tingle sources`. Chat history covers chats this profile has seen; exclude individual chats with `/tingle chat exclude`. Google Drive is not a Tingle source.

For beta setup details, see [beta setup](docs/beta-setup.md). The repository code is MIT licensed; see [license scope](LICENSE-SCOPE.md) and the [no-warranty disclaimer](DISCLAIMER.md).

## Sources and first run

Fresh setup proposes Gmail, read-only Google Calendar and chats in this profile. The owner approves them together once; no account or message content is read before approval. On approval, Tingle checks each connection. Anything that cannot be verified stays off. Later, change source access individually with `/tingle source enable <kind>` or `/tingle source disable <kind>`.

- **Gmail** — recent mail and sent requests, used to find follow-ups and replies.
- **Google Calendar** — upcoming events, used for scheduling context and availability. It is read-only and may include organizer/attendee context; Tingle never creates or changes events.
- **Chat** — chats this profile has already seen, used to find commitments and follow-ups. Individual chats can be excluded with `/tingle chat exclude`.

Relevant source text and tracked details may reach the configured AI provider and Hermes history. The optional extra AI pass for private-chat suggestions is off; it would send additional excerpts to the configured model (possibly cloud/billed), so it needs separate consent. Google Drive is not used. Disabling a source stops future scans but does not undo prior checks, reads or provider/history copies. Source content is untrusted data; Tingle never sends email or takes external action from it.

After fresh setup, Tingle offers a one-off review of the last 30 days of Gmail. Choose model picks or scan-only; it then walks possible leads in the private DM. Nothing is added until each item is confirmed. If a decision times out, resume with `/tingle lookback review LB-ID`; if you skip the offer, start later with `/tingle lookback start 30`. An existing configured profile that is still empty retains its earlier one-time onboarding import prompt; `/tingle undo onboarding` applies only to that import.

## Configuration

`hermes-tingle.json` lives in the active profile directory: `~/.hermes` for the default profile, `~/.hermes/profiles/<profile>` for a named one (`$HERMES_HOME` wins when set). Keep this file private - it can identify your accounts. Missing or invalid configuration fails closed.

| Key | Required | Default | Meaning |
| --- | --- | --- | --- |
| `timezone` | yes | - | IANA timezone, e.g. `Europe/Berlin` |
| `date_only_time` | yes | - | Local `HH:MM` used for date-only deadlines and snoozes |
| `availability_start` / `availability_end` | yes | - | Local `HH:MM` bounds for suggested free windows (same day) |
| `deadline_poll_minutes` | yes | - | Deadline-check cadence, 1-1440 |
| `daily_schedule` | yes | - | Hermes Cron schedule string for the daily review |
| `delivery` | yes | - | Cron destination: `telegram:<chat-id>[:<topic-id>]`, `<platform>:<target>`, or a bare platform's home channel |
| `owner_principals` | no | `[]` | `["<platform>:<numeric-user-id>"]` allowed to make global decisions in DM; without it those commands refuse (fail-closed) |
| `calendar_exclude_substrings` | yes | - | Calendars whose events are never scanned as candidates |
| `workstream_exclude_values` | yes | - | Workstreams the tracker skips entirely |
| `source_exclude_substrings` | yes | - | Source identifiers/URLs to exclude |
| `ticket_exclude_patterns` | yes | - | Regexes for ticket IDs to exclude |
| `calendar_workstream_aliases` | no | - | Map of calendar-name substrings to an excluded workstream |
| `verbose_diagnostics` | no | `false` | `true` adds per-cycle technical detail to review output |

Synthetic example - replace every value with your own:

```json
{
  "timezone": "Etc/UTC",
  "date_only_time": "09:00",
  "availability_start": "09:00",
  "availability_end": "18:00",
  "deadline_poll_minutes": 30,
  "daily_schedule": "0 0,8,16 * * *",
  "delivery": "telegram:123456789:42",
  "owner_principals": ["telegram:123456789"],
  "calendar_exclude_substrings": ["Example excluded calendar"],
  "workstream_exclude_values": ["Example excluded workstream"],
  "source_exclude_substrings": ["tracker.example.invalid"],
  "ticket_exclude_patterns": ["\\bTASK-\\d+\\b"]
}
```

Date-only inputs use the configured time and zone; ambiguous or nonexistent DST times are rejected. Changing the config does not update existing scheduled jobs or stored items - coordinate that migration separately.

## Commands

Run in a DM with the bot. Commands marked *owner* additionally require your ID in `owner_principals`. Items are shared profile context, not private per sender. This does not add visitor access or isolation.

| Command | What it does |
| --- | --- |
| `/tingle` or `/tingle list [page]` | List active items, ordered by due/next-check time |
| `/tingle done HT-1A2B3C` | Complete an item |
| `/tingle snooze HT-1A2B3C 2d` | Snooze an item |
| `/tingle dismiss HT-1A2B3C` | Dismiss an item |
| `/tingle skip HT-1A2B3C 2` | Skip step 2 of a goal |
| `/tingle ack <receipt>` | Confirm only the batch identified by the receipt in a reminder you received |
| `/tingle delivery [B<last ID>]` | Page through unconfirmed batches without exposing receipts |
| `/tingle retry B<id>` | Explicitly retry an uncertain batch; may duplicate an arrived reminder |
| `/tingle lookback` | *owner* - optional private two-pass email review, explicit accept only; no ongoing scans |
| `/tingle identity` | Display authenticated sender and chat IDs only; never enroll an owner |
| `/tingle setup` | *owner verified DM* - fresh profile: one approval for the default source checks/grants and both jobs; private-chat filter stays off. Existing valid policy previews job activation only |
| `/tingle evaluation status` | *owner verified DM only* - report enabled/disabled policy consent, not current route/price support |
| `/tingle evaluation enable` | *owner verified DM only* - review disclosure and explicitly select enable; policy-only, no scan/model call or source/job change |
| `/tingle evaluation disable` | *owner verified DM only* - remove evaluator consent; already-dispatched requests may finish but no new ones start |
| `/tingle sources` | List source kinds and their consent state |
| `/tingle source enable <kind> [days]` | *owner* - opt in a source (`email`, `calendar`, `chat`) if it was turned off. Fresh installs propose email, calendar and chat on; Google Drive is not a source. Optional `days` (1-30) caps how far back every scan reads; omit it for up to 30 days. Calendar reads only upcoming events; new Calendar opt-ins include organizer/count fields by default (never recordings/transcripts) |
| `/tingle source review-again <chat>` | *owner* - requeue current matching chat leads after a lost report; markers do not prove delivery |
| `/tingle source disable <kind>` | *owner* - revoke a source opt-in: stops future scans and clears its stored review markers |
| `/tingle source gap-reviewed email` | *owner* - record that you personally reviewed a reported historical email coverage gap; an owner assertion, not provider proof, and it never widens the lookback |
| `/tingle approvals` | *configured owner verified private DM only* - list pending approvals for this exact owner conversation |
| `/tingle approve AR-1A2B3C` / `/tingle reject AR-1A2B3C` | *configured owner verified private DM only* - decide a pending approval bound to this platform, user ID and chat ID |
| `/tingle rules` | List feedback rule candidates |
| `/tingle rule add <your words>` | *owner* - save a standing preference about what to track or ignore; takes effect at once |
| `/tingle rule approve FB-1A2B3C` / `dismiss` | *owner* - promote or drop a learned behavior rule |
| `/tingle discover [candidate-id]` | *owner DM only* - preview one private chat-discovery suggestion; explicit accept creates a shared item, decline adds nothing |
| `/tingle chats` | List chats in scope by default and any exclusions |
| `/tingle chat exclude <platform:chat-id>` | *owner* - keep one chat out of review (retained chat leads for it are purged) |
| `/tingle chat include <platform:chat-id>` | *owner* - bring an excluded chat back into default scope |

## One-off email look-back

The one-time automatic import offer requires an empty profile. Separately, configured owners can use `/tingle lookback` in DM. Choose up to 30 days, review two-pass email leads privately and add only individually confirmed items. No ongoing source access or settings change is implied; accepted items become shared profile context. [Exact consent, scope, retention and review steps](docs/email-lookback.md).

## Sources and consent

Tingle reads Gmail and Google Calendar (through your profile's Google Workspace skill), plus Hermes's own chat history. Google Drive is not a source. Support for other providers may be added later. `meeting` is not a separate service: it adds organizer and attendee-count detail to Google Calendar meeting events, and never reads recordings or transcripts.

Fresh profiles see one plain-language **Start Tingle / Not now** approval in the verified owner DM. Start checks the proposed email, Calendar and chat sources, enables each one that passes, and creates both jobs; Not now leaves policy, grants and jobs unchanged. At the end of fresh setup, Tingle offers a one-off 30-day Gmail review. The optional extra AI pass for private-chat suggestions remains off; it would send additional private-chat excerpts to the configured model (possibly cloud/billed), so it requires separate consent. Existing profiles are never re-asked or silently migrated; explicit recorded grants are preserved, and the old `migration:install-default` email/calendar designation was not a grant and is disabled with its caches cleared. Before Start, no account check runs. After approval, Gmail and Calendar run a minimal identity check before their grant is saved: Gmail profile address or Calendar primary identity (hashed for account binding). Chat checks the retained-history interface without reading messages. Only the hashed identity and grant generation are retained; success does not prove all collection scopes or full coverage. Failure preserves the prior grant and gives connection guidance. The Google helper may refresh its existing local OAuth token; Tingle never prints it. A bounded email grant uses date-filtered provider queries on every scan instead of unfiltered history deltas. Whole-thread checks outside that date scope are refused; they may need a wider, separately approved grant.

Source collection, regrants, revocation and retained-data writes serialize across threads and processes. Revocation waits for an in-flight source operation, then disables and clears its caches atomically, including unreadable IDs and failure markers. If that operation is still busy after 30 seconds, revocation fails visibly and must be retried; it must not be described as complete. Reads that already began cannot be recalled, nor can content already given to model/session history be erased by source revocation. Your tracked items and Hermes session history are not deleted.

| Source | What Tingle reads | Bounds |
| --- | --- | --- |
| `email` | Gmail metadata via the profile's Google Workspace skill; selectively fetched bodies treated as untrusted data | Incremental cursor, 14-day bootstrap, 100 candidates per run; standing bodies have no dedicated cache. Optional one-off look-back stores bounded body text in separate private review staging for up to 7 days, with access refusal at expiry and cleanup on the next look-back operation |
| `calendar` | Event metadata for commitment scans and free/busy for availability | Next 7 days for availability; read-only, never creates or moves events |
| `chat` | Recent history of chats this profile has seen (admitted inbound traffic or the owner's own commands), minus the exclusion list | Current segment of the most recent live session, capped and disclosed |
| Calendar meeting context | When the Calendar grant includes context, the provider fetch can include attendee identities and organizer details; normalized records use organizer identity and attendee count alongside event metadata | No recordings/transcripts; existing basic-only grants stay basic-only |

For a clear unanswered sent request, Tingle can track a `waiting` item on the exact email thread, with per-message timing, and stop nudging after four tries.

Tingle also retires stale active items automatically: dated items close 7 days after their due date once three reminder attempts have been reserved; open-ended waits close after 270 days without meaningful activity; other undated items after 90 days; and no item stays active beyond 365 days from creation. Real edits and structured progress reset inactivity, routine reminders do not. A newly changed source gets one review before soft retirement; the one-year cap still applies. `auto_closed` items leave active follow-ups but remain in history, distinct from work marked done. Existing items get these dates on the next normal daily review; no manual scan is needed.

## Permissions and safety

- **Item approvals are record-only in this beta.** Each request binds one action and its exact parameters and expires; approve/reject records your choice but does not perform the action. Start Tingle is a separate setup approval: it applies defaults, runs source checks and creates the two jobs. Any future executor for item approvals needs separate implementation and review.
- **Global authority fails closed.** Chat job activation, source opt-ins, rule approvals and approval listings/decisions require `owner_principals` and a verified private DM. Approvals are scoped to the exact platform, user ID and chat ID.
- **Quiet by default.** Internal errors stay out of chat (`verbose_diagnostics: true` opts in); persistent degradation gets one deduplicated plain-language notice per 24 hours.
- **Corrections become rules only with your approval.** A correction first fixes the current item; it becomes a reusable rule candidate you approve or dismiss with `/tingle rule`.

The optional private-chat suggestion filter is **off by default** on new and existing profiles; it is an extra AI pass for untracked suggestions, not the ordinary enabled-source review. It can send additional private-chat excerpts to this profile's configured model (possibly cloud/billed), so enabling it requires separate consent. In a verified owner DM, `/tingle evaluation enable` offers a €5 monthly spending cap (UTC month), no monthly spending cap, or cancel after showing the disclosure. No-cap mode can allow provider billing to exceed €5; estimates are not provider bills. Enabling the filter does not grant chat access or change the model/provider default.

Discovery considers at most 20 bounded excerpts (500 characters each) from authenticated owner DMs in the existing traversal, after title-token updates reserve capacity. A tool-free evaluator uses the installed profile's own configured default model/provider at runtime; it never uses a maintainer-selected model or credential. At most five validated keeps become minimized suggestions. The evaluator makes one bounded request, with no retries or fallback. Unsupported, unpriced or expired route metadata disables evaluation but preserves the conservative local-cue path; a configured evaluation failure stages nothing and never falls back. Only an authenticated owner-DM `/tingle discover` acceptance creates a shared item. The private discovery lane is stripped from shared review input.

Owner-DM `/tingle debug spend` shows content-free per-call estimates, usage totals and reserved spend. Estimates use the admitted route's verified pricing and may differ from provider billing; missing usage remains unknown rather than fabricated zero. Source revocation does not erase already-held provider or history copies.

Unchanged chat leads are suppressed only after a successful, version-bound review acknowledgment, never just because a snapshot was emitted.

## Privacy and data

Tingle stores data; it is not a "no data collected" plugin. Local storage means the machine running your Hermes profile, which may be a server rather than your personal device.

- **On the Hermes host:** the profile's SQLite database retains tracked item text, deadlines, linked records, preferences, consent/review markers, approval and delivery history. Older installations may also retain contacts, message drafts and captured replies from removed features; upgrading does not erase them. Private configuration, repair backups, helper scripts and scheduled job definitions may also remain on that host. Items are shared profile context, not private per sender.
- **No built-in Tingle service:** the plugin code contains no Tingle-operated telemetry, analytics or content-upload service. This does not describe the behavior of Hermes itself, installed helpers, model providers or messaging services.
- **Models and history:** existing title-token chat-update excerpts, authorized source metadata and selectively fetched email bodies can be returned to Hermes as tool results or daily-review input. They may go to the profile's configured model provider, such as OpenAI if you chose it, and remain in Hermes session/cron history or logs. The private chat-discovery lane strips all discovery data before shared review-model input. Separately consented bounded excerpts may go to a verified tool-free evaluator using this installed profile's own runtime default model/provider. Unknown or unsupported route pricing at initial admission leaves evaluation unconfigured and preserves conservative local-cue discovery; once an evaluation attempt is admitted and then fails, it stages nothing and never falls back to local cues. The v1 capped consent admits only the exact registered `openai-api` direct Chat Completions contract; the v2 no-cap consent admits that same supported direct OpenAI route without a Tingle cap and additionally admits reviewed models on the built-in direct `nous` Chat Completions route, and only after a fresh exact-endpoint public catalog attestation that matches complete Hermes pricing metadata, with no unknown or nonzero request fee. Admitted routes use Hermes-maintained pricing with the documented conservative buffer; reports are estimates, not invoices. Catalog checks send neither credentials nor excerpts. An evaluator failure never falls back. No maintainer route or credential is used. Only minimized survivor labels/locators and content-free evaluation/cost markers are retained locally, never evaluation spans, rationale or responses. Cost reservations survive revocation/failure until bounded lazy expiry. Standing email bodies are not saved as a dedicated standing-source cache. An explicitly authorized one-off look-back can stage bounded bodies privately for up to 7 days before review; accept/skip/revoke removes candidate content, and expiry is enforced on access with later cleanup. Both can enter Hermes command/session history as disclosed. A separate content-free account/message-hash duplicate-read ledger expires after 14 days while authorized and is cleared for its scope on revocation. The separate deadline-check job is configured without an agent/model run.
- **Connected services:** Google source checks and reads use the profile's Google Workspace helpers and existing OAuth connection; a helper may refresh its local OAuth token. Reminders leave through the configured gateway and destination. Daily reviews can inspect relevant public links through Hermes web tools, making requests to the configured web service and visited sites.
- **Your choices:** review the model, hosting, gateway, destination, connected accounts, source scope and exclusions before activation or source opt-in. The local wizard creates policy without reading accounts; an explicit Google source-enable command performs a minimal account probe before recording the grant; chat enable checks the local history interface without reading messages. Choose services and scopes suitable for the content you authorize. This disclosure does not replace those services' own terms or privacy controls.
- **Revocation is not erasure:** a successful source revoke stops future scans and clears source-specific retained state and the shared email duplicate-read audit, not existing tracked items, Hermes history, backups or copies already supplied to outside services. The operator-local remover deletes Tingle-owned data/config/helpers by default; `--keep-data` is explicit. It leaves the plugin install and manager record intact; after data cleanup, use `hermes -p <profile-name> plugins remove hermes-tingle`. Bare native manager removal before Tingle cleanup has no Tingle cleanup hook on the pinned runtime and can leave data/jobs. See [clean removal and limits](docs/uninstall.md).

Source text is treated as untrusted data, never as instructions.

## Honest limits

- Independent reviews covered the code candidates; local gates on this tree are recorded in the [changelog](CHANGELOG.md). Live evaluator inference has not been exercised, so provider quality and billing remain unverified. This beta is published from a clean-history export.
- Chat history covers only the current segment of the most recent live session; ended sessions and compaction-archived rows are not readable through the public API.
- The plugin cannot send, reply to or delete email; write calendar events; or write Linear.
- Availability checks are best-effort: interrupted checks and ongoing churn can delay coverage.

## Icon

The mascot in `docs/images/` is Hermes-inspired woodland artwork, with a winged helmet, caduceus staff and winged sandals. The README header banner (`tingle-banner-1200x600.png`, 2:1) is composed from the 1024px full-body icon; the 256px/64px full-body and centered 256px head are alternate exports, not wired into runtime behavior.

## Shared status and release checks

Public pre-1.0 beta: installation does not imply catalog approval or live deployment. See the [documentation index](docs/README.md), [installation](docs/installation.md), [diagnostics](docs/diagnostics.md), [uninstall and retained data](docs/uninstall.md), [contributing](CONTRIBUTING.md) and [security reporting](SECURITY.md).

Existing `OL-` references remain accepted as aliases after migration to displayed `HT-` IDs. The mapping is durable and applies to commands, tool actions and linked records; no clean start is needed. Keep a consistent database backup before any upgrade.


Unconfirmed reminder diagnostics can run without a newly due item, once per batch after a six-hour grace; `/tingle delivery` remains available afterward. The note uses the same possibly broken channel: stdout flush is emission, not a delivery receipt. Disclosure-only polls do not create their own outbox batches or consume item notice counts.

## Development

Supported runtime: Python >=3.11,<3.15, matching the tested Hermes revisions. CI uses Python 3.12 for standalone tests and each Hermes revision's declared environment for compatibility runs. Older Python is not a supported deployment target.

```sh
python3 -m pytest tests/ -q          # synthetic suite
python3 tools/check_privacy.py       # privacy gate: no secrets or personal patterns
```

Both GitHub workflows run on pushes to `main` and pull requests, and remain available by manual dispatch. Cloud results are separate from local tests.

## Rollback

Use `tools/tingle_remove.py` locally with an explicit `-p <profile-name>` for inventory, confirmation and Tingle-owned data/config/helper cleanup. It requires a clean Tingle Cron inventory: stop writers, remove exact listed IDs through Hermes and rerun `--plan` before either cleanup mode. `--keep-data` retains Tingle data/config/backups but not helpers; it does not affect Cron jobs. The Tingle remover leaves the plugin install/manager record untouched and prints the next command: `hermes -p <profile-name> plugins remove hermes-tingle`. See [removal](docs/uninstall.md) for the full sequence, provenance/adoption, recovery and history/provider limits.
