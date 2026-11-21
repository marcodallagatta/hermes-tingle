"""Local-only expiry claims. Form checks are not proof of semantic attribution.

No mailbox/model access. All source text stays transient or in the existing
seven-day pending assessment store; accepted nudges keep derived metadata only.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone

HORIZON_DAYS = 45
CATEGORIES = ('vouchers', 'returns', 'trials', 'other')
_MONTHS = {
    'january': 1, 'januar': 1, 'february': 2, 'februar': 2, 'march': 3, 'märz': 3,
    'april': 4, 'may': 5, 'mai': 5, 'june': 6, 'juni': 6, 'july': 7, 'juli': 7,
    'august': 8, 'september': 9, 'october': 10, 'oktober': 10, 'november': 11,
    'december': 12, 'dezember': 12,
}
MONTH = '(?:' + '|'.join(_MONTHS) + ')'
COMPLETE = re.compile(r'\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}\.?\s+' + MONTH + r'\s+\d{4}|' + MONTH + r'\s+\d{1,2},?\s+\d{4})\b', re.I)
# Broad date-like detection intentionally demotes more than the supported parser accepts.
DATE_LIKE = re.compile(COMPLETE.pattern + r'|\b\d{1,4}[./-]\d{1,2}(?:[./-]\d{1,4})?\b|\b\d{1,2}\.?\s+' + MONTH + r'\b|\b' + MONTH + r'\s+\d{1,2}\b|\b(?:today|tomorrow|yesterday|next week|heute|morgen|gestern|nächste woche)\b', re.I)
END = re.compile(r'\b(?:expires?|ends?|valid until|use by|expires on|gültig bis|endet|verfällt|ablauf)\b', re.I)
DEMO = re.compile(r"\b(?:no|not|never|isn't|wasn't|don't|doesn't|cancelled|canceled|instead|correction|corrected|starts?|begins?|valid from|if|unless|subject to|kein\w*|nicht|statt|korrigiert|beginnt|ab|falls|wenn)\b", re.I)
CLOCK = re.compile(r'\b\d{1,2}:\d{2}(?::\d{2})?\b|\b\d{1,2}\s*(?:am|pm|uhr)\b|\b(?:UTC|GMT|CET|CEST|PST|PDT|EST|EDT|[A-Z][a-z]+/[A-Z][A-Za-z_]+)\b')


def parse_date(text):
    """A deliberately small closed grammar; no model-normalized date is trusted."""
    if not isinstance(text, str) or not COMPLETE.fullmatch(text.strip()):
        return None
    text = text.strip().casefold()
    try:
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', text):
            return date.fromisoformat(text)
        parts = text.replace('.', '').replace(',', '').split()
        if parts[0].isdigit():
            return date(int(parts[2]), _MONTHS[parts[1]], int(parts[0]))
        return date(int(parts[2]), _MONTHS[parts[0]], int(parts[1]))
    except (ValueError, KeyError, IndexError):
        return None


def inferred_category(text):
    """Fallible lexical category, disclosed as inferred, never a personal preference."""
    if re.search(r'\b(voucher|coupon|gutschein|credit)\b', text, re.I):
        return 'vouchers'
    if re.search(r'\b(return|returns|rückgabe|retoure)\b', text, re.I):
        return 'returns'
    if re.search(r'\b(trial|testphase|probeabo)\b', text, re.I):
        return 'trials'
    return 'other'


def classify(claim, *, quote, excerpt, message_date, now, zone, due_at=None,
             reply_watch=False, context_complete=True, direction='received'):
    """Return nothing/uncertain/automatic plus a code-derived proposal.

    Quote provenance does not establish that this date belongs to the open step.
    Only complete date-only claims are proposed: clocks or partial dates remain
    uncertain with no invented time. No item state is read or written here.
    """
    if not isinstance(claim, dict) or not claim.get('span'):
        return None
    span = claim['span']
    parsed = parse_date(span)
    result = {'tier': 'uncertain', 'date': parsed.isoformat() if parsed else '',
              'fire_at': '', 'category': inferred_category(quote), 'reason': 'unverified_claim'}
    if not parsed or direction != 'received' or span not in quote or due_at or reply_watch:
        return result
    local_now = now.astimezone(zone)
    fire = wall_time(parsed - timedelta(days=1), time(10), zone)
    if fire is None:
        result['reason'] = 'ambiguous_or_missing_local_time'
        return result
    days = (parsed - local_now.date()).days
    # The same 45-day TTL bounds confirmed and automatic records. No unsupported
    # far-future promise; a direct owner schedule can still use the normal path.
    if days < 2 or days > HORIZON_DAYS or fire <= now:
        result['reason'] = 'outside_window'
        return result
    if CLOCK.search(excerpt):
        result['reason'] = 'time_or_zone_needs_explicit_owner_time'
        return result
    result['fire_at'] = fire.astimezone(timezone.utc).isoformat()
    dates = list(DATE_LIKE.finditer(excerpt))
    try:
        msg_day = datetime.fromisoformat(str(message_date).replace('Z', '+00:00')).date()
    except (ValueError, TypeError):
        msg_day = None
    if (claim.get('is_item_own') is True and claim.get('date') == parsed.isoformat()
            and context_complete and msg_day and parsed > msg_day and days >= 2
            and len(dates) == 1 and dates[0].group(0).casefold() == span.casefold()
            and END.search(quote) and not DEMO.search(excerpt) and not CLOCK.search(excerpt)):
        result.update(tier='automatic', reason='form_checks_passed')
    return result


def utcnow():
    return datetime.now(timezone.utc)


def _store():
    try:
        from . import hermes_tingle_store as store
    except ImportError:
        import hermes_tingle_store as store
    return store


def ensure_schema(conn):
    # Called under the store's schema transaction on every local entrypoint.
    # Never executes the ordinary reservation/attempt-advancement path.
    for sql in (
        '''CREATE TABLE IF NOT EXISTS expiry_nudges (
           item_id TEXT PRIMARY KEY, fire_at TEXT NOT NULL, source_date TEXT NOT NULL,
           tier TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
           created_at TEXT NOT NULL, expires_at TEXT NOT NULL, window_end TEXT NOT NULL,
           category TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS expiry_emissions (
           item_id TEXT PRIMARY KEY, emitted_at TEXT NOT NULL, expires_at TEXT NOT NULL,
           state TEXT NOT NULL DEFAULT 'reserved')''',
        '''CREATE TABLE IF NOT EXISTS expiry_streaks (
           category TEXT PRIMARY KEY, declines INTEGER NOT NULL DEFAULT 0,
           last_prompt TEXT NOT NULL DEFAULT '', suggested INTEGER NOT NULL DEFAULT 0)''',
        '''CREATE TABLE IF NOT EXISTS expiry_suppressions (
           rule_id TEXT NOT NULL, category TEXT NOT NULL, suppressed_at TEXT NOT NULL,
           PRIMARY KEY(rule_id,category))''',
        '''CREATE TRIGGER IF NOT EXISTS expiry_item_changed AFTER UPDATE OF status,due_at,completion_signal,snoozed_until ON hermes_tingle_items
           BEGIN DELETE FROM expiry_nudges WHERE item_id=NEW.id AND
           (NEW.status NOT IN ('open','waiting','blocked','missed') OR NEW.due_at IS NOT NULL
            OR NEW.completion_signal='source_email_reply_received' OR
            (NEW.snoozed_until IS NOT NULL AND NEW.snoozed_until>expiry_nudges.fire_at)); END''',
        '''CREATE TRIGGER IF NOT EXISTS expiry_item_deleted AFTER DELETE ON hermes_tingle_items
           BEGIN DELETE FROM expiry_nudges WHERE item_id=OLD.id;
                 DELETE FROM expiry_emissions WHERE item_id=OLD.id; END''',
    ):
        conn.execute(sql)
    sweep(conn, utcnow())


def sweep(conn, now):
    stamp = now.astimezone(timezone.utc).isoformat()
    conn.execute('DELETE FROM expiry_nudges WHERE expires_at<=? OR window_end<=? OR item_id NOT IN '
                 "(SELECT id FROM hermes_tingle_items WHERE status IN ('open','waiting','blocked','missed') "
                 "AND due_at IS NULL AND completion_signal!='source_email_reply_received')", (stamp, stamp))
    conn.execute('DELETE FROM expiry_emissions WHERE expires_at<=?', (stamp,))
    conn.execute('DELETE FROM expiry_suppressions WHERE suppressed_at<=?',
                 ((now - timedelta(days=HORIZON_DAYS)).isoformat(),))


def create_nudge(item_id, proposal, *, now=None, explicit_time=None):
    """Recheck authoritative item state; no ordinary field is ever changed.

    Date-only default is a heuristic eligibility window, not a verified cutoff.
    An explicitly chosen instant gets 45 minutes' grace, not date-only logic.
    """
    store = _store()
    now = now or utcnow()
    zone = store.local_timezone()
    if not proposal or proposal.get('tier') not in ('automatic', 'confirmed', 'explicit'):
        return False
    try:
        source_day = date.fromisoformat(proposal['date'])
        fire = datetime.fromisoformat(explicit_time or proposal['fire_at'])
        if fire.tzinfo is None:
            return False
        fire = fire.astimezone(timezone.utc)
        end = (fire + timedelta(minutes=45) if explicit_time or proposal['tier'] == 'confirmed' else
               wall_time(source_day, time.min, zone))
        if end is None:
            return False
        end = end.astimezone(timezone.utc)
    except (KeyError, TypeError, ValueError):
        return False
    if (not now < fire < now + timedelta(days=HORIZON_DAYS) or end <= fire
            or not 0 < (source_day - now.astimezone(zone).date()).days <= HORIZON_DAYS):
        return False
    if proposal['tier'] != 'explicit':
        try:
            from . import hermes_tingle_expiry_preferences as preferences
        except ImportError:
            import hermes_tingle_expiry_preferences as preferences
        rule = preferences.suppression(proposal.get('category', 'other'))
        if rule:
            preferences.log_suppressed(proposal.get('category', 'other'), rule)
            return False
    conn = store._connect()
    try:
        conn.execute('BEGIN IMMEDIATE')
        sweep(conn, now)
        item = conn.execute('SELECT * FROM hermes_tingle_items WHERE id=?', (item_id,)).fetchone()
        if (not item or item['status'] not in ('open', 'waiting', 'blocked', 'missed') or item['due_at']
                or item['completion_signal'] == 'source_email_reply_received'
                or (item['snoozed_until'] and datetime.fromisoformat(item['snoozed_until']) > now)
                or conn.execute('SELECT 1 FROM expiry_emissions WHERE item_id=?', (item_id,)).fetchone()):
            conn.rollback()
            return False
        if proposal['tier'] == 'explicit':
            conn.execute('DELETE FROM expiry_nudges WHERE item_id=?', (item_id,))
        conn.execute('INSERT OR IGNORE INTO expiry_nudges '
                     '(item_id,fire_at,source_date,tier,state,created_at,expires_at,window_end,category) '
                     'VALUES(?,?,?,?,?,?,?,?,?)',
                     (item_id, fire.isoformat(), source_day.isoformat(), proposal['tier'], 'pending',
                      now.isoformat(), (now + timedelta(days=HORIZON_DAYS)).isoformat(), end.isoformat(),
                      proposal.get('category', 'other')))
        changed = bool(conn.execute('SELECT changes()').fetchone()[0])
        conn.commit()
        return changed
    finally:
        conn.close()


def reserve_due(*, now=None):
    """Log before emit, delete queue payload, one attempt only even if flush fails.

    Never call record_alert_emission or _advance_reminder_attempts. Separate log
    contains item id, reservation time, TTL and state only, no derived date.
    """
    store = _store()
    now = now or utcnow()
    conn = store._connect()
    try:
        conn.execute('BEGIN IMMEDIATE')
        sweep(conn, now)
        rows = conn.execute('SELECT n.* FROM expiry_nudges n JOIN hermes_tingle_items i ON i.id=n.item_id '
                            'WHERE n.fire_at<=? AND n.window_end>? AND i.due_at IS NULL '
                            "AND i.status IN ('open','waiting','blocked','missed') "
                            "AND i.completion_signal!='source_email_reply_received' "
                            'AND (i.snoozed_until IS NULL OR i.snoozed_until<=n.fire_at) '
                            'ORDER BY n.fire_at LIMIT 10', (now.isoformat(), now.isoformat())).fetchall()
        out = []
        try:
            from . import hermes_tingle_expiry_preferences as preferences
        except ImportError:
            import hermes_tingle_expiry_preferences as preferences
        for row in rows:
            # Reuse this transaction connection to avoid nested schema write locks.
            # Ordinary collisions never drop the one-off: the same message gets the
            # expiry line merged alongside the ordinary reminder.
            rule = preferences.suppression(row['category'], conn=conn)
            if rule and row['tier'] != 'explicit':
                preferences.log_suppressed(row['category'], rule, conn=conn)
                conn.execute('DELETE FROM expiry_nudges WHERE item_id=?', (row['item_id'],))
                continue
            conn.execute('INSERT OR IGNORE INTO expiry_emissions(item_id,emitted_at,expires_at) VALUES(?,?,?)',
                         (row['item_id'], now.isoformat(), row['expires_at']))
            if conn.execute('SELECT changes()').fetchone()[0]:
                out.append(dict(row))
            conn.execute('DELETE FROM expiry_nudges WHERE item_id=?', (row['item_id'],))
        conn.commit()
        return out
    finally:
        conn.close()


def render_nudges(rows, *, now=None):
    now = now or utcnow()
    lines = []
    for row in rows:
        today = now.astimezone(_store().local_timezone()).date().isoformat()
        caution = ('The source date is today or earlier and may already have expired. '
                   if row['source_date'] <= today else '')
        lines.append(f"Expiry suggestion for `{row['item_id']}`: the email gave {row['source_date']} "
                     f"(not verified; cutoff time unknown). {caution}This accepted lead payload was cleared; sibling leads may retain shared thread context until their scan expires or is discarded. "
                     "Check your original email. This is an extra one-off, not a deadline or delivery guarantee.")
    return '\n'.join(lines)


def cancel(item_id):
    conn = _store()._connect()
    try:
        conn.execute('DELETE FROM expiry_nudges WHERE item_id=?', (item_id,))
        changed = bool(conn.execute('SELECT changes()').fetchone()[0])
        conn.commit()
        return changed
    finally:
        conn.close()


def wall_time(day, clock, zone):
    """Refuse missing/folded wall times in any IANA zone, not only night DST."""
    naive = datetime.combine(day, clock)
    candidate = naive.replace(tzinfo=zone, fold=0)
    other = naive.replace(tzinfo=zone, fold=1)
    if candidate.utcoffset() != other.utcoffset():
        return None
    if candidate.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != naive:
        return None
    return candidate
