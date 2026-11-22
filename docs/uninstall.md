# Remove Tingle cleanly

Use the operator-local Tingle remover to inventory first. If it lists Tingle Cron jobs, stop all writers and remove those exact IDs with Hermes as shown below. If a listed job disappears or Hermes does not confirm the expected target, stop and rerun `--plan`; do not retry a stale ID because Hermes may resolve it as a job name. After removal, rerun `--plan` and confirm the new plan shows no Tingle jobs. Only then run the remover; it refuses cleanup while any remain.

```text
python <Tingle plugin directory>/tools/tingle_remove.py -p <profile-name> --plan
hermes -p <profile-name> cron remove <exact-job-id>
python <Tingle plugin directory>/tools/tingle_remove.py -p <profile-name> --plan
python <Tingle plugin directory>/tools/tingle_remove.py -p <profile-name>
hermes -p <profile-name> plugins remove hermes-tingle
```

Use the same Python environment as Hermes. Every command is bound to the same explicit profile name. Use `-p default` for the default profile rather than omitting the profile flag. The inventory shows the absolute profile path. This is local OS-user authority, not a chat/model tool. No email/account access is needed.

DELETE is the default. The command requires the exact displayed inventory token. Old or changed Tingle files without creation provenance need an additional explicit adoption of that exact inventory. Cron jobs are a separate prerequisite: this remover never pauses or deletes them, even when their provenance is known. Hermes owns output/notepad cleanup. Cancellation or `--plan` changes no resources. Do not run this tool merely to reset a reminder budget or repair a migration.

## What the default removes

After explicit confirmation and quiescence checks, the remover only proceeds when no Tingle Cron jobs remain. It never changes shared Cron records or job artifacts; remove listed jobs manually through Hermes first. It then deletes the inventoried Tingle-owned profile resources below. The plugin install and manager record remain intact; after the Tingle cleanup reports success, remove the plugin with the separate Hermes command shown above.

- `plugin-data/hermes-tingle`, including tracked items, rules, grants, pending/recovery history and context-review receipts/markers;
- `plugin-data/hermes-tingle-lookback`, including private staged email bodies;
- `plugin-data/hermes-tingle-body-audit`, including hashed duplicate-read records;
- their SQLite WAL/SHM and source lock sidecar and legacy lock sidecars from earlier versions;
- `hermes-tingle.json`, inventoried `.tingle-policy-backup-*` files and the wizard lock;
- exact owned/adopted copied snapshot/deadline helper files;
- known crash-temporary `.tingle-resources-*`/`.tingle-init-*` policy/manifest files and temporary helper copies, inventoried as unproven and removed only after explicit adoption;
- inventoried Tingle-named manager backups, only with explicit adoption where provenance is absent;
- its own resource manifest/lock and temporary recovery bundle/maintenance marker after verified success.

Parent directories shared with other plugins are not deleted. No unrelated profile, job, helper, account, OAuth token or arbitrary backup is selected. The native manager owns its own install/config bookkeeping and any plugin-specific grants; the remover never edits shared manager records itself.

### Explicit keep

Add `--keep-data` only when you want the inventoried Tingle data, configuration and backups retained. Tingle Cron jobs must still be removed manually through Hermes before either cleanup mode can proceed. Helpers are removed, but the plugin install remains until you run the separate Hermes plugin-remove command with the same explicit profile. KEEP does not affect Cron jobs. The inventory lists what is retained; the private ownership manifest is retained with that data to support later review. Reinstalling after KEEP can expose that deliberately retained state again.

## Safety and recovery

New wizard writes, newly created canonical data namespaces, helper creations and verified job creations record profile-bound provenance in a private `.tingle-resources.json` manifest. Existing same-byte helpers and pre-existing resources are not silently claimed. The manifest contains private job definitions, prompts and routes in addition to file identities/hashes; it is not content-free. Treat it and its crash-temporary copies as private. Source hashes are not anonymization. File identity/checksum changes, unknown namespace files, symlinks, hard links, mounted filesystem/bind-mount boundaries, malformed inventory, missing runtime seams or ambiguous ownership fail visibly or require explicit adoption before any affected deletion. Inventory is bounded to 10,000 resources; larger inventories need local inspection rather than an incomplete success claim. Large regular files are hashed in fixed-size chunks instead of loaded into memory.

The local remover and wizard/setup/database-creation entry points share a reentrant profile mutation fence. A writer holds it through its mutation and provenance registration; Tingle removal holds it from a second inventory validation through maintenance, final inventory and cleanup of Tingle-owned resources. It requires all Hermes/Tingle writers to be stopped and refuses to proceed while any relevant Cron job remains. The remover's Cron inventory is read-only: it does not use private Cron lock state, patch Hermes modules, or mutate job records. The operator must remove exact Cron IDs through Hermes separately; Hermes owns job output/notepad cleanup. The Tingle remover does not invoke the plugin manager; after its data cleanup succeeds, the operator runs Hermes plugin removal with the explicit profile shown above. The Tingle remover checks process/gateway status, fences source operations, legacy locks and SQLite writers. It does not stop an unrelated gateway or kill a process. The explicit stopped-writers requirement plus cooperative locks are not protection from a hostile OS user or arbitrary external writer that ignores Tingle's entry points.

A maintenance marker blocks cooperative Tingle database entry points while removal runs. A private fixed-file recovery bundle is created before deleting Tingle-owned data so a failed cleanup remains retryable while the plugin is still installed. Successful Tingle cleanup deletes both the bundle and marker. On failure the command says removal is incomplete, lists remaining resources or says which inventory check is unverified, and names the recovery command:

```text
python <profile-home>/.tingle-removal-recovery/tingle_remove.py -p <profile-name> --plan
python <profile-home>/.tingle-removal-recovery/tingle_remove.py -p <profile-name>
```

The recovery wrapper does not infer the profile from its path. Use the same `<profile-name>` as the original removal. For the default profile, use `-p default`.

The maintenance marker deliberately remains after failure to prevent silent data recreation. Resolve the blocker, review the fresh inventory and retry using the recovery copy with the same explicit `-p <profile-name>`. Do not delete the marker merely to hide a failed cleanup. A stale native manager record without an install tree still requires the separate Hermes plugin-remove command; Tingle-owned data cleanup does not claim to uninstall the plugin. Changed files or directory identities are not deleted using an old preview. A fresh final inventory detects new Tingle jobs/helpers/resources without deleting unapproved additions.

## Native manager boundary

The pinned Hermes runtime has no plugin uninstall callback. Bare `hermes plugins remove hermes-tingle` and dashboard removal delete the install tree/manager bookkeeping but cannot call Tingle cleanup; they can leave jobs, helpers and data. This Tingle remover also never invokes the manager: first remove listed Cron jobs through Hermes, rerun `--plan`, then run the Tingle remover with `-p <profile-name>` to delete its owned data. After it reports cleanup complete, run `hermes -p <profile-name> plugins remove hermes-tingle` as a separate operator action. Use `-p default` for the default profile. If the native plugin was already removed, use a separately retained reviewed copy of the remover or its recovery bundle. This version does not patch Hermes core or pretend an uninstall hook exists.

The Cron adapter reads the profile's job store only to build inventory; it rejects malformed or duplicate-key JSON and never pauses/removes records or touches Cron output/notepad files. Cron cleanup is deliberately delegated to Hermes' native CLI so Hermes handles those artifacts. The Tingle data-removal path still uses POSIX locking/process inspection and refuses unsupported platforms rather than using an unverified cleanup path.

## What cannot be erased by this workflow

Hermes session/model/Cron execution history, shared execution metadata, independent backups outside the inventoried Tingle paths, gateway/provider copies and messages already sent are outside this deletion scope. Manually removing each Cron job through Hermes lets Hermes remove that job's output/notepad files; the Tingle remover does not touch them. This is not whole-Hermes history erasure. Existing external effects cannot be recalled. Review those systems' own supported retention controls separately. The remover does not disconnect Gmail/Calendar accounts, delete mail/events, or delete unrelated credentials.

Repository verification uses synthetic disposable profiles and a pinned-runtime PluginManager rehearsal for host and in-process isolation, plus read-only Cron inventory/refusal tests. It does not run Tingle's remover against real Cron jobs or a live profile. A clean-reinstall rehearsal checks that default deletion leaves no old items or source grants; KEEP deliberately retains data instead.
