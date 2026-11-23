# First run and local repair

Owners set up Tingle in chat, not by running a Python script. On a fresh Telegram profile the owner principal is taken from the profile's configured home channel during `/tingle setup`; older profiles, a non-DM home channel or a deliberate owner change need one operator provisioning step. Local file-write access remains trusted: anyone running as the Hermes OS user can change plugin policy. Do not share that OS account with an untrusted person.

## Before choosing accounts or reminders

Tingle retains tracker content and operational records on the Hermes profile's host, not in a private area per sender. There is no built-in Tingle-operated telemetry/content-upload service, but that is not a promise about the rest of Hermes. Authorized source content and tracked details can enter the configured model's context, Hermes history/logs and reminders sent through the chosen gateway. Relevant public-link checks use Hermes web tools. Review hosting, model, accounts, destination and source scope first. See the [data-flow disclosure](../README.md#privacy-and-data).

Draft setup does not read accounts or messages. After explicit source consent and `WRITE`, Gmail/Calendar enable performs a minimal account-identity check (Google helpers may refresh their local OAuth token); chat checks the history interface without reading messages. Failed checks leave sources off. Revocation does not erase tracked items or prior history/provider copies. The local remover deletes inventoried Tingle-owned data/config/backups by default, with explicit keep available; it leaves the plugin install/manager record intact. After manual Cron cleanup, run Tingle cleanup with an explicit profile, then run `hermes -p <profile-name> plugins remove hermes-tingle`. Bare native manager removal cannot invoke Tingle cleanup. Neither workflow erases independent history/provider copies. See [removal](uninstall.md).

## Operator provisioning

On a fresh Telegram profile no operator file is needed. `/tingle setup` in the owner DM takes the owner from `platforms.telegram.home_channel` in the profile config. The home channel must be a positive Telegram user/chat ID (a DM) and the sender must match its configured user ID; a group home channel, a different sender, a non-Telegram platform or a malformed/ambiguous config is refused without creating a draft. A stranger cannot enroll themselves by DMing the bot. Automatic bootstrap is also refused when the profile still has prior Tingle policy backups, recovery data, manifest records for helpers/jobs or other non-wizard resources, non-empty lookback/body-audit namespaces, tracked items, source grants or onboarding/journal records: that is an older install needing operator repair, not a fresh default. Admitted-chat metadata and the setup wizard's own draft record in an otherwise empty database are harmless.

The operator file below is for older profiles, a non-DM home channel, or a deliberate owner change. It remains an installer/admin action, **not an owner chat-enrollment step**. No caller, first message, button or typed reply creates an owner.

1. Resolve the intended existing profile's real home (`HERMES_HOME`, or the explicit profile directory). Do not rely on a CLI sticky profile. Provision only a fresh Tingle install with no prior policy, source consent or tracker records; missing policy on an existing install requires restoration, not new defaults.
2. Have the intended owner use `/tingle identity`; it displays gateway-authenticated sender and chat IDs without enrollment. Verify that principal through the operator's trusted relationship with the owner. A display name or chat ID alone is not ownership.
3. As the Hermes OS user, create the private directory `<profile-home>/plugin-data/hermes-tingle` and a regular, non-symlink, mode-0600 `setup-owner.json` inside it. Write the explicitly approved principal, for example this **synthetic** identity:

   ```json
   {"owner_principals": ["telegram:42"], "fresh_profile": true}
   ```

   Set the directory mode to 0700. `fresh_profile: true` explicitly attests that fresh defaults are appropriate: no prior Tingle policy/source grants/tracker data are being reconstructed. The admitted-inbound chat registry may already have created an otherwise-new database before setup; database existence alone is not proof of an old install. Do not add this attestation to recover an existing install. Do not overwrite existing policy or a prior owner file as part of owner onboarding. Changing this file is a new operator authority decision and invalidates an in-progress draft. This bootstrap file is never written by the chat flow. It resides in plugin data; review it with the operator during removal/reinstallation. Never restore it automatically to enroll a new owner.
4. Tell the owner to run `/tingle setup` in a verified DM. No terminal work is required from the owner. Unsupported/missing principal provisioning refuses with this documentation link and changes nothing.

Existing **valid** policy uses its already-configured `owner_principals` for job activation and needs no bootstrap file. Missing/invalid policy never authenticates someone from a valid-looking owner fragment. A bootstrap file cannot bypass invalid existing policy. Before committing, setup writes a private `setup-consumed.json` one-shot marker; if policy is later lost, or a write crashes after consumption, chat cannot reconstruct defaults from that bootstrap. An operator must inspect/restore the reviewed policy; do not delete the marker to retry blindly. Repair is operator-local below.

## Guided owner setup

In the authenticated owner DM, start with **`/tingle setup`**. Tingle asks one question at a time. Reply with the displayed `/tingle setup <field> <value>` command; these are typed authenticated commands, not free text in a choice panel:

1. `destination` — explicit gateway destination. Telegram uses `telegram:<chat-id>[:<topic-id>]`; sender ID is not necessarily the destination. A bare supported platform selects its configured home channel. Reply `/tingle setup here` (or `/tingle setup destination here`) to use the verified DM you are in; the shortcut is only accepted with no extra arguments and on the first field. Syntax/platform validation does not prove a target exists or delivery works.
2. `timezone` — IANA timezone, e.g. `Europe/Berlin`.
3. `review` — recurring cron; suggestion `0 0,8,16 * * *` (00:00, 08:00, 16:00 in that timezone).
4. `deadline` — script-only deadline-check cadence in minutes; suggestion 30.
5. `date-only` — local HH:MM due time for date-only dates; suggestion 09:00.
6. `availability` — same-day start and end, e.g. `09:00 18:00`.
7. `sources` — type `/tingle setup sources all` to approve Gmail, Google Calendar and chat (the proposed defaults), or `/tingle setup sources none` to keep them off. Drive is not a source. Selected checks/grants occur only after final `WRITE`, never while answering questions. After setup, enable selected sources individually with `/tingle source enable <kind> [days]`. Existing source consent is not reset or widened by first-run setup.
8. `evaluation` — reply `yes` for the recommended €5 cap, `no-cap` for explicitly no Tingle cap, or `no` for OFF; the default suggestion is **no (OFF)**, independent of the source choice. `yes` stages only the exact runtime-default private-evaluation consent described below, authenticated to this owner; `no` omits it. Source `none` may be combined with evaluator consent, but chat remains off and no excerpts can be sent.
9. `exclusions` — one JSON object with explicit `calendar_exclude_substrings`, `workstream_exclude_values`, `source_exclude_substrings` and `ticket_exclude_patterns` string lists. Empty lists exclude nothing. No reply can edit the owner list.

Answers are staged privately in `plugin-data/hermes-tingle/setup-state.json`, not runtime policy. The preview shows every policy value, the owners (defaulted from the profile home channel or operator-provisioned), source selection and data disclosure. Retype the exact destination with `/tingle setup confirm-destination <destination>`, then reply **`/tingle setup confirm WRITE`**. `write` or `Write` does not confirm. This uses the same validator and checked, atomic, no-clobber mode-0600 writer as the operator tool, and verifies the saved policy before proceeding.

`/tingle setup cancel` discards the draft without policy, source or job changes. Unsupported/out-of-order replies, a mismatched destination/confirmation, expiry (30 minutes from start), or changed profile/authority/policy fail closed. Every reply is bound to the same authenticated sender and exact DM; another owner/chat cannot continue or cancel it. Expired drafts are discarded on the next setup reply; no timeout job is created. When filesystem maintenance prevents draft cleanup, the reply says so and requires operator inspection. No raw source content is retained in setup reports.

## Optional private discovery evaluation consent

V2 consent is **explicit per-profile**, never an upgrade of v1 or inferred from a group “yes”. In a verified human owner DM, `/tingle evaluation enable` offers **€5 cap (recommended)**, **NO Tingle spending cap**, or cancel. Guided setup offers the distinct typed `no-cap` choice with destination retype and `WRITE`. No-cap saves `owner-dm-excerpts-v2` with exact integer `monthly_limit_microeur: 0`; existing v1 consent keeps its positive cap until fresh explicit re-consent. **NO monthly Tingle spending ceiling** means provider billing can exceed €5; reports are estimates, not invoices. Bounded requests/tokens, no retries/fallback, source consent and owner approval still apply. No model/provider default is changed.

V2 also admits reviewed Chat Completions models on the exact built-in direct `nous` route, only with complete Hermes provider-models API pricing and a fresh exact-endpoint public catalog attestation. Named/custom/proxy/alias routes, unsupported wires/models, missing price dimensions and unknown/nonzero request fees remain local-only. Free pricing is never inferred from a suffix: all input/output/cache rates must be verified zero. Catalog metadata is re-read before each evaluation and immediately before dispatch, never under a source lock or with credentials/excerpts; locked metadata expires after 60 seconds (core endpoint cache is 300 seconds). If rates become positive, v2 records the buffered usage estimate without a monthly block. Exact route, price and policy are pinned through dispatch.


This is **default-OFF** for new and existing profiles, separate from source consent. The setup question and full preview disclose that only bounded eligible owner-DM excerpts may reach the chosen provider in a separate tool-free request to this profile's default model/provider. Only routes with verified Hermes pricing plus the **1.5 EUR/USD** conservative buffer are evaluated. Unknown/unsupported routes keep local cues and cost nothing for this evaluator. The v1 **€5 per UTC month** ceiling (or a configured lower cap) is on buffered estimates, **not an invoice or a universal guarantee about provider charges**. The evaluator can fail, with no retries or fallback; a failed attempt stages nothing. Chat must already be enabled separately; evaluator consent never grants source access.

With valid policy, use these commands in the authenticated configured owner's verified human DM (`dm`/`private`, with a nonempty chat ID):

- `/tingle evaluation status` — policy consent enabled/disabled only; no current route/pricing claim.
- `/tingle evaluation enable` — read the disclosure panel, then explicitly choose **€5 cap (recommended)** or the distinct **NO Tingle spending cap** option. Cancel, unavailable or unexpected choices write nothing. No scan or model call occurs.
- `/tingle evaluation disable` — remove only the nested consent. An already-dispatched request may finish, but no new ones start; prior provider/history copies and cost reservations cannot be recalled/reset.

Changes use the shared exact-byte checked atomic writer, mode-0600 backup and saved-policy readback. All other settings, sources, jobs, destination and an existing lower cap on re-consent are preserved. Non-owner, bot, group, unknown/missing DM metadata, malformed policy, stale bytes, profile/authority changes and maintenance fail closed. An ambiguous write/readback failure says policy may already be saved and requires operator inspection, not blind retry. No automatic enablement or migration occurs. `/tingle debug spend` is the separate owner-only buffered-accounting surface. See [evaluation behavior](../README.md#sources-and-consent).

## Separate automation consent

After policy save, Tingle applies only the explicitly approved first-run source choice (with the existing minimal probes; failures are reported as off), then shows the existing helper/two-job preview. **Create both jobs** is a separate explicit consent. Cancel/unavailable/unexpected choice copies no helpers and creates no jobs; already-saved policy and separately approved source grants remain. Job cancellation is not an undo of `WRITE`.

The review job uses the chosen cron/timezone; the deadline job uses the chosen cadence and runs without an agent/model. Existing same-name jobs stop setup rather than being duplicated or edited. Config/profile/authority changes stop further scheduling. Saved definitions are read back; partial failures use the existing exact-ID rollback. Ambiguous writes or unconfirmed rollback require operator inspection, not blind retry. Returned IDs prove saved definitions, not firing or delivery. No test message is sent.

With valid policy, `/tingle setup` still previews job activation rather than editing policy. The local operator alternative remains `python3 <plugin>/tools/tingle_init.py -p <profile> --activate`; its typed consent is `CREATE BOTH JOBS`. Linux/POSIX checked-write semantics are required; unsupported systems fail closed. If a newly installed command/tool/action is missing, have an operator restart Hermes from a separate terminal (`hermes gateway restart`), then start a new session. Setup itself never restarts Hermes.

Each reminder cycle has at most four reserved attempts, including uncertain output. Optional receipts are separate; no DM acknowledgment is needed. An unresolved item stays in `/tingle list` after the budget is spent.

## Import first if wanted, then track items

Before adding a manual item, run `/tingle` in an owner DM to see the one-time import offer while the profile is empty. Then tell Hermes one real commitment and its due date or next check. `/tingle` lists unresolved active and missed items; edits, snooze and dismiss use the existing tracker behavior. Items belong to shared profile context, not a private area per sender. Setup adds no visitor access.

## One-time hands-off import

On the first `/tingle` with an empty profile, an authenticated owner in a DM is asked once whether to import the last 30 days of Gmail. The yes covers exactly one bounded look-back scan plus one model pick pass: picks backed by an exact quote from a received email are added, partial-context ones flagged in the shared-profile item, everything else stays private, and the completion reply lists descriptive titles and truthful counts. `/tingle undo onboarding` removes exactly that import's items; changed items are left alone. Declined, failed, unavailable and undone states are recorded. Setup does not alter these import/undo semantics. The import itself creates no source grants, jobs or settings.

## Optional first look-back

A configured owner can separately choose `/tingle lookback` for a one-off email-only review of up to 30 days. Its upfront preview covers a noise filter and bounded relevant body/thread reads; nothing is auto-added and ongoing source settings stay untouched. Private candidates need individual acceptance with a shared-profile disclosure. See [scope and private review](email-lookback.md).

## Optional sources

Orientation offers only installed Google bridge or wired retained-chat capabilities. A bridge file is not an account connection. Explicit owner `/tingle source enable <kind> [days]` permits a minimal check: Gmail profile identity or Calendar primary identity (hashed locally for account binding). Calendar may include attendee identities and organizer details; normalized records use organizer/count context, never recordings/transcripts. Existing basic-only grants stay basic-only during migration. New explicit Calendar enable includes organizer/count context. Old `source enable meeting` does not widen Calendar; old disable narrows only organizer/count context.

Chat verifies interface presence without reading conversation content. This profile's known chats are in scope by default: admitted inbound traffic registers a DM/group; a configured owner's command also registers its originating chat. Unauthorized commands register nothing. Exclude chats with `/tingle chat exclude`. Retained-history chats without admitted traffic since this feature enter scope only on their next admitted message (the pinned public session API cannot enumerate chats). Legacy per-chat consents stay as history. No probe occurs on orientation, unauthorized commands or pre-WRITE cancellation.

Raw probe identities are discarded; hashed account identity and grant generation refuse account switches without a fresh grant. Failed checks leave prior grants unchanged and request a fresh enable after fixing the connection. A successful check does not prove every downstream scope or complete coverage. Source operations serialize with revocation; generation checks prevent restoring a later-revoked consent. Existing source bounds and retention remain.

## Local repair and operator alternative

The operator-only full wizard remains available for controlled installs without guided bootstrap:

```sh
python3 <plugin>/tools/tingle_init.py -p <profile>
python3 <plugin>/tools/tingle_init.py -p <profile> --repair
```

Use the Hermes environment's Python, not an unrelated interpreter. Omit `-p` to use `HERMES_HOME`/default home; the tool does not follow the CLI sticky profile. It displays the resolved profile and trusts OS file access, not chat authentication. The full wizard refuses fresh defaults if plugin data already exists; use guided setup for the owner-only bootstrap above, not both provisioning routes.

A parseable invalid policy is shown for explicit local repair. Blank answers preserve prior values; exclusions, aliases and owners use JSON. Unknown fields survive. The shared validator/writer checks exact prior bytes, saves a private `.tingle-policy-backup-*` and atomically replaces the config. Existing valid policy is never overwritten by that wizard, even with `--repair`. Treat backups as private; default local removal includes them. Cooperative locks catch concurrent edits, not a hostile process with the same OS access.

Corrupt/non-object policy is not reset. Missing policy alongside an existing database requires restoring reviewed configuration; no chat fragment can reconstruct old exclusions, destinations or schedules. Symlink/device targets and symlink locks are refused. No tests imply live installation, source acceptance or delivery.

### Cadence upgrade note

The three-times-daily suggestion applies only to new policies; timezone is chosen explicitly. Hours are wall-clock times in that zone, not conversions from UTC/Berlin; daylight-saving changes can alter elapsed gaps. Existing policy files/jobs are not migrated by an upgrade. Any live cadence change needs separate approval. The 30-minute script-only deadline-check suggestion is unchanged.
