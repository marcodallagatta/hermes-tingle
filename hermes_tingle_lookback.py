"""Owner-only one-shot two-pass Gmail review, isolated from standing collection.

No model-tool entry point. Content appears only in the authenticated slash-command
reply. Accepted items become ordinary profile-global records after exact preview.
"""
from __future__ import annotations

import html
import json
import hashlib
from contextvars import ContextVar
import re
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from . import hermes_tingle_store as store, hermes_tingle_sources as sources
    from . import hermes_tingle_assess as assess, hermes_tingle_expiry as expiry
    from . import hermes_tingle_expiry_preferences as expiry_prefs
    from .hermes_tingle_config import load_settings, local_timezone
except ImportError:
    import hermes_tingle_store as store
    import hermes_tingle_sources as sources
    import hermes_tingle_assess as assess
    import hermes_tingle_expiry as expiry
    import hermes_tingle_expiry_preferences as expiry_prefs
    from hermes_tingle_config import load_settings, local_timezone

CAP = 100
THREAD_CAP = 10
BODY_CAP = 20
BODY_CHARS = 12000
_account_user = ContextVar("tingle_lookback_account", default=None)
_read_budget = ContextVar("tingle_lookback_read_budget", default=None)
RETENTION_DAYS = 7
ID = re.compile(r"^[A-Za-z0-9_-]{1,256}$")


# Closed set of content-free stage codes for candidates.stage: which stage kept or removed
# one conversation. Never free text, never message content. Codes live and die with the
# candidate row (scan TTL or discard); acceptance clears its content as before.
STAGE_STAGED = 'staged'                        # row exists; no later stage recorded yet
STAGE_SUPPRESSED = 'suppressed_routine'        # routine/no-action heuristic kept it out of review
STAGE_SKIPPED_DRAFT = 'skipped_draft'          # a saved owner draft kept it out of assessment
STAGE_SKIPPED_OWNER_LAST = 'skipped_owner_last'  # owner's own message is newest, nothing earlier staged
STAGE_CAPPED = 'capped'                        # beyond the per-run conversation cap, never sent
STAGE_REQUEST_FAILED = 'request_failed'        # its model request failed; no verdict returned
STAGE_REQUEST_NOT_SENT = 'request_not_sent'    # run stopped before its request was sent
STAGE_NO_VERDICT = 'no_valid_verdict'          # request answered, but no usable verdict for it
STAGE_MODEL_NOT_ACTION = 'model_not_action'    # model judged it not actionable or already resolved
STAGE_PICK_UNSUPPORTED = 'pick_unsupported'    # model picked it, but the quoted evidence failed validation
STAGE_PICK_CHANGED = 'pick_changed'            # picked, but staged content changed before storing
STAGE_PICK_SKIPPED_BY_RULE = 'pick_skipped_by_rule'  # an owner skip term removed the pick
STAGE_PICK_LISTED = 'pick_listed'              # stored as a review result
STAGE_ACCEPTED = 'accepted'                    # owner accepted it into the shared profile
STAGES = (STAGE_STAGED, STAGE_SUPPRESSED, STAGE_SKIPPED_DRAFT, STAGE_SKIPPED_OWNER_LAST, STAGE_CAPPED,
          STAGE_REQUEST_FAILED, STAGE_REQUEST_NOT_SENT, STAGE_NO_VERDICT, STAGE_MODEL_NOT_ACTION,
          STAGE_PICK_UNSUPPORTED, STAGE_PICK_CHANGED, STAGE_PICK_SKIPPED_BY_RULE, STAGE_PICK_LISTED,
          STAGE_ACCEPTED)


def now():
    return datetime.now(timezone.utc)


def connect():
    # A separate plugin namespace/database cannot leak into ordinary item queries,
    # source snapshots, review markers, standing cursor or body-read ledgers.
    conn = store.open_database("hermes-tingle-lookback")
    import sqlite3
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS scans (
          id TEXT PRIMARY KEY, principal TEXT NOT NULL, policy TEXT NOT NULL,
          source_epoch INTEGER NOT NULL, generation INTEGER NOT NULL DEFAULT 1,
          state TEXT NOT NULL, days INTEGER NOT NULL, created TEXT NOT NULL,
          expires TEXT NOT NULL, listed INTEGER NOT NULL DEFAULT 0,
          checked INTEGER NOT NULL DEFAULT 0, staged INTEGER NOT NULL DEFAULT 0,
          coverage TEXT NOT NULL DEFAULT 'not started',account_key TEXT NOT NULL DEFAULT '',
          error_detail TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS candidates (
          id TEXT PRIMARY KEY, scan_id TEXT NOT NULL, generation INTEGER NOT NULL,
          payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', item_id TEXT);
    """)
    columns = {r[1] for r in conn.execute("PRAGMA table_info(candidates)")}
    if 'draft' not in columns:
        conn.execute("ALTER TABLE candidates ADD COLUMN draft TEXT NOT NULL DEFAULT ''")
    if 'draft_rev' not in columns:
        conn.execute("ALTER TABLE candidates ADD COLUMN draft_rev INTEGER NOT NULL DEFAULT 0")
    # Content-free per-conversation stage code (closed set, see STAGES). Same lifetime and
    # deletion as the candidate row; rows from before this column keep '' = not recorded.
    if 'stage' not in columns:
        conn.execute("ALTER TABLE candidates ADD COLUMN stage TEXT NOT NULL DEFAULT ''")
    scan_columns = {r[1] for r in conn.execute("PRAGMA table_info(scans)")}
    if 'error_detail' not in scan_columns:
        conn.execute("ALTER TABLE scans ADD COLUMN error_detail TEXT NOT NULL DEFAULT ''")
    # Old scans get no assessment approval: the new columns default to none.
    for name, ddl in (('assess_grant', "TEXT NOT NULL DEFAULT ''"), ('assess_runs', 'INTEGER NOT NULL DEFAULT 0'),
                      ('assess_state', "TEXT NOT NULL DEFAULT ''")):
        if name not in scan_columns:
            conn.execute(f"ALTER TABLE scans ADD COLUMN {name} {ddl}")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS assessments (
          id TEXT PRIMARY KEY, scan_id TEXT NOT NULL, version TEXT NOT NULL, generation INTEGER NOT NULL,
          created TEXT NOT NULL, considered INTEGER NOT NULL, assessed INTEGER NOT NULL, over_cap INTEGER NOT NULL,
          failed_calls INTEGER NOT NULL, qualified INTEGER NOT NULL, pages INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS assessment_results (
          assessment_id TEXT NOT NULL, slot INTEGER NOT NULL, page INTEGER NOT NULL, candidate_id TEXT NOT NULL,
          action TEXT NOT NULL, quote TEXT NOT NULL, partial INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(assessment_id, slot));
    """)
    if 'review_plan' not in {r[1] for r in conn.execute('PRAGMA table_info(assessment_results)')}:
        conn.execute("ALTER TABLE assessment_results ADD COLUMN review_plan TEXT NOT NULL DEFAULT ''")
    if 'review_plan_reason' not in {r[1] for r in conn.execute('PRAGMA table_info(assessment_results)')}:
        conn.execute("ALTER TABLE assessment_results ADD COLUMN review_plan_reason TEXT NOT NULL DEFAULT ''")
    if 'plan_faults' not in {r[1] for r in conn.execute('PRAGMA table_info(assessments)')}:
        conn.execute("ALTER TABLE assessments ADD COLUMN plan_faults INTEGER NOT NULL DEFAULT 0")
    if 'topic_emoji' not in {r[1] for r in conn.execute('PRAGMA table_info(assessment_results)')}:
        conn.execute("ALTER TABLE assessment_results ADD COLUMN topic_emoji TEXT NOT NULL DEFAULT '📌'")
    if 'expiry' not in {r[1] for r in conn.execute('PRAGMA table_info(assessment_results)')}:
        conn.execute("ALTER TABLE assessment_results ADD COLUMN expiry TEXT NOT NULL DEFAULT ''")
    if 'excluded' not in {r[1] for r in conn.execute("PRAGMA table_info(assessments)")}:
        conn.execute("ALTER TABLE assessments ADD COLUMN excluded INTEGER NOT NULL DEFAULT 0")
    return conn


def expire(conn):
    # Every private operation enforces TTL, not only an optional background cleanup.
    stamp = now().isoformat()
    conn.execute("DELETE FROM assessment_results WHERE assessment_id IN (SELECT a.id FROM assessments a "
                 "JOIN scans s ON s.id=a.scan_id WHERE s.expires<=?)", (stamp,))
    conn.execute("DELETE FROM assessments WHERE scan_id IN (SELECT id FROM scans WHERE expires<=?)", (stamp,))
    conn.execute("DELETE FROM candidates WHERE scan_id IN (SELECT id FROM scans WHERE expires<=?)", (stamp,))
    conn.execute("UPDATE scans SET state='expired',generation=generation+1,coverage='review window expired' "
                 "WHERE expires<=? AND state NOT IN ('expired','revoked')", (stamp,))
    _purge_assessments(conn)
    conn.commit()


def epoch():
    rows = store.list_source_optins()
    row = next((r for r in rows if r['kind'] == 'email'), {})
    return int(row.get('generation', -1))


def binding(source):
    import hermes_constants
    try:
        from .hermes_tingle import _source_sender_error, _deciding_principal
    except ImportError:
        from hermes_tingle import _source_sender_error, _deciding_principal
    if str(getattr(source, "chat_type", "")).casefold() != "dm":
        raise ValueError("Private email look-back requires a verified direct-message context")
    refusal = _source_sender_error(source, noun="Private email look-back")
    if refusal:
        raise ValueError(refusal)
    platform, user, chat = _deciding_principal(source)
    return (str(Path(hermes_constants.get_hermes_home()).resolve()),
            json.dumps(load_settings(), sort_keys=True), f"{platform}:{user}", chat, epoch())


def checked(source, expected):
    if binding(source) != expected:
        raise ValueError("Owner, profile, policy or email consent changed; no further read or acceptance allowed")


def get_scan(conn, scan_id, principal):
    expire(conn)
    row = conn.execute("SELECT * FROM scans WHERE id=? AND principal=?", (scan_id, principal)).fetchone()
    if not row or row['state'] in ('revoked', 'expired'):
        raise ValueError("Scan unavailable, revoked or expired")
    return row


def authorize_scan(conn, scan_id, source, expected, *, reading=False):
    checked(source, expected)
    row = get_scan(conn, scan_id, expected[2])
    if row['policy'] != expected[1] or row['source_epoch'] != expected[4]:
        raise ValueError("Scan belongs to an older policy/consent epoch. Discard it with /tingle lookback discard LB-ID, then start a new one with /tingle lookback start <days>.")
    if reading and row['state'] != 'reading':
        raise ValueError("One-off read permission is no longer active")
    return row


def provider(conn, scan_id, source, expected, *args):
    authorize_scan(conn, scan_id, source, expected, reading=True)
    budget = _read_budget.get()
    if budget is not None:
        if now() >= budget['deadline'] or budget['calls'] >= 250:
            raise sources.SourceReadError('Scan time/call budget reached; coverage incomplete')
        budget['calls'] += 1
        budget['inner_calls'] = budget.get('inner_calls',0)+1
    args = list(args)
    if _account_user.get() is not None and len(args) >= 2 and args[-2] == '--params':
        params = json.loads(args[-1])
        if params.get('userId') == 'me':
            params['userId'] = _account_user.get()
            args[-1] = json.dumps(params)
    result = sources._gws_bridge(*args)
    authorize_scan(conn, scan_id, source, expected, reading=True)
    return result


def provider_dates(conn, scan_id, source, expected, ids):
    """Content-free 20-ID chunks; every retry consumes the existing read budget."""
    stamps, gaps = {}, []
    script = Path(__file__).resolve().parent / 'scripts' / 'gws_batch_bridge.py'
    for offset in range(0, len(ids), 20):
        chunk = ids[offset:offset + 20]
        authorize_scan(conn, scan_id, source, expected, reading=True)
        budget = _read_budget.get()
        if budget is not None:
            # Reserve the worst case before entering the helper, so hidden retries
            # cannot overrun the cap. Each helper also has a 20-second ceiling.
            remaining = (budget['deadline'] - now()).total_seconds()
            if remaining <= 0 or budget['calls'] + 3 > 250:
                gaps.append('Date read time/call budget reached')
                break
            timeout = min(25, remaining)
            budget['calls'] += 3
            budget['inner_calls'] = budget.get('inner_calls',0)+3*len(chunk)
        else:
            timeout = 25
        payload = json.dumps({'userId': _account_user.get(), 'ids': chunk})
        try:
            result = sources._run_json([sys.executable, str(script), payload], timeout=timeout)
        except sources.SourceReadError:
            authorize_scan(conn, scan_id, source, expected, reading=True)
            # Timeout/failed helpers have no trustworthy response, but successful
            # earlier chunks remain usable. Do not repeat an unknown operation.
            gaps.append('Date metadata helper did not complete')
            continue
        authorize_scan(conn, scan_id, source, expected, reading=True)
        # Accept legacy full-success responses from existing injected bridges.
        if isinstance(result, list):
            entries, calls, missing, diagnostic = result, 1, 0, ''
            inner_calls = len(chunk)
        elif isinstance(result, dict):
            entries = result.get('dates')
            calls, missing = result.get('calls'), result.get('unverified')
            diagnostic = result.get('diagnostic')
            inner_calls = result.get('inner_calls', calls*len(chunk) if type(calls) is int else None)
            if (type(calls) is not int or not 1 <= calls <= 3 or type(missing) is not int
                    or not 0 <= missing <= len(chunk) or not isinstance(diagnostic, str)
                    or type(inner_calls) is not int or not len(chunk)<=inner_calls<=3*len(chunk)):
                raise sources.SourceReadError('Invalid batch date response')
        else:
            raise sources.SourceReadError('Invalid batch date response')
        if not isinstance(entries, list) or len(entries) + missing != len(chunk):
            raise sources.SourceReadError('Invalid batch date response')
        found = set()
        for entry in entries:
            mid = entry.get('id') if isinstance(entry, dict) else None
            if mid not in chunk or mid in found:
                raise sources.SourceReadError('Batch date identity mismatch')
            found.add(mid)
            stamps[mid] = entry.get('internalDate')
        if budget is not None:
            budget['calls'] -= 3 - calls
            budget['inner_calls'] -= 3*len(chunk)-inner_calls
        if missing:
            # Helper-generated diagnostic is count/status only, never provider bodies.
            # Keep even a corrupted helper from reflecting identifiers into replies.
            statuses = re.findall(r'(?<![0-9])(?:429|503|[45][0-9]{2})(?![0-9])', diagnostic)
            detail = ', '.join(sorted(set(statuses))) or 'read failure'
            gaps.append(f'{missing}/{len(chunk)} date checks unverified ({detail})')
    return stamps, gaps



def provider_metadata(conn, scan_id, source, expected, ids):
    """Lazily fetch fixed metadata chunks, after all IDs have passed date gates."""
    for offset in range(0,len(ids),20):
        chunk=ids[offset:offset+20]
        check=lambda:authorize_scan(conn,scan_id,source,expected,reading=True)
        check()
        budget=_read_budget.get()
        timeout=25
        if budget is not None:
            remaining=(budget['deadline']-now()).total_seconds()
            if remaining<=0 or budget['calls']>=250:
                raise sources.SourceReadError('Scan time/call budget reached; coverage incomplete')
            timeout=min(timeout,remaining)
            # Reserve before launch: a timeout may have reached every inner request.
            budget['calls']+=1
            budget['inner_calls']=budget.get('inner_calls',0)+len(chunk)
        result=sources._metadata_batch(_account_user.get(),chunk,'lookback',check=check,timeout=timeout)
        for mid in chunk:
            check()
            yield mid,result[mid]

def body_text(payload):
    """Use the same strict bounded MIME parser as standing body reads."""
    return sources.inline_email_text(payload, BODY_CHARS)


def thread_context(conn, scan_id, source, expected, thread, cutoff, start, budget):
    try:
        from . import hermes_tingle_body_audit as audit
    except ImportError:
        import hermes_tingle_body_audit as audit
    result = {'thread_context': [], 'context_status': 'Bounded thread context, not proof of current/unanswered status.',
              'context_provenance': {'version':1, 'enumeration_complete':False, 'members':[], 'gaps':[]}}
    provenance = result['context_provenance']
    def gap(kind, position=None, message_id=None):
        provenance['gaps'].append({'kind':kind,'stage':'acquisition','position':position,'message_id':message_id})
    gaps = []
    if thread in budget['threads']:
        return budget['contexts'][thread]
    if len(budget['threads']) >= THREAD_CAP or budget['used'] >= BODY_CAP:
        gap('uninspected_thread')
        result['context_status'] = 'Body/thread cap reached; metadata-only lead, context incomplete.'
        return result
    budget['threads'].add(thread)
    response = provider(conn, scan_id, source, expected, 'gmail', 'users', 'threads', 'get', '--params',
                        json.dumps({'userId':'me','id':thread,'format':'minimal','fields':'id,messages(id)'}))
    if not isinstance(response, dict) or response.get('id') != thread or not isinstance(response.get('messages'),list):
        raise sources.SourceReadError('Invalid thread ID response')
    entries = response['messages']
    ids = [entry.get('id') if isinstance(entry,dict) else None for entry in entries]
    if any(not isinstance(mid,str) or not ID.fullmatch(mid) for mid in ids) or len(set(ids)) != len(ids):
        raise sources.SourceReadError('Invalid or duplicate thread message IDs')
    provenance['members'] = [{'message_id':mid,'date':None,'body_acquired':False} for mid in ids[:BODY_CAP]]
    provenance['enumeration_complete'] = len(entries) <= BODY_CAP
    # Bound metadata/date work as well as bodies; no arbitrary-depth thread scan.
    if len(entries) > BODY_CAP:
        gaps.append('thread ID cap reached'); gap('id_cap')
    for position, entry in enumerate(entries[:BODY_CAP]):
        mid = entry.get('id') if isinstance(entry,dict) else None
        if not isinstance(mid,str) or not ID.fullmatch(mid):
            raise sources.SourceReadError('Invalid thread message ID')
        date_data = provider(conn, scan_id, source, expected, 'gmail','users','messages','get','--params',
                             json.dumps({'userId':'me','id':mid,'format':'minimal','fields':'id,internalDate'}))
        try:
            stamp = datetime.fromtimestamp(int(date_data['internalDate'])/1000,tz=timezone.utc)
        except (KeyError,TypeError,ValueError,OverflowError):
            raise sources.SourceReadError('Invalid thread message date') from None
        if date_data.get('id') != mid:
            raise sources.SourceReadError('Thread date identity mismatch')
        provenance['members'][position]['date'] = stamp.isoformat()
        if not cutoff <= stamp <= start:
            gap('outside_scope',position,mid)
            gaps.append('out-of-window thread message omitted')
            continue
        metadata = provider(conn, scan_id, source, expected, 'gmail','users','messages','get','--params',
                            json.dumps({'userId':'me','id':mid,'format':'metadata','metadataHeaders':['From','To','Subject','Date','List-Id','List-Unsubscribe','Auto-Submitted'],
                                        'fields':'id,threadId,labelIds,internalDate,snippet,payload(headers)'}))
        if (not isinstance(metadata,dict) or metadata.get('id')!=mid or metadata.get('threadId')!=thread
                or metadata.get('internalDate')!=date_data.get('internalDate')):
            raise sources.SourceReadError('Thread metadata mismatch')
        labels = metadata.get('labelIds')
        if labels is None:  # Gmail omits the field when the message has no labels.
            labels = []
        if not isinstance(labels,list) or any(not isinstance(v,str) for v in labels):
            raise sources.SourceReadError('Thread labels missing')
        if {'TRASH','SPAM','DRAFT'} & set(labels):
            gap('excluded_label',position,mid)
            gaps.append('excluded-label thread message omitted');continue
        header_payload = metadata.get('payload') or {}
        if not isinstance(header_payload,dict) or not isinstance(header_payload.get('headers',[]),list):
            raise sources.SourceReadError('Malformed thread headers')
        headers=header_payload.get('headers',[])
        mapped=sources._mime_headers(header_payload)
        if sources._list_mail_flagged(header_payload):
            gap('list_mail',position,mid)
            gaps.append('list-mail thread member omitted before body read');continue
        text=' '.join((str(metadata.get('snippet','')),json.dumps(headers),f'gmail:{thread}#{mid}'))
        policy=json.loads(expected[1])
        if (any(v.casefold() in text.casefold() for v in policy['source_exclude_substrings'])
                or any(re.search(pattern,text,re.I) for pattern in policy['ticket_exclude_patterns'])):
            gap('policy_excluded',position,mid)
            gaps.append('policy-excluded thread message omitted');continue
        if budget['used']>=BODY_CAP:
            gap('body_cap',position,mid)
            gaps.append('body cap reached');break
        identity = provider(conn, scan_id, source, expected, 'gmail','users','getProfile','--params',
                            json.dumps({'userId':'me','fields':'emailAddress'}))
        address=identity.get('emailAddress') if isinstance(identity,dict) else None
        account=hashlib.sha256(address.strip().casefold().encode()).hexdigest() if isinstance(address,str) else None
        if account!=budget['account']:
            raise sources.SourceReadError('Gmail account changed')
        authorize_scan(conn,scan_id,source,expected,reading=True)
        conn.execute('BEGIN IMMEDIATE')
        live = conn.execute('SELECT state,generation FROM scans WHERE id=?',(scan_id,)).fetchone()
        if live['state'] != 'reading' or live['generation'] != 1:
            conn.rollback()
            raise ValueError('One-off read revoked before body reservation')
        claimed = audit.claim(mid,account,origin='lookback:'+scan_id)
        conn.commit()
        if not claimed:
            gap('body_dedupe',position,mid)
            gaps.append('body previously read within 14 days, not fetched again');continue
        budget['used']+=1  # Attempts, including failed/lost output, not only received bodies.
        try:
            body=provider(conn,scan_id,source,expected,'gmail','users','messages','get','--params',
                          json.dumps({'userId':'me','id':mid,'format':'full','fields':'id,threadId,internalDate,payload'}))
            if (not isinstance(body,dict) or body.get('id')!=mid or body.get('threadId')!=thread
                    or body.get('internalDate')!=date_data.get('internalDate')):
                raise sources.SourceReadError('Body identity/date mismatch')
            content=body_text(body.get('payload'))
            provenance['members'][position]['body_acquired'] = True
            result['thread_context'].append({'message_id':mid,'date':stamp.isoformat(),'body':content,'outgoing':'SENT' in labels,
                                              'untrusted_source':True,'truncated_or_bounded':True})
        except sources.SourceReadError:
            gap('body_failed',position,mid)
            gaps.append('body attempt failed/lost; reservation retained 14 days')
    result['thread_context'].sort(key=lambda v:v['date'])
    if gaps:
        result['context_status']+=' Incomplete: '+ '; '.join(sorted(set(gaps)))
    budget['contexts'][thread] = result
    return result


def _fmt_when(value):
    """Readable local date for one lead; never raises on stored source data."""
    try:
        stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return 'date unavailable'
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    local = stamp.astimezone(local_timezone())
    return f"{local.strftime('%a')} {local.day} {local.strftime('%b %Y %H:%M')}"


def _coverage_label(coverage):
    """One-line honest coverage status; the full counters stay in details."""
    text = str(coverage or '')
    if text.startswith('Partial/failed'):
        return 'scan incomplete - a read or check failed partway; no retry performed'
    if text.startswith('Partial coverage'):
        return 'scan partial - candidate cap or page boundary reached'
    if text.startswith('Bounded ID search completed'):
        return 'scan complete - leads are unverified review candidates, not commitments'
    return 'scan status unavailable'


def _context_label(status):
    """Short human label for a lead's context state; gaps stay visible."""
    text = str(status or '')
    if text.startswith('List-mail noise flag'):
        return 'list-mail filtered - body not read; may be a false positive, review in Gmail if needed'
    if text.startswith('Body/thread cap reached'):
        return 'metadata only - body/thread cap reached, context incomplete'
    if 'Incomplete:' in text:
        return 'bounded context, incomplete: ' + text.split('Incomplete:', 1)[1].strip()
    if text.startswith('Bounded thread context'):
        return 'bounded thread context attached - not proof of current/unanswered status'
    return text[:120] or 'context status unavailable'


def _clean(text, limit=200):
    """One safe display line from untrusted text: control, line-break and bidi/format characters become
    spaces, whitespace collapses, length is bounded. Stops a subject or sender from forging extra lines."""
    import unicodedata
    text = ''.join(' ' if (unicodedata.category(c) in ('Cc', 'Cf', 'Zl', 'Zp') or c.isspace()) else c for c in str(text))
    text = ' '.join(text.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + '\u2026'


def _clean_block(text):
    """Multi-line variant for message bodies: keeps line breaks, drops other control/bidi characters."""
    import unicodedata
    return ''.join(c for c in str(text) if c in '\n' or unicodedata.category(c) not in ('Cc', 'Cf', 'Zl', 'Zp'))


def _redact_identifiers(text):
    """Best-effort shortening of long reference numbers, grouped/dashed digit runs, code-like strings and
    addresses in a proposed item. It is a heuristic, NOT a guarantee: the owner sees the exact words and confirms."""
    text = re.sub(r'\S+@\S+', '[address]', text)
    text = re.sub(r'(?<![\w])(?:\d[ \-./]?){8,}(?![\w])', '[ref]', text)
    text = re.sub(r'\b(?=[A-Za-z0-9\-]*\d)(?=[A-Za-z0-9\-]*[A-Za-z])[A-Za-z0-9\-]{9,}\b', '[ref]', text)
    text = re.sub(r'\d{5,}', '[ref]', text)
    return text


def _readable_thread(context):
    """Plain, readable thread text instead of raw JSON; the body stays marked untrusted by the caller."""
    blocks = []
    for message in context if isinstance(context, list) else []:
        if not isinstance(message, dict):
            continue
        body = _clean_block(html.unescape(str(message.get('body', '')))).strip() or '(no text)'
        note = ' (shortened)' if message.get('truncated_or_bounded') else ''
        blocks.append(f"✉️ {_fmt_when(message.get('date'))}{note}\n{body}")
    return "\n\n".join(blocks) or '(no message text kept)'


def _sender_name(data):
    raw = _clean(html.unescape(str(data.get('from', ''))), 200)
    name = re.sub(r'\s*<[^>]*>\s*', ' ', raw).strip().strip('"')
    return _clean(_redact_identifiers(name), 60) or 'a sender'


def _prepared_fields(data):
    """Proposed title/summary for a bare accept. Built only from the lead's own subject, sender and date;
    never invents a deadline or a commitment. The owner sees these exact fields and must confirm."""
    subject = _clean(html.unescape(str(data.get('subject', ''))), 300)
    while True:
        lowered = subject.lower()
        for prefix in ('re:', 'fwd:', 'fw:', 'aw:', 'wg:'):
            if lowered.startswith(prefix):
                subject = subject[len(prefix):].strip()
                break
        else:
            break
    subject = _redact_identifiers(subject)
    title = short_title('Review email from ' + _sender_name(data), limit=120)
    sender = _sender_name(data)
    summary = (f"Email lead from {sender}, {_fmt_when(data.get('date'))}. Subject: {subject or '(no subject)'}. Open the original email for details and any deadline.")[:1000]
    return title, summary, '-'


def _candidate_fields(conn, candidate):
    """Reuse a qualified same-call action, never invent an action from a subject.

    Owner-edited drafts are handled by callers first. No model or source read here.
    Only the latest assessment for this scan/generation can supply a title.
    """
    title, summary, due = _prepared_fields(json.loads(candidate['payload']))
    result = conn.execute(
        "SELECT r.action FROM assessment_results r JOIN assessments a ON a.id=r.assessment_id "
        "WHERE r.candidate_id=? AND a.scan_id=? AND a.generation=? "
        "AND a.id=(SELECT id FROM assessments WHERE scan_id=? ORDER BY created DESC LIMIT 1)",
        (candidate['id'], candidate['scan_id'], candidate['generation'], candidate['scan_id'])).fetchone()
    if result and str(result['action'] or '').strip():
        title = short_title(_redact_identifiers(result['action']), limit=120)
    return title, summary, due


# Narrow, deterministic display filter, not a source-access or learned policy.
# Positive informational evidence is required; an automated sender alone says nothing
# about whether the owner needs to act. Unknown language/context stays visible.
_ACTION_CUES = re.compile(
    r"\b(please|could you|can you|would you|must|need(?:s)? to|required|action|reply|respond|"
    r"review|approve|confirm|submit|complete|upload|provide|sign|pay|due|deadline|expires?|"
    r"overdue|failed|declined|cancelled|canceled|problem|urgent|remind|verify|verification|"
    r"suspended|blocked|disput(?:e|ed)|unrecognized|missing|promis(?:e|ed)|"
    r"i(?:'ll| will)|we(?:'ll| will)|bitte|frist|zahlen|rispondere|scadenza|pagare)\b", re.I)
_INFORMATIONAL_SUBJECT = re.compile(
    r"\b(receipt|payment received|booking confirmed|reservation confirmed|newsletter|"
    r"digest|account statement|vesting statement|security notice)\b", re.I)
_NO_ACTION = re.compile(r"\b(no action (?:is )?(?:needed|required)|for (?:your )?information only|"
                        r"this is (?:just )?a receipt)\b", re.I)


def _suppression_reason(data):
    """Heuristic only: never assert that an email cannot contain a commitment.
    Positive routine/no-action wording and automation evidence are required. Conflicting action cues, missing context, replies and owner drafts win.
    Legacy payloads without automation evidence remain visible.
    """
    status = str(data.get('context_status', ''))
    if not data.get('automated'):
        return ''
    if not status.startswith(('List-mail noise flag', 'Bounded thread context')) or 'Incomplete:' in status:
        return ''
    if re.match(r'\s*(re|aw|fwd|fw|wg):', str(data.get('subject', '')), re.I):
        return ''
    text = html.unescape(' '.join([str(data.get('subject', '')), str(data.get('snippet', ''))] +
                    [str(m.get('body', '')) for m in data.get('thread_context', []) if isinstance(m, dict)]))
    explicit = bool(_NO_ACTION.search(text) and _INFORMATIONAL_SUBJECT.search(str(data.get('subject', ''))))
    routine = bool(_ROUTINE.search(str(data.get('subject', '')).strip()) and
                   _ROUTINE_INFO.fullmatch(str(data.get('snippet', '')).strip()) and
                   not data.get('thread_context'))
    if not explicit and not routine:
        return ''
    # Conditional reassurance is not an unconditional no-action statement.
    # Veto the whole bounded record rather than guessing the clause's reach.
    if _CONDITIONAL.search(text):
        return ''
    # Remove only the precise no-action phrase before looking for conflicting cues.
    if _ACTION_CUES.search(_NO_ACTION.sub('', text)) or '?' in text:
        return ''
    return 'automated informational mail with positive routine/no-action wording'


def _pending_partition(conn, scan_id):
    visible, suppressed = [], []
    for candidate in conn.execute(
            "SELECT * FROM candidates WHERE scan_id=? AND state='pending' ORDER BY id", (scan_id,)):
        data = json.loads(candidate['payload'])
        target = suppressed if not candidate['draft'] and _suppression_reason(data) else visible
        target.append(candidate)
    return visible, suppressed


_ROUTINE = re.compile(
    r"^(?:your |order |shipping |delivery |payment |booking |reservation )*"
    r"(?:receipt|order confirmation|shipping confirmation|delivery confirmation|"
    r"payment received|booking confirmed|reservation confirmed|newsletter|weekly digest)\b", re.I)
# Extension beyond explicit no-action wording accepts only known boilerplate,
# not arbitrary text under a routine-looking subject. Unknown language stays visible.
_ROUTINE_INFO = re.compile(
    r"(?:(?:Thank you for your purchase|Your payment has been received|"
    r"Your order is confirmed|Your order has shipped|Your order was delivered|"
    r"This week['’]s news|Our latest news)[.!]?\s*)+", re.I)
_CONDITIONAL = re.compile(r"\b(if|unless|until|except|otherwise|provided|providing|however|but|when)\b", re.I)


def _groups(candidates):
    grouped = {}
    for candidate in candidates:
        data = json.loads(candidate['payload'])
        # Missing thread IDs never collapse unrelated legacy records.
        key = data.get('thread_id') or candidate['id']
        grouped.setdefault(key, []).append(candidate)
    result = []
    for members in grouped.values():
        members.sort(key=lambda c: (str(json.loads(c['payload']).get('date', '')), c['id']), reverse=True)
        newest = members[0]
        drafts = [c for c in members if c['draft']]
        representative = drafts[0] if drafts else newest
        data = json.loads(newest['payload'])
        rank = 1 if drafts else 0
        reason = 'saved draft' if drafts else ''
        result.append({'members': members, 'candidate': representative, 'newest': data,
                       'reason': reason, 'rank': rank})
    result.sort(key=lambda g: (g['rank'], str(g['newest'].get('date', '')),
                               g['candidate']['id']), reverse=True)
    return result


def _display(value, limit):
    # Fit UTF-16 surfaces even when all source characters are emoji.
    text = _clean(html.unescape(str(value)), limit)
    while _units(text) > limit:
        text = text[:-2] + '…'
    return text


def _group_line(group):
    data = json.loads(group['candidate']['payload'])
    subject = data.get('subject') or next((json.loads(c['payload']).get('subject') for c in group['members']
                    if json.loads(c['payload']).get('subject')), '(no subject)')
    date = _fmt_when(group['newest'].get('date'))
    match = re.search(r'\d{1,2} \w{3} \d{4}', date)
    date = match.group(0) if match else 'date unavailable'
    # Keep counts and IDs whole; the full source wording is in view/thread.
    return (f"{group['candidate']['id']} · {_display(subject, 32)} · "
            f"{_display(_sender_name(data), 22)} · "
            f"{date} · {len(group['members'])} msg")


# ---- Model-assisted selection -------------------------------------------------------------------
# Selection is a bounded model judgment over already-staged private excerpts, validated locally
# (hermes_tingle_assess). Nothing here fetches mail, adds items or acts: the owner still previews and
# confirms exact fields through _bulk_accept. Without an owner-approved assessment nothing is ranked.

PAGE_UNITS = 900  # Content budget per results reply, counted in UTF-16 units like all chat text.
STALE_RUN = timedelta(minutes=20)  # A heuristic, not a guarantee: host retry time is unverified. A replaced token stops the old worker.


def _purge_assessments(conn):
    """Drop stored model text for any candidate that is no longer pending (accepted, skipped, gone)."""
    conn.execute("DELETE FROM assessment_results WHERE candidate_id NOT IN "
                 "(SELECT id FROM candidates WHERE state='pending')")


def _assess_short():
    return (f"Scan + model picks: excerpts (up to {assess.CONV_CHARS} characters each) of up to "
            f"{assess.MAX_CONVERSATIONS} conversations, in at most {assess.MAX_CALLS} requests of {assess.BATCH} "
            f"(requesting {int(assess.TIMEOUT)} s timeout, {assess.MAX_TOKENS} output tokens), to your Hermes model "
            "(possibly cloud) to pick follow-ups. \n\nModel limits\nHost/provider may ignore timeout/output limits. "
            "Host may retry or fall back, adding attempts; Tingle never retries; 2 failures in a row stop it; "
            "provider may bill; Tingle cannot cap or see the cost. Approved rules go too. Model cannot add/send. "
            "Without it nothing is ranked. Import may add an unverified expiry one-off "
            "(timing metadata: up to 45 days). Also in onboarding.")


def _assess_disclosure(count):
    return (f"Optional model step: Tingle sends short excerpts of up to {count} of these conversations "
            f"(at most {assess.CONV_CHARS} characters each, in at most {assess.MAX_CALLS} requests of "
            f"{assess.BATCH}; requesting {int(assess.TIMEOUT)} s timeout and {assess.MAX_TOKENS} output tokens per request) "
            "to your configured Hermes model to pick the ones that look like they need "
            "your action. The same model response supplies a decorative topic emoji and a proposed next-actor/outcome/review-time plan, retained with accepted items until closure. Unknown timing stays review-needed. Partial history is eligible only for stated timing when every message from its anchor through newest is fully present, no policy/scope blind spot exists, and the same model judges the plan independent of older gaps; this judgment is not verified. Standing email permission is required for later checks; this one-off does not change it. \n\nModel limits\nHost/provider may ignore timeout/output limits. Your approved rules go too. "
            "That model may be a cloud provider and may bill you; Tingle cannot cap or see "
            "the cost, and the host may retry or fall back, which can add requests. Tingle itself never retries: "
            "after one failed request it still sends the next ones within that maximum, and it stops after two "
            "failures in a row. Excerpts then also "
            "live in your provider's and Hermes's history, which discard cannot erase. "
            "The model cannot add, send, fetch or change anything, and every pick is checked against the "
            "text it came from. Without this step nothing is ranked or suggested.\n\nExpiry metadata\n" + _timing_disclosure())


def _message_text(data):
    """Header plus the fullest staged text for one message. Clipping to the model budget happens once, later,
    keeping head and tail; earlier cuts at staging time are reported through prior_cut."""
    subject = _clean(html.unescape(str(data.get('subject', ''))), 160)
    return f"From {_sender_name(data)}. Subject: {subject}. "


STAGED_SNIPPET_CUT = 800  # Staging stores at most this many snippet characters.


class _ConversationMessages(list):
    """Transient acquisition metadata travels with the existing three-value input contract."""


def _conversation_input(group):
    """Newest-first excerpts for one conversation: staged messages plus bounded thread bodies."""
    full = BODY_CHARS + 400
    bodies = {}
    for member in group['members']:
        for entry in json.loads(member['payload']).get('thread_context', []):
            if isinstance(entry, dict) and entry.get('message_id'):
                bodies[entry['message_id']] = entry
    messages, used = _ConversationMessages(), set()
    for member in group['members']:
        data = json.loads(member['payload'])
        body = bodies.get(data.get('message_id'))
        snippet = str(data.get('snippet', ''))
        if body:
            used.add(data.get('message_id'))
            raw = str(body.get('body', ''))
            content = (raw if snippet.strip() and snippet.strip() in raw else
                       snippet if not raw.strip() or raw.strip() in snippet else snippet + ' ' + raw)
            cut = len(raw) >= BODY_CHARS or len(snippet) >= STAGED_SNIPPET_CUT
        else:
            content, cut = snippet, len(snippet) >= STAGED_SNIPPET_CUT
        messages.append({'message_id':data.get('message_id'), 'body_acquired':body is not None,
                         'date': data.get('date', ''), 'prior_cut': cut,
                         'text': _message_text(data) + _clean(html.unescape(content), full),
                         'direction': (('sent_by_owner' if data['outgoing'] else 'received') if 'outgoing' in data else 'unknown')})
    for mid, entry in bodies.items():
        if mid not in used:
            flag = entry.get('outgoing')  # Missing on scans staged before this field: stay conservative.
            raw = str(entry.get('body', ''))
            messages.append({'message_id':mid,'body_acquired':True,
                             'date': entry.get('date', ''), 'prior_cut': len(raw) >= BODY_CHARS,
                             'direction': 'unknown' if flag is None else ('sent_by_owner' if flag else 'received'),
                             'text': _clean(html.unescape(raw), full)})
    messages.sort(key=lambda m: str(m['date']), reverse=True)
    manifests = [json.loads(member['payload']).get('context_provenance') for member in group['members']]
    messages.provenance = manifests[0] if manifests and all(m == manifests[0] for m in manifests) else None
    status = str(group['newest'].get('context_status', ''))
    complete = bool(status.startswith('Bounded thread context')) and 'Incomplete:' not in status
    return messages, bool(group['newest'].get('automated')), complete


def _group_state(conn, ids):
    """Fingerprint of every member the model saw: state, generation, payload, draft and draft revision."""
    digest = hashlib.sha256()
    for lead in sorted(ids):
        row = conn.execute("SELECT id,state,generation,payload,draft,draft_rev FROM candidates WHERE id=?", (lead,)).fetchone()
        digest.update(repr(tuple(row) if row else (lead, None)).encode())
    return digest.hexdigest()


def _authorize_in_transaction(conn, scan_id, source, expected):
    """Same gates as authorize_scan, but read-only: it must never commit the caller's open transaction."""
    checked(source, expected)
    row = conn.execute("SELECT * FROM scans WHERE id=? AND principal=?", (scan_id, expected[2])).fetchone()
    if (not row or row['state'] in ('revoked', 'expired') or row['expires'] <= now().isoformat()
            or row['policy'] != expected[1] or row['source_epoch'] != expected[4]):
        raise ValueError("Scan unavailable, revoked, expired or from an older policy/consent epoch")
    return row


def _still_mine(conn, scan_id, token):
    live = conn.execute("SELECT assess_state FROM scans WHERE id=?", (scan_id,)).fetchone()
    if not live or not str(live['assess_state']).startswith(f'running:{token}:'):
        raise ValueError("Another assessment took over this scan; nothing more was sent or stored")


class AssessmentCancelled(ValueError):
    """The owner stopped this run: nothing more is sent and nothing is stored.

    Raised only from a verified `cancelled` marker written by the owner-DM stop
    command, never from an unavailable chooser, a timeout or any other cause.
    `assessed` counts conversations the model answered before the stop; the
    interrupted run's in-memory results are always discarded, never stored."""

    def __init__(self, assessed, total):
        self.assessed, self.total = assessed, total
        super().__init__(f"Stopped. {assessed} assessed, {total - assessed} left unassessed; "
                         "no further calls will start. The interrupted run's partial results "
                         "were discarded; nothing was added.")


def _run_gate(conn, scan_id, token, assessed, total):
    """Running-phase token check with owner-stop semantics.

    The stop command and the result-store transaction both take the database
    write lock, so exactly one wins: a committed stop makes this gate raise
    before anything more is gated, sent or stored; a committed store leaves no
    `running:` marker for stop to match. One request that already passed this
    gate as the stop lands may still start; its result is discarded here and
    nothing after it is gated."""
    live = conn.execute("SELECT assess_state FROM scans WHERE id=?", (scan_id,)).fetchone()
    state = str(live['assess_state']) if live else ''
    if state.startswith(f'running:{token}:'):
        return
    if state.startswith('cancelled'):
        raise AssessmentCancelled(assessed, total)
    raise ValueError("Another assessment took over this scan; nothing more was sent or stored")


def _stop(ctx, source, expected):
    """Owner-DM stop control for this profile's currently running model assessment.

    binding() has already verified an identified configured owner in a direct
    message. The atomic UPDATE targets only a live `running:` marker owned by
    this principal: it cannot touch another profile, a finished run, or a
    stored result. One already in-flight provider request cannot be cancelled,
    and one request that already passed its pre-send gate as the stop lands may
    still start; both results are discarded at the next gate and nothing is
    stored. Holding the gate lock across a network call would block the stop
    itself, so the bound is one extra request, never a later batch."""
    del ctx  # Stop never prompts; it only records the owner's decision.
    checked(source, expected)
    conn = connect()
    try:
        running = conn.execute(
            "SELECT id FROM scans WHERE principal=? AND assess_state LIKE 'running:%' "
            "ORDER BY created DESC LIMIT 1", (expected[2],)).fetchone()
        if not running:
            stopped = conn.execute(
                "SELECT 1 FROM scans WHERE principal=? AND assess_state LIKE 'cancelled:%' "
                "ORDER BY created DESC LIMIT 1", (expected[2],)).fetchone()
            if stopped:
                return ("Stop is already recorded for the current review - no further calls will start. "
                        "You'll get a short summary when the current step exits.")
            return "No model review is running right now; nothing was stopped."
        conn.execute('BEGIN IMMEDIATE')
        cur = conn.execute("UPDATE scans SET assess_state=? WHERE id=? AND assess_state LIKE 'running:%'",
                           (f'cancelled:{now().isoformat()}', running['id']))
        conn.commit()
        if cur.rowcount:
            return ("Stopping the current model review. At most one already-approved request may "
                    "still start or finish; its result is discarded and nothing is added. You'll "
                    "get a short summary when the current step exits.")
        return "That model review just finished on its own; nothing was stopped."
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _partition_groups(conn, row):
    """Split this scan's pending candidates into (eligible groups, skipped groups, suppressed rows).

    A skipped group carries the content-free reason code that kept it from the model:
    STAGE_SKIPPED_DRAFT or STAGE_SKIPPED_OWNER_LAST. Suppressed rows are the routine/no-action
    matches _pending_partition kept out of normal review.
    """
    visible, suppressed = _pending_partition(conn, row['id'])
    visible = [c for c in visible if c['generation'] == row['generation']]
    eligible, skipped = [], []
    for group in _groups(visible):
        if group['candidate']['draft']:
            skipped.append((group, STAGE_SKIPPED_DRAFT))
        # A thread whose newest message is the owner's own still counts when an earlier received
        # message is staged: the model judges the whole thread and the owner's reply only closes
        # it if it completes the open step.
        elif (group['newest'].get('outgoing')
              and all(json.loads(m['payload']).get('outgoing', True) for m in group['members'])):
            skipped.append((group, STAGE_SKIPPED_OWNER_LAST))
        else:
            eligible.append(group)
    return eligible, skipped, [c for c in suppressed if c['generation'] == row['generation']]


def _eligible_groups(conn, row):
    eligible, _, _ = _partition_groups(conn, row)
    return eligible


def _result_block(candidate_id, data, action, quote, partial=False):
    date = _fmt_when(data.get('date'))
    match = re.search(r'\d{1,2} \w{3} \d{4}', date)
    date = match.group(0) if match else 'date unavailable'
    return (f"{candidate_id} · {_display(data.get('subject') or '(no subject)', 32)} · "
            f"{_display(_sender_name(data), 22)} · {date}\n"
            f"  Ask: {_display(action, 80)}\n  \u201c{_display(quote, 90)}\u201d"
            + ("\n  Partial context: check the thread before acting." if partial else ""))


def _latest_assessment(conn, scan_id):
    return conn.execute("SELECT * FROM assessments WHERE scan_id=? ORDER BY created DESC LIMIT 1",
                        (scan_id,)).fetchone()


def _page_rows(conn, assessment, generation, page):
    return conn.execute(
        "SELECT r.candidate_id,r.action,r.quote,r.partial,c.payload FROM assessment_results r "
        "JOIN candidates c ON c.id=r.candidate_id WHERE r.assessment_id=? AND r.page=? "
        "AND c.state='pending' AND c.generation=? ORDER BY r.slot",
        (assessment['id'], page, generation)).fetchall()


def _assess_scan(ctx, source, expected, scan_id):
    """Run the owner-approved assessment once. Every request re-checks owner/profile/consent/TTL/generation."""
    token = secrets.token_hex(8)
    conn = connect()
    try:
        authorize_scan(conn, scan_id, source, expected)
        # One transaction checks and consumes the approval, so two racing runs cannot both send content.
        # Nothing between BEGIN and COMMIT may commit: the gates here are read-only.
        conn.execute('BEGIN IMMEDIATE')
        row = _authorize_in_transaction(conn, scan_id, source, expected)
        if row['state'] != 'consumed':
            conn.rollback()
            raise ValueError("⚠️ The scan is not finished or is no longer available. Nothing was sent to a model.")
        if row['assess_grant'] != assess.VERSION:
            conn.rollback()
            raise ValueError("⚠️ No current model-assessment approval for this scan. Nothing was sent to a model.")
        if int(row['assess_runs']) >= 2:
            conn.rollback()
            raise ValueError("⚠️ This scan has already used its model-assessment runs. Nothing was sent.")
        started = str(row['assess_state'])
        if started.startswith('running:'):
            try:
                busy = now() - datetime.fromisoformat(started.split(':', 2)[2]) < STALE_RUN
            except (ValueError, IndexError):
                busy = False
            if busy:
                conn.rollback()
                raise ValueError("⚠️ An assessment for this scan is already running. Nothing new was sent.")
        conn.execute("UPDATE scans SET assess_state=?,assess_runs=assess_runs+1,assess_grant='' WHERE id=?",
                     (f'running:{token}:{now().isoformat()}', scan_id))
        generation = row['generation']
        eligible, skipped, suppressed = _partition_groups(conn, row)
        chosen = eligible[:assess.MAX_CONVERSATIONS]
        capped = eligible[assess.MAX_CONVERSATIONS:]
        over_cap = len(capped)
        # Record, per conversation, why it never reaches the model: content-free stage codes only.
        # These writes share the approval-consumption transaction and cannot race another worker.
        for group, code in skipped:
            for member in group['members']:
                conn.execute("UPDATE candidates SET stage=? WHERE id=? AND state='pending'", (code, member['id']))
        for group in capped:
            for member in group['members']:
                conn.execute("UPDATE candidates SET stage=? WHERE id=? AND state='pending'", (STAGE_CAPPED, member['id']))
        for candidate in suppressed:
            conn.execute("UPDATE candidates SET stage=? WHERE id=? AND state='pending'", (STAGE_SUPPRESSED, candidate['id']))
        records, evidence, owner, fingerprints = [], {}, {}, {}
        for index, group in enumerate(chosen, 1):
            cid = f"C{index}"
            messages, automated, complete = _conversation_input(group)
            record, texts = assess.conversation_record(cid, messages, automated=automated, acquisition_complete=complete)
            records.append(record)
            evidence[cid] = texts
            owner[cid] = group
            fingerprints[cid] = _group_state(conn, [m['id'] for m in group['members']])
            for member in group['members']:
                conn.execute("UPDATE candidates SET stage=? WHERE id=? AND state='pending'",
                             (STAGE_STAGED, member['id']))
        conn.commit()
    except BaseException:
        conn.rollback()
        try:
            conn.execute("UPDATE scans SET assess_state='' WHERE id=? AND assess_state LIKE ?", (scan_id, f'running:{token}:%'))
            conn.commit()
        except Exception:  # noqa: BLE001
            pass
        conn.close()
        raise
    conn.close()
    outcomes, failed, in_a_row = {}, 0, 0
    attempted, failed_ids = set(), set()
    state = 'failed'
    try:
        rules = store.approved_triage_rules()
        terms = assess.skip_terms(rules)
        for batch in assess.batches(records):
            check = connect()
            try:
                live = authorize_scan(check, scan_id, source, expected)
                if live['generation'] != generation:
                    raise ValueError("Scan changed; stopped before sending more content")
                _run_gate(check, scan_id, token, len(attempted), len(records))  # Stop/take-over wins before anything more is sent.
            finally:
                check.close()
            try:
                parsed = assess.request(ctx, batch, rules)
                outcomes.update(assess.validate(parsed, {r['id']: evidence[r['id']] for r in batch}))
                attempted.update(r['id'] for r in batch)
                in_a_row = 0
            except assess.AssessmentError:
                failed += 1
                in_a_row += 1
                failed_ids.update(r['id'] for r in batch)
                # No retry by Tingle. A failed request does not discard the others, but two in a row stop the
                # run so an unavailable model is not asked again and again. The approved maximum is never exceeded.
                if in_a_row >= assess.MAX_FAILED_IN_A_ROW:
                    break
        conn = connect()
        try:
            live = authorize_scan(conn, scan_id, source, expected)
            if live['generation'] != generation:
                raise ValueError("Scan changed during assessment; nothing stored")
            conn.execute('BEGIN IMMEDIATE')
            try:
                locked = _authorize_in_transaction(conn, scan_id, source, expected)
                if locked['generation'] != generation:
                    raise ValueError("Scan changed during assessment; nothing stored")
                _run_gate(conn, scan_id, token, len(attempted), len(records))
            except ValueError:
                conn.rollback()
                raise
            # Only picks whose staged content is still exactly what the model saw and still pending.
            def unchanged(cid):
                ids = [m['id'] for m in owner[cid]['members']]
                rows = [conn.execute("SELECT state,generation,draft FROM candidates WHERE id=?", (lead,)).fetchone() for lead in ids]
                # Every member the model saw is unchanged, still pending, and nobody has drafted or edited since.
                return (all(r and r['state'] == 'pending' and r['generation'] == generation for r in rows)
                        and _group_state(conn, ids) == fingerprints[cid])
            qualified, excluded = [], 0
            stage_of = {}
            for r in records:
                cid = r['id']
                res = outcomes.get(cid)
                if res is None:
                    # Sent but no usable verdict returned for it, its request failed, or the run
                    # stopped before its request: three different causes, three different codes.
                    stage_of[cid] = (STAGE_REQUEST_FAILED if cid in failed_ids
                                     else STAGE_NO_VERDICT if cid in attempted else STAGE_REQUEST_NOT_SENT)
                    continue
                if not res.get('supported'):
                    # The model answered but named no supported action: either it judged the thread
                    # not actionable/already resolved, or its quoted evidence failed exact validation.
                    stage_of[cid] = (STAGE_MODEL_NOT_ACTION
                                     if res.get('disposition') != 'action_needed' or res.get('resolved')
                                     else STAGE_PICK_UNSUPPORTED)
                    continue
                if not unchanged(cid):
                    stage_of[cid] = STAGE_PICK_CHANGED
                    continue
                subject = json.loads(owner[cid]['candidate']['payload']).get('subject', '')
                if terms and assess.skipped_by(terms, subject, res['quote'], res['action']):
                    excluded += 1  # The owner's explicit skip term wins over the model, and is counted, not hidden.
                    stage_of[cid] = STAGE_PICK_SKIPPED_BY_RULE
                    continue
                qualified.append(cid)
                stage_of[cid] = STAGE_PICK_LISTED
            for cid, code in stage_of.items():
                for member in owner[cid]['members']:
                    conn.execute("UPDATE candidates SET stage=? WHERE id=? AND state='pending'", (code, member['id']))
            conn.execute("DELETE FROM assessment_results WHERE assessment_id IN (SELECT id FROM assessments WHERE scan_id=?)", (scan_id,))
            conn.execute("DELETE FROM assessments WHERE scan_id=?", (scan_id,))
            aid, page, used, slot = 'AS-' + secrets.token_hex(4).upper(), 1, 0, 0
            for cid in qualified:
                group, res = owner[cid], outcomes[cid]
                data = json.loads(group['candidate']['payload'])
                size = _units(_result_block(group['candidate']['id'], data, res['action'], res['quote'], True)) + 1
                if slot and used + size > PAGE_UNITS:
                    page, used = page + 1, 0
                used += size
                slot += 1
                partial = 0 if next(r for r in records if r['id'] == cid)['context_complete'] else 1
                record = next(r for r in records if r['id'] == cid)
                n = res.get('evidence_message', 0)
                message = next((m for m in record['messages'] if m['n'] == n), {})
                proposal = expiry.classify(res.get('expiry'), quote=res['quote'],
                    excerpt=' '.join(m['text'] for m in record['messages']),
                    message_date=message.get('date'), now=now(), zone=local_timezone(),
                    context_complete=record['context_complete'], direction=message.get('direction'))
                if proposal:
                    proposal['_source_ids'] = [m['id'] for m in group['members']]
                    proposal['_source_fingerprint'] = fingerprints[cid]
                plan, plan_reason = assess.checkpoint.diagnose(res.get('review_plan_proposal'), record, evidence[cid], now=now())
                conn.execute("INSERT INTO assessment_results(assessment_id,slot,page,candidate_id,action,quote,partial,expiry,topic_emoji,review_plan,review_plan_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                             (aid, slot, page, group['candidate']['id'], res['action'], res['quote'], partial,
                              json.dumps(proposal) if proposal else '', res.get('topic_emoji','📌'),
                              json.dumps(plan), plan_reason))
            assessed_count = sum(1 for outcome in outcomes.values()
                                 if outcome.get('supported') or outcome.get('resolved')
                                 or outcome.get('disposition') != 'action_needed')

            conn.execute("INSERT INTO assessments(id,scan_id,version,generation,created,considered,assessed,over_cap,failed_calls,qualified,pages,excluded) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                         (aid, scan_id, assess.VERSION, generation, now().isoformat(), len(records), assessed_count,
                          over_cap, failed, len(qualified), page if qualified else 0, excluded))
            conn.execute('UPDATE assessments SET plan_faults=? WHERE id=?', (sum(1 for r in outcomes.values() if r.get('plan_protocol_fault')), aid))
            state = 'failed' if failed and not outcomes else ('partial' if failed or over_cap or assessed_count < len(records) else 'done')
            # Result rows and the final marker commit together, so a stop either lands first
            # (this transaction's gate already raised and nothing was stored) or finds no
            # `running:` marker to match and reports the run as already finished.
            conn.execute("UPDATE scans SET assess_state=? WHERE id=? AND assess_state LIKE ?",
                         (state, scan_id, f'running:{token}:%'))
            conn.commit()
            try:
                from . import hermes_tingle_debug as debug
            except ImportError:
                import hermes_tingle_debug as debug
            tally = {code: 0 for code in STAGES}
            for entry in conn.execute('SELECT stage,COUNT(*) AS n FROM candidates WHERE scan_id=? GROUP BY stage', (scan_id,)):
                if entry['stage'] in tally:
                    tally[entry['stage']] = entry['n']
            debug.safe_record('assessment', dict(tally, considered=len(records), assessed=assessed_count,
                qualified=len(qualified), failed_calls=failed, over_cap=over_cap, excluded=excluded,
                plan_faults=sum(1 for r in outcomes.values() if r.get('plan_protocol_fault'))))
        finally:
            conn.close()
    finally:
        done = connect()
        try:
            done.execute("UPDATE scans SET assess_state=? WHERE id=? AND assess_state LIKE ?", (state, scan_id, f'running:{token}:%'))
            done.commit()
        finally:
            done.close()


def _results_text(conn, row, page=1, snapshot=None):
    """Stable results page. A page is a snapshot slot: handled items disappear, others never move.

    Routine replies follow the attention bar: the finding, why it matters, one next
    step. Scan IDs, counters and other commands stay in the drill-downs and help."""
    groups = len(_eligible_groups(conn, row))
    cov = _coverage_label(row['coverage'])
    prefix = f"⚠️ Scan {row['id']}: {cov}.\n" if cov.startswith(('scan incomplete', 'scan partial')) else ''
    a = _latest_assessment(conn, row['id'])
    if not a:
        visible, suppressed = _pending_partition(conn, row['id'])
        kept = f" ({len(suppressed)} routine matches kept out)" if suppressed else ''
        if not visible:
            return prefix + f"🔎 No possible follow-ups in the checked messages{kept} - nothing added."
        plural = 's' if len(visible) != 1 else ''
        return (prefix + f"🔎 {len(visible)} possible follow-up{plural} in the checked messages{kept} - nothing added yet.\n"
                f"Take a look: `/tingle lookback review {row['id']}` · more: `/tingle lookback help`")
    if a['failed_calls'] and not a['assessed']:
        return (prefix + "⚠️ The model step did not complete (the model was unavailable, timed out or its answer was unusable) - "
                "nothing ranked, nothing added, nothing retried automatically.\n"
                f"Take a look yourself: `/tingle lookback review {row['id']}`."
                + (f" One more model run is possible: `/tingle lookback assess {row['id']}`."
                   if int(row['assess_runs']) < 2 else ''))
    if snapshot and a['id'] != snapshot:
        raise ValueError(f"That results set was replaced by a newer assessment. Nothing changed. Current picks: `/tingle lookback results {row['id']}`.")
    pages = int(a['pages'])
    if page < 1 or page > max(pages, 1):
        raise ValueError('That results page is unavailable. Nothing changed.')
    lines = []
    if prefix:
        lines.append(prefix.rstrip())
    early = ''
    if a['plan_faults']:
        warnings.append(f"{int(a['plan_faults'])} model timing plans were missing or malformed; affected picks were not qualified.")
    if int(a['assessed']) < int(a['considered']) and not a['failed_calls']:
        early = f" ({int(a['considered']) - int(a['assessed'])} conversations not assessed; review is incomplete)"
    if a['failed_calls']:
        missing = max(0, int(a['considered']) - int(a['assessed']))  # considered is already capped; over_cap is separate
        early = (f" ({a['failed_calls']} model request{'s' if a['failed_calls'] != 1 else ''} failed, "
                 f"so {missing} conversation{'s' if missing != 1 else ''} were not assessed and may hold more)")
    if a['over_cap']:
        early += f" (size limit: {a['over_cap']} more conversations were not sent)"
    if a['excluded']:
        early += f" ({a['excluded']} matched your skip rules and were left out)"
    qualified = int(a['qualified'])
    if qualified:
        plural = 's' if qualified != 1 else ''
        lines.append(f"🔎 {qualified} possible follow-up{plural} your model picked out{early} - unverified, nothing added yet.")
    shown = _page_rows(conn, a, row['generation'], page) if pages else []
    if shown:
        lines.extend(_result_block(r['candidate_id'], json.loads(r['payload']), r['action'], r['quote'], bool(r['partial'])) for r in shown)
        lines.append(f"Act on them: `/tingle lookback review {row['id']}` · preview: `/tingle lookback track-top {row['id']} {a['id']} {page}`")
    elif pages:
        lines.append("Nothing left on this page.")
    else:
        if int(a['assessed']) < int(a['considered']):
            lines.append(f"⚠️ Review incomplete{early}. No usable picks from the assessed messages; nothing added. Review the remaining leads privately: `/tingle lookback list {row['id']}`.")
        else:
            lines.append(f"🔎 Your model found nothing that clearly needs your action in the checked messages{early} - nothing added; the rest stays private.")
    later = [p for p in range(page + 1, pages + 1) if _page_rows(conn, a, row['generation'], p)]
    if later:
        lines.append(f"More picks (page {later[0]} of {pages}): `/tingle lookback results {row['id']} {a['id']} {later[0]}`.")
    return '\n'.join(lines)


def _suppression_note(scan_id, count):
    if not count:
        return ''
    return (f"{count} noisy match{'es' if count != 1 else ''} kept out of normal review. "
            f"Private for {RETENTION_DAYS} days: `/tingle lookback suppressed {scan_id}` lists IDs.")


def _usage(parts):
    """Say what is missing for the command the person actually tried, with a copyable example."""
    sub = parts[0] if parts else ''
    examples = {
        'start': "Pick how many days to look back (1-30): `/tingle lookback start 14`",
        'list': "Which scan? Use the LB- ID from the scan summary: `/tingle lookback list LB-ID`",
        'view': "Which lead? Use an LC- ID from the list: `/tingle lookback view LC-ID`",
        'accept': "Which lead? Use an LC- ID from the list: `/tingle lookback accept LC-ID`. Tingle prepares the item and asks you to confirm.",
        'skip': "Which lead? Use an LC- ID from the list: `/tingle lookback skip LC-ID`",
        'discard': "Which scan? Use the LB- ID from the scan summary: `/tingle lookback discard LB-ID`",
        'review': "Which scan? Use the LB- ID from the scan summary: `/tingle lookback review LB-ID`",
        'preference': "Put the rule in quotes: `/tingle lookback preference \"ignore shipping notices\"`",
        'trace': "Name the scan and at least 3 characters to match: `/tingle lookback trace LB-ID sender-or-subject-words`",
    }
    if sub in examples:
        return "⚠️ " + examples[sub].rstrip('.') + ". Nothing changed."
    return ("⚠️ I don't know that look-back command. Try `/tingle lookback start 14`, "
            "`/tingle lookback review LB-ID`, or `/tingle lookback help` for the full list.")


def review(ctx, source, scan_id):
    """Walk pending leads one at a time: Track / Skip / Stop. Track uses the same prepared item and
    shared-profile confirmation as accept; nothing is added without that confirmation."""
    expected = binding(source)
    conn = connect()
    try:
        row = authorize_scan(conn, scan_id, source, expected)
        visible, suppressed = _pending_partition(conn, row['id'])
        assessment = _latest_assessment(conn, row['id'])
        if assessment:
            ids = [r['candidate_id'] for r in conn.execute(
                "SELECT r.candidate_id FROM assessment_results r JOIN candidates c ON c.id=r.candidate_id "
                "WHERE r.assessment_id=? AND c.state='pending' AND c.generation=? ORDER BY r.slot",
                (assessment['id'], row['generation'])).fetchall()]
            total = len(ids)
            ids = ids[:5]
        else:
            groups = _groups(visible)
            ids = [g['candidate']['id'] for g in groups[:5]]
            total = len(groups)
        noise_note = _suppression_note(row['id'], len(suppressed))
    finally:
        conn.close()
    if not ids:
        return "✅ No pending leads. Nothing tracked. Sit back and relax; Tingle will send follow-ups automatically when needed. " + noise_note
    tracked = skipped = 0
    problems = []
    later = 0
    left = len(ids)
    for position, lead_id in enumerate(ids, 1):
        conn = connect()
        try:
            authorize_scan(conn, scan_id, source, expected)
            candidate = conn.execute("SELECT payload FROM candidates WHERE id=? AND state='pending'", (lead_id,)).fetchone()
        finally:
            conn.close()
        if not candidate:
            continue
        data = json.loads(candidate['payload'])
        snippet = _clean(html.unescape(str(data.get('snippet', ''))), 160)
        card = (f"🔎 Lead {position} of {len(ids)} (untrusted email, not a verified commitment)\n"
                f"{_clean(html.unescape(str(data.get('subject', ''))), 120) or '(no subject)'}\n"
                f"From {_sender_name(data)} · {_fmt_when(data.get('date'))}\n"
                f"{_context_label(data.get('context_status'))}\n"
                + (f"\n{snippet}\n" if snippet else "") +
                "\nTrack it, skip it, or stop here? Nothing is added without a final confirmation. "
                f"If this decision times out, resume with `/tingle lookback review {scan_id}`.")
        pick = ctx.ask_choice(card, [{'value': 'track', 'label': 'Track it'}, {'value': 'skip', 'label': 'Skip'}, {'value': 'stop', 'label': 'Stop here'}])
        fence = connect()
        try:
            authorize_scan(fence, scan_id, source, expected)
        finally:
            fence.close()
        left -= 1
        if pick == 'track':
            try:
                outcome = handle(ctx, ['accept', lead_id], source)
            except ValueError as exc:
                text = str(exc)
                if 'account-unqualified' in text:
                    text = ("Tingle already has an item that may come from this same email, but it cannot tell for certain which "
                            "account it belongs to. Nothing was added. Check `/tingle list`.")
                problems.append(text)
                fence = connect()
                try:
                    authorize_scan(fence, scan_id, source, expected)
                finally:
                    fence.close()
                again = ctx.ask_choice("⚠️ " + text + "\n\nSkip this lead so it stops showing up, or leave it for later?",
                                       [{'value': 'skip', 'label': 'Skip this lead'}, {'value': 'later', 'label': 'Leave it'}])
                if again == 'skip':
                    handle(ctx, ['skip', lead_id], source)
                    skipped += 1
                else:
                    later += 1
                continue
            if 'Accepted as shared' in outcome:
                tracked += 1
            else:
                later += 1
        elif pick == 'skip':
            handle(ctx, ['skip', lead_id], source)
            skipped += 1
        else:
            break
    conn = connect()
    try:
        authorize_scan(conn, scan_id, source, expected)
        if assessment:
            remaining = conn.execute("SELECT COUNT(*) FROM assessment_results r JOIN candidates c ON c.id=r.candidate_id WHERE r.assessment_id=? AND c.state='pending'", (assessment['id'],)).fetchone()[0]
        else:
            pending, _ = _pending_partition(conn, scan_id)
            remaining = len(_groups(pending))
    finally:
        conn.close()
    note = (("\n\n⚠️ " + problems[0]) if problems else "") + ("\n" + noise_note if noise_note else "")
    parts = [f"{tracked} tracked", f"{skipped} skipped"]
    if remaining == 0:
        return (f"✅ Review done: {', '.join(parts)}. Finish with `/tingle lookback discard {scan_id}` to clear the scan."
                "\nSit back and relax; Tingle will send follow-ups automatically when needed." + note)
    return (f"⏸ Review paused: {', '.join(parts)}, {remaining} still waiting. "
            f"Pick up again with `/tingle lookback review {scan_id}`; finish with `/tingle lookback discard {scan_id}`." + note)


def _full_disclosure():
    """The complete look-back disclosure, shown on demand instead of in the preview.

    Sectioned for reading; every consent and privacy fact stays in the text."""
    return (
        "🔎 One-off Gmail look-back: the full picture\n\n"
        "What it does\n"
        f"- Looks at your chosen 1-30 day window, at most {CAP} message IDs, all inside that window.\n"
        "- First pass reads IDs, dates, headers and snippets to filter out list-mail noise.\n"
        f"- Second pass reads relevant bodies and thread context for up to {THREAD_CAP} promising threads "
        f"and {BODY_CAP} bodies in total, each at most {BODY_CHARS} displayed characters.\n\n"
        "What it can miss\n"
        "- A 120-second/250-provider-call scan budget can leave partial coverage; an already-started call can finish later.\n"
        "- Failed or lost reads reserve the shared 14-day duplicate-read budget unless access is revoked; gaps are disclosed.\n"
        "- This is not full mailbox coverage, and not proof of an unanswered request or a current commitment.\n\n"
        "Where it is kept\n"
        "- Suggestions stay private to the initiating configured owner, in DM on this profile.\n"
        f"- Bounded body text is STORED in private review staging for {RETENTION_DAYS} days.\n"
        "- At expiry access is refused; physical cleanup happens on the next look-back operation, not on an exact background timer.\n"
        "- Accept, skip or revoke removes staged candidate content; history, backups and outside copies may remain.\n"
        "- Full message payloads can contain inline MIME parts; only bounded text is retained.\n"
        "- No attachment-download endpoint, link following or HTML rendering is used.\n\n"
        "What you stay in control of\n"
        "- Nothing is auto-added. Accepted items become visible to everyone on this profile.\n"
        "- Explicit discard is always available.\n"
        "- No standing source setting, cursor or standing body-read record changes; "
        "a separate shared content-free body-read audit will change.\n\n"
        "Optional model step\n"
        f"- {_assess_disclosure(assess.MAX_CONVERSATIONS)}\n\n"
        "Privacy\n"
        f"- {sources.source_privacy_notice()}\n"
        "- Local means the Hermes host. This plugin version has no built-in Tingle-operated upload service; "
        "this does not cover Hermes or providers.\n"
        "- Removal does not erase history or outside copies.")


def _progress(ctx, text):
    """Post a best-effort progress note; the final result still comes from the command."""
    post = getattr(ctx, "post_message", None)
    if callable(post):
        try:
            post(text)
        except Exception:
            pass


def _start_disclosure(days):
    return (
        f"🔎 Review Gmail from the last {days} days once? Bounded scan, not full mailbox coverage.\n"
        f"Reads headers, snippets and selected bodies; text stays in private DM staging for {RETENTION_DAYS} days or until discard, visible only to you. "
        "Model picks send short excerpts to your configured Hermes model (possibly cloud/billed); Tingle can't cap or see the cost, "
        "and host retries may add requests. Provider/history/backups may keep copies. Scan only sends nothing to a model. "
        "Nothing is auto-added; confirmed items are shared on this profile. Ongoing source settings stay unchanged. "
        "Unverified expiry metadata may stay 45 days.\n\n"
        "Full details: `/tingle lookback help`"
    )


def scan(ctx, source, days):
    if not 1 <= days <= 30:
        raise ValueError("Choose 1-30 days")
    expected = binding(source)  # Before confirmation and any source-lock wait.
    if 'email' not in sources.source_capabilities():
        raise ValueError("Google Workspace bridge is not installed; no account read or grant created")
    pick = ctx.ask_choice(_start_disclosure(days), [{'value': 'assess', 'label': 'Scan + model picks'},
                                                       {'value': 'scan', 'label': 'Scan only, no model'},
                                                       {'value': 'cancel', 'label': 'Cancel'}])
    if pick not in ('assess', 'scan'):
        return "Look-back cancelled - nothing was read and no grant was created."
    scan_id = _run_scan(ctx, source, expected, days, grant_assess=(pick == 'assess'))
    return review_after_scan(ctx, source, expected, scan_id)


def _results_reply(source, expected, scan_id):
    conn = connect()
    try:
        row = authorize_scan(conn, scan_id, source, expected)
        text = _summary_text(conn, row)
        authorize_scan(conn, scan_id, source, expected)
        return text
    finally:
        conn.close()


def review_after_scan(ctx, source, expected, scan_id):
    """Show lead decisions immediately, but preserve scan failures and coverage warnings."""
    summary = _results_reply(source, expected, scan_id)
    result = review(ctx, source, scan_id)
    if result.startswith("✅ No pending leads"):
        if "⚠️" in summary or "failed" in summary.casefold() or "incomplete" in summary.casefold():
            return summary
        suffix = "Sit back and relax; Tingle will send follow-ups automatically when needed."
        return summary + "\n" + suffix
    markers = ("⚠️ scan", "coverage is incomplete", "partial context", "model request", "not assessed", "size limit")
    warnings = [line for line in summary.splitlines()
                if any(marker in line.casefold() for marker in markers)]
    return ("\n".join(warnings) + "\n" if warnings else "") + result


def _run_scan(ctx, source, expected, days, *, grant_assess):
    """The one bounded read path, shared by the interactive scan and the one-time
    onboarding import. The caller owns the consent question; grant_assess records
    this scan's single model-step approval from that same consent. Returns the scan ID."""
    _progress(ctx, "⏳ Starting the one-off Gmail look-back. This can take a while; I'll report what it found as soon as it finishes.")
    checked(source, expected)
    with store.serialized_operation('sources'):
        checked(source, expected)  # A later disable/re-enable must not be adopted as baseline.
        conn = connect()
        account_token = _account_user.set(None)
        read_token = _read_budget.set({'deadline':now()+timedelta(seconds=120),'calls':0,'inner_calls':0})
        scan_id = 'LB-' + secrets.token_hex(4).upper()
        start = now()
        cutoff = start - timedelta(days=days)
        try:
            expire(conn)
            conn.execute("INSERT INTO scans(id,principal,policy,source_epoch,state,days,created,expires) VALUES(?,?,?,?,?,?,?,?)",
                         (scan_id, expected[2], expected[1], expected[4], 'reading', days, start.isoformat(),
                          (start + timedelta(days=RETENTION_DAYS)).isoformat()))
            if grant_assess:  # Approval for this scan's first run only; see _assess_scan.
                conn.execute("UPDATE scans SET assess_grant=? WHERE id=?", (assess.VERSION, scan_id))
            conn.commit()
            listed = date_checked = staged = 0
            error_detail = ''
            body_budget = {'used': 0, 'threads': set(), 'account': None, 'contexts': {}}
            coverage = 'Bounded ID search completed; snippets are unverified review leads, not commitments.'
            try:
                identity = provider(conn, scan_id, source, expected, 'gmail', 'users', 'getProfile', '--params',
                                    json.dumps({'userId': 'me', 'fields': 'emailAddress'}))
                address = identity.get('emailAddress') if isinstance(identity, dict) else None
                if not isinstance(address, str) or '@' not in address or len(address) > 320:
                    raise sources.SourceReadError('Gmail account identity unavailable')
                _account_user.set(address.strip())
                body_budget['account'] = hashlib.sha256(address.strip().casefold().encode()).hexdigest()
                conn.execute('UPDATE scans SET account_key=? WHERE id=?', (body_budget['account'], scan_id))
                conn.commit()
                params = {'userId': 'me', 'q': f'in:anywhere newer_than:{days}d -in:trash -in:spam -in:drafts',
                          'maxResults': CAP, 'fields': 'messages(id),nextPageToken,resultSizeEstimate'}
                result = provider(conn, scan_id, source, expected, 'gmail', 'users', 'messages', 'list', '--params', json.dumps(params))
                if not isinstance(result, dict) or not isinstance(result.get('messages', []), list) or 'error' in result:
                    raise sources.SourceReadError('Invalid list response')
                rows = result.get('messages', [])
                if len(rows) > CAP:
                    raise sources.SourceReadError('Provider exceeded requested cap')
                listed = len(rows)
                if result.get('nextPageToken') or len(rows) >= CAP or int(result.get('resultSizeEstimate', 0)) > CAP:
                    coverage = 'Partial coverage: candidate cap/page boundary reached; no further pages read.'
                seen = set()
                ids = []
                for entry in rows:
                    message_id = entry.get('id') if isinstance(entry, dict) else None
                    if not isinstance(message_id, str) or not ID.fullmatch(message_id):
                        raise sources.SourceReadError('Invalid message ID')
                    if message_id in seen:
                        continue
                    seen.add(message_id)
                    ids.append(message_id)
                dates, date_gaps = provider_dates(conn, scan_id, source, expected, ids)
                if date_gaps:
                    error_detail = '; '.join(dict.fromkeys(date_gaps))[:200]
                    coverage = 'Partial coverage: date metadata unavailable for some listed messages.'
                eligible=[]
                for message_id in ids:
                    if message_id not in dates:
                        continue
                    try:
                        stamp=datetime.fromtimestamp(int(dates[message_id])/1000,tz=timezone.utc)
                    except (ValueError,TypeError,OverflowError):
                        raise sources.SourceReadError('Missing trustworthy message date') from None
                    date_checked+=1
                    if cutoff<=stamp<=start:
                        eligible.append(message_id)
                for message_id,data in provider_metadata(conn,scan_id,source,expected,eligible):
                    if isinstance(data,sources.SourceReadError):
                        error_detail='Some metadata checks failed ('+str(data.kind)+')'
                        coverage='Partial coverage: metadata unavailable for some date-verified messages.'
                        continue
                    minimal={'internalDate':dates[message_id]}
                    stamp=datetime.fromtimestamp(int(minimal['internalDate'])/1000,tz=timezone.utc)
                    if not isinstance(data, dict) or data.get('id') != message_id or data.get('internalDate') != minimal.get('internalDate'):
                        raise sources.SourceReadError('Metadata identity/date mismatch')
                    labels = data.get('labelIds')
                    if labels is None:  # Gmail omits the field when the message has no labels.
                        labels = []
                    if not isinstance(labels, list) or any(not isinstance(v,str) for v in labels):
                        raise sources.SourceReadError('Missing message labels')
                    if {'TRASH', 'SPAM', 'DRAFT'} & set(labels):
                        continue
                    header_payload = data.get('payload') or {}
                    if not isinstance(header_payload,dict):
                        raise sources.SourceReadError('Malformed message payload')
                    headers = header_payload.get('headers')
                    if not isinstance(headers, list):
                        raise sources.SourceReadError('Missing headers')
                    mapped = {str(h.get('name', '')).lower(): str(h.get('value', '')) for h in headers if isinstance(h, dict)}
                    thread = data.get('threadId')
                    if not isinstance(thread, str) or not ID.fullmatch(thread):
                        raise sources.SourceReadError('Invalid thread ID')
                    payload = {'message_id': message_id, 'thread_id': thread, 'subject': mapped.get('subject', '')[:500],
                               'from': mapped.get('from', '')[:300], 'snippet': str(data.get('snippet', ''))[:800],
                               'date': stamp.isoformat(), 'source_ref': sources.gmail_ref(body_budget['account'], thread, message_id)}
                    policy = json.loads(expected[1])
                    text = ' '.join((payload['subject'], payload['snippet'], payload['source_ref']))
                    if (any(v.casefold() in text.casefold() for v in policy['source_exclude_substrings']) or
                            any(re.search(pattern, text, re.I) for pattern in policy['ticket_exclude_patterns'])):
                        continue
                    noise = sources._list_mail_flagged(header_payload)
                    payload['outgoing'] = 'SENT' in labels
                    payload['automated'] = noise or any(
                        isinstance(h, dict) and str(h.get('name', '')).casefold() == 'auto-submitted'
                        and str(h.get('value', '')).strip().casefold() not in ('', 'no')
                        for h in headers) or bool(re.search(r'\b(?:no[._-]?reply|do[._-]?not[._-]?reply)@', payload['from'], re.I))
                    if noise:
                        payload['context_status'] = 'List-mail noise flag: body not read; may be a false positive, review in Gmail if needed.'
                        payload['thread_context'] = []
                    else:
                        payload.update(thread_context(conn, scan_id, source, expected, thread, cutoff, start, body_budget))
                    authorize_scan(conn, scan_id, source, expected, reading=True)
                    conn.execute("BEGIN IMMEDIATE")
                    live = conn.execute("SELECT generation,state,expires FROM scans WHERE id=?", (scan_id,)).fetchone()
                    if live['state'] != 'reading' or live['generation'] != 1 or live['expires'] <= now().isoformat():
                        conn.rollback()
                        raise ValueError("One-off grant revoked or expired before staging")
                    conn.execute("INSERT INTO candidates(id,scan_id,generation,payload,stage) VALUES(?,?,1,?,?)",
                                 ('LC-' + secrets.token_hex(4).upper(), scan_id, json.dumps(payload), STAGE_STAGED))
                    conn.commit()
                    staged += 1
            except (ValueError, sources.SourceReadError, TypeError, KeyError) as exc:
                error_detail = _clean(f"{type(exc).__name__}: {exc}", 200)
                coverage = 'Partial/failed scan: account, response or authority check failed; already-started reads cannot be recalled. No retry performed.'
            coverage += f" Thread candidates considered {len(body_budget['threads'])}; body attempts {body_budget['used']}; provider calls {_read_budget.get()['calls']} (logical helper/batch calls); inner API attempts {_read_budget.get()['inner_calls']} (observed/reserved; unknown upstream retries and token refresh excluded)."
            # Permission is consumed on success OR failure, never reused to fetch again.
            conn.execute("UPDATE scans SET state=CASE WHEN state='reading' THEN 'consumed' ELSE state END, "
                         "listed=?,checked=?,staged=?,coverage=?,error_detail=? WHERE id=?",
                         (listed, date_checked, staged, coverage, error_detail, scan_id))
            conn.commit()
            checked(source, expected)
            row = conn.execute('SELECT * FROM scans WHERE id=?', (scan_id,)).fetchone()
            if row['state'] in ('revoked', 'expired'):
                raise ValueError(f"Scan {scan_id}: scan failed or revoked. Nothing added; private leads are unavailable.")
            authorize_scan(conn, scan_id, source, expected)

        finally:
            _account_user.reset(account_token)
            _read_budget.reset(read_token)
            conn.close()
    # Outside the source lock: the model step is slow and never needs provider access.
    if grant_assess:
        _assess_scan(ctx, source, expected, scan_id)
    return scan_id


@store.serialized("onboarding")
def auto_accept_qualified(source, expected, scan_id, *, stats=None):
    """Onboarding only: add the assessment's quote-backed picks, partial-context ones flagged, without another
    question. The single onboarding yes is the consent for exactly this bounded
    auto-add; each lead still passes every normal acceptance check (_commit_lead),
    due dates stay unset, and failures skip that lead instead of failing the import.
    Clear (automatic-tier) expiry claims get the same separate one-off as reviewed
    acceptance, rechecked against the same snapshot rules. Uncertain claims are
    reported back honestly; no timing is invented for them.
    Returns (added_item_ids, considered, skipped_private, uncertain_item_ids)."""
    stats = stats if stats is not None else {}
    stats.update(qualified=0, accepted=0, rejected=0, unassessed=0)
    conn = connect()
    try:
        row = authorize_scan(conn, scan_id, source, expected)
        assessment = _latest_assessment(conn, scan_id)
        considered = int(assessment['considered']) if assessment else 0
        if not assessment:
            return [], considered, considered, []
        # Partial-context picks are added too, and say so in the item, rather than staying hidden: a tracked
        # item the owner can dismiss is more useful than a silently missing one.
        rows = conn.execute(
            "SELECT r.candidate_id,r.partial,r.expiry,a.version FROM assessment_results r "
            "JOIN candidates c ON c.id=r.candidate_id JOIN assessments a ON a.id=r.assessment_id "
            "WHERE r.assessment_id=? AND c.state='pending' AND c.generation=? ORDER BY r.slot",
            (assessment['id'], row['generation'])).fetchall()
        stats['unassessed'] = max(0, considered - int(assessment['assessed'])) + int(assessment['over_cap'])
        stats['qualified'] = len(rows)
        picks = [r['candidate_id'] for r in rows]
        partial_picks = {r['candidate_id'] for r in rows if r['partial']}
        proposals = {}
        for r in rows:
            if r['version'] == assess.VERSION and r['expiry']:
                proposal = json.loads(r['expiry'])
                if proposal.get('tier') in ('automatic', 'uncertain'):
                    proposals[r['candidate_id']] = proposal
    finally:
        conn.close()
    added, uncertain = [], []
    for lead in picks:
        conn = connect()
        try:
            authorize_scan(conn, scan_id, source, expected)
            candidate = conn.execute("SELECT * FROM candidates WHERE id=?", (lead,)).fetchone()
            if not candidate or candidate['state'] != 'pending' or candidate['draft']:
                stats['rejected'] += 1
                continue
            if _checkpoint_plan(conn, candidate).get('suffix_model_judgment'):
                stats['rejected'] += 1
                stats['pending_confirmation'] = stats.get('pending_confirmation', 0) + 1
                continue  # Preserve pending evidence for actual per-item informed preview.
            title, summary, due = _candidate_fields(conn, candidate)
            if lead in partial_picks:
                summary = _clean(summary + ' (Only part of this thread was read: check it.)', 1000)
            summary = _final_summary(summary, note=ONBOARDING_NOTE)
            proposal = proposals.get(lead)
            if proposal and _group_state(conn, proposal.get('_source_ids', [lead])) != proposal.get('_source_fingerprint'):
                proposal = None
            timing = None
            if proposal and proposal['tier'] == 'automatic' and proposal.get('fire_at'):
                # Same snapshot revalidation as reviewed acceptance; a source change
                # between assessment and import refuses rather than trusting stale text.
                timing = {'snapshot': _timing_snapshot(conn, lead), 'proposal': proposal, 'note': ''}
            _commit_lead(conn, source, expected, lead, candidate['generation'], title, summary, '-', None, timing, onboarding=True)
            item_row = conn.execute("SELECT item_id FROM candidates WHERE id=?", (lead,)).fetchone()
            if item_row and item_row['item_id']:
                added.append(item_row['item_id'])
                stats['accepted'] += 1
                if proposal and proposal['tier'] == 'uncertain':
                    uncertain.append(item_row['item_id'])
        except ValueError:
            stats['rejected'] += 1
            continue  # Refused picks remain private and are reported, never called routine.
        finally:
            conn.close()
    return added, considered, max(0, considered - len(picks)), uncertain


def propose_edit(principal, lead_id, title, summary, due):
    """Proposal only: store an edited draft for a pending lead. Never adds an item, never marks the
    lead accepted; the owner's own `/tingle lookback accept LC-ID` shows the exact fields and asks for
    confirmation. Each call replaces the previous draft, so older wording can never be confirmed."""
    if not principal:
        raise ValueError("Cannot tell who this chat belongs to, so no draft was saved.")
    lead_id = str(lead_id or '').strip()
    title = _clean(str(title or ''), 180).strip()
    summary = _clean(str(summary or ''), 1000).strip()
    due = str(due or '-').strip() or '-'
    if not re.fullmatch(r'LC-[A-Fa-f0-9]+', lead_id):
        raise ValueError("Which lead? Give an LC- ID from the owner's review.")
    if not title:
        raise ValueError("A draft needs a short title.")
    if due != '-':
        try:
            stamp = datetime.fromisoformat(due)
        except ValueError:
            raise ValueError("Due must be an ISO time with offset, or omitted when no deadline was stated. No draft saved.") from None
        if stamp.tzinfo is None:
            raise ValueError("Due needs an explicit UTC offset; omit it when no deadline was stated. No draft saved.")
    conn = connect()
    try:
        expire(conn)
        row = conn.execute("SELECT c.*, s.principal AS principal, s.generation AS scan_generation, s.state AS scan_state, "
                           "s.source_epoch AS source_epoch FROM candidates c JOIN scans s ON s.id=c.scan_id WHERE c.id=?",
                           (lead_id,)).fetchone()
        if (not row or row['principal'] != principal or row['state'] != 'pending'
                or row['generation'] != row['scan_generation'] or row['scan_state'] != 'consumed'
                or row['source_epoch'] != epoch()):
            raise ValueError("That lead is not available to edit (not pending, expired, or the email consent changed). No draft saved.")
        try:
            from .hermes_tingle import _excluded_work
        except ImportError:
            from hermes_tingle import _excluded_work
        if _excluded_work({'title': title, 'summary': summary, 'source_ref': json.loads(row['payload']).get('source_ref', '')}):
            raise ValueError("That wording is excluded by the profile configuration. No draft saved.")
        conn.execute("BEGIN IMMEDIATE")
        changed = conn.execute("UPDATE candidates SET draft=?, draft_rev=draft_rev+1 WHERE id=? AND state='pending'",
                               (json.dumps({'title': title, 'summary': summary, 'due': due}), lead_id)).rowcount
        if changed != 1:
            conn.rollback()
            raise ValueError("That lead is not available to edit (not pending). No draft saved.")
        rev = int(conn.execute("SELECT draft_rev FROM candidates WHERE id=?", (lead_id,)).fetchone()['draft_rev'])
        conn.commit()
        return {'lead_id': lead_id, 'draft': rev, 'added': False,
                'next': f"Draft saved, nothing added. Tell the owner to run `/tingle lookback accept {lead_id}` to see the exact fields and confirm; "
                        "a new proposal replaces this one. You cannot confirm it yourself."}
    finally:
        conn.close()


PROVENANCE = "Added after owner review from an email; not a verified commitment."
DRAFT_NOTE = "Wording drafted by the assistant in chat; the owner confirmed it when adding."
ONBOARDING_NOTE = "Added by the one-time onboarding import after the owner's yes; not a verified commitment."


def _final_summary(body, drafted=False, note=PROVENANCE):
    """Stored summary is always body + fixed provenance (+ fixed draft note), built structurally.
    Text inside the body can never suppress or imitate these notes."""
    tail = note + ((' ' + DRAFT_NOTE) if drafted else '')
    body = (body or '').strip()[:max(0, 1200 - len(tail) - 1)]
    return (body + ' ' + tail).strip()


def _checkpoint_plan(conn, candidate):
    if candidate['draft']:
        return assess.checkpoint.unknown()  # An owner-edited action must not inherit a stale model plan.
    row = conn.execute(
        "SELECT r.review_plan FROM assessment_results r JOIN assessments a ON a.id=r.assessment_id "
        "WHERE r.candidate_id=? AND a.scan_id=? AND a.generation=? AND a.version=? "
        "AND a.id=(SELECT id FROM assessments WHERE scan_id=? ORDER BY created DESC LIMIT 1)",
        (candidate['id'], candidate['scan_id'], candidate['generation'], assess.VERSION, candidate['scan_id'])).fetchone()
    try:
        return assess.checkpoint.item_plan(json.loads(row['review_plan'])) if row and row['review_plan'] else assess.checkpoint.unknown()
    except (ValueError, TypeError):
        return assess.checkpoint.unknown()


def _plan_time(value):
    return datetime.fromisoformat(value).astimezone(local_timezone()).strftime('%d %b %Y, %H:%M %Z')


def _checkpoint_preview(plan):
    if plan['review_needed']:
        return 'Tracking: context/timing review needed; no reminder scheduled.'
    text = (f"Tracking: {'waiting on others' if plan['actor'] == 'other' else 'your action'}; "
            f"proposed check {_plan_time(plan['review_at'])} ({plan['basis']}, not a deadline).")
    if plan.get('suffix_model_judgment'):
        text += (' Proposed outcome: ' + plan['outcome'] + '. Partial history: older context is missing. The model judged this stated wait independent '
                 'of those gaps, but that judgment is not verified. Confirm this item and check only if that '
                 'current action/outcome and wait are still right.')
    return text


def _commit_lead(conn, source, expected, lead_id, generation, title, summary, due, draft_rev, timing=None, checkpoint_plan=None, onboarding=False):
    """The one locked acceptance path, used by single accept and bulk alike. It adds exactly the fields
    the owner was shown (title, summary, due) and refuses if the lead, its scan or its draft revision moved since."""
    candidate = conn.execute("SELECT * FROM candidates WHERE id=?", (lead_id,)).fetchone()
    if not candidate:
        raise ValueError('That lead is unavailable: it was tracked, skipped, or its scan was revoked, discarded or expired; nothing added.')
    row = authorize_scan(conn, candidate['scan_id'], source, expected)
    data = json.loads(candidate['payload'] or '{}')
    if not data.get('source_ref'):
        raise ValueError('That lead is no longer pending; nothing added.')
    store.validate_source_ref(data['source_ref'])
    with store.serialized_operation('sources'):
        conn.execute("BEGIN IMMEDIATE")
        try:
            checked(source, expected)  # owner, profile, policy and consent epoch, again inside the lock
        except ValueError:
            conn.rollback()
            raise
        live = conn.execute("SELECT generation,state,expires,policy,source_epoch FROM scans WHERE id=?", (row['id'],)).fetchone()
        latest = conn.execute("SELECT state,draft_rev,draft FROM candidates WHERE id=?", (candidate['id'],)).fetchone()
        if live['policy'] != expected[1] or live['source_epoch'] != expected[4]:
            conn.rollback()
            raise ValueError('Owner, policy or email consent changed during confirmation; nothing added.')
        if (live['generation'] != generation or live['state'] != 'consumed'
                or live['expires'] <= now().isoformat() or not latest or latest['state'] != 'pending'):
            conn.rollback()
            raise ValueError('Candidate changed during confirmation')
        if draft_rev is not None:
            current = json.loads(latest['draft']) if latest['draft'] else None
            same = (int(latest['draft_rev']) == draft_rev and (
                current is None or (current['title'], _final_summary(current['summary'], True), current['due']) == (title, summary, due)))
            if not same:
                conn.rollback()
                raise ValueError('The draft changed after you saw it; nothing added. Run accept again to see the new one.')
        try:
            from .hermes_tingle import _excluded_work
        except ImportError:
            from hermes_tingle import _excluded_work
        if _excluded_work({'title':title,'summary':summary,'source_ref':data['source_ref']}):
            conn.rollback()
            raise ValueError('Reviewed item is excluded by the profile configuration; nothing added')
        # Existing Gmail refs are account-unqualified. Never report another
        # account's item as accepted, or guess that a legacy collision is safe.
        existing_db = store._connect()
        try:
            collision = existing_db.execute(
                "SELECT id FROM hermes_tingle_items WHERE source_type='email' AND source_ref IN (?,?)",
                (data['source_ref'], "gmail:" + data['source_ref'].split("/",1)[-1])).fetchone()
        finally:
            existing_db.close()
        if collision:
            conn.rollback()
            raise ValueError('Existing item uses this account-unqualified email identity; account cannot be verified. '
                             'Nothing accepted. Review the existing item manually; do not infer it is this account email.')
        current_plan = _checkpoint_plan(conn, candidate)
        if current_plan.get('suffix_model_judgment') and (onboarding or checkpoint_plan is None):
            conn.rollback()
            raise ValueError('Partial-history timing needs the actual per-item informed preview and confirmation; nothing added.')
        if checkpoint_plan is not None and checkpoint_plan != current_plan:
            conn.rollback()
            raise ValueError('Contextual plan changed after preview; review it again. Nothing added.')
        if timing and _timing_snapshot(conn, lead_id) != timing['snapshot']:
            conn.rollback()
            raise ValueError('Timing source or assessment changed during confirmation; retry acceptance.')
        icon_row = conn.execute("SELECT r.topic_emoji,r.review_plan FROM assessment_results r JOIN assessments a ON a.id=r.assessment_id WHERE r.candidate_id=? ORDER BY a.created DESC LIMIT 1", (lead_id,)).fetchone()
        item = store.add_item(topic_emoji=icon_row['topic_emoji'] if icon_row else '📌', title=title, summary=summary, due_at=None if due == '-' else due,
                              source_type='email', source_ref=data['source_ref'],
                              onboarding_scan=row['id'] if onboarding else None,
                              completion_signal='source_email_checkpoint',
                              review_plan=current_plan)
        if item.get('deduplicated'):
            conn.rollback()
            raise ValueError('Concurrent account-unqualified email collision; nothing accepted. Review the existing item manually.')
        conn.execute("UPDATE candidates SET state='accepted',payload='{}',draft='',item_id=?,stage=? WHERE id=?",
                     (item['id'], STAGE_ACCEPTED, candidate['id']))
        _purge_assessments(conn)
        conn.commit()
        timing_note = ''
        if timing and timing.get('proposal'):
            proposal = timing['proposal']
            try:
                made = expiry.create_nudge(item['id'], proposal, now=now())
            except (ValueError, store.OperationBusy):
                made = False  # The independently approved item is already added; never misreport it as absent.
            timing_note = (f" Extra one-off expiry suggestion eligible at {proposal['fire_at']} (from the email, not verified; not a delivery guarantee)."
                           if made else ' No extra expiry suggestion created; ordinary reminders unchanged.')
        if timing:
            timing_note += timing.get('note', '')
        return f"✅ Accepted as shared profile item {item['id']} - see it with /tingle list. No standing source grant changed." + timing_note


BULK_MAX = CAP
PREVIEW_UNITS = 1400  # Full fields plus choice labels fit the pinned Discord 2000-char control prompt.


def _units(text):
    return len(text.encode("utf-16-le")) // 2


def _preview_pages(text):
    # Split without dropping or eliding any character, including long edited fields.
    pages, page = [], ""
    for char in text:
        if _units(page + char) > PREVIEW_UNITS:
            pages.append(page)
            page = ""
        page += char
    if page:
        pages.append(page)
    return pages


def _bulk_accept(ctx, ids, source, expected):
    """Owner-selected IDs or one scan's normal-review set; exact fields shown, one confirmation.
    Each item then runs the normal single-item checks on its own, so a failure never hides or adds another."""
    ids = list(dict.fromkeys(ids))
    if len(ids) > BULK_MAX:
        raise ValueError(f"⚠️ Track up to {BULK_MAX} leads at a time, by ID. Nothing was added.")
    conn = connect()
    try:
        blocks, plans = [], {}
        for lead in ids:
            candidate = conn.execute("SELECT * FROM candidates WHERE id=?", (lead,)).fetchone()
            if not candidate:
                raise ValueError(f"⚠️ {lead} is unavailable (already tracked, skipped, discarded or expired). Nothing was added.")
            row = authorize_scan(conn, candidate['scan_id'], source, expected)
            if candidate['generation'] != row['generation'] or candidate['state'] != 'pending':
                raise ValueError(f"⚠️ {lead} is no longer pending. Nothing was added.")
            rev = int(candidate['draft_rev']) if candidate['draft'] else 0
            if rev:
                draft = json.loads(candidate['draft'])
                title, summary, due = draft['title'], draft['summary'], draft['due']
            else:
                title, summary, due = _candidate_fields(conn, candidate)
            summary = _final_summary(summary, drafted=bool(rev))
            if len(title) > 180 or not title.strip() or len(summary) > 1200:
                raise ValueError(f"⚠️ {lead} has a title or summary outside the allowed length. Nothing was added.")
            plan = _checkpoint_plan(conn, candidate)
            plans[lead] = (candidate['generation'], title, summary, due, rev, plan)
            blocks.append(f"{lead}{' (draft saved in chat)' if rev else ''}\nTitle: {title}\nSummary: {summary}\nDue: {'none' if due == '-' else due}\n{_checkpoint_preview(plan)}")
    finally:
        conn.close()
    preview = (f"➕ Add these {len(ids)} items to the SHARED profile? (everyone using this profile will see them)\n\n"
               + "\n\n".join(blocks)
               + "\n\nEach comes from an untrusted email and is not a verified commitment; no deadline is invented. Edited items were drafted by the assistant and may reflect email text; only your Add below approves them. "
                 "Each is checked and added on its own, so one that cannot be added does not stop the others. Email checks need an existing standing source grant; import does not change it. " + (_timing_accept_disclosure() if any(_has_timing_id(lead) for lead in ids) else ''))
    pages = _preview_pages(preview)
    for index, page in enumerate(pages):
        final = index == len(pages) - 1
        question = f"SHARED profile preview {index + 1}/{len(pages)}\n" + page
        choices = ([{'value': 'accept', 'label': f'Add these {len(ids)}'}] if final else
                   [{'value': 'next', 'label': 'Next preview page'}]) + [{'value': 'cancel', 'label': 'Cancel'}]
        if ctx.ask_choice(question, choices) != ('accept' if final else 'next'):
            return 'Cancelled; nothing added.'
    lines, added = [], 0
    for lead in ids:
        generation, title, summary, due, rev, plan = plans[lead]
        conn = connect()
        try:
            timing = _timing_plan(ctx, conn, source, expected, lead, due)
            result = _commit_lead(conn, source, expected, lead, generation, title, summary, due, rev, timing, plan)
            added += 1
            lines.append(result.replace('✅ Accepted as shared profile item', f'✅ {lead} →').replace(' - see it with /tingle list. No standing source grant changed.', ''))
        except ValueError as exc:
            lines.append(f"⚠️ {lead}: {str(exc).splitlines()[0]} Nothing added for it.")
        finally:
            conn.close()
    return f"{'✅' if added == len(ids) else '⏸'} {added} of {len(ids)} added.\n" + "\n".join(lines) + "\nSee them with `/tingle list`."



def _results_args(args, tail):
    """Optional AS-ID (the exact results set) then optional page number."""
    snapshot, page = None, 1
    rest = list(args)
    if rest and re.fullmatch(r'AS-[A-F0-9]{8}', rest[0]):
        snapshot = rest.pop(0)
    if rest:
        if len(rest) != 1 or not rest[0].isdigit():
            raise ValueError('That results page is unavailable. ' + tail)
        page = int(rest[0])
    return snapshot, page


def _track_all(ctx, scan_id, source, expected, *, shortlist=False, page=1, snapshot=None):
    conn = connect()
    try:
        row = authorize_scan(conn, scan_id, source, expected)
        visible, suppressed = _pending_partition(conn, row['id'])
        eligible = [c for c in visible if c['generation'] == row['generation']]
        if shortlist:
            # Only the still-pending picks of this one stable results page; never a refilled or wider set.
            a = _latest_assessment(conn, row['id'])
            if not a:
                raise ValueError('No model assessment exists for this scan, so there are no picks to preview. Nothing added.')
            if snapshot != a['id']:
                raise ValueError('That results set was replaced by a newer assessment, so it will not be previewed. Nothing added. '
                                 f"See the current picks with `/tingle lookback results {row['id']}`.")
            if page < 1 or page > max(int(a['pages']), 1):
                raise ValueError('That results page is unavailable. Nothing added.')
            ids = [r['candidate_id'] for r in _page_rows(conn, a, row['generation'], page)] if a['pages'] else []
        else:
            ids = [c['id'] for c in eligible]
        checked(source, expected)
    finally:
        conn.close()
    if not ids:
        return ('No picks left on that page. Nothing added. ' if shortlist else
                'No pending leads in normal review. Nothing added. ') + _suppression_note(scan_id, len(suppressed))
    return _bulk_accept(ctx, ids, source, expected)


def _trace(conn, source, expected, scan_id, fragment):
    """Owner-only local lookup: which staged candidates match a fragment the owner typed, and the
    content-free stage code each one carries. Nothing else is read, no content is repeated beyond
    the typed fragment, and nothing is exported, logged or sent anywhere. Works only while the
    scan's rows still exist (scan TTL); accepted/skipped rows no longer carry a payload to match."""
    row = authorize_scan(conn, scan_id, source, expected)
    needle = fragment.strip().casefold()
    if len(needle) < 3:
        raise ValueError("Type at least 3 characters to match: /tingle lookback trace LB-ID sender-or-subject-words. Nothing changed.")
    matches = []
    for candidate in conn.execute("SELECT id,state,stage,payload FROM candidates WHERE scan_id=? ORDER BY id",
                                  (row['id'],)):
        data = json.loads(candidate['payload'])
        haystack = ' '.join(str(data.get(key, '')) for key in ('subject', 'from')).casefold()
        if needle in haystack:
            matches.append(candidate)
    authorize_scan(conn, row['id'], source, expected)
    if not matches:
        return (f"🔎 {row['id']}: no staged candidate matches what you typed. That is inconclusive: the message "
                "may never have reached staging (the coverage counters stay the only record of those), its rows "
                "may have expired or already been handled, or the words differ. Nothing else was checked.")
    lines = [f"🔎 {row['id']}: {len(matches)} staged candidate{'s' if len(matches) != 1 else ''} match. "
             "Stage codes only; no message content is repeated."]
    for candidate in matches[:10]:
        code = candidate['stage'] if candidate['stage'] in STAGES else 'not recorded'
        lines.append(f"{candidate['id']} · {code} · {candidate['state']}")
    if len(matches) > 10:
        lines.append(f"First 10 shown of {len(matches)}.")
    lines.append("Code meanings: `/tingle lookback help`. A code says which stage kept or removed the conversation, "
                 "not whether the message matters.")
    return '\n'.join(lines)


def _assess_command(ctx, source, expected, scan_id):
    """Explicit, per-run owner approval for the model step on an existing scan."""
    conn = connect()
    try:
        row = authorize_scan(conn, scan_id, source, expected)
        if row['state'] != 'consumed':
            raise ValueError("⚠️ That scan is not finished or is no longer available. Nothing was sent to a model.")
        if int(row['assess_runs']) >= 2:
            raise ValueError("⚠️ This scan has already used its model-assessment runs. Nothing was sent.")
        count = min(len(_eligible_groups(conn, row)), assess.MAX_CONVERSATIONS)
    finally:
        conn.close()
    if not count:
        return "No conversations are eligible for a model assessment. Nothing was sent."
    pick = ctx.ask_choice(f"Run the model step on scan {scan_id}?\n\n" + _assess_disclosure(count) +
                          "\n\nThis approves one run for this scan only.",
                          [{'value': 'assess', 'label': 'Run it once'}, {'value': 'cancel', 'label': 'Cancel'}])
    if pick != 'assess':
        return "Cancelled. Nothing was sent to a model."
    conn = connect()
    try:
        authorize_scan(conn, scan_id, source, expected)
        conn.execute("UPDATE scans SET assess_grant=? WHERE id=? AND state='consumed'", (assess.VERSION, scan_id))
        conn.commit()
    finally:
        conn.close()
    _assess_scan(ctx, source, expected, scan_id)
    conn = connect()
    try:
        row = authorize_scan(conn, scan_id, source, expected)
        text = _results_text(conn, row)
        authorize_scan(conn, scan_id, source, expected)
        return text
    finally:
        conn.close()


def handle(ctx, parts, source):
    expected = binding(source)
    cleanup = connect()
    try:
        expire(cleanup)
    finally:
        cleanup.close()
    if not parts:
        return ("🔎 One-off email look-back\n\n"
                "A one-time, owner-only review of your recent Gmail. Nothing is tracked unless you say so, and no ongoing scan starts.\n\n"
                "- Start: `/tingle lookback start 14` (any number of days from 1 to 30; you see the details and confirm first)\n"
                "- Walk through a scan's leads: `/tingle lookback review LB-ID`\n"
                "- Everything else and the full privacy details: `/tingle lookback help`")
    if parts[0] == 'help':
        return (_full_disclosure() + "\n\n"
                "\nTiming details\n"
                "- Inspect item timing: /tingle timing HT-ID (owner DM); recover context: /tingle replan HT-ID\n\n"
                "Commands\n"
                "- Start: /tingle lookback start <days 1-30>\n"
                "- Review: /tingle lookback list LB-ID [page], or add `details` for coverage counters\n"
                "- Read one: /tingle lookback view LC-ID [page]\n"
                "- Every ID in a conversation: /tingle lookback thread LC-ID [page]\n"
                "- Model picks: /tingle lookback results LB-ID [AS-ID] [page] (pages are fixed once made; handled picks just disappear)\n"
                "- Model step for an existing scan: /tingle lookback assess LB-ID (asks first; one run per approval, at most two per scan)\n"
                "- Stop a running model step: /tingle lookback stop\n"
                "- Preview picks: /tingle lookback track-top LB-ID AS-ID [page] (the named results set and page only, exact fields then confirm; a replaced set is refused)\n"
                "- Track: /tingle lookback accept LC-ID (Tingle prepares the title and summary, or shows the draft you asked for in chat; you confirm before anything is added. Typing your own: accept LC-ID \"title\" \"summary\" \"due ISO with offset or -\")\n"
                "- Track a few at once: /tingle lookback accept LC-ID LC-ID (up to 100 IDs you pick; you see every title, summary and due, then confirm once)\n"
                "\nAdvanced options\n"
                "- Track normal-review leads in one scan: /tingle lookback track-all LB-ID (up to 100; full preview pages, one final confirmation; suppressed matches excluded)\n"
                "- Recover noisy matches: /tingle lookback suppressed LB-ID [page] (private IDs, then view or accept one)\n"
                "- Trace one conversation's stage: /tingle lookback trace LB-ID sender-or-subject-words "
                "(staged, suppressed_routine, skipped_draft, skipped_owner_last, capped, request_failed, "
                "request_not_sent, no_valid_verdict, model_not_action, pick_unsupported, pick_changed, "
                "pick_skipped_by_rule, pick_listed, accepted; codes only, no content repeated)\n"
                "- Change the wording: just tell me in chat what you want; I save a draft, then you run accept and confirm. I can't add anything myself\n"
                "- Pass: /tingle lookback skip LC-ID\n"
                "- Clear a scan: /tingle lookback discard LB-ID\n"
                "- Undo the onboarding import: /tingle undo onboarding (removes only the items that import added)\n"
                "- Teach it: /tingle lookback preference \"rule text\"\n\n"
                "Accept needs your own reviewed fields: nothing is added without them, "
                "and accepted items become profile-global.\n\n"
                "Example: /tingle lookback start 14")
    if parts[0] == 'start' and len(parts) == 2:
        if not parts[1].isdigit():
            raise ValueError(_usage(parts))
        return scan(ctx, source, int(parts[1]))
    if parts[0] == 'assess' and len(parts) == 2:
        return _assess_command(ctx, source, expected, parts[1])
    if parts[0] == 'stop' and len(parts) == 1:
        return _stop(ctx, source, expected)
    if (parts[0] == 'track-all' and len(parts) == 2) or (parts[0] == 'track-top' and len(parts) in (3,4)):
        snapshot, page = _results_args(parts[2:], 'Nothing added.') if parts[0] == 'track-top' else (None, 1)
        if parts[0] == 'track-top' and not snapshot:
            raise ValueError('Name the results set to preview, as shown under the picks: `/tingle lookback track-top LB-ID AS-ID [page]`. Nothing added.')
        return _track_all(ctx, parts[1], source, expected, shortlist=parts[0] == 'track-top', page=page, snapshot=snapshot)
    if parts[0] == 'track-top':
        raise ValueError('Name the results set to preview, as shown under the picks: `/tingle lookback track-top LB-ID AS-ID [page]`. Nothing added.')
    if parts[0] == 'review' and len(parts) == 2:
        return review(ctx, source, parts[1])
    if parts[0] == 'accept' and len(parts) >= 3 and all(re.fullmatch(r'LC-[A-Fa-f0-9]+', p) for p in parts[1:]):
        return _bulk_accept(ctx, parts[1:], source, expected)
    conn = connect()
    try:
        if parts[0] in ('discard', 'revoke') and len(parts) == 2:
            # Revocation remains available after expiry and is retryable/idempotent.
            # It does not reopen read, list, view or acceptance authority.
            row = conn.execute('SELECT * FROM scans WHERE id=? AND principal=?',(parts[1],expected[2])).fetchone()
            if not row:
                raise ValueError("⚠️ That scan is unavailable (it may be discarded or expired). Start a new one with `/tingle lookback start 14`.")
            checked(source, expected)
            conn.execute('BEGIN IMMEDIATE')
            conn.execute("DELETE FROM assessment_results WHERE assessment_id IN (SELECT id FROM assessments WHERE scan_id=?)", (row['id'],))
            conn.execute("DELETE FROM assessments WHERE scan_id=?", (row['id'],))
            conn.execute("DELETE FROM candidates WHERE scan_id=?", (row['id'],))
            conn.execute("UPDATE scans SET state='revoked',generation=generation+1,coverage='owner discarded' WHERE id=?", (row['id'],))
            try:
                from . import hermes_tingle_body_audit as audit
            except ImportError:
                import hermes_tingle_body_audit as audit
            # Invalidate grant first. A cleanup failure cannot leave a readable grant;
            # a later authenticated revoke retries cleanup even for revoked/expired rows.
            conn.commit()
            audit.clear(origin='lookback:'+row['id'])
            return "One-off permission revoked and private candidates discarded. Standing sources/items/history unchanged."
        if parts[0] == 'results' and len(parts) in (2,3,4):
            row = authorize_scan(conn, parts[1], source, expected)
            snapshot, page = _results_args(parts[2:], 'Nothing changed.')
            result = _results_text(conn, row, page, snapshot)
            authorize_scan(conn, row['id'], source, expected)
            return result
        if parts[0] == 'trace' and len(parts) >= 3:
            return _trace(conn, source, expected, parts[1], ' '.join(parts[2:]))
        if ((parts[0] == 'list' and len(parts) in (2, 3) and parts[-1].casefold() != 'details') or
                (parts[0] == 'thread' and len(parts) in (2, 3))):
            page = int(parts[2]) if len(parts) == 3 else 1
            if page < 1:
                raise ValueError('Page must be positive')
            if parts[0] == 'thread':
                candidate = conn.execute("SELECT * FROM candidates WHERE id=? AND state='pending'", (parts[1],)).fetchone()
                if not candidate:
                    raise ValueError('That private lead is unavailable.')
                row = authorize_scan(conn, candidate['scan_id'], source, expected)
                pool = conn.execute("SELECT * FROM candidates WHERE scan_id=? AND state='pending'", (row['id'],)).fetchall()
                selected = next(g for g in _groups(pool) if any(c['id'] == candidate['id'] for c in g['members']))
                groups = [_groups([c])[0] for c in selected['members']]
            else:
                row = authorize_scan(conn, parts[1], source, expected)
                visible, suppressed = _pending_partition(conn, row['id'])
                groups = _groups(visible)
            shown = groups[(page-1)*5:page*5]
            lines = [f"🔎 {row['id']} · {len(groups)} {'messages' if parts[0] == 'thread' else 'conversations'}.",
                     f"{_coverage_label(row['coverage'])}. UNTRUSTED leads. Nothing tracked yet."]
            for group in shown:
                lines.append(_group_line(group))
                data = group['newest']
                lines.append('  ' + _display(data.get('snippet', '') or 'No excerpt available', 70))
                if 'Incomplete:' in str(data.get('context_status', '')) or str(data.get('context_status', '')).startswith('Body/thread cap'):
                    lines.append('  Context incomplete; open original email before deciding.')
                if group['candidate']['draft']:
                    lines.append('  ✏️ draft saved from chat')
            if len(groups) > page*5:
                lines.append(f"More: `/tingle lookback {parts[0]} {parts[1]} {page+1}`")
            if shown:
                lines.append(f"Walk through these: `/tingle lookback review {row['id']}`.")
            if any(g['candidate']['draft'] for g in shown):
                lines.append('✏️ saved chat drafts are used in the exact preview.')
            if parts[0] == 'list' and suppressed:
                lines.append(_suppression_note(row['id'], len(suppressed)))
            if parts[0] == 'list':
                lines.append(f"Coverage details: `/tingle lookback list {row['id']} details`.")
            lines.append(f"Private for {RETENTION_DAYS} days. More options: `/tingle lookback help`.")
            authorize_scan(conn, row['id'], source, expected)
            return '\n'.join(lines)
        if parts[0] in ('list', 'suppressed') and len(parts) in (2, 3):
            row = authorize_scan(conn, parts[1], source, expected)
            if len(parts) == 3 and parts[2].casefold() == 'details':
                checked(source, expected)
                authorize_scan(conn, row['id'], source, expected)
                text = (f"{row['id']} scan detail: {row['coverage']} "
                        f"Listed {row['listed']}, date-checked {row['checked']}, stored {row['staged']}. "
                        "Counters are diagnostics, not coverage guarantees.")
                tally = conn.execute("SELECT stage, COUNT(*) AS n FROM candidates WHERE scan_id=? "
                                     "GROUP BY stage ORDER BY n DESC", (row['id'],)).fetchall()
                if tally:
                    text += " Candidate-row stages (messages, not conversation totals): " + ", ".join(f"{r['stage'] if r['stage'] in STAGES else 'not recorded'} {r['n']}" for r in tally) + "."
                if row['error_detail']:
                    text += f" Failure detail: {row['error_detail']}."
                return text
            page = int(parts[2]) if len(parts) == 3 else 1
            if page < 1:
                raise ValueError('Page must be positive')
            visible, suppressed = _pending_partition(conn, row['id'])
            rows = suppressed[(page-1)*5:page*5+1]
            checked(source, expected)
            shown = rows[:5]
            lines = [f"🔎 {row['id']} · {len(suppressed)} suppressed matches.",
                     "UNTRUSTED email leads, not verified commitments. Nothing tracked yet.",
                     'Kept privately, not deleted. Read or accept any ID below.']
            for candidate in shown:
                data = json.loads(candidate['payload'])
                lines.append(f"{candidate['id']} · {_display(data.get('subject', ''), 70)}")
            if len(rows) > 5:
                lines.append(f"More: `/tingle lookback suppressed {row['id']} {page+1}`")
            if shown:
                lines.append(f"Open one: `/tingle lookback view {shown[0]['id']}`. Its view offers Track or Skip.")
            lines.append(f"Private for {RETENTION_DAYS} days. More options: `/tingle lookback help`.")
            authorize_scan(conn, row['id'], source, expected)
            return '\n'.join(lines)
        if parts[0] == 'view' and len(parts) in (2,3):
            candidate = conn.execute("SELECT * FROM candidates WHERE id=? AND state='pending'", (parts[1],)).fetchone()
            if not candidate:
                raise ValueError("⚠️ That lead is unavailable (already tracked, skipped, discarded or expired). Check the scan again with `/tingle lookback list LB-ID`.")
            row = authorize_scan(conn,candidate['scan_id'],source,expected)
            if row['generation'] != candidate['generation']:
                raise ValueError('Candidate generation changed')
            page = int(parts[2]) if len(parts)==3 else 1
            if page < 1:
                raise ValueError('Page must be positive')
            data=json.loads(candidate['payload'])
            text=_readable_thread(data['thread_context'])
            pages=max(1,(len(text)+3999)//4000)
            if page > pages:
                raise ValueError('No context on this page')
            authorize_scan(conn,row['id'],source,expected)
            return (f"📖 {candidate['id']} - UNTRUSTED email text, page {page}/{pages}. "
                    + _clean(data.get('subject', ''), 120) + '\n' + _clean(data.get('snippet', ''), 200) + '\n'
                    +data['context_status']+"\n\n"+text[(page-1)*4000:page*4000]+
                    f"\n\nNot proof of a current commitment or unanswered request. "
                    f"Track it: `/tingle lookback accept {candidate['id']}` · skip: `/tingle lookback skip {candidate['id']}`")
        if parts[0] == 'preference' and len(parts) == 2:
            checked(source, expected)
            # Explicit typed rule only; never copies candidate snippets automatically.
            feedback = store.capture_feedback_candidate(parts[1], source_ref='owner:lookback-preference')
            return f"Preference pending, not active. Approve separately: /tingle rule approve {feedback['id']}"
        if parts[0] in ('accept', 'skip') and len(parts) >= 2:
            candidate = conn.execute("SELECT * FROM candidates WHERE id=?", (parts[1],)).fetchone()
            if not candidate:
                raise ValueError("⚠️ That lead is unavailable (already tracked, skipped, discarded or expired). Check the scan again with `/tingle lookback list LB-ID`.")
            row = authorize_scan(conn, candidate['scan_id'], source, expected)
            if candidate['generation'] != row['generation'] or candidate['state'] != 'pending':
                raise ValueError('Candidate is no longer pending in this generation')
            if parts[0] == 'skip' and len(parts) != 2:
                raise ValueError("⚠️ Skip takes just the lead ID: `/tingle lookback skip LC-ID`. Nothing changed.")
            if parts[0] == 'skip' and len(parts) == 2:
                authorize_scan(conn, row['id'], source, expected)
                conn.execute("UPDATE candidates SET state='skipped',payload='{}',draft='' WHERE id=?", (candidate['id'],))
                _purge_assessments(conn)
                conn.commit()
                return 'Skipped only for this scan; no permanent preference recorded.'
            if parts[0] == 'accept' and len(parts) in (3, 4):
                raise ValueError('Accept takes either just the lead ID (Tingle prepares a title and summary for you to confirm) '
                                 'or all three quoted fields: /tingle lookback accept LC-ID "title" "summary" "due or -". '
                                 'Nothing was added.')
            if parts[0] == 'accept' and len(parts) in (2, 5):
                prepared = len(parts) == 2
                draft_rev = int(candidate['draft_rev']) if candidate['draft'] else 0
                if prepared and draft_rev:
                    draft = json.loads(candidate['draft'])
                    title, summary, due = draft['title'], draft['summary'], draft['due']
                elif prepared:
                    title, summary, due = _candidate_fields(conn, candidate)
                else:
                    title, summary, due = parts[2:]
                if prepared:
                    summary = _final_summary(summary, drafted=bool(draft_rev))
                if not title.strip() or len(title) > 180 or len(summary) > 1200:
                    raise ValueError('Provide a reviewed title up to 180 chars and summary up to 1200')
                if due != '-':
                    try:
                        stamp = datetime.fromisoformat(due)
                    except ValueError:
                        raise ValueError('⚠️ The due time should look like 2026-12-01T09:00:00+01:00 (with the offset), or - for no deadline. Nothing was added.') from None
                    if stamp.tzinfo is None:
                        raise ValueError('Due time needs an explicit UTC offset; use - for no deadline')
                data = json.loads(candidate['payload'])
                store.validate_source_ref(data['source_ref'])
                preview = ("➕ Add this to the SHARED profile? (everyone using this profile will see it)\n\n"
                           f"Title: {title}\n"
                           f"Summary: {summary}\n"
                           f"Due: {'none' if due == '-' else due}\n\n"
                           "Written from an untrusted email, not a verified commitment" + (", and no deadline is set" if due == '-' else "") + ". "
                           "Email checks need an existing standing source grant; import does not change it. " + (_timing_accept_disclosure() if _has_timing(conn, candidate['id']) else ''))
                if prepared and draft_rev:
                    preview += (" These fields were drafted by the assistant in chat and may reflect text from the email, so read them: nothing in them proves it is your wording."
                                " Only your Add below approves it. Wrong wording? Cancel and tell me what to change; a new draft replaces this one.")
                elif prepared:
                    preview += (" Wrong wording? Cancel, then tell me in chat what to change: I save a draft and you confirm again."
                                " (Typing your own fields is in `/tingle lookback help`.)")
                plan = _checkpoint_plan(conn, candidate)
                preview += "\n" + _checkpoint_preview(plan)
                preview += f"\nRef: {data['source_ref']}"
                if ctx.ask_choice(preview, [{'value': 'accept', 'label': 'Add this shared item'}, {'value': 'cancel', 'label': 'Cancel'}]) != 'accept':
                    return 'Acceptance cancelled; nothing added.'
                timing = _timing_plan(ctx, conn, source, expected, candidate['id'], due)
                return _commit_lead(conn, source, expected, candidate['id'], candidate['generation'], title, summary, due,
                                    draft_rev if prepared else None, timing, plan)
        raise ValueError(_usage(parts))
    finally:
        conn.close()


def _timing_disclosure():
    return ('The model also returns unverified expiry claims. Clear date-only claims can add an extra one-off '
            'suggestion at 10:00 local time on the day before the source date; uncertain claims ask separately. '
            'Importing is independent of that answer. During onboarding, clear claims can also add the one-off; '
            'uncertain claims are reported without timing. Ordinary due dates and '
            'reminder attempts are unchanged. A private queue retains item id, fire time, normalized source date, '
            'inferred category, tier/state, creation time, eligibility-end and expiry time for at most 45 days, '
            'deleted on emission/drop/completion/dismissal. The accepted lead payload is cleared; sibling leads may retain shared thread excerpts until their scan expires or is discarded. '
            '\n\nExpiry logging\nA separate emission log retains item id, attempt time, state and expiry time until that same TTL; '
            'it records an attempt, not delivery. Derived dates are not copied into ordinary item exports or item-history views. Alert/command text still travels through gateway/session history as disclosed. '
            '\n\nExpiry limits\nThe source date and its attribution can be wrong; semantic accuracy is unmeasured. '
            'The date-only eligibility window ends at the start of the source date as a heuristic, not a '
            'verified expiry cutoff. Cancel with /tingle expiry cancel HT-ID. '
            '\n\nExpiry preferences\nYour editable preferences file is read-only to the plugin and outranks '
            'owner-command and learned database rules. Three consecutive '
            'explicit declines in an inferred category suggest a visible rule requiring approval. '
            'Category counters retain one bounded streak and last prompt fingerprint, not source content or '
            'a response history; corrections/acceptance reset it. Suppressions retain rule/category/time '
            'for at most 45 days. Reset learned rules with /tingle rules reset learned.')


def _timing_snapshot(conn, lead):
    row = conn.execute('SELECT c.*,r.expiry,r.quote,a.id AS assessment,a.version FROM candidates c '
                       'JOIN assessment_results r ON r.candidate_id=c.id '
                       'JOIN assessments a ON a.id=r.assessment_id WHERE c.id=? ORDER BY a.created DESC LIMIT 1',
                       (lead,)).fetchone()
    if not row:
        return ''
    proposal = json.loads(row['expiry']) if row['expiry'] else {}
    current = _group_state(conn, proposal.get('_source_ids', [lead]))
    return hashlib.sha256((repr(tuple(row)) + current).encode()).hexdigest()


def _timing_plan(ctx, conn, source, expected, lead, due):
    candidate = conn.execute('SELECT * FROM candidates WHERE id=?', (lead,)).fetchone()
    if not candidate:
        raise ValueError('Candidate unavailable or revoked; nothing added.')
    authorize_scan(conn, candidate['scan_id'], source, expected)
    row = conn.execute('SELECT r.expiry,r.quote,a.version FROM assessment_results r '
                       'JOIN assessments a ON a.id=r.assessment_id WHERE candidate_id=? '
                       'ORDER BY a.created DESC LIMIT 1', (lead,)).fetchone()
    if not row or row['version'] != assess.VERSION or not row['expiry'] or due != '-':
        return None
    proposal = json.loads(row['expiry'])
    if _group_state(conn, proposal.get('_source_ids', [lead])) != proposal.get('_source_fingerprint'):
        return None  # A source member changed since assessment; do not trust the stale timing claim.
    snapshot = _timing_snapshot(conn, lead)
    result = {'snapshot': snapshot, 'proposal': None, 'note': ''}
    try:
        rule = expiry_prefs.suppression(proposal['category'])
    except ValueError as exc:
        result['note'] = ' ' + str(exc)
        return result
    if rule:
        expiry_prefs.log_suppressed(proposal['category'], rule)
        result['note'] = f' Expiry suggestion suppressed by rule {rule}; ordinary reminders unchanged.'
        return result
    # Recheck scan-time proposals at interaction time; never offer a past instant.
    fire = proposal.get('fire_at')
    if not fire or datetime.fromisoformat(fire) <= now():
        result['note'] = ' The email may have a time limit; it may already have expired or its date is uncertain. No extra reminder set.'
        return result
    if proposal['tier'] == 'automatic':
        result['proposal'] = proposal
        return result
    display_fire = datetime.fromisoformat(fire).astimezone(local_timezone()).isoformat()
    question = (f"The email may have a time limit (not verified). Source quote: {_display(row['quote'], 200)}\n"
                f"Extra one-off reminder eligible from {display_fire}? Date-only timing is a heuristic, not a known expiry cutoff. "
                "This answer never blocks importing the item. Choose a different time using an explicit owner reminder command after import.")
    answer = ctx.ask_choice(question, [{'value': 'yes', 'label': 'Add extra one-off'},
                                     {'value': 'no', 'label': 'No extra reminder'},
                                     {'value': 'correction', 'label': 'I want another time'},
                                     {'value': 'cancel', 'label': 'Leave timing unchanged'}])
    authorize_scan(conn, candidate['scan_id'], source, expected)
    if _timing_snapshot(conn, lead) != snapshot:
        raise ValueError('Timing source changed during the question; retry acceptance.')
    suggestion = expiry_prefs.answer(proposal['category'], snapshot, answer)
    if suggestion:
        result['note'] += (f" You've declined 3 {proposal['category']} expiry suggestions (inferred category). "
                           f"Stop suggesting them? Review /tingle rules; approve /tingle rule approve {suggestion['id']}.")
    if answer == 'correction':
        result['note'] += ' No extra timing set. After import, choose an exact offset time with /tingle expiry at HT-ID ISO-instant YYYY-MM-DD-source-date.'
    if answer == 'yes':
        result['proposal'] = dict(proposal, tier='confirmed')
    return result


def _timing_accept_disclosure():
    return ('If an assessed expiry claim qualifies, an extra one-off may be created (uncertain cases ask separately). '
            'Private retained metadata: item id, fire time, normalized source date, inferred category, tier/state, '
            'creation/eligibility-end/expiry times, at most 45 days and deleted on emission/drop/completion/dismissal. '
            'No extra quote retained; the accepted payload is cleared, but sibling leads may retain shared excerpts until scan expiry/discard. A separate attempt log keeps item id/time/state/TTL '
            'until the same expiry, not a delivery receipt. Ordinary timing stays unchanged. '
            'Day-before 10:00 local is a heuristic, not a verified expiry cutoff; source attribution can be wrong. '
            'Cancel the extra one-off with /tingle expiry cancel HT-ID.')


def _has_timing(conn, lead):
    return bool(conn.execute("SELECT 1 FROM assessment_results r JOIN assessments a ON a.id=r.assessment_id "
                             "WHERE r.candidate_id=? AND r.expiry!='' AND a.version=?", (lead, assess.VERSION)).fetchone())


def _has_timing_id(lead):
    conn = connect()
    try:
        return _has_timing(conn, lead)
    finally:
        conn.close()




def short_title(text, limit=72):
    # Untrusted email-derived output: readable title only, no markup or links.
    text = html.unescape(str(text or 'Follow up'))
    text = re.sub(r'<[^>]*>|https?://\S+|www\.\S+', ' ', text)
    text = re.sub(r'\[([^]]*)\]\([^)]*\)', r'\1', text)
    text = ''.join(ch if ch.isprintable() else ' ' for ch in text)
    text = re.sub(r'[`*_~#>|\[\]<>]+', '', text)
    text = ' '.join(text.split()) or 'Follow up'
    return text if len(text)<=limit else text[:limit-1].rstrip()+'…'




def failure_reason(detail):
    # Closed wording only. Never echo provider stderr, tokens, URLs or addresses.
    text = str(detail or '').casefold()
    if any(x in text for x in ('timeout','timed out','network','connection reset','temporary failure')):
        return 'the Gmail connection timed out or failed on the network'
    if any(x in text for x in ('invalid_grant','unauthorized','invalid credentials','401','insufficient scope','permission denied')):
        return 'Gmail authorization could not be verified; check the connection'
    if any(x in text for x in ('rate limit','ratelimit','429')):
        return 'Gmail limited the request rate'
    return 'a Gmail read or check failed; the cause is not confirmed'


def _summary_text(conn, row):
    a = _latest_assessment(conn,row['id'])
    if not a:
        return _results_text(conn,row)  # already bounded scan-only/empty reply
    cov = _coverage_label(row['coverage'])
    warnings=[]
    if cov.startswith(('scan incomplete','scan partial')):
        warnings.append('Coverage is incomplete; more may be missed.')
    if str(row['coverage']).startswith('Partial/failed'):
        reason = failure_reason(row['error_detail'])
        warnings.append(reason[:1].upper()+reason[1:]+'.')
    if a['plan_faults']:
        warnings.append(f"{int(a['plan_faults'])} model timing plans were missing or malformed; affected picks were not qualified.")
    if int(a['assessed']) < int(a['considered']) and not a['failed_calls']:
        warnings.append(f"{int(a['considered']) - int(a['assessed'])} conversations not assessed; review is incomplete.")
    if a['failed_calls']:
        missing=max(0,int(a['considered'])-int(a['assessed']))
        failed=int(a['failed_calls'])
        warnings.append(f"{failed} model request{'s' if failed!=1 else ''} failed; {missing} "
                        f"conversation{'s were' if missing!=1 else ' was'} not assessed.")
    if a['over_cap']:
        capped=int(a['over_cap'])
        warnings.append(f"The size limit left {capped} conversation{'s' if capped!=1 else ''} unassessed.")
    rows=conn.execute("SELECT r.action,r.partial,r.topic_emoji FROM assessment_results r JOIN candidates c ON c.id=r.candidate_id "
                      "WHERE r.assessment_id=? AND c.state='pending' AND c.generation=? ORDER BY r.slot",
                      (a['id'],row['generation'])).fetchall()
    if not rows:
        text=('The model step did not complete; nothing ranked or added.' if a['failed_calls'] and not a['assessed']
              else ('No pending model picks remain.' if a['qualified'] else
                    ('Review incomplete; nothing ranked or added.' if a['assessed'] < a['considered'] else
                     'No clear follow-ups found in the checked messages. Nothing added.')))
        return text+('\n'+' '.join(warnings) if warnings else '')+f"\nReview: `/tingle lookback review {row['id']}`. Details in help."
    lines=[f"{len(rows)} possible follow-up{'s' if len(rows)!=1 else ''}, unverified. Nothing added yet."]
    for r in rows[:5]:
        title=short_title(r['action'])
        lines.append(f"{assess.safe_icon(r['topic_emoji'])} {title}")
    if len(rows)>5:
        lines.append(f"Plus {len(rows)-5} more.")
    if any(r['partial'] for r in rows):
        warnings.append('Some threads have partial context; check before acting.')
    if a['excluded']:
        warnings.append(f"{a['excluded']} matched your skip rules and were left out.")
    if warnings:lines.append(' '.join(warnings))
    lines.append(f"Review: `/tingle lookback review {row['id']}`. Evidence and full detail are in review/results/help.")
    return '\n'.join(lines)


def replan(ctx, source, item_id):
    """Explicit single-item recovery, not a pending-plan polling job."""
    expected = binding(source)
    item = store.get_item(item_id)
    if (not item or item.get('completion_signal') != 'source_email_checkpoint'
            or item.get('status') not in ('open','waiting','blocked')
            or item.get('next_check_at') or item.get('due_at') or item.get('hard_deadline')):
        raise ValueError('Replan selects one active unscheduled email checkpoint without an explicit deadline. Nothing changed.')
    if not store.source_enabled('email'):
        raise ValueError('Replan needs existing standing email permission; this command does not grant it.')
    if not callable(getattr(ctx,'ask_choice',None)) or not callable(getattr(getattr(ctx,'llm',None),'complete_structured',None)):
        raise ValueError('Replan needs interactive owner confirmation and the configured model facade.')
    disclosure = ('Review timing for this one item? Read current thread metadata and up to four eligible bodies '
        '(at most 20000 characters) under existing source scope and body-read limits. '
        'Existing content-free read-reservation ledgers change, even if the review cannot finish. '
        'One explicit request sends that bounded context to your configured Hermes model, possibly cloud and billed; '
        'Tingle cannot cap or see its cost and host retries may add requests. Model/gateway history and backups may keep copies. '
        'No reminder, schedule or item changes before a second confirmation. Unknown or incomplete context stays unscheduled.')
    if ctx.ask_choice(disclosure,[{'value':'review','label':'Review this item'},{'value':'cancel','label':'Cancel'}]) != 'review':
        return 'Replan cancelled; nothing read or changed.'
    checked(source, expected)
    if store.get_item(item['id']) != item:
        raise ValueError('Item changed before review; nothing scheduled.')
    record, receipt = sources.checkpoint_replan_context(item)
    evidence = {m['n']:(m['text'],m['direction']) for m in record['messages']}
    def fresh():
        checked(source, expected)
        if store.get_item(item['id']) != item:
            raise ValueError('Item changed during replan; nothing scheduled.')
        checks, errors = sources._read_email_checkpoint_checks([item], recovery=True)
        if errors or len(checks)!=1 or checks[0]['receipt'] != receipt:
            raise ValueError('Thread or source permission changed during replan; nothing scheduled.')
    fresh()
    parsed = assess.request(ctx,[record], store.approved_triage_rules())
    fresh()
    res = assess.validate(parsed, {'C1':evidence}).get('C1',{})
    if not res.get('supported') or res.get('resolved'):
        return 'Replan did not establish a current supported next action. Nothing scheduled or completed.'
    plan, reason = assess.checkpoint.diagnose(res.get('review_plan_proposal'),record,evidence,now=now())
    if plan['review_needed']:
        return f'Replan remains unscheduled ({reason}); no timing was guessed.'
    question = ('Set only this item contextual review plan?\nItem: ' + assess.checkpoint._flat(item['title']) +
                '\nProposed next action/outcome: ' + plan['outcome'] + '\n' + _checkpoint_preview(plan) +
                ' This is a check time, not a hard deadline. The title and summary stay unchanged. '
                'The item owner/status follows the proposed next actor. '
                'No notification is sent now; other items are unchanged.')
    if ctx.ask_choice(question,[{'value':'apply','label':'Set this contextual check'},{'value':'cancel','label':'Cancel'}]) != 'apply':
        return 'Replan cancelled; item and schedule unchanged. Read reservations remain.'
    with store.serialized_operation('sources'), store.serialized_operation('reminders'):
        fresh()  # Recheck after owner confirmation, before any item write.
        store.update_item(item['id'],review_plan=plan)
    return 'Contextual check scheduled for this item. No reminder sent; other items unchanged.'
