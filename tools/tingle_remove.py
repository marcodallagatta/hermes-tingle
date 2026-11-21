#!/usr/bin/env python3
"""Remove Tingle's own resources, then its managed install. Local operator only."""
from __future__ import annotations
import argparse
import json
import os
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from hermes_tingle_removal import inventory,remove


def _run(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('-p','--profile',help='Existing Hermes profile name; otherwise current HERMES_HOME/default')
    parser.add_argument('--keep-data',action='store_true',help='Explicitly keep Tingle data/config/backups; jobs/helpers/install are still removed')
    parser.add_argument('--plan',action='store_true',help='Inventory only, no changes')
    args=parser.parse_args(argv)
    if args.profile:
        from hermes_cli.profiles import get_profile_dir
        home=get_profile_dir(args.profile).resolve(strict=True)
        os.environ['HERMES_HOME']=str(home)
    else:
        from hermes_constants import get_hermes_home
        home=Path(get_hermes_home()).resolve(strict=True)
    plan=inventory(home)
    kinds={}
    adopt=0
    for entry in plan['resources'].values():
        kinds[entry['kind']]=kinds.get(entry['kind'],0)+1
        adopt+=entry['ownership']=='needs explicit adoption'
    print('🧹 Tingle removal check: '+(', '.join(f'{n} {k}' for k,n in sorted(kinds.items())) or 'nothing found')+'.')
    adopt+=sum(j['ownership']=='needs explicit adoption' for j in plan['jobs'])
    if adopt:print(f'⚠️ {adopt} item(s) need your ownership decision before they can be removed.')
    print('Exact inventory this will act on:')
    print(json.dumps(plan,ensure_ascii=False,indent=2,sort_keys=True))
    print('Native Hermes remove has no Tingle cleanup hook on the pinned runtime. Use this remover first.')
    print('DELETE is the default. Hermes history, unrelated backups/accounts/tokens and provider copies are not erased.')
    if args.plan:return 0
    print('KEEP DATA explicitly selected.' if args.keep_data else 'All listed Tingle data/config/backups will be deleted.')
    stopped=input('Stop all Tingle/Hermes writers yourself. Type ALL WRITERS STOPPED: ').strip()=='ALL WRITERS STOPPED'
    needs_adoption=any(r['ownership']=='needs explicit adoption' and not (args.keep_data and r['kind'] in ('data','metadata')) for r in plan['resources'].values()) or any(j['ownership']=='needs explicit adoption' for j in plan['jobs'])
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
    print(json.dumps(result,sort_keys=True));return 0


def main(argv=None):
    try:return _run(argv)
    except (EOFError,KeyboardInterrupt):
        print('Cancelled before removal confirmation; no cleanup applied.');return 0
    except (ValueError,OSError,ImportError) as exc:
        print('Removal not verified: '+str(exc),file=sys.stderr);return 1


if __name__=='__main__':raise SystemExit(main())
