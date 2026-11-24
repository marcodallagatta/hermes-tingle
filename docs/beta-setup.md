# Beta setup — sources enabled

This is the current setup packet for the public pre-1.0 beta. The owner runs setup in a verified DM; no terminal script is required for the owner. Installation and enabling alone do not grant source access, schedule jobs or send messages.

## Prerequisites

- The tester has their own OS account and Hermes profile on their own host. Do not share an OS account: local file-write access is trusted as policy authority, and profile items are shared profile context, not private per sender.
- A messaging gateway (Telegram is the tested path) with a private DM for owner commands and a chosen destination for reports and reminders.
- Google Workspace connected in the same Hermes profile for Gmail and Calendar review. A connected account is not source consent; consent happens in setup.
- The latest Hermes runtime supported by the compatibility floor declared in `plugin.yaml`, Python 3.11–3.14, and Linux for the tested path.
- Their own GitHub access to the public repository. Do not share any deploy key used for automated installs.

## Install the plugin

```sh
hermes -p <profile> plugins install https://github.com/marcodallagatta/hermes-tingle.git
hermes -p <profile> plugins list --json
```

For a reproducible reviewed pin, add `--ref <full-commit>` to the install command. Confirm the plugin is enabled and note the version the installed manifest reports. Omit `-p <profile>` for the default profile. The manager installs and enables a reviewed plugin in one step; enabling schedules no jobs, reads no sources and grants no scopes. Use `--no-enable` only when deliberately inspecting an unreviewed candidate before enabling it. Verify command names with `hermes plugins --help`.

## Configure the profile in the owner DM

1. Send `/tingle identity`. It shows the gateway-authenticated sender and chat IDs and never enrolls anyone. Owner format is `telegram:<user-id>`; destination format is `telegram:<chat-id>` or `telegram:<chat-id>:<topic-id>`. A destination is not proven to deliver until the first real cycle.
2. On a fresh Telegram profile no operator file is needed: `/tingle` in the verified DM takes the owner from the profile's configured home channel. `/tingle setup` is an optional shortcut to the same first-run approval. Older profiles, a non-DM home channel or a deliberate owner change still need [operator provisioning](onboarding.md#operator-provisioning). Nothing is enrolled by a first caller or a username.
3. In the owner DM, send `/tingle` and choose **Start Tingle**. The defaults are the current chat for reminders, the profile's Hermes timezone, reviews at 00:00/08:00/16:00, due reminders every 30 minutes, date-only deadlines at 09:00 and today's calendar suggestions from 09:00–18:00. Exclusions start empty. The approval covers Gmail, read-only Google Calendar (event context may include organizers and attendees), chats this profile has seen, the minimum connection checks, and both scheduled jobs. Relevant source text and tracked details may reach the configured AI provider and Hermes history; Google Drive is not included. The optional extra AI pass for private-chat suggestions stays off; it would send additional chat excerpts to the configured model (possibly cloud/billed), so it needs separate consent. No mail or event content is read during setup and no test message is sent. Cancel or choose **Not now** and nothing changes. If the choice panel is unavailable, use `/tingle setup confirm WRITE` in the owner DM. A rejected reply leaves the approval ready to retry.
4. Send `/tingle sources` to review current grants. Enable only desired sources with `/tingle source enable <kind> [days]`; each enable performs a minimal connection check and grants that source only on success. `/tingle chats` and `/tingle chat exclude|include` control chat scope.
5. When the email bridge is available, fresh setup offers a one-off 30-day Gmail review. Choose model picks or scan-only; it walks possible leads privately, and nothing is added without item confirmation. A timed-out lead decision resumes with `/tingle lookback review LB-ID`; a skipped offer can be started with `/tingle lookback start 30`. If the email bridge is unavailable, Tingle says no review ran; ask your operator to wire it, then run `/tingle lookback start 30`. Existing configured empty profiles retain their earlier one-time onboarding import and `/tingle undo onboarding` path.
6. Confirm `/tingle` in the DM reports the expected version. If a tool action still behaves stale after installation, restart the gateway from a separate operator terminal and start a new session; do not restart from the active chat. Registration and file hashes alone do not prove live serving.

## What the tester should expect

- Add one real commitment and watch the next scheduled cycle. Reviews are quiet when nothing needs attention; the 30-minute job sends only due reminders.
- A case-merge proposal appears only when two active items genuinely share one current outcome. In the owner DM, bare `/tingle merge` opens the single pending proposal; several are listed by ID. The interactive preview needs the authenticated DM command path.
- The confirmation holds the one-time undo command. Undo is no longer repeated in `/tingle case` or `/tingle`, but remains in that confirmation and in `/tingle help`.

## Consent and data-flow checklist

Before turning sources on, the tester should understand and accept:

- Profile SQLite on the Hermes host retains tracked item text, deadlines, preferences, consent markers and delivery history. Items are shared profile context, not private per chat sender.
- Authorized source metadata, bounded title-token chat excerpts and selectively fetched email bodies can reach the profile's configured model provider and remain in Hermes session, cron history and logs. Cloud providers may therefore see source-derived content.
- Reminders leave through the configured gateway and destination; if that destination is a group, other participants can see reports and detailed previews. Keep the loop in the tester's DM unless they explicitly choose otherwise.
- Public-link checks use read-only Hermes web tools. There is no Tingle-operated telemetry or upload service in this build; that is not a claim about Hermes itself, helpers, model providers or messaging services.
- Revoking a source stops future reads but does not erase tracked items or independent copies held by providers or Hermes history.

## Known limits to report honestly

- Public pre-1.0 beta code with no catalog entry. Report sensitive findings to the maintainers privately; do not create public issues for personal or security-sensitive reports.
- The previously failing lookback-ordering test is fixed in this release; the full suite is green.
- Local chat-discovery cues are deliberately conservative; implicit commitments and other phrasings can be missed. No automatic item creation without explicit owner-DM acceptance.
- The static plugin validator reports private Hermes-core imports that may break on any runtime release; the manifest's compatibility floor is not a stability guarantee.
- Tingle's Cron inventory is read-only. The local remover refuses while any Tingle job remains; after stopping writers, remove the exact IDs through Hermes and rerun the inventory. Pinned-runtime tests check registration and tool invocation under both `host` and `in_process` isolation.
