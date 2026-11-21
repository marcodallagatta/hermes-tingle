"""Contextual proposals only; actual merge/undo is an authenticated owner-DM preview.

No source/model calls. Original records and references survive a merge. A contextual
model suggestion is not identity proof or consent. Distinct obligations stay separate.
"""
import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta
try:
    from . import hermes_tingle_store as store
    from .hermes_tingle_config import load_settings, local_timezone
except ImportError:
    import hermes_tingle_store as store
    from hermes_tingle_config import load_settings, local_timezone

MAX_MEMBERS = 5
INSTRUCTIONS = (
    'Consider whether supplied active items describe the SAME current awaited outcome, not merely the same '
    'topic, provider, participant or similar title. Use available chronological context and current actors/steps. '
    'Distinct obligations and dependencies remain separate even within one case. If genuinely supported, use '
    'propose_case_merge with existing item_ids, canonical_id and a short explanation of the shared current outcome. '
    'This only proposes; never update/dismiss/complete other items to simulate merging. Proactively ask the owner '
    'in plain language whether to merge them into one reminder. Scheduled review output lands in a shared topic: '
    'never put item titles, IDs, reasons or the review command there - ask generally and keep details private. '
    'If the owner answers yes in natural language, call open_case_merge_review (with the proposal_id you received, '
    'if any); it opens the authenticated paginated preview only in a verified owner DM and fails closed everywhere '
    'else. If the interactive review cannot open on a tool turn, the call changes nothing, keeps the proposal pending '
    'and names the typed /tingle merge command for the private chat; never claim the preview was shown when it was '
    'not; the yes to the question never confirms the merge itself, which only the owner can do inside that '
    'preview. If the owner clearly answers no in natural language, record it '
    'with decline_case_merge so the group is not proposed again; an ambiguous answer is not a decline. Every '
    'source and historical step remains reachable; one existing '
    'canonical check is retained, not a new deadline. Do not infer completion from silence. Recognition is '
    'bounded to supplied context, not global deduplication.'
)


def _binding(conn=None):
    if conn is None:
        conn=store._connect()
        try: return _binding(conn)
        finally: conn.close()
    grant=conn.execute("SELECT enabled,generation FROM hermes_tingle_source_optins WHERE kind='email'").fetchone()
    return json.dumps({'policy':load_settings(), 'email_grant':list(grant) if grant else None},sort_keys=True)


def _fingerprint(rows):
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()


def _short_check(value):
    """Compact owner-DM check time: 3 Dec, with a year only when it differs."""
    if not value: return 'not scheduled'
    try:
        zone=local_timezone()
        dt=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        if dt.tzinfo is None: dt=dt.replace(tzinfo=zone)
        dt=dt.astimezone(zone)
        return dt.strftime('%-d %b')+('' if dt.year==datetime.now(zone).year else ' '+str(dt.year))
    except (TypeError,ValueError):
        return 'date unavailable'


def _rows(conn, ids):
    out=[]
    for iid in ids:
        row=conn.execute('SELECT * FROM hermes_tingle_items WHERE id=?',(iid,)).fetchone()
        if row is None: raise ValueError('An item is unavailable; nothing merged.')
        record=dict(row)
        record['_relations']={table:[dict(v) for v in conn.execute('SELECT * FROM '+table+' WHERE item_id=? ORDER BY position',(iid,))]
            for table in ('hermes_tingle_documents','hermes_tingle_requirements','hermes_tingle_options','hermes_tingle_bookings')}
        record['_relations']['steps']=[dict(v) for v in conn.execute('SELECT * FROM hermes_tingle_steps WHERE goal_id=? OR wait_item_id=? ORDER BY goal_id,position',(iid,iid))]
        out.append(record)
    return out


def members(conn, canonical):
    ids=[r[0] for r in conn.execute('SELECT item_id FROM case_merge_members WHERE canonical_id=? ORDER BY item_id',(canonical,))]
    return _rows(conn, ids) if ids else []


def _eligible(conn, rows):
    try:
        from .hermes_tingle import _excluded_work
    except ImportError:
        from hermes_tingle import _excluded_work
    owners={r['owner'] for r in rows}
    if len(owners)!=1 or 'unknown' in owners:
        raise ValueError('Different or unknown next actors need separate review; nothing merged.')
    emails=[r for r in rows if r['source_type']=='email']
    if emails and (len(emails)!=len(rows) or any(r['completion_signal']!='source_email_checkpoint' for r in rows)
                   or len({r['source_ref'].split('/')[0] for r in rows})!=1
                   or any(not re.fullmatch(r'gmail:[a-f0-9]{64}/[^#]+#[^#]+',r['source_ref']) for r in rows)):
        raise ValueError('Merge needs checkpoints from one verified account; reply watches or mixed sources stay separate.')
    if not emails and any(r['source_type'] not in {'conversation','manual'} or r['completion_signal'] for r in rows):
        raise ValueError('This source completion rule needs separate review; nothing merged.')
    if emails: store._require_source(conn,'email')
    for r in rows:
        iid=r['id']
        if r.get('reminder_budget_uncertain'): raise ValueError('Resolve uncertain reminder budgets before merging.')
        if r['status'] not in store._ACTIVE_STATUSES or r['due_at'] or r['recur_interval'] or r['hard_deadline'] or r.get('watch_ref'):
            raise ValueError('Only active non-recurring items without deadlines or linked watches can merge; nothing changed.')
        if _excluded_work(r): raise ValueError('An item is excluded by current policy; nothing merged.')
        if conn.execute('SELECT 1 FROM case_merge_members WHERE item_id=?',(iid,)).fetchone():
            raise ValueError('An item already belongs to a merge; undo or review it separately.')
        if conn.execute('SELECT 1 FROM hermes_tingle_steps WHERE goal_id=? OR wait_item_id=?',(iid,iid)).fetchone():
            raise ValueError('Goal/dependency items need separate steps; nothing merged.')
        for table in ('hermes_tingle_documents','hermes_tingle_requirements','hermes_tingle_options','hermes_tingle_bookings'):
            if conn.execute('SELECT 1 FROM '+table+' WHERE item_id=?',(iid,)).fetchone():
                raise ValueError('Structured steps or records need separate review; nothing merged.')


@store.serialized('reminders')
def propose(item_ids, canonical_id, reason, *, principal='model-proposal', now=None):
    if not isinstance(item_ids,list) or not 2<=len(item_ids)<=MAX_MEMBERS or len(set(item_ids))!=len(item_ids):
        raise ValueError('Choose two to five distinct existing items for one shared outcome.')
    ids=[store.resolve_item_id(i) for i in item_ids]
    canonical=store.resolve_item_id(canonical_id)
    if len(set(ids))!=len(ids): raise ValueError('Choose distinct item IDs.')
    if canonical not in ids: raise ValueError('Choose one supplied item to retain as the shared follow-up.')
    if not isinstance(reason,str) or not 1<=len(reason.strip())<=240:
        raise ValueError('Explain the shared current outcome briefly, without source quotes.')
    stamp=store._now(now)
    conn=store._connect()
    try:
        conn.execute('DELETE FROM case_merge_proposals WHERE state IN (\'pending\',\'declined\') AND expires<=?',(stamp.isoformat(),))
        rows=_rows(conn,ids); _eligible(conn,rows)
        policy=_binding(conn)
        existing=conn.execute('SELECT id,payload,state FROM case_merge_proposals WHERE state IN (\'pending\',\'declined\') AND policy=? AND expires>?',(policy,stamp.isoformat())).fetchall()
        for r in existing:
            data=json.loads(r['payload'])
            old_rows=_rows(conn,data['ids'])
            if set(data['ids'])==set(ids) and data['fingerprint']==_fingerprint(old_rows):
                return {'proposal_id':r['id'],'review_command':None if r['state']=='declined' else '/tingle merge '+r['id'], 'note':'Owner declined; do not repeat this suggestion.' if r['state']=='declined' else 'Existing proposal; no item changed.'}
        if conn.execute('SELECT count(*) FROM case_merge_proposals WHERE state=\'pending\'').fetchone()[0]>=20:
            raise ValueError('Review existing merge proposals first; nothing changed.')
        mid='CM-'+secrets.token_hex(4).upper()
        payload={'ids':ids,'canonical':canonical,'reason':reason.strip(),'fingerprint':_fingerprint(rows)}
        conn.execute('INSERT INTO case_merge_proposals VALUES(?,?,?,?,?,?,?)',(mid,'pending',stamp.isoformat(),(stamp+timedelta(days=1)).isoformat(),policy,principal,json.dumps(payload)))
        conn.commit()
        return {'proposal_id':mid,'review_command':'/tingle merge '+mid,'note':'Proposal only, nothing merged. Owner review required; expires in one day.'}
    finally:conn.close()


def resolve_pending(mid, *, now=None):
    """Resolve which pending proposal a natural-language yes refers to.

    Returns the proposal ID, or a dict listing pending proposals when the owner must
    pick. Raises ValueError when nothing reviewable exists. Read-only: nothing is
    shown, opened, declined or merged here.
    """
    stamp=store._now(now)
    conn=store._connect()
    try:
        conn.execute("DELETE FROM case_merge_proposals WHERE state IN ('pending','declined') AND expires<=?",(stamp.isoformat(),))
        policy=_binding(conn)
        rows=conn.execute("SELECT id,payload FROM case_merge_proposals WHERE state='pending' AND policy=? AND expires>? ORDER BY created",(policy,stamp.isoformat())).fetchall()
        if mid:
            if not re.fullmatch(r'CM-[A-F0-9]{8}',mid): raise ValueError('Name one pending proposal as CM-ABCDEF12.')
            if not any(r['id']==mid for r in rows): raise ValueError('That merge proposal is unavailable or expired; nothing changed.')
            return mid
        if not rows: raise ValueError('No pending merge proposal to review.')
        if len(rows)==1: return rows[0]['id']
        return {'proposals':[{'proposal_id':r['id'],'reason':json.loads(r['payload'])['reason']} for r in rows]}
    finally:conn.close()


@store.serialized('reminders')
def decline(mid, *, now=None):
    """Record a clear owner no in natural language; engages the existing declined-group suppression.

    Proposal-only state change: no item, check, budget or source is touched. Only a pending
    proposal under the unchanged policy binding can be declined; anything else fails closed.
    """
    if not isinstance(mid,str) or not re.fullmatch(r'CM-[A-F0-9]{8}',mid):
        raise ValueError('Name one pending proposal as CM-ABCDEF12; nothing changed.')
    conn=store._connect()
    try:
        conn.execute('BEGIN IMMEDIATE')
        row,_p=_proposal(conn,mid,'pending',now=now)
        conn.execute("UPDATE case_merge_proposals SET state='declined' WHERE id=? AND state='pending'",(row['id'],))
        conn.commit()
        return {'proposal_id':row['id'],'state':'declined','note':'Owner declined; do not repeat this suggestion. No item changed.'}
    finally:conn.close()


def _proposal(conn, mid, state, *, now=None):
    row=conn.execute('SELECT * FROM case_merge_proposals WHERE id=?',(mid,)).fetchone()
    if not row or row['state']!=state or (state=='pending' and row['expires']<=store._now(now).isoformat()):
        raise ValueError('That merge is unavailable or expired; nothing changed.')
    if row['policy']!=_binding(conn): raise ValueError('Profile, policy or source permission changed; review a fresh proposal.')
    return row,json.loads(row['payload'])


def preview(mid, *, undo=False):
    conn=store._connect()
    try:
        row,p=_proposal(conn,mid,'applied' if undo else 'pending')
        rows=_rows(conn,p['ids'])
        if undo:
            if _fingerprint(rows)!=p['after']: raise ValueError('Items changed after merging; undo would overwrite work. Review them separately.')
        else:
            _eligible(conn,rows)
            if _fingerprint(rows)!=p['fingerprint']: raise ValueError('Items changed; review a fresh proposal before merging.')
        return dict(p,rows=rows,token=_fingerprint(rows))
    finally:conn.close()


@store.serialized('reminders')
def commit(mid, previewed, principal, *, undo=False):
    conn=store._connect()
    try:
        conn.execute('BEGIN IMMEDIATE')
        row,p=_proposal(conn,mid,'applied' if undo else 'pending')
        rows=_rows(conn,p['ids'])
        if undo and any(r['source_type']=='email' for r in rows): store._require_source(conn,'email')
        expected=p['after'] if undo else p['fingerprint']
        if _fingerprint(rows)!=expected or previewed!=expected:
            raise ValueError('Items changed after preview; nothing changed. Review again.')
        stamp=store._now().isoformat()
        if undo:
            for r in rows:
                before=p['before'][r['id']]
                conn.execute('UPDATE hermes_tingle_items SET status=?,next_check_at=?,updated_at=?,reminder_revision=reminder_revision+1 WHERE id=?',
                             (before['status'],before['next_check_at'],stamp,r['id']))
            conn.execute('DELETE FROM case_merge_members WHERE merge_id=?',(mid,))
            conn.execute('UPDATE case_merge_proposals SET state=\'undone\',principal=? WHERE id=?',(principal,mid))
        else:
            _eligible(conn,rows)
            p['before']={r['id']:{'status':r['status'],'next_check_at':r['next_check_at']} for r in rows}
            for r in rows:
                if r['id']!=p['canonical']:
                    conn.execute('UPDATE hermes_tingle_items SET status=\'merged\',next_check_at=NULL,updated_at=?,reminder_revision=reminder_revision+1 WHERE id=?',(stamp,r['id']))
                else:
                    conn.execute('UPDATE hermes_tingle_items SET updated_at=?,reminder_revision=reminder_revision+1 WHERE id=?',(stamp,r['id']))
                conn.execute('INSERT INTO case_merge_members VALUES(?,?,?)',(r['id'],p['canonical'],mid))
            p['after']=_fingerprint(_rows(conn,p['ids']))
            conn.execute('UPDATE case_merge_proposals SET state=\'applied\',principal=?,payload=? WHERE id=?',(principal,json.dumps(p),mid))
        conn.commit()
        return p['canonical']
    finally:conn.close()


def handle(ctx,source,mid,*,undo=False):
    try:
        from .hermes_tingle import _source_sender_error,_deciding_principal,_format_local_time
    except ImportError:
        from hermes_tingle import _source_sender_error,_deciding_principal,_format_local_time
    refusal=_source_sender_error(source,noun='Case merge')
    if refusal: return refusal
    if not re.fullmatch(r'CM-[A-F0-9]{8}',mid):return 'Review one proposal: /tingle merge CM-ABCDEF12'
    binding=(_deciding_principal(source),_binding())
    p=preview(mid,undo=undo)
    lines=['↩️ Undo this merge?' if undo else '🤝 Merge these into one reminder?',
           '💡 Why (proposed, not verified): '+p['reason'].replace('\n',' ')]
    for r in p['rows']:
        kept=' · kept' if r['id']==p['canonical'] else ''
        lines.append('• `'+r['id']+'` '+r['title']+' · check '+_short_check(r['next_check_at'])+kept)
    lines.append('↩️ Separate reminders and their checks return.' if undo else '✅ One reminder stays; all records kept.')
    try:
        from .hermes_tingle_lookback import _preview_pages
    except ImportError:
        from hermes_tingle_lookback import _preview_pages
    pages=_preview_pages('\n\n'.join(lines))
    for i,page in enumerate(pages):
        final=i==len(pages)-1
        value='undo' if undo else 'merge'
        answer=ctx.ask_choice(page,[{'value':value if final else 'next','label':('Undo merge' if undo else 'Merge these items') if final else 'Next page'},{'value':'cancel','label':'Keep unchanged'}])
        if answer=='cancel':
            # An explicit owner 'Keep unchanged' is the only answer that records a decline.
            if not undo and binding==(_deciding_principal(source),_binding()) and not _source_sender_error(source,noun='Case merge'):
                conn=store._connect()
                try:
                    conn.execute("UPDATE case_merge_proposals SET state='declined' WHERE id=? AND state='pending'",(mid,));conn.commit()
                finally:conn.close()
            return 'Kept unchanged; no items or checks changed.'
        if answer!=(value if final else 'next'):
            # No interactive chooser bound on this turn (for example a model tool call),
            # or the host returned an unexpected value. Neither is an owner choice:
            # never treat an unavailable or unrecognized answer as cancel or decline.
            if undo:
                return 'Interactive review could not continue in this conversation; nothing changed.'
            return ('Interactive review could not continue in this conversation; nothing changed and the proposal is still pending. '
                    'Review it with `/tingle merge '+mid+'` in your private chat with me.')
    if binding!=(_deciding_principal(source),_binding()) or _source_sender_error(source,noun='Case merge'):
        raise ValueError('Owner, profile or source permission changed during preview; nothing changed.')
    canonical=commit(mid,p['token'],str(binding[0]),undo=undo)
    return ('↩️ Restored the separate items; original checks may now be due.' if undo else '✅ Merged into `'+canonical+'`. One reminder, history kept. Undo: `/tingle undo merge '+mid+'`.')
