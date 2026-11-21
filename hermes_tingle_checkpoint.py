"""Pure contextual email review-plan validation. No source access or side effects.

An exact span proves only that timing text was present, not its interpretation.
Inferred timing remains a model proposal. Neither is an obligation deadline.
"""
from datetime import datetime, timedelta, timezone
import re

FIELDS = {'actor', 'outcome', 'basis', 'anchor_message', 'after_days', 'timing_quote'}
SCHEMA = {
    'type': 'object', 'additionalProperties': False, 'required': sorted(FIELDS | {'older_context_independent'}),
    'properties': {
        'older_context_independent': {'type':'boolean'},
        'actor': {'type': 'string', 'enum': ['me', 'other', 'unknown']},
        'outcome': {'type': 'string', 'maxLength': 120},
        'basis': {'type': 'string', 'enum': ['stated', 'inferred', 'unknown']},
        'anchor_message': {'type': 'integer', 'minimum': 0, 'maximum': 99},
        'after_days': {'type': 'integer', 'minimum': 0, 'maximum': 36500},
        'timing_quote': {'type': 'string', 'maxLength': 200},
    },
}
INSTRUCTIONS = (
    'Every result MUST include review_plan, including explicit unknown when genuinely unclear. '
    'A review/check time is not an obligation deadline: contextual inference of a CHECK is allowed '
    'even when no obligation deadline is stated. Never omit the field or replace unknown with a guessed delay. '
    'In the SAME response give review_plan, a proposed reminder/check plan, never a deadline. '
    'actor is who needs to act next (me/other/unknown); outcome briefly states the concrete action '
    'or awaited outcome. Read the full supplied chronological context: do not make an already-sent '
    'request into another task to send it. basis=stated only for explicit response/processing timing '
    'in a message; timing_quote must copy its exact contiguous text. basis=inferred may propose '
    'a reasonable wait from urgency and expected turnaround, never an institution-name lookup or '
    'one fixed delay. after_days counts from the DATE OF anchor_message, not from import. '
    'Respect processing estimates in days/weeks and do not chase before their expected interval. '
    'Use the message establishing the current state as anchor_message. '
    'If timing, ownership, source date or current state is unclear, use unknown, empty outcome/quote '
    'as appropriate, anchor_message=0 and after_days=0; do not invent a deadline. '
    'Partial context normally cannot establish an automatic check plan. Explicit owner schedules win. '
    'Exception for stated timing only: context_provenance may prove every message from anchor through '
    'newest was acquired fully and remains unshortened in this input, with only strictly older non-policy '
    'gaps. Never assert this from context_complete=false alone. Set older_context_independent=true ONLY '
    'if supplied full suffix itself establishes current next actor, action/outcome and exact stated wait '
    'without relying on omitted historical conditions or references. Read every later message for '
    'cancellation, fulfilment, changed timing or actor. If any ambiguity, conditional older agreement, '
    'as-agreed reference, missing middle/newest, clipped text, equal-time ambiguity, scope/policy/list-mail '
    'exclusion or insufficient evidence remains, return unknown and older_context_independent=false. '
    'An exact quote proves presence, not interpretation or current state. No inferred waits from partial '
    'history, no generic fallback. older_context_independent is an unverified model judgment, not authority. '
)


def _flat(value):
    return ' '.join(str(value).split())


def unknown():
    return {'actor': 'unknown', 'outcome': '', 'basis': 'unknown',
            'review_at': None, 'review_needed': True}



def _instant(value):
    result = datetime.fromisoformat(value.replace('Z','+00:00'))
    if result.tzinfo is None:
        raise ValueError('offset required')
    return result


def suffix_admissible(record, anchor_n, *, now):
    """Pure structural gate. Does not verify semantic independence of older context."""
    try:
        provenance = record['context_provenance']
        if (not isinstance(provenance,dict) or provenance.get('version') != 1
                or provenance.get('enumeration_complete') is not True):
            return False
        members = provenance['members']
        if not isinstance(members,list) or not 1 <= len(members) <= 100:
            return False
        by_id = {}
        for member in members:
            mid = member['message_id']
            if not isinstance(mid,str) or not mid or mid in by_id or type(member['body_acquired']) is not bool:
                return False
            by_id[mid] = (member,_instant(member['date']))
        dates = [date for member,date in by_id.values()]
        if any(date > now for date in dates):
            return False
        if len(set(dates)) != len(dates):
            return False
        gaps = provenance['gaps']
        if not isinstance(gaps,list):
            return False
        # Deliberate scope blindness is never reinterpreted as harmless old context.
        for gap in gaps:
            if gap.get('kind') not in ('body_dedupe','body_failed','body_cap') or gap.get('stage') != 'acquisition':
                return False
            position = gap.get('position')
            if type(position) is not int or not 0 <= position < len(members) or members[position]['message_id'] != gap.get('message_id'):
                return False
        missing_body_ids = {mid for mid,(member,date) in by_id.items() if not member['body_acquired']}
        if not missing_body_ids <= {gap['message_id'] for gap in gaps}:
            return False
        supplied = record['messages']
        if not isinstance(supplied,list) or not supplied:
            return False
        anchor = next(m for m in supplied if m['n']==anchor_n)
        anchor_date = _instant(anchor['date'])
        supplied_ids = [m['message_id'] for m in supplied]
        if len(supplied_ids) != len(set(supplied_ids)):
            return False
        for message in supplied:
            source = by_id.get(message['message_id'])
            if source is None or source[1] != _instant(message['date']):
                return False
        suffix = {mid for mid,(member,date) in by_id.items() if date >= anchor_date}
        if anchor['message_id'] not in suffix:
            return False
        if any(by_id[gap['message_id']][1] >= anchor_date for gap in gaps):
            return False
        for mid in suffix:
            message = next((m for m in supplied if m['message_id']==mid), None)
            if (message is None or message.get('shortened') or message.get('body_acquired') is not True
                    or by_id[mid][0]['body_acquired'] is not True
                    or message.get('direction') not in ('received','sent_by_owner')):
                return False
        return True
    except (KeyError,ValueError,TypeError,AttributeError,StopIteration,OverflowError):
        return False

def diagnose(plan, record, evidence, *, now):
    """Return a minimal plan, without source quotes. Invalid/partial stays unknown.

    record is the exact bounded model input; evidence maps message n to text/direction.
    The source-relative review instant may be past: check now, never restart the wait.
    No false precision is added to undated/offset-less message dates.
    """
    result = unknown()
    if plan is None:
        return result, 'missing_plan'
    if not isinstance(plan, dict) or set(plan)-{'older_context_independent'} != FIELDS:
        return result, 'malformed_plan'
    if (plan['actor'] not in ('me', 'other', 'unknown')
            or plan['basis'] not in ('stated', 'inferred', 'unknown')
            or not isinstance(plan['outcome'], str) or len(plan['outcome']) > 120
            or not isinstance(plan['timing_quote'], str) or len(plan['timing_quote']) > 200
            or ('older_context_independent' in plan and type(plan['older_context_independent']) is not bool)
            or type(plan['anchor_message']) is not int or type(plan['after_days']) is not int
            or not 0 <= plan['after_days'] <= 36500):
        return result, 'invalid_fields'
    partial_suffix = False
    if not record.get('context_complete'):
        partial_suffix = (plan['basis']=='stated' and plan.get('older_context_independent') is True
                          and suffix_admissible(record,plan['anchor_message'],now=now))
        if not partial_suffix:
            if record.get('omitted_older_messages'):
                return result, 'omitted_messages'
            if any(m.get('shortened') for m in record.get('messages', [])):
                return result, 'clipped_context'
            return result, 'incomplete_acquisition'
    if plan['actor'] == 'unknown' or plan['basis'] == 'unknown' or not plan['outcome'].strip():
        return result, 'explicit_unknown'
    source = evidence.get(plan['anchor_message'])
    message = next((m for m in record.get('messages', []) if m['n'] == plan['anchor_message']), None)
    if not source or not message or source[1] not in ('received', 'sent_by_owner'):
        return result, 'invalid_anchor'
    if plan['basis'] == 'stated':
        quote = _flat(plan['timing_quote'])
        if len(quote) < 5 or quote not in _flat(source[0]):
            return result, 'unsupported_timing_quote'
    try:
        date = datetime.fromisoformat(message['date'].replace('Z', '+00:00'))
        if date.tzinfo is None or now.tzinfo is None or date > now:
            return result, 'invalid_source_date'
        review = date + timedelta(days=plan['after_days'])
    except (ValueError, TypeError, KeyError, OverflowError):
        return result, 'invalid_source_date'
    outcome = re.sub(r'https?://\S+|www\.\S+', '[link]', _flat(plan['outcome']))
    result.update(actor=plan['actor'], outcome=outcome, basis=plan['basis'],
                  review_at=max(now, review).astimezone(timezone.utc).isoformat(timespec='seconds'),
                  review_needed=False)
    if partial_suffix:
        result['suffix_model_judgment'] = True
    return result, 'scheduled_suffix_model_judgment' if partial_suffix else 'scheduled'



def validate(plan, record, evidence, *, now):
    return diagnose(plan, record, evidence, now=now)[0]


def protocol_valid(plan):
    """A well-formed explicit unknown is valid. Omitted/broken output is not."""
    return (isinstance(plan, dict) and set(plan)-{'older_context_independent'} == FIELDS
            and ('older_context_independent' not in plan or type(plan['older_context_independent']) is bool)
            and plan['actor'] in ('me', 'other', 'unknown')
            and plan['basis'] in ('stated', 'inferred', 'unknown')
            and isinstance(plan['outcome'], str) and len(plan['outcome']) <= 120
            and isinstance(plan['timing_quote'], str) and len(plan['timing_quote']) <= 200
            and type(plan['anchor_message']) is int and 0 <= plan['anchor_message'] <= 99
            and type(plan['after_days']) is int and 0 <= plan['after_days'] <= 36500)

def item_plan(value):
    """Validate minimal stored proposal. No semantic verification is claimed here."""
    if not isinstance(value, dict) or set(value)-{'suffix_model_judgment'} != set(unknown()):
        return unknown()
    if ('suffix_model_judgment' in value and (value['suffix_model_judgment'] is not True or value.get('basis') != 'stated')
            or value['actor'] not in ('me', 'other', 'unknown')
            or value['basis'] not in ('stated', 'inferred', 'unknown')
            or not isinstance(value['outcome'], str) or len(value['outcome']) > 120
            or type(value['review_needed']) is not bool):
        return unknown()
    if value['review_needed'] or value['actor'] == 'unknown' or value['basis'] == 'unknown':
        return unknown()
    try:
        date = datetime.fromisoformat(value['review_at'].replace('Z', '+00:00'))
        if date.tzinfo is None:
            return unknown()
    except (AttributeError, TypeError, ValueError):
        return unknown()
    return dict(value, outcome=_flat(value['outcome']),
                review_at=date.astimezone(timezone.utc).isoformat(timespec='seconds'))
