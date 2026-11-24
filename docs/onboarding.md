# First run and local repair

Owners set up Tingle in chat, not by running a Python script. On a fresh Telegram profile the owner principal is taken from the profile's configured home channel during the first `/tingle`; older profiles, a non-DM home channel or a deliberate owner change need one operator provisioning step. Local file-write access remains trusted: anyone running as the Hermes OS user can change plugin policy. Do not share that OS account with an untrusted person.

## Before choosing accounts or reminders

Tingle retains tracker content and operational records on the Hermes profile's host, not in a private area per sender. There is no built-in Tingle-operated telemetry/content-upload service, but that is not a promise about the rest of Hermes. Authorized source content and tracked details can enter the configured model's context, Hermes history/logs and reminders sent through the chosen gateway. Relevant public-link checks use Hermes web tools. Review hosting, model, accounts, destination and source scope first. See the [data-flow disclosure](../README.md#privacy-and-data).

Draft setup does not read accounts or messages. After the owner chooses Start Tingle (or uses the typed `WRITE` fallback), Gmail/Calendar enable performs a minimal account-identity check (Google helpers may refresh their local OAuth token); chat checks the history interface without reading messages. Failed checks leave sources off. Revocation does not erase tracked items or prior history/provider copies. The local remover deletes inventoried Tingle-owned data/config/backups by default, with explicit keep available; it leaves the plugin install/manager record intact. After manual Cron cleanup, run Tingle cleanup with an explicit profile, then run `hermes -p <profile-name> plugins remove hermes-tingle`. Bare native manager removal cannot invoke Tingle cleanup. Neither workflow erases independent history/provider copies. See [removal](uninstall.md).

## Operator provisioning

On a fresh Telegram profile no operator file is needed. The first `/tingle` in the owner DM takes the owner from `platforms.telegram.home_channel` in the profile config. The home channel must be a positive Telegram user/chat ID (a DM) and the sender must match its configured user ID; a group home channel, a different sender, a non-Telegram platform or a malformed/ambiguous config is refused without creating a draft. A stranger cannot enroll themselves by DMing the bot. Automatic bootstrap is also refused when the profile still has prior Tingle policy backups or recovery data, manifest records for helpers/jobs or other non-wizard resources, copied Tingle helpers or Tingle Cron jobs even without a manifest, non-empty lookback/body-audit namespaces, tracked items, source grants, or onboarding state/journal records: that is an older install needing operator repair, not a fresh default. An unreadable or malformed Cron inventory also fails closed. Admitted-chat metadata and the setup wizard's own draft record in an otherwise empty database are harmless.

The operator file below is for older profiles, a non-DM home channel, or a deliberate owner change. It remains an installer/admin action, **not an owner chat-enrollment step**. No caller, first message, button or typed reply creates an owner.

1. Resolve the intended existing profile's real home (`HERMES_HOME`, or the explicit profile directory). Do not rely on a CLI sticky profile. Provision only a fresh Tingle install with no prior policy, source consent or tracker records; missing policy on an existing install requires restoration, not new defaults.
2. Have the intended owner use `/tingle identity`; it displays gateway-authenticated sender and chat IDs without enrollment. Verify that principal through the operator's trusted relationship with the owner. A display name or chat ID alone is not ownership.
3. As the Hermes OS user, create the private directory `<profile-home>/plugin-data/hermes-tingle` and a regular, non-symlink, mode-0600 `setup-owner.json` inside it. Write the explicitly approved principal, for example this **synthetic** identity:

   ```json
   {"owner_principals": ["telegram:42"], "fresh_profile": true}
   ```

   Set the directory mode to 0700. `fresh_profile: true` explicitly attests that fresh defaults are appropriate: no prior Tingle policy/source grants/tracker data are being reconstructed. The admitted-inbound chat registry may already have created an otherwise-new database before setup; database existence alone is not proof of an old install. Do not add this attestation to recover an existing install. Do not overwrite existing policy or a prior owner file as part of owner onboarding. Changing this file is a new operator authority decision and invalidates an in-progress draft. This bootstrap file is never written by the chat flow. It resides in plugin data; review it with the operator during removal/reinstallation. Never restore it automatically to enroll a new owner.
4. Tell the owner to send `/tingle` in a verified DM. No terminal work is required from the owner. Unsupported/missing principal provisioning refuses with this documentation link and changes nothing.

Existing **valid** policy uses its already-configured `owner_principals` for job activation and needs no bootstrap file. Missing/invalid policy never authenticates someone from a valid-looking owner fragment. A bootstrap file cannot bypass invalid existing policy. Before committing, setup writes a private `setup-consumed.json` one-shot marker; if policy is later lost, or a write crashes after consumption, chat cannot reconstruct defaults from that bootstrap. An operator must inspect/restore the reviewed policy; do not delete the marker to retry blindly. Repair is operator-local below.

## First-run setup

In a fresh profile, the owner can start directly with `/tingle` in the verified home DM; `/tingle setup` is an optional shortcut. The user sees one plain-language approval screen and chooses **Start Tingle** or **Not now**. No values need to be typed.

The defaults are the current DM for reminders, the profile's Hermes `timezone`, reviews at 00:00/08:00/16:00, due checks every 30 minutes, 09:00 for date-only deadlines, 09:00–18:00 for today's calendar suggestions, and empty exclusions. Gmail, read-only Google Calendar (event context may include organizers and attendees) and chats already seen by this profile are proposed on. Relevant source text and tracked details may reach the configured AI provider and Hermes history. Google Drive is not included. The optional extra AI pass for private-chat suggestions is off; it would send additional chat excerpts to the configured model (possibly cloud/billed), so it requires separate consent.

Nothing is read before approval. **Start Tingle** saves the defaults, runs minimal source-connection checks, and creates the review and reminder jobs. Setup does not read email or event details and sends no test message. A source that cannot be verified stays off. **Not now** or `/tingle setup cancel` leaves policy, sources and jobs unchanged. If the choice panel is unavailable, the summary offers `/tingle setup confirm WRITE` as a typed fallback. If a reply is not accepted, the same approval stays available; it does not send the user back through questions. The draft is private, bound to the same owner DM/profile, and expires after 30 minutes. A profile, owner or policy change invalidates it.

Fresh Telegram profiles derive the owner from `platforms.telegram.home_channel`; older profiles, a non-DM home channel or a deliberate owner change still need [operator provisioning](#operator-provisioning). Existing valid profiles are not rewritten by `/tingle setup`; there it continues to preview job activation only. Source access can later be managed with `/tingle source enable <kind> [days]`, `/tingle source disable <kind>` and `/tingle sources`.

After fresh setup saves both jobs, Tingle offers a one-off 30-day Gmail review when the email bridge is available. Choose model picks or scan-only; Tingle then walks possible leads in the private DM, and nothing is added without confirming each item. A timed-out lead decision can be resumed with `/tingle lookback review LB-ID`. If you skip the offer or the prompt times out, start later with `/tingle lookback start 30`. If the email bridge is unavailable, Tingle says no review ran; ask your operator to wire it, then run `/tingle lookback start 30`. This one-off scan does not change standing source settings.

## Optional private-chat suggestion filter

The optional private-chat filter is **off unless enabled separately**; setup never turns it on. It is an extra AI pass for untracked private-chat suggestions, not the ordinary enabled-source review; turning it on sends additional bounded excerpts to this profile's configured model (possibly cloud/billed). In the verified owner DM, `/tingle evaluation enable` shows a plain-language disclosure and asks for one choice: a €5 monthly spending cap (UTC month), no monthly spending cap, or cancel. The filter sends a bounded excerpt from private chats to this profile's default AI model only on a supported, priced route. No-cap mode means the provider bill can exceed €5. The saved `owner-dm-excerpts-v2` policy uses `monthly_limit_microeur: 0`; a prior lower cap stays in effect when the owner chooses the €5 option again. No profile model/provider default is changed.

V2 also admits reviewed Chat Completions models on the exact built-in direct `nous` route, only with complete Hermes provider-models API pricing and a fresh exact-endpoint public catalog attestation. Named/custom/proxy/alias routes, unsupported wires/models, missing price dimensions and unknown/nonzero request fees remain local-only. Free pricing is never inferred from a suffix: all input/output/cache rates must be verified zero. Catalog metadata is re-read before each evaluation and immediately before dispatch, never under a source lock or with credentials/excerpts; locked metadata expires after 60 seconds (core endpoint cache is 300 seconds). If rates become positive, v2 records the buffered usage estimate without a monthly block. Exact route, price and policy are pinned through dispatch.


Private chat filtering is OFF unless enabled. If enabled, Tingle sends a short, bounded excerpt to this profile's default AI model in a separate request with no tools; the provider may see it. Tingle sends excerpts only when Hermes confirms supported routing and pricing. Otherwise it keeps using local text checks and sends no filter request. A 1.5 EUR/USD safety margin is added to supported-route estimates; estimates are not provider bills. Request and token sizes are limited. If the filter fails, Tingle does not retry or switch providers and saves no suggestion. This does not grant chat access. Turning the filter off cannot recall excerpts already sent or remove copies from provider or Hermes history.

With valid policy, use `/tingle evaluation status`, `/tingle evaluation enable` or `/tingle evaluation disable` in the configured owner's verified human DM. Status says whether the filter is on; it does not claim the current route is supported. These commands do not scan sources or change jobs, destination or schedule. Enabling requires the owner's explicit choice. Disabling stops new filter requests but cannot remove excerpts already sent. `/tingle debug spend` shows separate content-free estimates.

Changes use the shared exact-byte checked atomic writer, mode-0600 backup and saved-policy readback. All other settings, sources, jobs, destination and a lower saved cap are preserved. Non-owner, bot, group, unknown/missing DM metadata, malformed policy, stale bytes, profile/authority changes and maintenance fail closed. An ambiguous write/readback failure says policy may already be saved and requires operator inspection, not blind retry. See [evaluation behavior](../README.md#sources-and-consent).

## What Start Tingle creates

The one **Start Tingle** choice covers the profile defaults, proposed source checks/grants and both scheduled jobs. There is no second job-creation prompt. Failed source checks stay off; job creation still proceeds, and the reply names any source check that failed. Existing same-name jobs stop setup rather than being duplicated or edited. Config/profile/authority changes stop further scheduling. Saved definitions are read back; partial failures use the existing exact-ID rollback. Ambiguous writes or unconfirmed rollback require operator inspection, not blind retry. Returned IDs prove saved definitions, not firing or delivery. No test message is sent.

With valid policy, `/tingle setup` still previews job activation rather than editing policy. The local operator alternative remains `python3 <plugin>/tools/tingle_init.py -p <profile> --activate`; its typed consent is `CREATE BOTH JOBS`. Linux/POSIX checked-write semantics are required; unsupported systems fail closed. If a newly installed command/tool/action is missing, have an operator restart Hermes from a separate terminal (`hermes gateway restart`), then start a new session. Setup itself never restarts Hermes.

Each reminder cycle has at most four reserved attempts, including uncertain output. Optional receipts are separate; no DM acknowledgment is needed. An unresolved item stays in `/tingle list` after the budget is spent.

## Import first if wanted, then track items

Before adding a manual item, run `/tingle` in an owner DM. On an existing configured profile with an empty tracker, it may still show the one-time import offer; fresh setup instead offers the separately confirmed private 30-day Gmail look-back described above. Then tell Hermes one real commitment and its due date or next check. `/tingle` lists unresolved active and missed items; edits, snooze and dismiss use the existing tracker behavior. Items belong to shared profile context, not a private area per sender. Setup adds no visitor access.

## One-time hands-off import

An existing configured profile with an empty tracker can still receive the one-time legacy import offer on `/tingle`; this does not change standing source settings. `/tingle undo onboarding` removes only items created by that import.

## Optional first look-back

A configured owner can separately choose `/tingle lookback` for a one-off email-only review of up to 30 days. Its upfront preview covers a noise filter and bounded relevant body/thread reads; nothing is auto-added and ongoing source settings stay untouched. Private candidates need individual acceptance with a shared-profile disclosure. See [scope and private review](email-lookback.md).

## Optional sources

Orientation offers only installed Google bridge or wired retained-chat capabilities. A bridge file is not an account connection. Explicit owner `/tingle source enable <kind> [days]` permits a minimal check: Gmail profile identity or Calendar primary identity (hashed locally for account binding). Calendar may include attendee identities and organizer details; normalized records use organizer/count context, never recordings/transcripts. Existing basic-only grants stay basic-only during migration. New explicit Calendar enable includes organizer/count context. Old `source enable meeting` does not widen Calendar; old disable narrows only organizer/count context.

Chat verifies interface presence without reading conversation content. This profile's known chats are in scope by default: admitted inbound traffic registers a DM/group; a configured owner's command also registers its originating chat. Unauthorized commands register nothing. Exclude chats with `/tingle chat exclude`. Retained-history chats without admitted traffic since this feature enter scope only on their next admitted message (the pinned public session API cannot enumerate chats). Legacy per-chat consents stay as history. No probe occurs on orientation, unauthorized commands or before the owner approves Start Tingle.

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

Fresh setup uses the Hermes profile timezone and the three-times-daily schedule at 00:00, 08:00 and 16:00. These are wall-clock times in that zone; daylight-saving changes can alter elapsed gaps. Existing policy files/jobs are not migrated by an upgrade. Any live cadence change needs separate approval. The 30-minute due-check interval is unchanged.
