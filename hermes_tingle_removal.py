"""Operator-local removal with exact inventory, provenance and maintenance fencing.

POSIX only. No live resources are changed merely by importing or planning.
"""
from __future__ import annotations
import json
import os
import stat
import sys
import subprocess
from contextlib import ExitStack
from pathlib import Path
try:
    from . import hermes_tingle_resources as resources
except ImportError:
    import hermes_tingle_resources as resources

HELPERS=('hermes_tingle_snapshot.py','hermes_tingle_due.py')


def fingerprint(path, home):
    path=resources.safe(path,home)
    st=path.lstat()
    if stat.S_ISDIR(st.st_mode):
        return {'type':'directory','device':st.st_dev,'inode':st.st_ino}
    if not stat.S_ISREG(st.st_mode) or st.st_nlink!=1:
        raise ValueError('Only ordinary single-link Tingle files can be removed')
    import hashlib
    digest=hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda:handle.read(1024*1024),b''):digest.update(block)
    return {'type':'file','device':st.st_dev,'inode':st.st_ino,'sha256':digest.hexdigest()}


def runtime(home):
    try:
        from .hermes_tingle_removal_runtime import runtime as build
    except ImportError:
        from hermes_tingle_removal_runtime import runtime as build
    return build(home)


def recovery_bundle(home):
    root=resources.safe(home/'.tingle-removal-recovery',home)
    source=Path(__file__).resolve().parent
    sources={name:source/name for name in ('hermes_tingle_removal.py','hermes_tingle_removal_runtime.py','hermes_tingle_resources.py')}
    wrapper=source/'tools'/'tingle_remove.py'
    if not wrapper.exists():wrapper=source/'tingle_remove.py'
    sources['tingle_remove.py']=wrapper
    payloads={name:path.read_bytes() for name,path in sources.items()}
    existed=root.exists()
    root.mkdir(mode=0o700,exist_ok=True)
    if not existed:resources.record(root,home=home,directory=True)
    for name,content in payloads.items():
        path=resources.safe(root/name,home)
        if path.exists():
            if path.read_bytes()!=content:raise ValueError('Recovery helper changed; inspect '+str(path))
        else:
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'wb') as out:out.write(content)
            resources.record(path,home=home)
    return root,{name:resources.digest(content) for name,content in payloads.items()}


def inventory(home, api=None):
    home=Path(home).resolve(strict=True)
    api=api or runtime(home)
    manifest=resources.read(home)
    found={}
    # Do not traverse/delete a mounted unrelated filesystem or bind mount just
    # because its mountpoint happens to sit inside a Tingle namespace.
    import re
    mounts=set()
    mountinfo=Path('/proc/self/mountinfo')
    if mountinfo.exists():
        for line in mountinfo.read_text().splitlines():
            fields=line.split()
            if len(fields)<5:raise ValueError('Unverifiable mount inventory')
            mounts.add(Path(re.sub(r'\\([0-7]{3})',lambda m:chr(int(m[1],8)),fields[4])))
    def add(path,kind):
        if not path.exists() and not path.is_symlink():return
        relative=str(path.relative_to(home))
        for parent in [path,*path.parents]:
            if parent==home:break
            if parent in mounts or os.path.ismount(parent):raise ValueError('Mounted resource boundary refused: '+str(parent))
        current=fingerprint(path,home)
        recorded=manifest['directories' if current['type']=='directory' else 'files'].get(relative)
        proven=bool(recorded and all(current.get(k)==v for k,v in recorded.items() if k in ('device','inode','sha256')))
        if len(found)>=10000:raise ValueError('Resource inventory exceeds safe count; inspect locally')
        found[relative]={'kind':kind,'fingerprint':current,'ownership':'created' if proven else 'needs explicit adoption'}
    def tree(path,kind,depth=0):
        if depth>128:raise ValueError('Resource directory nesting exceeds safe inventory')
        add(path,kind)
        if path.is_dir():
            import itertools
            children=list(itertools.islice(path.iterdir(),10001))
            if len(children)>10000-len(found):raise ValueError('Resource inventory exceeds safe count')
            for child in sorted(children):tree(child,kind,depth+1)
    for namespace in resources.NAMES:
        root=resources.safe(home/'plugin-data'/namespace,home)
        if root.exists():
            tree(root,'data')
            for relative,entry in found.items():
                path=home/relative
                # Only normal direct SQLite files inherit a created namespace.
                if path.name in ('data.db','data.db-wal','data.db-shm','data.db.sources.lock','data.db.coordination.lock') and path.parent==root and found[str(root.relative_to(home))]['ownership']=='created':
                    entry['ownership']='created namespace SQLite file'
    for backup in sorted((home/'plugins-backup').glob('hermes-tingle-*')):
        tree(backup,'data')
    for path in [home/'hermes-tingle.json',home/'.tingle-init.lock',*home.glob('.tingle-policy-backup-*')]:add(path,'data')
    for name in HELPERS:add(home/'scripts'/name,'helper')
    # Known crash-temporary names are inventory leads, never ownership proof.
    for pattern in ('.tingle-resources-*','.tingle-init-*'):
        for path in home.glob(pattern):add(path,'data')
    for name in HELPERS:
        for path in (home/'scripts').glob('.'+name+'.*'):add(path,'helper')
    # The private provenance file contains job definitions/routes, not merely hashes.
    # Show it and its lock too; cleanup handles their own workflow updates separately.
    for name in (resources.MANIFEST,resources.LOCK):add(home/name,'metadata')
    install=resources.safe(home/'plugins'/'hermes-tingle',home)
    if install.exists():
        # Manager owns installation metadata. Its existing remove command handles
        # that state; an explicit inventory confirmation adopts the exact tree.
        tree(install,'install')
    recovery=resources.safe(home/'.tingle-removal-recovery',home)
    if recovery.exists():tree(recovery,'recovery')
    jobs=[]
    listing=api['list']()
    if not isinstance(listing,list) or any(not isinstance(j,dict) or not isinstance(j.get('id'),str) for j in listing):
        raise ValueError('Unverifiable complete Cron inventory')
    ids=[j['id'] for j in listing]
    if len(ids)!=len(set(ids)):raise ValueError('Duplicate Cron IDs')
    for summary in listing:
        job=api['get'](summary['id'])
        if not isinstance(job,dict) or job.get('id')!=summary['id']:raise ValueError('Cron readback disagreement')
        entry=manifest['jobs'].get(job['id'])
        relevant=entry is not None or job.get('name') in ('Tingle daily review','Tingle nudges','[SA] Hermes Tingle daily review','[S] Hermes Tingle deadline nudges') or Path(str(job.get('script') or '')).name in HELPERS
        if relevant:
            definition=resources.job_definition(job)
            jobs.append({'id':job['id'],'definition':definition,'ownership':'created' if entry and entry.get('definition')==definition else 'needs explicit adoption',
                         'busy':bool(job.get('fire_claim') or job.get('run_claim'))})
    plan={'home':str(home),'resources':found,'jobs':jobs,'manifest':manifest,
          'native_hook':False,'manager_record_present':not api.get('metadata_absent',lambda:True)(),'limits':'Hermes history, unrelated backups/accounts/tokens and external copies are outside removal scope.'}
    plan['token']=resources.digest(json.dumps(plan,sort_keys=True).encode())[:12]
    return plan


def verify_quiescent(home,api):
    if api['gateway'](Path(home)) is not None:
        raise ValueError('Gateway is live for this profile. Stop it explicitly before removal; no gateway was stopped.')
    # Conservative POSIX cross-process screen, no command lines or environment
    # secrets are returned. Other Hermes processes can hold plugin writers too.
    proc=Path('/proc')
    if not proc.is_dir():raise ValueError('Quiescence process inspection unavailable; no cleanup attempted')
    for path in proc.iterdir():
        if not path.name.isdigit() or int(path.name)==os.getpid():continue
        try:
            args=(path/'cmdline').read_bytes().split(b'\0')
        except (FileNotFoundError,ProcessLookupError):continue
        except PermissionError:raise ValueError('Cannot verify other process quiescence') from None
        words=[os.fsdecode(v) for v in args[:4] if v]
        if words and (Path(words[0]).name in ('hermes','hermes-agent') or any(
                v in ('hermes_cli.main','gateway.run') or Path(v).name in ('hermes','source_snapshot.py','due_alerts.py','tingle_init.py',*HELPERS) for v in words[1:])):
            raise ValueError('Another Hermes/Tingle process may still write. Stop writers explicitly and retry.')


def remove(home, plan, *, confirmation, writers_stopped=False, adopt_legacy=False, keep_data=False, api=None, manager=None):
    import fcntl
    home=Path(home).resolve(strict=True)
    api=api or runtime(home)
    if not writers_stopped or confirmation!='REMOVE '+plan['token']:
        raise ValueError('Explicit stopped-writers statement and exact inventory confirmation required')
    if str(home)!=plan['home']:raise ValueError('Profile changed')
    current=inventory(home,api)
    if current!=plan:raise ValueError('Inventory changed; review again before removal')
    legacy=[p for p,r in plan['resources'].items() if r['ownership']=='needs explicit adoption' and not (keep_data and r['kind'] in ('data','metadata'))]
    legacy += ['job:'+j['id'] for j in plan['jobs'] if j['ownership']=='needs explicit adoption']
    if legacy and not adopt_legacy:raise ValueError('Legacy/changed resources need explicit inventory adoption: '+', '.join(legacy))
    if plan['manager_record_present'] and not any(r['kind']=='install' for r in plan['resources'].values()):
        raise ValueError('Native manager record remains without its install tree; repair/inspect that manager record before cleanup. Nothing changed.')
    lock_existed=(home/resources.LOCK).exists()
    with resources.locked(home):
        checked=inventory(home,api)
        comparable=json.loads(json.dumps(checked))
        if not lock_existed:comparable['resources'].pop(resources.LOCK,None)
        comparable['token']=plan['token']
        if comparable!=plan:raise ValueError('Inventory changed while acquiring mutation fence')
        resources._mutation_epochs[home]=resources._mutation_epochs.get(home,0)+1
        return _remove_fenced(home,plan,keep_data=keep_data,api=api,manager=manager)


def _remove_fenced(home,plan,*,keep_data,api,manager):
    import fcntl
    verify_quiescent(home,api)
    marker=resources.safe(home/resources.MARKER,home)
    if marker.exists():
        info=fingerprint(marker,home)
        saved=json.loads(marker.read_text())
        if saved!={'profile':str(home),'reason':'local removal in progress'}:
            raise ValueError('Unrecognized maintenance marker; inspect locally')
    else:
        fd=os.open(marker,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w') as out:json.dump({'profile':str(home),'reason':'local removal in progress'},out)
    remaining=[]
    try:
        recovery,recovery_hashes=recovery_bundle(home)
        # These are the only permitted self-writes after the reviewed inventory.
        # Recovery registration atomically replaces the manifest. Freeze exact
        # resulting bytes AND identities, never adopt later manager-phase edits.
        workflow_expected={str(path.relative_to(home)):fingerprint(path,home)
                           for path in (home/resources.MANIFEST,home/resources.LOCK,marker,recovery,
                                        *(recovery/name for name in recovery_hashes)) if path.exists()}
        def verify_workflow():
            for relative,expected in workflow_expected.items():
                path=home/relative
                if not path.exists() or fingerprint(path,home)!=expected:
                    raise ValueError('Workflow resource changed after creation: '+relative)
        with resources.locked(home), ExitStack() as stack:
            # Fence every existing owned DB writer and source or legacy lock without
            # opening plugin storage (which would create missing resources).
            for namespace in resources.NAMES:
                root=home/'plugin-data'/namespace
                for name in ('data.db.sources.lock','data.db.coordination.lock'):
                    lock=root/name
                    if lock.exists():
                        fd=os.open(resources.safe(lock,home),os.O_RDWR|os.O_NOFOLLOW)
                        handle=stack.enter_context(os.fdopen(fd,'r+b'))
                        fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
                database=root/'data.db'
                if database.exists():
                    import sqlite3
                    conn=sqlite3.connect(str(resources.safe(database,home)),timeout=0)
                    stack.callback(conn.close)
                    conn.execute('BEGIN IMMEDIATE')
            verify_quiescent(home,api)
            from contextlib import nullcontext
            transaction=api.get('transaction',nullcontext)
            with transaction() if plan['jobs'] else nullcontext():
                for job in plan['jobs']:
                    live=api['get'](job['id'])
                    if live is None:continue
                    if resources.job_definition(live)!=job['definition']:
                        raise ValueError('Cron definition changed; no guessed job deletion')
                    paused=api['pause'](job['id'])
                    if not isinstance(paused,dict) or paused.get('id')!=job['id'] or paused.get('enabled') is not False:
                        raise ValueError('Job pause unconfirmed')
                    live=api['get'](job['id'])
                    if resources.job_definition(live)!=job['definition']:
                        raise ValueError('Cron definition changed during pause')
                    if live.get('fire_claim') or live.get('run_claim'):
                        raise ValueError('Job still has an in-flight claim; wait/inspect before retrying')
                    if api['remove'](job['id']) is not True or api['get'](job['id']) is not None:
                        raise ValueError('Exact job removal unconfirmed')
            # A manager failure must leave cleanup retryable and visible. Do not
            # claim uninstall merely from a successful subprocess exit.
            if any(r['kind']=='install' for r in plan['resources'].values()):
                current_install={p:r for p,r in inventory(home,api)['resources'].items() if r['kind']=='install'}
                expected_install={p:r for p,r in plan['resources'].items() if r['kind']=='install'}
                if current_install!=expected_install:raise ValueError('Install tree changed before native removal')
                if manager is None:
                    def manager():
                        result=subprocess.run([sys.executable,'-m','hermes_cli.main','plugins','remove','hermes-tingle'],capture_output=True,text=True,env={**os.environ,'HERMES_HOME':str(home)})
                        if result.returncode:raise ValueError('Native plugin manager removal failed; inspect locally')
                manager()
                if (home/'plugins'/'hermes-tingle').exists():raise ValueError('Native plugin tree removal not verified')
                if not api.get('metadata_absent',lambda:True)():raise ValueError('Native manager metadata removal not verified')
        verify_workflow()
        # Locks/SQLite handles close before unlink. The maintenance marker stops
        # cooperative Tingle entry points from reopening data in this interval.
        for relative,entry in sorted(plan['resources'].items(),key=lambda kv:len(Path(kv[0]).parts),reverse=True):
            if entry['kind'] in ('install','metadata','recovery') or (keep_data and entry['kind']=='data'):continue
            path=resources.safe(home/relative,home)
            if not path.exists():continue
            actual=fingerprint(path,home)
            if actual!=entry['fingerprint']:
                sqlite_file=Path(relative).parent in [Path('plugin-data')/n for n in resources.NAMES] and Path(relative).name in ('data.db','data.db-wal','data.db-shm')
                same_inode=all(actual.get(k)==entry['fingerprint'].get(k) for k in ('device','inode','type'))
                if not (sqlite_file and same_inode):
                    raise ValueError('Resource changed after preview: '+relative)
            if path.is_dir():path.rmdir()
            else:path.unlink()
        # Reconcile a fresh full relevant inventory, not just the original IDs.
        # Concurrent unexpected creations are blockers, never implicit adoption.
        final=inventory(home,api)
        unexpected=['job:'+j['id'] for j in final['jobs']]
        for relative,entry in final['resources'].items():
            if entry['kind'] in ('metadata','recovery'):continue
            if keep_data and entry['kind']=='data':continue
            unexpected.append(relative)
        if unexpected:raise ValueError('Remaining/new unapproved resources: '+', '.join(unexpected))
        verify_workflow()
        if not keep_data:
            (home/resources.MANIFEST).unlink(missing_ok=True)
            (home/resources.LOCK).unlink(missing_ok=True)
        for job in plan['jobs']:
            if api['get'](job['id']) is not None:remaining.append('job:'+job['id'])
        for relative,entry in plan['resources'].items():
            if keep_data and entry['kind'] in ('data','metadata'):continue
            if entry['kind']=='recovery':continue
            if (home/relative).exists():remaining.append(relative)
        if not api.get('metadata_absent',lambda:True)():remaining.append('native manager install metadata')
        if remaining:raise ValueError('Remaining resources: '+', '.join(remaining))
        # Only the fixed, byte-verified bundle created by this workflow is removed.
        for name,expected in recovery_hashes.items():
            path=resources.safe(recovery/name,home)
            if path.exists():
                if resources.digest(path.read_bytes())!=expected:raise ValueError('Recovery helper changed; inspect locally')
                path.unlink()
        if recovery.exists():recovery.rmdir()
        marker.unlink()
        return {'removed':True,'kept_data':keep_data,'limits':plan['limits']}
    except Exception as exc:
        # Keep the small maintenance marker deliberately after failure. It blocks
        # resurrection and is named in recovery instructions, never hidden.
        try:
            rest=inventory(home,api)
            remaining_names=sorted(rest['resources'])+['job:'+j['id'] for j in rest['jobs']]
            detail='Remaining inventory: '+', '.join(remaining_names)
        except Exception as inspect_error:
            detail='Remaining inventory could not be verified: '+str(inspect_error)
        raise ValueError('Removal incomplete: '+str(exc)+'. '+detail+'. Maintenance marker remains at '+str(marker)+'; rerun '+str(home/'.tingle-removal-recovery'/'tingle_remove.py')+' after resolving the reported blocker.') from exc
