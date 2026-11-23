#!/usr/bin/env python3
"""Remove Tingle-owned profile resources; plugin uninstall is a separate Hermes command."""
from __future__ import annotations
import argparse
import json
import os
import shlex
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from hermes_tingle_removal import inventory,remove


def _run(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('-p','--profile',required=True,help='Explicit existing Hermes profile name; use default for the default profile')
    parser.add_argument('--keep-data',action='store_true',help='Retain Tingle data/config/backups; Cron jobs must be removed manually first; plugin uninstall is a separate Hermes command')
    parser.add_argument('--plan',action='store_true',help='Inventory only, no changes')
    args=parser.parse_args(argv)
    from hermes_cli.profiles import get_profile_dir
    home=get_profile_dir(args.profile).resolve(strict=True)
    os.environ['HERMES_HOME']=str(home)
    plan=inventory(home)
    kinds={}
    adopt=0
    for entry in plan['resources'].values():
        kinds[entry['kind']]=kinds.get(entry['kind'],0)+1
        adopt+=entry['ownership']=='needs explicit adoption'
    print('🧹 Tingle removal check: '+(', '.join(f'{n} {k}' for k,n in sorted(kinds.items())) or 'nothing found')+'.')
    if adopt:print(f'⚠️ {adopt} item(s) need your ownership decision before they can be removed.')
    print('Exact inventory this will act on:')
    print(json.dumps(plan,ensure_ascii=False,indent=2,sort_keys=True))
    print('Cron jobs are never changed by this Tingle remover; remove any listed jobs through Hermes after stopping writers, then rerun this inventory.')
    plugin_command='hermes -p '+shlex.quote(args.profile)+' plugins remove hermes-tingle'
    if plan['plugin_installed'] or plan['manager_record_present']:
        print('After Tingle-owned data cleanup, remove the plugin separately with: '+plugin_command)
    if plan['jobs']:
        ids=', '.join(job['id'] for job in plan['jobs'])
        print('Tingle Cron jobs still require manual Hermes cleanup. Exact IDs: '+ids)
        print('If an ID disappears or Hermes does not confirm that target, stop and rerun --plan; do not reuse a stale ID.')
        if args.plan:return 0
        print('No Tingle resources were changed. Stop writers, remove those jobs through Hermes, then rerun --plan.',file=sys.stderr)
        return 1
    if args.plan:return 0
    print('DELETE is the default. Hermes history, unrelated backups/accounts/tokens and provider copies are not erased.')
    print('KEEP DATA explicitly selected.' if args.keep_data else 'All listed Tingle data/config/backups will be deleted.')
    stopped=input('Confirm no other Tingle/Hermes writers are running (the gateway may stay up). Type ALL WRITERS STOPPED: ').strip()=='ALL WRITERS STOPPED'
    needs_adoption=any(r['ownership']=='needs explicit adoption' and not (args.keep_data and r['kind'] in ('data','metadata')) for r in plan['resources'].values())
    adopted=False
    if needs_adoption:
        adopted=input('Unproven/changed resources above require your ownership decision. Type ADOPT LEGACY '+plan['token']+': ').strip()=='ADOPT LEGACY '+plan['token']
    confirmation=input('Type REMOVE '+plan['token']+' to apply this exact inventory; anything else cancels: ').strip()
    if not stopped or confirmation!='REMOVE '+plan['token'] or (needs_adoption and not adopted):
        print('Cancelled; nothing changed.');return 0
    try:
        result=remove(home,plan,confirmation=confirmation,writers_stopped=stopped,adopt_legacy=adopted,keep_data=args.keep_data)
    except Exception as exc:
        print(str(exc),file=sys.stderr);return 1
    print('Tingle cleanup complete. The plugin install and manager record were not changed.')
    print(json.dumps(result,sort_keys=True))
    if result.get('gateway_restart_required'):
        print('The gateway was left running. Restart it from a separate terminal with `hermes gateway restart`, then start a new session. Do not restart from the active chat.')
    if result['plugin_uninstall_required']:
        print('Run this explicit Hermes command to remove the plugin: '+plugin_command)
    return 0


def main(argv=None):
    try:return _run(argv)
    except (EOFError,KeyboardInterrupt):
        print('Cancelled before removal confirmation; no cleanup applied.');return 0
    except (ValueError,OSError,ImportError) as exc:
        print('Removal not verified: '+str(exc),file=sys.stderr);return 1


if __name__=='__main__':raise SystemExit(main())
