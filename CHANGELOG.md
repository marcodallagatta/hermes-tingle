# Changelog

## 0.9.10 - keep owner-default setup usable across replies

- The setup wizard's own `.tingle-resources.json` is no longer mistaken for evidence of a prior install; it is created on the first typed answer. Genuine prior state still blocks automatic defaults. Added a full-path regression through destination selection, subsequent reply, cancellation and restart.
- Local verification: **2,716 passed, 30 skipped** in the standalone test environment; privacy gate passed. CI will run on the exact pull-request head.

## 0.9.9 - harden automatic owner defaulting

- Fresh-profile owner lookup now parses the profile config with Hermes's own YAML policy (`hermes_yaml`), not a hand-rolled scanner. Nested mappings, duplicate keys and invalid documents fail closed instead of admitting a different owner.
- Automatic enrollment is Telegram-only; other gateways have no verified home-channel DM predicate here and still need operator provisioning.
- Automatic bootstrap additionally refuses when prior Tingle state remains: policy backups, resource manifest/recovery data, non-empty lookback/body-audit namespaces, tracked items, source grants or onboarding/journal records. Admitted-chat metadata in an otherwise empty database is still harmless. Those profiles need operator repair, not reconstructed defaults.
- Test harness mirrors Hermes's duplicate-key-rejecting YAML policy via an isolated PyYAML stub; `requirements-dev.txt` now pins PyYAML. Docs updated.
- Local verification: **2,715 passed, 30 skipped** in the standalone test environment; privacy gate passed. All three independent-review findings were reproduced as failing regressions before the fix. CI will run on the exact pull-request head.

## 0.9.8 - removal without stopping the gateway

- The local remover no longer refuses cleanup just because the profile's gateway is running. With no Tingle Cron jobs left there is no scheduled Tingle writer; the process screen skips only the exact gateway PID reported for that profile, and other Hermes/Tingle processes still block removal.
- Successful cleanup reports `gateway_restart_required`. The CLI tells the operator to restart from a separate terminal (`hermes gateway restart`), start a new session, and not to restart from the active chat. Running gateways keep stale slash commands/tool code until then.
- The stopped-writers prompt now says the gateway may stay up. `docs/uninstall.md` documents the new flow and the restart notice.
- Local verification: **2,708 passed, 30 skipped** in the standalone test environment; privacy gate passed. The live-gateway regression was mutation-probed against the old strict guard. CI will run on the exact pull-request head.

## 0.9.7 - less onboarding friction

- A fresh profile no longer needs `setup-owner.json`: `/tingle setup` in the owner DM takes `owner_principals` from the profile's configured home channel. A different sender, a group home channel or a missing/non-DM home channel is refused without creating a draft, so a stranger cannot self-enroll by DMing the bot. The operator file stays as an override and as the repair path for older profiles.
- The missing-owner reply is plain language with copyable commands/filenames in backticks, and distinguishes a fresh profile from an older or broken one that needs operator repair.
- The destination step gained `/tingle setup here` (and `/tingle setup destination here`) to use the verified DM you are in. The short form is accepted only with no extra arguments and only on the first field; the prompt shows the exact chat ID. Source grants, destination retype, `WRITE` confirmation and job activation are unchanged.
- Docs and version metadata updated. Local verification: **2,705 passed, 30 skipped** in the standalone test environment; privacy gate passed. The owner guards and both shortcut paths were mutation-probed to confirm the regressions fail when the guards are removed. CI will run on the exact pull-request head.

## 0.9.6 - accept the newer Hermes pricing schema

- Private evaluation now accepts the known `cache_write_1h_cost_per_million` field on Hermes `PricingEntry` when it is absent or `None`. A rated value (including zero) still disables evaluation because Tingle's reservation does not bound a separate 1-hour cache-write dimension; every other added or missing field continues to fail closed. No consent, source, destination, schedule or spending behavior changed.
- Added regression coverage for absent, `None`, rated, unusable, unrelated-unknown and missing pricing fields. The tested Hermes revision was added to both pinned compatibility matrices.
- Local verification: **2,698 passed, 30 skipped** in the standalone test environment; **2,731 passed, 5 skipped** against the pinned Hermes runtime with required runtime imports; privacy gate passed. CI will run on the exact pull-request head.

## 0.9.5 - clarify unadded suggestions

- Daily-review reports now identify actionable leads not added in the current review and invite the owner to reply if they want them tracked. The wording avoids claiming a lead is globally absent based only on bounded context; automatic tracking rules remain unchanged.
- Added owner-DM `/tingle setup refresh-review` to prepare an operator-only helper command for existing profiles. Chat makes no Cron changes; the helper refuses paused/unscheduled jobs and a profile name that does not resolve to the explicit profile home, requires exact confirmation, saves the old prompt to a private mode-0600 scratch backup, applies through `hermes cron edit`, verifies the prompt and job state, and reconciles Tingle ownership metadata when the prior record matches. Legacy ownership records are recognized only when newly tracked Cron fields remain at their defaults. It does not run the review. The full prompt is passed in CLI arguments, so the operator must use a trusted host where other local users cannot inspect process arguments and avoid concurrent edits to that Cron job. Scratch backups are outside Tingle removal inventory and Hermes scratch cleanup may prune them after roughly 24 hours.
- Local verification on Python 3.14: **2,691 passed, 30 skipped, 243 deprecation warnings**; privacy gate passed. GitHub checks will run on the pull request head.

## 0.9.4 - automatic retirement for stale follow-ups

- Store a deterministic retirement class/date on new active items. Deadline items retire 7 days after due once at least three reminder attempts were reserved; waits on another actor retire after 270 inactive days; other undated work after 90 inactive days; an absolute 365-day cap applies from creation.
- Daily review backfills missing dates for legacy active items after collecting current evidence. Distinct item-specific source evidence defers soft retirement once so the agent can interpret it; unchanged evidence cannot extend retirement on later reviews, and the one-year cap still wins.
- Meaningful edits, snoozes and structured progress reset inactivity; reminder reservations, schedule checks and routine audits do not.
- Auto-closed items leave active reports, remain inspectable as `auto_closed`, and are distinct from done/dismissed. No rows are hard-deleted. The 30-minute nudge job, source scope, schedule and destination are unchanged.
- The first ordinary daily review after upgrade will perform legacy backfill; no manual source/model scan or reminder send is needed.
- Full local suite on the final code: **2,655 passed, 30 skipped, 243 deprecation warnings** (Python 3.14.9). Pinned Hermes runtime/admission checks will run on the pull request head.

## 0.9.3 - quiet unconfirmed-batch reminders

- Removed the scheduled unconfirmed-batch diagnostic from the script-only nudge output. Reminder attempts, stored cadence, the four-attempt cycle cap, batch history and `/tingle delivery` inspection remain unchanged.
- Local verification: 2,600 passed, 30 skipped, 238 deprecation warnings; privacy gate passed. GitHub CI will run on the follow-up pull request.

## 0.9.2 - explicit-profile manual cleanup

- Tingle removal now deletes only Tingle-owned profile resources. It requires an explicit profile, refuses while relevant Cron jobs remain, and leaves the plugin install and manager record intact. On success it prints `hermes -p <profile-name> plugins remove hermes-tingle` for the operator to run separately.
- The pinned Hermes compatibility job fails on missing runtime imports and runs the native removal rehearsal separately from Tingle data cleanup in both DELETE and KEEP modes, with an explicit profile and isolated scratch home.
- Local verification: 2,634 passed / 5 skipped under required Hermes runtime; 2,601 passed / 30 skipped in the clean environment. All required-run skips are retired Drive-source tests. Privacy gate, plugin validator (`host_ready: true`), Doctor, and DELETE/KEEP native-removal smoke passed; the pinned GitHub matrix has not run on this unpublished branch.

## 0.9.1 - nudge calibration

- The generated daily-review prompt suppresses unchanged calendar transition pairs after an alert attempt; event-version/time changes are eligible for review again. Calendar event evidence is bound to the source grant generation captured before those event reads; stale-generation receipts fail closed, and Calendar revoke/purge deletes all marker/receipt rows. The local store keeps only a pair hash, source generation and alert timestamp for the 30-day suppression window; pending receipt rows contain a receipt hash, source generation, profile-policy hash and expiry, and are valid for 24 hours. Both are lazily purged on a later enabled scan. No event titles or descriptions are stored. The marker records an attempt, not delivery confirmation, and transition-only evidence does not wake a new model pass. Also suppress low-urgency service/account notices already received by email; create one quiet reminder only for a real deadline and reuse an existing reminder. Routine retry/delivery bookkeeping stays out of the follow-up list, while failures needing an owner decision remain reportable. Local verification: 2,587 passed, 22 skipped, 238 warnings; privacy gate passed; plugin validator passed with existing private-core and static `in_process`/`host_ready: false` caveats.

## 0.9.0 - public beta

- Removed third-party messaging and reply capture; ordinary owner reminders and generic approvals remain.

- Requires a Hermes runtime supported by the compatibility floor declared in `plugin.yaml`; Python 3.11-3.14, Linux tested.
- Consequential owner commands, including approval decisions and listings, require a configured owner and verified private DM. Refusals cannot change owner-DM bindings.
- Private chat discovery shares that verified-owner gate and accepts explicit `dm`/`private` labels case-insensitively.
- Added explicit per-profile no-cap consent (v2, `monthly_limit_microeur: 0`) alongside the existing €5-capped v1 consent; profile defaults are unchanged. V2 also admits the documented direct OpenAI route without a Tingle cap, and only v2 can additionally admit reviewed models on the exact built-in direct Nous Chat Completions route after a fresh public catalog attestation; v1 capped admission remains limited to the documented direct OpenAI route. All evaluation is bounded, single-shot, tool-free and fail-closed for unsupported or unpriced routes; accounting remains content-free estimates, not invoices.
- Setup and installation docs follow the latest published revision by default; exact commit pins remain available for reproducibility.
- CI runs on pushes to `main`, pull requests and manual dispatch. Security reports use private vulnerability reporting.
- Fresh first-run source choice is all-on or all-off; type `/tingle setup sources all` to keep the proposal or `/tingle setup sources none` to skip initial checks/grants, then enable selected sources individually if desired.
- `/tingle chats` discloses bounded unmatched owner-DM local-cue discovery and distinguishes it from separately consented model evaluation.
- Local verification: 2585 passed, 17 skipped, 238 warnings; `HERMES_PRICING_SOURCE` enabled. Privacy and whitespace checks pass. The plugin validator returns `ok: true` with private Hermes-core import warnings; its static isolation audit reports `in_process`/`host_ready: false` for removal-runtime Cron lock-state writes. This classification does not establish actual host-isolated execution.

## Earlier releases

- Guided in-chat owner setup, consent gates and source defaults.
- Quiet follow-up reports, compact case-merge review and learned feedback rules.
- Bounded email look-back, chat-discovery lane, spend estimates and safe upgrade/removal paths.
