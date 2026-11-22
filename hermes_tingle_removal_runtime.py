"""Read-only Cron inventory for the operator-local Tingle remover.

This adapter never pauses or removes Hermes jobs. The operator must stop writers
and remove any listed job with Hermes' native Cron command before Tingle data or
the plugin is removed; Hermes owns the matching output/notepad cleanup. Reads are
profile-scoped, non-scaffolding, and reject ambiguous or unsafe job stores.
"""
from __future__ import annotations
import json
import os
import stat
from pathlib import Path
try:
    from . import hermes_tingle_resources as resources
except ImportError:
    import hermes_tingle_resources as resources


def _reject_constant(value):
    raise ValueError('non-standard JSON constant in Cron database: '+str(value))


def _unique_json_object(pairs):
    """Reject ambiguous Cron records rather than presenting a lossy inventory."""
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('duplicate JSON key in Cron database')
        result[key]=value
    return result


def runtime(home):
    from gateway.status import live_gateway_pid_for_home

    home=Path(home).resolve(strict=True)
    jobs_path=resources.safe(home/'cron'/'jobs.json',home)

    def listing():
        resources.safe(jobs_path,home)
        if not jobs_path.exists() and not jobs_path.is_symlink():return []
        info=jobs_path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:
            raise ValueError('Unverifiable Cron database')
        fd=os.open(jobs_path,os.O_RDONLY|os.O_NOFOLLOW)
        try:
            opened=os.fstat(fd)
            if (opened.st_dev,opened.st_ino,opened.st_nlink)!=(info.st_dev,info.st_ino,info.st_nlink):
                raise ValueError('Cron database changed during inspection')
            with os.fdopen(fd,'rb',closefd=False) as handle:raw=handle.read()
            current=jobs_path.lstat()
            if (current.st_dev,current.st_ino,current.st_nlink)!=(info.st_dev,info.st_ino,info.st_nlink):
                raise ValueError('Cron database path changed during inspection')
        finally:os.close(fd)
        data=json.loads(raw.decode('utf-8-sig'),object_pairs_hook=_unique_json_object,parse_constant=_reject_constant)
        if not isinstance(data,dict) or not isinstance(data.get('jobs'),list):
            raise ValueError('Malformed Cron database; no automatic repair during removal')
        jobs=data['jobs']
        if any(not isinstance(job,dict) or not isinstance(job.get('id'),str)
               or not isinstance(job.get('name'),str) or not isinstance(job.get('script'),(str,type(None)))
               for job in jobs):
            raise ValueError('Malformed Cron records')
        ids=[job['id'] for job in jobs]
        if len(ids)!=len(set(ids)):raise ValueError('Duplicate Cron IDs')
        return [dict(job) for job in jobs]

    def get(job_id):
        if not isinstance(job_id,str) or not job_id:raise ValueError('Invalid Cron ID')
        return next((job for job in listing() if job['id']==job_id),None)

    def metadata_absent():
        # Read only the manager's own install ledger, never edit it here.
        from hermes_cli.plugins_cmd_git import _read_install_metadata
        from hermes_constants import get_hermes_home
        if Path(get_hermes_home()).resolve()!=home:raise ValueError('Native manager profile changed')
        return 'hermes-tingle' not in _read_install_metadata()

    return {'list':listing,'get':get,'gateway':live_gateway_pid_for_home,
            'metadata_absent':metadata_absent}
