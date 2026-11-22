# Installation and first-run rehearsal

Tingle is a public pre-1.0 beta. No catalog name or release is promised. These steps prepare an installation; they do not approve live source access, scheduling or sends. For an existing installation, use the plugin manager update path below; do not rerun first-time setup.

## Requirements and version selection

Install the latest published revision, or pass an explicit reviewed commit for a reproducible pin. The plugin manager enforces the Hermes compatibility floor declared in `plugin.yaml`, but that floor does not guarantee private API seams remain stable; the current validator flags private Hermes-core imports that may break on any runtime release. Python 3.11–3.14 must satisfy the chosen Hermes runtime. Linux is the tested platform; other OSes and gateway versions are not independently accepted.

The current static plugin audit reports isolation `in_process` and `host_ready: false` because the removal runtime patches private Cron lock-state attributes. This is a static code-path classification, not proof of what a host-isolated runtime actually executes; runtime isolation behavior remains unverified.


For a maintainer-only no-account rehearsal, use a new disposable HOME and HERMES_HOME outside the source tree. Keep real profiles, credentials, Google connections and gateway routes unavailable. Run the synthetic tests and privacy check described in [contributor development guidance](../CONTRIBUTING.md#development) first. Doctor registers a staged copy in an isolated home; this is not a full installer or live gateway test.

## Reviewed custom-source installation

Until a catalog entry exists, install from the project repository. Use the latest published revision, or add `--ref <full-commit>` for a reproducible reviewed pin. Do not use a made-up catalog name:

```sh
hermes -p <profile> plugins install https://github.com/marcodallagatta/hermes-tingle.git --no-enable
hermes -p <profile> plugins show hermes-tingle
```

Inspect the source, scanner warnings, capabilities and compatibility before enabling:

```sh
hermes -p <profile> plugins enable hermes-tingle
hermes -p <profile> plugins list
```

Omit `-p <profile>` for the default profile. Replace any remaining angle-bracket placeholder; do not paste it literally. Verify syntax on the selected runtime with `hermes plugins --help`. If an unattended install refuses caution findings or dependency consent, stop for review. Do not add force flags, suppress the scanner or grant new capabilities to make a test green.

Installing/enabling alone schedules no Tingle jobs or source reads. For a fresh install, an operator provisions the owner principal as described in [onboarding](onboarding.md); the owner completes `/tingle setup` in a verified DM and reviews the source choice before explicit `WRITE`. Gmail, Calendar and chat are proposed on for fresh profiles through one **all-or-none** choice: type `/tingle setup sources all` to keep the proposal, or `/tingle setup sources none` to avoid all initial checks and grants. These are typed commands in the new-profile wizard. After setup, enable desired sources individually with `/tingle source enable <kind>` (`email`, `calendar` or `chat`), each with its own minimal check and grant. Selected checks/grants are applied only after `WRITE`. Private evaluation remains OFF unless separately consented. Existing valid policy and grants are not reset. Owner identity is never inferred from a first caller or username.

## Updating an existing installation

Update through the Hermes plugin manager, for example `hermes -p <profile> plugins update hermes-tingle`, or reinstall an exact reviewed commit with `hermes -p <profile> plugins install https://github.com/marcodallagatta/hermes-tingle.git --ref <full-commit> --force`, then verify in a DM that `/tingle` answers. Do not hand-checkout the installed plugin tree with raw git: the running gateway keeps its stale slash-command registry (`Unknown command /tingle`) until a restart, because only the manager's reload step refreshes it. Verify the exact verbs on the selected runtime with `hermes plugins --help`. If replies are stale after an update, have an operator restart the gateway from a separate terminal (`hermes gateway restart`) and start a new session before using `/tingle`: a manager reload can leave the long-running plugin host on the old Python tool code, so replies may still show the previous version. Check the version your replies report. An update changes no policy, jobs, source grants or destinations by itself.

## Starting real operation is a separate step

After approval of the exact profile, destinations, schedules and exclusions, the operator can review `/tingle setup` and confirm only the intended jobs. Source opt-ins and windows require separate permission; a connected account is not consent. Any live smoke test requires separate explicit approval; distinguish reserved attempts, emission and actual receipt.

## Future catalog path

After public-copy and submission approval, a maintainer-reviewed catalog entry pins the public repository at an exact commit. Users then install by the actual accepted catalog name and enable it. Catalog admission does not deploy it or grant data access. See the [official catalog guide](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/catalog-submission).
