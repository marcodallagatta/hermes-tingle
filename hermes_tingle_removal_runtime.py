"""Pinned Hermes local removal adapter. Read-only inventory does not scaffold state.

The runtime has no plugin uninstall hook and no strict public Cron CAS-removal
API. This adapter uses narrow verified private locking/normalization seams. It
fails rather than using Cron's normal cross-process-lock fallback.
"""
from __future__ import annotations
import json
import os
from contextlib import contextmanager
from pathlib import Path
try:
    from . import hermes_tingle_resources as resources
except ImportError:
    import hermes_tingle_resources as resources


def runtime(home):
    from cron import jobs
    from gateway.status import live_gateway_pid_for_home
    home=Path(home).resolve(strict=True)
    def listing():
        with jobs.use_cron_store(home):
            path=resources.safe(jobs._current_cron_store().jobs_file,home)
            if not path.exists():return []
            data=json.loads(path.read_text(encoding='utf-8-sig'))
            if not isinstance(data,dict) or not isinstance(data.get('jobs'),list):
                raise ValueError('Malformed Cron database; no automatic repair during removal')
            if any(not isinstance(j,dict) or not isinstance(j.get('id'),str) for j in data['jobs']):
                raise ValueError('Malformed Cron records')
            return [jobs._normalize_job_record(j) for j in data['jobs']]
    def get(job_id):return next((j for j in listing() if j['id']==job_id),None)
    def scoped(fn,job_id):
        with jobs.use_cron_store(home):return fn(job_id)
    @contextmanager
    def transaction():
        import fcntl
        with jobs.use_cron_store(home),jobs._jobs_file_lock:
            if getattr(jobs._jobs_lock_state,'depth',0):raise ValueError('Unexpected nested Cron transaction')
            path=resources.safe(jobs._jobs_lock_file(),home)
            fd=os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
            try:
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                # Native public pause/remove then reuse this same verified strict
                # lock through their documented-in-source nesting convention.
                jobs._jobs_lock_state.depth=1
                jobs._jobs_lock_state.load_stamp=None
                try:yield
                finally:
                    jobs._jobs_lock_state.depth=0
                    jobs._jobs_lock_state.load_stamp=None
            finally:os.close(fd)
    def metadata_absent():
        # Read only the manager's own install ledger, never edit it here.
        from hermes_cli.plugins_cmd_git import _read_install_metadata
        from hermes_constants import get_hermes_home
        if Path(get_hermes_home()).resolve()!=home:raise ValueError('Native manager profile changed')
        return 'hermes-tingle' not in _read_install_metadata()
    return {'list':listing,'get':get,'pause':lambda job_id:scoped(jobs.pause_job,job_id),
            'remove':lambda job_id:scoped(jobs.remove_job,job_id),'gateway':live_gateway_pid_for_home,
            'transaction':transaction,'metadata_absent':metadata_absent}
