# Remove Tingle cleanly

Use the operator-local Tingle remover before removing the install tree:

```text
python <Tingle plugin directory>/tools/tingle_remove.py --plan
python <Tingle plugin directory>/tools/tingle_remove.py
```

Use the same Python environment as Hermes. `-p <existing-profile>` selects an existing named profile; otherwise the current `HERMES_HOME`/default home is shown. Check the absolute profile path in the inventory. This is local OS-user authority, not a chat/model tool. No email/account access is needed.

DELETE is the default. The command inventories first, asks you to stop all Hermes/Tingle writers, then requires the exact displayed inventory token. Old or changed resources without creation provenance need an additional explicit adoption of that exact inventory. A matching name or identical bytes is not ownership proof. Cancellation or `--plan` changes no resources. Do not run it merely to reset a reminder budget or repair a migration.

## What the default removes

After explicit confirmation and quiescence checks, it pauses and removes the exact owned/adopted daily/deadline Cron jobs and verifies their absence. It uses the native Cron manager for job output/notepad cleanup. It then removes the managed plugin through Hermes's existing plugin manager, verifies the install tree and manager record are absent, and deletes the inventoried Tingle resources:

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

Add `--keep-data` only when you want the inventoried Tingle data, configuration and backups retained. Jobs are still paused/removed, helpers are removed and the plugin is uninstalled. KEEP does not leave automation active. The inventory lists what is retained; the private ownership manifest is retained with that data to support later review. Reinstalling after KEEP can expose that deliberately retained state again.

## Safety and recovery

New wizard writes, newly created canonical data namespaces, helper creations and verified job creations record profile-bound provenance in a private `.tingle-resources.json` manifest. Existing same-byte helpers and pre-existing resources are not silently claimed. The manifest contains private job definitions, prompts and routes in addition to file identities/hashes; it is not content-free. Treat it and its crash-temporary copies as private. Source hashes are not anonymization. File identity/checksum changes, unknown namespace files, symlinks, hard links, mounted filesystem/bind-mount boundaries, malformed inventory, missing runtime seams or ambiguous ownership fail visibly or require explicit adoption before any affected deletion. Inventory is bounded to 10,000 resources; larger inventories need local inspection rather than an incomplete success claim. Large regular files are hashed in fixed-size chunks instead of loaded into memory.

The local remover and wizard/setup/database-creation entry points share a reentrant profile mutation fence. A writer holds it through its mutation and provenance registration; removal holds it from a second inventory validation through maintenance, native removal, final inventory and cleanup. The local remover requires a stopped gateway and other writers, checks process/gateway status, fences source operations, legacy locks and SQLite writers, and uses a strict cross-process Cron transaction. It refuses a busy job claim instead of assuming an in-flight action was recalled. It does not stop an unrelated gateway or kill a process. The explicit stopped-writers requirement plus cooperative locks are not protection from a hostile OS user or an arbitrary external writer that ignores Tingle's entry points.

A maintenance marker blocks cooperative Tingle database entry points while removal runs. A private fixed-file recovery bundle is created before native uninstall, so a failure after the install tree disappears remains retryable. Successful removal deletes both. On failure the command says removal is incomplete, lists remaining resources or says which inventory check is unverified, and names the recovery command:

```text
python <profile>/.tingle-removal-recovery/tingle_remove.py --plan
python <profile>/.tingle-removal-recovery/tingle_remove.py
```

The marker deliberately remains after a failure to prevent silent data recreation. Resolve the reported blocker, review the fresh inventory and retry. Do not delete it merely to hide a failed removal. A stale native manager record with no install tree requires local manager repair/inspection; it is not reported as a complete uninstall. Changed files or directory identities are not deleted using an old preview. The complete install tree is inventoried recursively and rechecked immediately before native recursive deletion. A fresh final relevant inventory detects new Tingle jobs/helpers/resources without deleting unapproved additions.

## Native manager boundary

The pinned Hermes runtime has no plugin uninstall callback. Bare `hermes plugins remove hermes-tingle` and dashboard removal delete the install tree/manager bookkeeping but cannot call Tingle's cleanup. They can leave jobs, copied helpers and plugin data. Use this remover first. If the native tree was already removed, use a separately retained reviewed copy of the remover, or the recovery bundle if one exists. Native automatic cleanup requires a supported upstream lifecycle seam; this version does not patch Hermes core or pretend such a hook exists.

The strict Cron adapter and readback use narrow pinned-runtime private seams because the current public APIs do not offer strict conditional removal. They are covered by isolated actual-runtime rehearsals, but remain a compatibility/launch gate. Unsupported runtimes and Windows are refused rather than given an unverified deletion path. The script uses POSIX locking and process inspection.

## What cannot be erased by this workflow

Hermes session/model/cron execution history, shared execution metadata, independent backups outside the inventoried Tingle paths, gateway/provider copies and messages already sent are outside this deletion scope. The native job manager removes its own output/notepad files but this is not whole-Hermes history erasure. Existing external effects cannot be recalled. Review those systems' own supported retention controls separately. The remover does not disconnect Gmail/Calendar accounts, delete mail/events, or delete unrelated credentials.

Repository verification uses synthetic disposable profiles, fake providers and an actual pinned-runtime local Cron/native-manager rehearsal. No original user profile, account, running gateway or live installation is removed. A clean-reinstall rehearsal checks that default deletion leaves no old items or source grants; KEEP deliberately retains data instead.
