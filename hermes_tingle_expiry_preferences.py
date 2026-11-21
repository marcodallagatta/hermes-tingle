"""Owner-editable, local expiry suggestion preferences, not triage/item filters.

Ownership protocol: the user-owned file hermes-tingle-preferences.json is
authoritative and the plugin NEVER writes it, so no plugin write can race an
owner edit. Learned and command-added rules live only in the existing
feedback_candidates table (the plugin-owned provenance/applied store). The
file outranks both. Invalid files stop expiry suggestions loudly, never
ordinary tracking/reminders.
"""
from __future__ import annotations

import json
from pathlib import Path
import re

try:
    from . import hermes_tingle_expiry as expiry
except ImportError:
    import hermes_tingle_expiry as expiry

LIMIT = 20
LEARNED_REF = 'expiry-preference:'
EXPLICIT_REF = 'owner command:expiry:'


def path():
    import hermes_constants
    return Path(hermes_constants.get_hermes_home()) / 'plugin-data' / 'hermes-tingle' / 'hermes-tingle-preferences.json'


def load_file():
    """User-owned file rules. Every file rule is explicit by definition."""
    p = path()
    if p.is_symlink() or (p.exists() and not p.is_file()):
        raise ValueError('Invalid expiry preferences file; fix it. No expiry suggestions applied.')
    if not p.exists():
        return []
    if p.stat().st_size > 20000:
        raise ValueError('Expiry preferences file too large; fix it. No expiry suggestions applied.')
    try:
        raw = p.read_bytes()
    except OSError:
        raise ValueError('Expiry preferences file cannot be read; no expiry suggestions applied.') from None
    if len(raw) > 20000:
        raise ValueError('Expiry preferences file too large; fix it. No expiry suggestions applied.')
    try:
        data = json.loads(raw)
        if (not isinstance(data, dict) or set(data) != {'version', 'rules'} or data['version'] != 1
                or not isinstance(data['rules'], list) or len(data['rules']) > LIMIT):
            raise ValueError
        ids = set()
        out = []
        for r in data['rules']:
            if (not isinstance(r, dict) or not {'id', 'text', 'category', 'action'} <= set(r)
                    or not {'id', 'text', 'category', 'action', 'approved_at', 'origin'} >= set(r)
                    or not isinstance(r['id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', r['id'])
                    or r['id'] in ids or not isinstance(r['text'], str) or not 1 <= len(r['text']) <= 200
                    or r['category'] not in expiry.CATEGORIES or r['action'] not in ('suppress', 'resume')):
                raise ValueError
            ids.add(r['id'])
            out.append({'id': r['id'], 'text': r['text'], 'category': r['category'],
                        'action': r['action'], 'origin': 'explicit', 'tier': 'file'})
        return out
    except (ValueError, TypeError):
        raise ValueError('Invalid expiry preferences file; fix it. No expiry suggestions applied.') from None


def _db_rules(conn, tier):
    refs = (LEARNED_REF,) if tier == 'learned' else (EXPLICIT_REF,)
    rows = conn.execute(
        "SELECT id,rule_text,approved_at,source_ref FROM feedback_candidates "
        "WHERE status='approved' AND category='reminders' ORDER BY approved_at ASC,rowid ASC").fetchall()
    out = []
    for row in rows:
        ref = row['source_ref'] or ''
        if not any(ref.startswith(r) for r in refs):
            continue
        category = ref.rsplit(':', 1)[-1]
        if category not in expiry.CATEGORIES:
            continue
        out.append({'id': row['id'], 'text': row['rule_text'], 'category': category,
                    'action': ('resume' if tier == 'explicit' and ':resume:' in ref else 'suppress'), 'origin': tier, 'tier': tier})
    return out


def effective_rules(conn=None):
    """File rules first (authoritative), then owner-command rules, then learned.

    Pass the caller's open store connection when already inside a store write
    transaction: a fresh connection's schema guard would need the write lock.
    """
    rules = load_file()
    own = conn is None
    if own:
        conn = expiry._store()._connect()
    try:
        rules += _db_rules(conn, 'explicit')
        rules += _db_rules(conn, 'learned')
        return rules
    finally:
        if own:
            conn.close()


def suppression(category, conn=None):
    """Winning rule for one category: file beats owner-command beats learned."""
    matches = [r for r in effective_rules(conn) if r['category'] == category]
    if not matches:
        return None
    for tier in ('file', 'explicit', 'learned'):
        tiered = [r for r in matches if r['tier'] == tier]
        if tiered:
            winner = tiered[-1]
            return winner['id'] if winner['action'] == 'suppress' else None
    return None


def explicit_rule(category, action, *, decided_by, evidence):
    """Owner command rule in the plugin-owned store; never writes the user file.

    A user-file rule for the same category still wins; the caller says so.
    """
    if category not in expiry.CATEGORIES or action not in ('suppress', 'resume'):
        raise ValueError('Use expiry category vouchers|returns|trials|other and suppress|resume.')
    store = expiry._store()
    with store.serialized_operation('expiry-preferences'):
        conn = store._connect()
        try:
            previous = [r['id'] for r in _db_rules(conn, 'explicit') if r['category'] == category]
        finally:
            conn.close()
        for rule_id in previous:
            store.resolve_feedback_candidate(rule_id, 'dismissed', decided_by=decided_by, evidence=evidence)
        row = store.capture_feedback_candidate(
            f'{action} expiry suggestions for {category}', category='reminders',
            source_ref=EXPLICIT_REF + action + ':' + category)
        return store.resolve_feedback_candidate(row['id'], 'approved', decided_by=decided_by, evidence=evidence)


def file_rule_for(category):
    return next((r for r in reversed(load_file()) if r['category'] == category), None)


def reset_learned(*, decided_by, evidence):
    """Dismiss learned approvals and clear streaks. File and command rules stay."""
    store = expiry._store()
    conn = store._connect()
    try:
        learned = [r['id'] for r in _db_rules(conn, 'learned')]
    finally:
        conn.close()
    for rule_id in learned:
        store.resolve_feedback_candidate(rule_id, 'dismissed', decided_by=decided_by, evidence=evidence)
    conn = store._connect()
    try:
        conn.execute('DELETE FROM expiry_streaks')
        conn.commit()
    finally:
        conn.close()
    return len(learned)


def log_suppressed(category, rule_id, conn=None):
    own = conn is None
    if own:
        conn = expiry._store()._connect()
    try:
        conn.execute('INSERT OR REPLACE INTO expiry_suppressions(rule_id,category,suppressed_at) VALUES(?,?,?)',
                     (rule_id, category, expiry.utcnow().isoformat()))
        if own:
            conn.commit()
    finally:
        if own:
            conn.close()


def answer(category, prompt_id, choice):
    """Only explicit no counts; yes or correction resets; silence counts nothing.

    Dedupe by the generation-bound prompt id. Persist just one counter per closed
    category, not a response history. Candidate approval remains explicit only.
    """
    if category not in expiry.CATEGORIES or choice not in ('no', 'yes', 'correction'):
        return None
    store = expiry._store()
    with store.serialized_operation('expiry-preferences'):
        conn = store._connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM expiry_streaks WHERE category=?', (category,)).fetchone()
            if row and row['last_prompt'] == prompt_id:
                conn.rollback()
                return None
            count = (int(row['declines']) if row else 0) + 1 if choice == 'no' else 0
            suggested = int(row['suggested']) if row and choice == 'no' else 0
            make = count >= 3 and not suggested
            conn.execute('INSERT OR REPLACE INTO expiry_streaks(category,declines,last_prompt,suggested) VALUES(?,?,?,?)',
                         (category, min(count, 3), prompt_id, int(suggested or make)))
            conn.commit()
        finally:
            conn.close()
        if make:
            return store.capture_feedback_candidate(
                f"Stop suggesting expiry reminders for {category} (inferred category; 3 consecutive declines).",
                category='reminders', source_ref=LEARNED_REF + category)
    return None
