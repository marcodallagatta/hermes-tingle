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
hermes -p <profile> plugins install https://github.com/marcodallagatta/hermes-tingle.git --no-enable
hermes -p <profile> plugins show hermes-tingle
hermes -p <profile> plugins enable hermes-tingle
hermes -p <profile> plugins list --json
```

For a reproducible reviewed pin, add `--ref <full-commit>` to the install command. Confirm the plugin is enabled and note the version the installed manifest reports. Omit `-p <profile>` for the default profile. Verify command names with `hermes plugins --help`.

## Configure the profile in the owner DM

1. Send `/tingle identity`. It shows the gateway-authenticated sender and chat IDs and never enrolls anyone. Owner format is `telegram:<user-id>`; destination format is `telegram:<chat-id>` or `telegram:<chat-id>:<topic-id>`. A destination is not proven to deliver until the first real cycle.
2. Have the operator provision the owner principal once as described in [operator provisioning](onboarding.md#operator-provisioning). Nothing is enrolled by a first caller or a username.
3. Send `/tingle setup` in the verified DM. It asks one question at a time for destination, timezone, review/deadline cadence, date-only time, availability, sources and optional private evaluation (suggestion OFF). New-policy defaults are reviews at 00:00, 08:00 and 16:00 in the chosen timezone, a 30-minute deadline job, 09:00 date-only time and 09:00–18:00 availability. The new-profile wizard proposes Gmail, Calendar and chat on; type `/tingle setup sources all` to keep them or `/tingle setup sources none` to avoid initial checks and grants. These are typed commands, not buttons. After setup, enable only desired sources individually with `/tingle source enable <kind> [days]`. Review the preview, retype the exact destination, then send `/tingle setup confirm WRITE`. Creating the two jobs is a separate choice.
4. Send `/tingle sources` to review current grants. Enable only desired sources with `/tingle source enable <kind> [days]`; each enable performs a minimal connection check and grants that source only on success. `/tingle chats` and `/tingle chat exclude|include` control chat scope.
5. On the first `/tingle` with an empty profile, an authenticated owner gets one bounded 30-day Gmail import offer. Accept or decline explicitly; `/tingle undo onboarding` removes exactly that import's items.
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
