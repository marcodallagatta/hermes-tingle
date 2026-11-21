"""Bounded owner-DM discovery. Digests are identifiers, not anonymization."""
from __future__ import annotations

import json
import re
import secrets
import unicodedata
from datetime import timedelta
from types import SimpleNamespace

try:
    from . import hermes_tingle_store as store
    from .hermes_tingle_config import load_settings
    from . import hermes_tingle_review as review
except ImportError:
    import hermes_tingle_store as store
    from hermes_tingle_config import load_settings
    import hermes_tingle_review as review

MAX_EXCERPTS = 20
MAX_SUGGESTIONS = 5




_COMMITMENT_START = re.compile(
    r"^\s*(?:i\s+will|i'll|i’ll|i\s+should|i\s+need\s+to|i\s+have\s+to|i\s+must|i\s+plan\s+to|i\s+am\s+going\s+to|"
    r"we\s+will|we'll|we’ll|we\s+need\s+to|we\s+should)\s+(?P<action>.+)$", re.I)


def local_action_label(content):
    """Extract only explicit first-person commitment cues; not a semantic classifier."""
    if not isinstance(content, str) or not prefilter(content) or not (
            8 <= len(content) <= 4000 and len(content.split()) >= 3
            and not content.startswith(("'", '‘', '’'))
            and not re.search(r'[\n\r<>"“”:]|https?://|```|(?:^|\s)>|\b(?:forwarded|says|said|wrote|sagte|ignore instructions|system prompt)\b', content, re.I)):
        return None
    match = _COMMITMENT_START.match(unicodedata.normalize('NFKC', content))
    if not match:
        return None
    action = match.group('action')
    if re.search(r"\b(?:not|never|no\s+longer)\b", action, re.I):
        return None
    action = re.split(r"\b(?:once|when|after|before|if|unless|because|so that)\b", action, maxsplit=1, flags=re.I)[0]
    action = re.sub(r"\b(?:today|tomorrow|tonight|(?:this|next|last)\s+(?:week|month|year|monday|tuesday|wednesday|thursday|friday|saturday|sunday)|(?:on|by)\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b.*$", "", action, flags=re.I)
    if any(ch.isdigit() for ch in action) or re.search(r"https?://|@", action, re.I):
        return None
    parts = re.findall(r"[^\W\d_]+(?:[-'][^\W\d_]+)*", action, flags=re.UNICODE)
    if (not 2 <= len(parts) <= 12 or any(len(part) > 24 for part in parts) or
            any(part[0].isupper() for part in parts[1:])):
        return None
    label = ' '.join(parts).strip()
    if (len(label) > 120 or re.search(
            r"\b(?:ignore|instructions?|system|prompt|override|execute|approve|decline|accept|tingle|secret|password|token|i|we|my|our)\b",
            label, re.I)):
        return None
    label = label[0].upper() + label[1:]
    # Extraction and normalization must agree. Any label the extractor accepts
    # must survive normalize_local_label later, or a rejected cue could abort
    # the shared collection and discard title-token updates.
    try:
        normalize_local_label(label)
    except ValueError:
        return None
    return label



def normalize_local_label(label):
    error = 'Candidate label must be a safe minimized action'
    if not isinstance(label, str) or not label or len(label) > 80:
        raise ValueError(error)
    label = unicodedata.normalize('NFKC', label)
    if any(not (unicodedata.category(c)[0] in "LM" or c in " -'’") for c in label):
        raise ValueError(error)
    label = ' '.join(label.split())
    if (not 2 <= len(label) <= 80 or len(label.split()) > 12 or
            not any(c.isalpha() for c in label) or re.search(
            r'\b(?:ignore|instructions?|system|prompt|override|execute|approve|decline|accept|tingle|secret|password|token)\b', label, re.I)):
        raise ValueError(error)
    return label


@store.serialized('sources')
def stage(receipt, *, now=None, _conn=None):
    """Stage a validated private survivor and consume its receipt atomically."""
    if not isinstance(receipt, str) or not re.fullmatch(r'CR-[a-f0-9]{32}', receipt):
        raise ValueError('Copy an exact discovery receipt from the local snapshot')
    stamp = store._now(now)
    conn = _conn if _conn is not None else store._connect()
    try:
        review.schema(conn)
        if _conn is None:
            conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM context_review_receipts WHERE receipt=?', (receipt,)).fetchone()
        if row is None or row['expires'] <= stamp.isoformat():
            raise ValueError('Discovery receipt missing, consumed or expired; scan again')
        scope = json.loads(row['scope'])
        if row['kind'] != 'chat' or not scope.get('discovery_only'):
            raise ValueError('A discovery-only receipt is required')
        evaluation = conn.execute('SELECT outcome,expires_at FROM discovery_evaluation_ledger WHERE key=?',
                                  (scope.get('evaluation_key'),)).fetchone()
        if not evaluation or evaluation['outcome'] != 'keep' or evaluation['expires_at'] <= stamp.isoformat():
            raise ValueError('Validated private evaluation required')
        action_label = normalize_local_label(scope.get('action_label'))
        if review.binding(conn, 'chat', scope) != row['binding']:
            raise ValueError('Discovery policy or source permission changed; scan again')
        conn.execute('DELETE FROM hermes_tingle_chat_discovery_candidates WHERE expires_at<=?', (stamp.isoformat(),))
        conn.execute('''INSERT INTO hermes_tingle_chat_discovery_candidates
            (id,action_label,source_locator,source_version,binding,state,created_at,expires_at)
            VALUES(?,?,?,?,?,'pending',?,?)
            ON CONFLICT(source_locator,source_version,binding) DO NOTHING''',
            ('DC-' + secrets.token_hex(8).upper(), action_label, scope['source_locator'], row['version'],
             row['binding'], stamp.isoformat(), (stamp + timedelta(days=7)).isoformat()))
        conn.execute('INSERT OR IGNORE INTO source_seen VALUES(?,?,?,?)',
                     ('chat', row['source_key'], row['version'], stamp.isoformat()))
        conn.execute('DELETE FROM context_review_receipts WHERE receipt=?', (receipt,))
        if _conn is None:
            conn.commit()
    finally:
        if _conn is None:
            conn.close()


def stage_local_candidates(snapshot, *, now=None):
    """Private assessment OR explicitly unconfigured legacy lane; never failure fallback."""
    private = (snapshot.get('chat_review') or {}).get('discovery') or {}
    try:
        from . import hermes_tingle_discovery_assess as assess
    except ImportError:
        import hermes_tingle_discovery_assess as assess
    assessment = private.pop('_assessment', None)
    if assessment is not None:
        return assess.persist(assessment, private, now=now)
    legacy = private.pop('_legacy_assessment', None)
    # A legacy copied helper calls this directly and never sets either marker.
    # Accept only the old local-cue shape (no receipt/action label) and only while
    # evaluation is unconfigured; an attempted evaluation never falls back.
    if legacy is None and not private.get('_attempted') and not assess.configured():
        # The collector preserves local cues separately from the ordinary-text
        # excerpt selection; use that lane first so ordinary messages cannot
        # starve a later cue under the shared candidate cap. Old snapshots that
        # never had that lane fall back to explicit local-cue matches only.
        preserved = private.get('_legacy_matches')
        if isinstance(preserved, list) and preserved:
            legacy = [dict(lead) for lead in preserved if lead.get('_local_label')]
        else:
            legacy = [dict(lead) for lead in (private.get('matches') or [])
                      if lead.get('_local_label') and not lead.get('review_receipt')
                      and not lead.get('action_label')]
        if legacy:
            private['evaluation_status'] = 'not_configured'
    if legacy is not None and private.get('evaluation_status') == 'not_configured':
        return _stage_legacy(legacy, now=now)
    return 0  # Failed/budget-blocked evaluations NEVER enter the legacy lane.




@store.serialized('sources')
def _stage_legacy(leads, *, now=None):
    """Internal local-cue writer, not a model tool/receipt bypass."""
    try:
        from . import hermes_tingle_discovery_assess as assess
        from .hermes_tingle_sources import _case_item_excluded
    except ImportError:
        import hermes_tingle_discovery_assess as assess
        from hermes_tingle_sources import _case_item_excluded
    if assess.configured():
        raise ValueError('Discovery evaluation configuration changed')
    stamp = store._now(now)
    conn = store._connect()
    try:
        conn.execute('BEGIN IMMEDIATE')
        staged = 0
        for lead in leads[:MAX_SUGGESTIONS]:
            current = assess.revalidate(lead, conn=conn)
            label = normalize_local_label(lead['_local_label'])
            if _case_item_excluded({'title': label, 'source_ref': lead['source_locator']}):
                raise ValueError('Local discovery label excluded')
            key = review.digest({'discovery_only': True, 'source_locator': lead['source_locator']})
            version = review.digest({'legacy-cue-v1': lead['source_locator'], 'binding': current})
            if conn.execute('SELECT 1 FROM source_seen WHERE source_type=? AND source_id=? AND source_version=?',
                            ('chat', key, version)).fetchone():
                continue
            conn.execute("""INSERT INTO hermes_tingle_chat_discovery_candidates
                (id,action_label,source_locator,source_version,binding,state,created_at,expires_at)
                VALUES(?,?,?,?,?,'pending',?,?) ON CONFLICT(source_locator,source_version,binding) DO NOTHING""",
                ('DC-' + secrets.token_hex(8).upper(), label, lead['source_locator'], version,
                 current, stamp.isoformat(), (stamp + timedelta(days=7)).isoformat()))
            conn.execute('INSERT OR IGNORE INTO source_seen VALUES(?,?,?,?)', ('chat', key, version, stamp.isoformat()))
            staged += 1
        conn.commit()
        return staged
    finally:
        conn.close()



def _scope(locator):
    if not isinstance(locator, str) or not locator.startswith('chat-discovery:'):
        raise ValueError('Invalid discovery locator')
    chat_ref, _, identity = locator[len('chat-discovery:'):].rpartition(':')
    if not re.fullmatch(r'[a-f0-9]{64}', identity):
        raise ValueError('Invalid discovery locator')
    return {'item_ids': [], 'chat_ref': chat_ref, 'discovery_only': True}


def _private_sender_error(source):
    try:
        from .hermes_tingle import _source_sender_error
    except ImportError:
        from hermes_tingle import _source_sender_error
    return _source_sender_error(source, noun='Discovery review')


def cleanup(conn, *, now=None):
    current = store._now(now)
    stamp = current.isoformat()
    conn.execute('DELETE FROM discovery_evaluation_ledger WHERE expires_at<=?', (stamp,))
    conn.execute('DELETE FROM discovery_evaluation_claims WHERE expires_at<=?', (stamp,))
    conn.execute('DELETE FROM discovery_evaluation_reservations WHERE created_at<? AND month<>?',
                 ((current-timedelta(days=62)).isoformat(), current.strftime('%Y-%m')))
    conn.execute('DELETE FROM discovery_evaluation_spend WHERE request_id NOT IN '
                 '(SELECT id FROM discovery_evaluation_reservations)')
    conn.execute('DELETE FROM discovery_evaluation_prices WHERE request_id NOT IN '
                 '(SELECT id FROM discovery_evaluation_reservations)')
    conn.execute('DELETE FROM hermes_tingle_chat_discovery_candidates WHERE expires_at<=?', (stamp,))
    for row in conn.execute('SELECT id,action_label,source_locator,binding FROM hermes_tingle_chat_discovery_candidates').fetchall():
        try:
            valid = review.binding(conn, 'chat', _scope(row['source_locator'])) == row['binding']
            try:
                from .hermes_tingle import _excluded_work
            except ImportError:
                from hermes_tingle import _excluded_work
            valid = valid and not _excluded_work({'title': row['action_label'], 'source_ref': row['source_locator']})
        except Exception:
            valid = False
        if not valid:
            conn.execute('DELETE FROM hermes_tingle_chat_discovery_candidates WHERE id=?', (row['id'],))


def _candidate(conn, candidate_id):
    row = conn.execute("SELECT * FROM hermes_tingle_chat_discovery_candidates WHERE id=? AND state='pending'",
                       (candidate_id,)).fetchone()
    if row is None or row['expires_at'] <= store._now().isoformat():
        raise ValueError('Discovery candidate unavailable')
    if review.binding(conn, 'chat', _scope(row['source_locator'])) != row['binding']:
        raise ValueError('Discovery scope changed')
    try:
        from .hermes_tingle import _excluded_work
    except ImportError:
        from hermes_tingle import _excluded_work
    if _excluded_work({'title': row['action_label'], 'source_ref': row['source_locator']}):
        raise ValueError('Discovery item excluded by policy')
    return dict(row)


@store.serialized('sources')
def _preview(candidate_id=None):
    conn = store._connect()
    try:
        cleanup(conn)
        conn.commit()
        if candidate_id is None:
            row = conn.execute("SELECT id FROM hermes_tingle_chat_discovery_candidates WHERE state='pending' ORDER BY created_at,id LIMIT 1").fetchone()
            if row is None:
                return None
            candidate_id = row[0]
        return _candidate(conn, candidate_id)
    finally:
        conn.close()


@store.serialized('sources')
def _decide(source, preview, principal, answer):
    try:
        from .hermes_tingle import _deciding_principal
    except ImportError:
        from hermes_tingle import _deciding_principal
    if _private_sender_error(source) or _deciding_principal(source) != principal:
        raise ValueError('Owner changed during preview')
    conn = store._connect()
    try:
        conn.execute('BEGIN IMMEDIATE')
        current = _candidate(conn, preview['id'])
        if current != preview:
            raise ValueError('Discovery candidate changed after preview')
        item = None
        if answer == 'accept':
            item = store.add_item(title=current['action_label'], source_type='conversation',
                                  source_ref=current['source_locator'], due_at=None, status='open',
                                  owner='me', priority='normal', _conn=conn)
        conn.execute('UPDATE hermes_tingle_chat_discovery_candidates SET state=? WHERE id=?',
                     ('accepted' if answer == 'accept' else 'declined', current['id']))
        conn.commit()
        return item
    finally:
        conn.close()


def handle(ctx, source, args):
    """Authenticated slash-command only: model actions cannot accept or decline."""
    try:
        from .hermes_tingle import _deciding_principal
    except ImportError:
        from hermes_tingle import _deciding_principal
    refusal = _private_sender_error(source)
    if refusal:
        return refusal
    if len(args) > 1 or (args and not re.fullmatch(r'DC-[A-F0-9]{16}', args[0])):
        return 'Open `/tingle discover` in your owner DM to review one pending suggestion.'
    principal = _deciding_principal(source)
    candidate = _preview(args[0] if args else None)
    if candidate is None:
        return 'No pending discovery suggestions. Nothing changed.'
    text = ('Possible commitment (unverified suggestion, please verify): ' + candidate['action_label'] +
            '\nAccept creates an ordinary open item, owned by you, visible to everyone on this profile.'
            '\nNo due date is inferred. Normal priority; ordinary next check defaults to 7 days after approval.'
            '\nSource locator (identifier, not anonymization): ' + candidate['source_locator'] +
            '\nNo raw quote is retained or displayed. Pending suggestions expire after 7 days.'
            '\nAccept this exact action, decline the suggestion, or leave it pending?')
    try:
        answer = ctx.ask_choice(text, [{'value': 'accept', 'label': 'Accept and track'},
                                      {'value': 'decline', 'label': 'Decline suggestion'},
                                      {'value': 'cancel', 'label': 'Leave pending'}])
    except Exception:
        return 'Interactive review unavailable; nothing changed.'
    if answer not in ('accept', 'decline'):
        return 'No decision received; nothing changed.'
    item = _decide(source, candidate, principal, answer)
    return ('Added `' + item['id'] + '`. No due date inferred.' if item else
            'Declined suggestion; nothing added or changed in shared items.')


def observe_source(source):
    """Only runtime-authenticated source metadata can establish an owner DM."""
    try:
        from .hermes_tingle import _source_sender_error
    except ImportError:
        from hermes_tingle import _source_sender_error
    if isinstance(source, dict):
        source = SimpleNamespace(**source)
    platform = getattr(source, 'platform', '')
    platform = getattr(platform, 'value', platform)
    chat_id = getattr(source, 'chat_id', '')
    if not platform or not chat_id:
        return
    chat_ref = f'{platform}:{chat_id}'
    principal = None
    if _source_sender_error(source) is None:
        principal = f'{platform}:{source.user_id}'.casefold()
    conn = store._connect()
    try:
        conn.execute('UPDATE known_chats SET owner_dm_principal=? WHERE chat_ref=?', (principal, chat_ref))
        conn.commit()
    finally:
        conn.close()


def owner_dm(conn, chat_ref, *, settings=None):
    row = conn.execute('SELECT owner_dm_principal FROM known_chats WHERE chat_ref=?', (chat_ref,)).fetchone()
    settings = load_settings() if settings is None else settings
    owners = {str(p).strip().casefold() for p in settings.get('owner_principals', [])}
    return bool(row and row[0] and str(row[0]).strip().casefold() in owners and not conn.execute(
        'SELECT 1 FROM chat_source_exclusions WHERE chat_ref=?', (chat_ref,)).fetchone())


def eligible_chats():
    conn = store._connect()
    try:
        cleanup(conn)
        conn.commit()
        return {ref for ref in store.chats_in_scope() if owner_dm(conn, ref)}
    finally:
        conn.close()


def eligible_chat_bindings():
    """Pin each eligible owner DM's full review binding before history reads."""
    settings = load_settings()
    refs = store.chats_in_scope()
    conn = store._connect()
    try:
        cleanup(conn)
        conn.commit()
        conn.execute('BEGIN')
        snapshots = {}
        for ref in refs:
            if not owner_dm(conn, ref, settings=settings):
                continue
            row = conn.execute('SELECT owner_dm_principal FROM known_chats WHERE chat_ref=?', (ref,)).fetchone()
            if row is None or not row[0]:
                continue
            owner = str(row[0]).strip().casefold()
            scope = {'item_ids': [], 'chat_ref': ref, 'discovery_only': True,
                     'discovery_owner': owner}
            try:
                expected = review.binding(conn, 'chat', scope, settings=settings)
            except ValueError:
                continue
            snapshots[ref] = {'owner': owner, 'binding': expected}
        return snapshots
    finally:
        conn.close()


def prefilter(content):
    """Language-neutral shape/secret gate, NOT evidence of an outstanding action."""
    if not isinstance(content, str) or not content.strip() or len(content) > 4000:
        return False
    normalized = unicodedata.normalize('NFKC', content)
    return not (re.search(r'```|~~~|(?m:^[ ]{4}\S|^\s*(?:>|Forwarded message:|Begin forwarded|-{2,}\s*Forwarded))', normalized, re.I)
                or re.search(r'(?i)\b(?:password|passwd|passwort|mot de passe|api[_ -]?key|access[_ -]?token|client[_ -]?secret|authorization|otp|verification[_ -]?code)\s*(?:[:=]|\bis\b)|\bBearer\s+\S+|-----BEGIN .*PRIVATE KEY-----|\bsk-[A-Za-z0-9_-]{12,}|\bAKIA[A-Z0-9]{16}\b|\b(?:github_pat|ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{20,}|\bxox[baprs]-[A-Za-z0-9-]{10,}|\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}|\bAIza[0-9A-Za-z_-]{35}', normalized)
                or any(unicodedata.category(c) == 'Cc' and c not in '\n\r\t' for c in content))


def lead(chat_ref, session_id, timestamp, content, pinned_owner=None,
         pinned_binding=None):
    if not prefilter(content):
        raise ValueError('Ineligible discovery text')
    identity = review.digest({'chat_ref': chat_ref, 'session_id': session_id,
                              'timestamp': float(timestamp), 'content_digest': review.digest(content)})
    lead = {'chat_ref': chat_ref, 'source_locator': f'chat-discovery:{chat_ref}:{identity}',
            'role': 'user', 'excerpt': content[:500], 'timestamp': float(timestamp),
            'clipped': len(content) > 500, 'item_ids': [], 'discovery_only': True,
            'untrusted_source': True, '_local_label': local_action_label(content)}
    if pinned_owner is not None:
        lead['discovery_owner'] = pinned_owner
    if pinned_binding is not None:
        lead['discovery_binding'] = pinned_binding
    return lead
