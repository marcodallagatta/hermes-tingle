# Diagnostics without exposing private data

## First checks

Record Tingle commit, Hermes commit/version, Python/OS, install method and gateway type. Check `hermes plugins list`, `hermes plugins show hermes-tingle` and the isolated Doctor/admission reports. Read warnings as well as exit codes. Never post a full profile, database, configuration, source snapshot or raw log. Redact identifiers and review screenshots manually.

## Common states

| Symptom | Safe next check |
| --- | --- |
| Plugin absent or disabled | Check selected profile, manifest, loader error and runtime compatibility. Do not enable unrelated plugins or grant overrides. |
| Missing/invalid Tingle config | Compare private configuration keys and types with the README. Do not paste real values into an issue. |
| No reminders | Check tracked state, due time/zone, snooze, attempt budget, configured jobs and delivery route with the operator. A quiet cycle is not proof every source was checked. |
| Unconfirmed reminder | Use `/tingle delivery` and its returned cursor for later pages. Missing receipt is not proof of failure. Retry can duplicate and does not create a fresh budget. |
| Source disabled or inaccessible | Check explicit source consent and approved window. A connected account does not grant access; never widen scope to clear an error. |
| Incomplete coverage or legacy budget uncertainty | Preserve the diagnostic and current data. Follow reviewed recovery guidance; do not reset counters, erase a gap or invent a confirmed outcome. |
| Busy revoke/lock/migration error | Stop affected work and report failure honestly. Do not delete lock/database files or claim revocation succeeded. |

- To diagnose skipped review cycles, check whether the pre-run script had new leads or due items. A quiet cycle is not proof of full source coverage.
- A review acknowledgment is required after the model interprets a lead and completes any item changes; emitting a snapshot is not enough. If the model run or acknowledgment fails, the lead can be retried on a later bounded scan.
- Review markers are hashes and identifiers, not source text. They expire after one day and are cleared by relevant source, item, policy or consent changes and by source revocation. Hashing is not anonymization.
- Maintainers can run the daily helper with `--metrics` for content-free gate counters on stderr (wake reasons, category counts, UTF-8 output bytes). They are not model token counts, provider usage, latency, cost savings or durable cron telemetry; default cron output stays quiet and the pinned scheduler discards successful stderr.

Use commands documented by the installed revision. A later branch's recovery command may not exist in an earlier build. Internal paths and private API warnings are maintenance evidence, not public API guarantees.

## Reproduction and escalation

Prefer a disposable profile, fake time/providers and synthetic destinations. Record exact steps, expected/actual result and whether external effects are unknown. Keep passed, failed, blocked and not-run evidence separate. A security issue belongs only through the verified private route described in [SECURITY.md](../SECURITY.md), never a bug form with exploit details.

If a production issue needs investigation, pause through the approved operator process and keep a consistent latest checkpoint. Do not restore an older snapshot over new writes, run fresh setup over existing jobs or disable approval/consent checks. No generic fallback is established; use an operator-reviewed recovery path.
