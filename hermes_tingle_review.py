"""Content-free success receipts for document/chat review leads.

Emission is not review. A missing/failed model run keeps the exact lead eligible.
All operations share the source lock. Hashes are identifiers, not anonymization.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import timedelta
from pathlib import Path
from typing import Any

try:
    from . import hermes_tingle_store as store
    from .hermes_tingle_config import load_settings
except ImportError:
    import hermes_tingle_store as store
    from hermes_tingle_config import load_settings


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()


def binding(conn, kind, scope, *, settings=None):
    import hermes_constants
    store._require_source(conn,kind)
    settings = load_settings() if settings is None else settings
    generation=conn.execute("SELECT generation FROM hermes_tingle_source_optins WHERE kind=?",(kind,)).fetchone()[0]
    items = []
    for item_id in scope['item_ids']:
        row=conn.execute('SELECT * FROM hermes_tingle_items WHERE id=?',(item_id,)).fetchone()
        if row is None:
            raise ValueError('Review item missing')
        item=dict(row)
        for name,reader in [('steps',store._steps),('documents',store._documents),('requirements',store._requirements),('options',store._options),('bookings',store._bookings)]:
            item[name]=reader(conn,item_id)
        if item['status'] not in store._ACTIVE_STATUSES:
            raise ValueError('Review item is no longer active')
        items.append(item)
    excluded = None
    if kind == 'chat':
        row=conn.execute('SELECT * FROM chat_source_exclusions WHERE chat_ref=?',(scope['chat_ref'],)).fetchone()
        if row is not None:
            raise ValueError('Chat is excluded from source reviews')
        excluded=[dict(r) for r in conn.execute('SELECT chat_ref,created_at FROM chat_source_exclusions ORDER BY chat_ref')]
    discovery_binding = {}
    if scope.get('discovery_only'):
        try:
            from .hermes_tingle_discovery import owner_dm
        except ImportError:
            from hermes_tingle_discovery import owner_dm
        if not owner_dm(conn, scope['chat_ref'], settings=settings):
            raise ValueError('Discovery requires a verified in-scope owner DM')
        current_owner = conn.execute('SELECT owner_dm_principal FROM known_chats WHERE chat_ref=?',
                                     (scope['chat_ref'],)).fetchone()[0]
        pinned = scope.get('discovery_owner')
        if pinned is not None and str(current_owner).strip().casefold() != pinned:
            raise ValueError('Discovery owner changed during review; scan again')
        discovery_binding = {'discovery_only': True, 'chat_ref': scope['chat_ref'],
                             'owner_dm': current_owner}
    current = digest({**discovery_binding, 'profile':str(Path(hermes_constants.get_hermes_home()).resolve()),
                     'policy':settings,'generation':generation,
                     'items':items,'exclusions':excluded,
                     'approved_rules':[dict(row) for row in conn.execute(
                         "SELECT id,rule_text,category,approved_at,source_ref,decided_by FROM feedback_candidates WHERE status='approved' ORDER BY id")],
                     'rule_epoch':(conn.execute("SELECT value FROM plugin_meta WHERE key='feedback_review_epoch'").fetchone() or [None])[0]})
    expected_binding = scope.get('discovery_binding')
    if expected_binding is not None and expected_binding != current:
        raise ValueError('Discovery source or policy changed during review; scan again')
    return current


def schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS context_review_receipts (
        receipt TEXT PRIMARY KEY,kind TEXT NOT NULL,source_key TEXT NOT NULL,
        version TEXT NOT NULL,binding TEXT NOT NULL,scope TEXT NOT NULL,expires TEXT NOT NULL,
        UNIQUE(kind,source_key,version))''')


@store.serialized('sources')
def attach(kind, review, *, now=None, _conn=None):
    if kind not in ('document','chat'):
        raise ValueError('Invalid context review kind')
    stamp=store._now(now)
    conn=_conn if _conn is not None else store._connect()
    try:
        schema(conn)
        conn.execute('DELETE FROM context_review_receipts WHERE expires<=?',(stamp.isoformat(),))
        matches=[]
        suppressed=0
        for match in review['matches']:
            scope: dict[str, Any] = {'item_ids':sorted(set(match['item_ids']))}
            if kind=='chat':scope['chat_ref']=match['chat_ref']
            if match.get('discovery_only'):
                if kind != 'chat' or scope['item_ids']:
                    raise ValueError('Invalid discovery scope')
                try:
                    from .hermes_tingle_discovery import normalize_local_label
                except ImportError:
                    from hermes_tingle_discovery import normalize_local_label
                key = match.get('evaluation_key')
                if not conn.execute("SELECT 1 FROM discovery_evaluation_ledger WHERE key=? AND outcome='keep' AND expires_at>?",
                                    (key, stamp.isoformat())).fetchone():
                    raise ValueError('Validated private evaluation required')
                scope['evaluation_key'] = key
                scope.update(discovery_only=True, source_locator=match['source_locator'],
                             action_label=normalize_local_label(match['action_label']))
                if match.get('discovery_owner') is not None:
                    scope['discovery_owner'] = match['discovery_owner']
                if match.get('discovery_binding') is not None:
                    scope['discovery_binding'] = match['discovery_binding']
            current=binding(conn,kind,scope)
            source_key=digest({'file_id':match['file_id']} if kind=='document' else {
                'chat_ref':match['chat_ref'],'timestamp':match['timestamp'],'role':match['role']})
            if scope.get('discovery_only'):
                source_key=digest({'discovery_only':True,'source_locator':match['source_locator']})
            version=(digest({'evaluation_key': match['evaluation_key'], 'binding': current})
                     if scope.get('discovery_only') else digest({'lead':match,'binding':current}))
            if conn.execute('SELECT 1 FROM source_seen WHERE source_type=? AND source_id=? AND source_version=?',
                            (kind,source_key,version)).fetchone():
                suppressed+=1
                continue
            row=conn.execute('SELECT receipt FROM context_review_receipts WHERE kind=? AND source_key=? AND version=?',
                             (kind,source_key,version)).fetchone()
            receipt=row[0] if row else 'CR-'+secrets.token_hex(16)
            if row and scope.get('discovery_only'):
                conn.execute('UPDATE context_review_receipts SET scope=?,expires=? WHERE receipt=?',
                             (json.dumps(scope), (stamp+timedelta(days=1)).isoformat(), receipt))
            if not row:
                conn.execute('INSERT INTO context_review_receipts VALUES(?,?,?,?,?,?,?)',
                             (receipt,kind,source_key,version,current,json.dumps(scope),
                              (stamp+timedelta(days=1)).isoformat()))
            matches.append({**match,'review_receipt':receipt})
        if _conn is None:
            conn.commit()
        return {**review,'matches':matches,'unchanged_reviewed':suppressed}
    finally:
        if _conn is None:
            conn.close()


@store.serialized('sources')
def attachment_seen(kind, match, *, now=None):
    """Content/binding/evaluator identity, never the generated action label."""
    if kind != 'chat' or not match.get('discovery_only'):
        return False
    try:
        from .hermes_tingle_discovery_assess import evaluated
    except ImportError:
        from hermes_tingle_discovery_assess import evaluated
    return evaluated(match, now=now)


@store.serialized('sources')
def acknowledge(receipt, *, now=None):
    if not isinstance(receipt,str) or len(receipt)!=35 or not receipt.startswith('CR-'):
        raise ValueError('Copy an exact context review receipt from the current snapshot')
    conn=store._connect()
    try:
        schema(conn)
        row=conn.execute('SELECT * FROM context_review_receipts WHERE receipt=?',(receipt,)).fetchone()
        if row is None or row['expires']<=store._now(now).isoformat():
            raise ValueError('Review receipt missing, consumed or expired; scan again')
        conn.execute('BEGIN IMMEDIATE')
        scope=json.loads(row['scope'])
        if scope.get('discovery_only'):
            raise ValueError('Discovery receipts must be staged locally, not acknowledged as update context')
        if binding(conn,row['kind'],scope)!=row['binding']:
            raise ValueError('Review profile, policy, item or consent changed; scan again')
        conn.execute('INSERT OR IGNORE INTO source_seen VALUES(?,?,?,?)',
                     (row['kind'],row['source_key'],row['version'],store._now(now).isoformat()))
        conn.execute('DELETE FROM context_review_receipts WHERE receipt=?',(receipt,))
        conn.commit()
        return True
    finally:
        conn.close()
