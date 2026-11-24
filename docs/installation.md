# Installation and first-run rehearsal

Tingle is a public pre-1.0 beta. No catalog name or release is promised. These steps prepare an installation; they do not approve live source access, scheduling or sends. For an existing installation, use the plugin manager update path below; do not rerun first-time setup.

## Requirements and version selection

Install the latest published revision, or pass an explicit reviewed commit for a reproducible pin. The plugin manager enforces the Hermes compatibility floor declared in `plugin.yaml`, but that floor does not guarantee private API seams remain stable; the current validator flags private Hermes-core imports that may break on any runtime release. Python 3.11–3.14 must satisfy the chosen Hermes runtime. Linux is the tested platform; other OSes and gateway versions are not independently accepted.

The remover's Cron inventory is read-only and does not patch Hermes Cron state; it refuses Tingle cleanup until every listed job is removed manually through Hermes after writers stop. The pinned-runtime CI checks exercise PluginManager registration and a tool call under both `host` and `in_process` isolation. See [uninstall](uninstall.md) for the manual Cron step and its limits.


For a maintainer-only no-account rehearsal, use a new disposable HOME and HERMES_HOME outside the source tree. Keep real profiles, credentials, Google connections and gateway routes unavailable. Run the synthetic tests and privacy check described in [contributor development guidance](../CONTRIBUTING.md#development) first. Doctor registers a staged copy in an isolated home; this is not a full installer or live gateway test.

## Reviewed custom-source installation

Until a catalog entry exists, install from the project repository. Use the latest published revision, or add `--ref <full-commit>` for a reproducible reviewed pin. Do not use a made-up catalog name. The plugin manager installs and enables the reviewed plugin in one step:

```sh
hermes -p <profile> plugins install https://github.com/marcodallagatta/hermes-tingle.git
hermes -p <profile> plugins list
```

Afterwards verify registration with:

```sh
hermes -p <profile> plugins show hermes-tingle
```

Enabling a reviewed plugin schedules no jobs, reads no sources and grants no scopes. Only add `--no-enable` when you are deliberately inspecting an unreviewed candidate before enabling it; install-then-enable is not the normal path.

Omit `-p <profile>` for the default profile. Replace any remaining angle-bracket placeholder; do not paste it literally. Verify syntax on the selected runtime with `hermes plugins --help`. If an unattended install refuses caution findings or dependency consent, stop for review. Do not add force flags, suppress the scanner or grant new capabilities to make a test green.

Installing/enabling alone schedules no Tingle jobs or source reads. On a fresh Telegram profile the owner starts with `/tingle` in a verified DM; the owner principal comes from that profile's configured home channel, so no operator file or terminal step is needed. `/tingle setup` is an optional shortcut to the same first-run approval. Older profiles, a non-DM home channel or a deliberate owner change still need [operator provisioning](onboarding.md#operator-provisioning). The single approval uses defaults: current DM destination, Hermes profile timezone, 00:00/08:00/16:00 reviews, 30-minute due checks, 09:00 date-only deadlines, 09:00–18:00 calendar suggestions, and empty exclusions. It proposes Gmail, read-only Calendar and chats already seen by the profile; relevant source text may reach the configured AI provider and Hermes history. Google Drive is excluded, and the separate private-chat filter remains off. Approval runs minimal connection checks and creates both jobs; there is no second job-creation prompt. An invalid reply keeps the approval available. If the choice panel is unavailable, use `/tingle setup confirm WRITE`. Change source access later with `/tingle source enable <kind>` or `/tingle source disable <kind>`. Existing valid policy and grants are not reset.

### If the model cannot use Tingle tools

Tingle's direct chat commands work without the model tool. In the owner's DM, use `/tingle done HT-1A2B3C` (or the matching `/tingle snooze` or `/tingle dismiss` command) for a one-off action; these commands do not depend on the model seeing `hermes_tingle`.

For natural-language or other model-driven Tingle actions, the active profile must enable the `hermes_tingle` toolset for that platform. If it is missing, explicitly opt in for that profile and platform with Hermes' supported CLI command:

```sh
hermes -p <profile> tools enable --platform telegram hermes_tingle
```

Use `-p default` to target the default profile explicitly; omitting `-p` can select a sticky active profile instead. Replace `telegram` with the target platform. The change takes effect in a fresh session or after `/reset`, but it is not strictly limited to that platform: Hermes may also clear a matching entry from the profile-wide `agent.disabled_toolsets` list, which can enable the toolset on other platforms whose saved lists already include it, and enabling the plugin through the plugin manager can add `hermes_tingle` to every saved platform toolset list for that profile. A saved platform list may intentionally omit a known plugin toolset, so Tingle does not enable itself or override that choice automatically; these Hermes enable paths change saved lists only when you explicitly enable the tool or plugin. Review the saved lists afterwards if you rely on a per-platform opt-out. Never edit `config.yaml` by hand.

## Updating an existing installation

Update through the Hermes plugin manager, for example `hermes -p <profile> plugins update hermes-tingle`, or reinstall an exact reviewed commit with `hermes -p <profile> plugins install https://github.com/marcodallagatta/hermes-tingle.git --ref <full-commit> --force`, then verify in a DM that `/tingle` answers. Do not hand-checkout the installed plugin tree with raw git: the running gateway keeps its stale slash-command registry (`Unknown command /tingle`) until a restart, because only the manager's reload step refreshes it. Verify the exact verbs on the selected runtime with `hermes plugins --help`. If replies are stale after an update, have an operator restart the gateway from a separate terminal (`hermes gateway restart`) and start a new session before using `/tingle`: a manager reload can leave the long-running plugin host on the old Python tool code, so replies may still show the previous version. Check the version your replies report. An update changes no policy, jobs, source grants or destinations by itself.

v0.9.4 adds automatic retirement for stale active items. The additive schema migration runs on store open; existing active items receive missing retirement fields during the next ordinary `Tingle daily review`, after that review collects sources. Do not manually run the live review just to backfill or smoke-test. The 30-minute script-only nudge helper and its schedule/destination are unchanged. Auto-closed rows remain in history and are not physically purged.

v0.9.5 clarifies when an actionable lead was not added to Tingle. Existing profiles keep their saved Cron prompt after a plugin update. In the owner's private DM, `/tingle setup refresh-review` prepares a one-line operator command; it makes no changes in chat and only prepares enabled review jobs with a scheduled next run. Run that command in a separate terminal as the Hermes OS user. The helper previews prompt hashes, asks for the exact `UPDATE <job-id>` confirmation, saves the original prompt in a private mode-0600 backup under the profile's scratch area, and delegates the prompt-only update to `hermes cron edit`; it does not run the review or change schedule, destination, scripts, source settings or other jobs. Because Hermes CLI receives the full prompt in its argument list, use this only on a trusted host where other local users cannot inspect process arguments. Do not run it while someone else is editing that Cron job. The private backup is under the selected profile's `cache/scratch`; Tingle removal does not inventory these transient backups, and Hermes scratch cleanup may prune them after roughly 24 hours. Copy a backup elsewhere if you need to retain it longer. New wording takes effect on the next scheduled review.

## Existing profiles and live tests

A plugin update does not change existing policy, source grants, jobs or destinations. On an existing valid profile, `/tingle setup` previews job activation only; source access can be adjusted later with `/tingle source enable <kind>` or `/tingle source disable <kind>`. A live smoke test is separate from normal setup and requires explicit approval.

## Future catalog path

After public-copy and submission approval, a maintainer-reviewed catalog entry pins the public repository at an exact commit. Users then install by the actual accepted catalog name and enable it. Catalog admission does not deploy it or grant data access. See the [official catalog guide](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/catalog-submission).
