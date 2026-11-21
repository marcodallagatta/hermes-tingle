# Changelog

## 0.9.0 - public beta

- Removed third-party messaging and reply capture; ordinary owner reminders and generic approvals remain.

- Requires a Hermes runtime supported by the compatibility floor declared in `plugin.yaml`; Python 3.11-3.14, Linux tested.
- Consequential owner commands, including approval decisions and listings, require a configured owner and verified private DM. Refusals cannot change owner-DM bindings.
- Private chat discovery shares that verified-owner gate and accepts explicit `dm`/`private` labels case-insensitively.
- Model evaluation stays default-off. When an owner opts in, requests are bounded, single-shot, tool-free and fail closed for unsupported or unpriced routes; accounting is content-free and labeled as estimates, not invoices.
- Setup and installation docs follow the latest published revision by default; exact commit pins remain available for reproducibility.
- CI runs on pushes to `main`, pull requests and manual dispatch. Security reports use private vulnerability reporting.
- Fresh first-run source choice is all-on or all-off; type `/tingle setup sources all` to keep the proposal or `/tingle setup sources none` to skip initial checks/grants, then enable selected sources individually if desired.
- `/tingle chats` discloses bounded unmatched owner-DM local-cue discovery and distinguishes it from separately consented model evaluation.
- Local verification: 2585 passed, 17 skipped, 238 warnings; `HERMES_PRICING_SOURCE` enabled. Privacy and whitespace checks pass. The plugin validator returns `ok: true` with private Hermes-core import warnings; its static isolation audit reports `in_process`/`host_ready: false` for removal-runtime Cron lock-state writes. This classification does not establish actual host-isolated execution.

## Earlier releases

- Guided in-chat owner setup, consent gates and source defaults.
- Quiet follow-up reports, compact case-merge review and learned feedback rules.
- Bounded email look-back, chat-discovery lane, spend estimates and safe upgrade/removal paths.
