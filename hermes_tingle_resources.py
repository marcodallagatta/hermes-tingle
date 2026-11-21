"""Private profile-local creation provenance. Not a boundary against the OS user."""
from __future__ import annotations
import hashlib
import json
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

MANIFEST='.tingle-resources.json'
LOCK='.tingle-resources.lock'
MARKER='.tingle-removing'
NAMES=('hermes-tingle','hermes-tingle-lookback','hermes-tingle-body-audit')


def safe(path, home):
    path=Path(path)
    home=Path(home).resolve(strict=True)
    relative=path.relative_to(home)
    current=home
    for part in relative.parts:
        current=current/part
        if current.is_symlink():raise ValueError('Resource symlink refused')
    return path


def digest(data):return hashlib.sha256(data).hexdigest()


def active(home):
    if (Path(home)/MARKER).exists() or (Path(home)/MARKER).is_symlink():
        raise ValueError('Tingle removal/maintenance is active; no Tingle operation allowed. Finish or explicitly resume local removal.')


def read(home):
    home=Path(home).resolve(strict=True)
    path=safe(home/MANIFEST,home)
    if not path.exists():return {'version':1,'home':str(home),'files':{},'directories':{},'jobs':{}}
    data=json.loads(path.read_text())
    if data.get('version')!=1 or data.get('home')!=str(home) or any(not isinstance(data.get(k),dict) for k in ('files','directories','jobs')):
        raise ValueError('Invalid/cross-profile Tingle resource manifest')
    return data


# A single process/thread-aware lifetime fence, always acquired before wizard,
# Cron, source-operation or SQLite locks. Nested provenance writes reuse it.
import threading
_local=threading.local()
_process_locks={}
_process_locks_guard=threading.Lock()
_process_pid=os.getpid()
_mutation_epochs={}


@contextmanager
def locked(home):
    import fcntl
    home=Path(home).resolve(strict=True)
    global _process_pid, _process_locks, _process_locks_guard
    if _process_pid != os.getpid():
        _process_locks={};_process_locks_guard=threading.Lock();_process_pid=os.getpid()
    with _process_locks_guard:
        process_lock=_process_locks.setdefault(home,threading.RLock())
    process_lock.acquire()
    try:
        if getattr(_local,'pid',None)!=os.getpid():
            _local.held={};_local.pid=os.getpid()
        held=getattr(_local,'held',{})
        if home in held:
            held[home]+=1
            try:yield
            finally:held[home]-=1
            return
        path=safe(home/LOCK,home)
        fd=os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:raise ValueError('Invalid ownership lock')
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            held[home]=1;_local.held=held
            try:yield
            finally:held.pop(home)
        finally:os.close(fd)
    finally:process_lock.release()


@contextmanager
def mutation(home):
    home=Path(home).resolve(strict=True)
    epoch=_mutation_epochs.get(home,0)
    active(home)
    with locked(home):
        active(home)
        if _mutation_epochs.get(home,0)!=epoch:raise ValueError('Tingle maintenance occurred while waiting; restart the operation')
        yield


def write(home,data):
    home=Path(home).resolve(strict=True)
    safe(home/MANIFEST,home)
    fd,name=tempfile.mkstemp(prefix='.tingle-resources-',dir=home)
    try:
        with os.fdopen(fd,'w') as out:
            json.dump(data,out,sort_keys=True);out.flush();os.fsync(out.fileno())
        os.replace(name,home/MANIFEST)
    finally:Path(name).unlink(missing_ok=True)


def record(path, *, home, directory=False):
    home=Path(home).resolve(strict=True)
    path=safe(Path(path),home)
    info=path.stat()
    if directory:
        if not stat.S_ISDIR(info.st_mode):raise ValueError('Owned namespace must be a directory')
        value={'device':info.st_dev,'inode':info.st_ino,'origin':'created'}
    else:
        if not stat.S_ISREG(info.st_mode):raise ValueError('Owned file must be regular')
        value={'device':info.st_dev,'inode':info.st_ino,'sha256':digest(path.read_bytes()),'origin':'created'}
    with locked(home):
        data=read(home);data['directories' if directory else 'files'][str(path.relative_to(home))]=value;write(home,data)


def job_definition(job):
    # Scheduler counters/claims/next-run values legitimately change, definition does not.
    keys=('id','name','prompt','script','schedule','timezone','deliver','failure_deliver','no_agent','toolsets','enabled_toolsets','catch_up','skills','skill','model','provider',
          'base_url','origin','context_from','workdir','reasoning_effort','pinned','interpreter',
          'attach_to_session','monitor_script','monitor_url','grace_minutes')
    return {key:job.get(key) for key in keys}


def record_job(home,job):
    with locked(home):
        data=read(home);data['jobs'][job['id']]={'origin':'created','definition':job_definition(job)};write(home,data)
