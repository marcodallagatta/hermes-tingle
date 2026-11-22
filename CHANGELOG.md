# Changelog

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
