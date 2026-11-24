"""Bounded, read-only Google Workspace source collection for Hermes Tingle reviews."""
from __future__ import annotations

import itertools
import functools
import hashlib
from contextvars import ContextVar
import json
import re
import subprocess
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    from . import hermes_tingle_calendar_transitions as calendar_transitions
except ImportError:
    import hermes_tingle_calendar_transitions as calendar_transitions


def _notice_due(meta_key: str, signature: str, *, now: datetime, immediate: bool) -> bool:
    """Deduplicated plain-language notice policy for degraded source coverage.

    State lives in plugin_meta as JSON {signature, first_seen, notified_at}. Emission is
    never treated as receipt: while the same degraded signature persists the notice
    re-fires at most once per 24h, so a lost delivery is corrected on a later cycle.
    immediate=True (the degradation is already long-lived by construction) notifies on a
    new signature at once; otherwise the signature must persist across cycles first, so a
    single transient blip never reaches the user. The signature names error KINDS only,
    never IDs or counts - new affected items with the same kind must not re-notify.
    """
    now_dt = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
    now_s = now_dt.isoformat(timespec="seconds")
    raw = store.get_plugin_meta(meta_key)
    state: dict[str, Any] = {}
    if raw:
        try:
            loaded = json.loads(raw)
            if isinstance(loaded, dict):
                state = loaded
        except ValueError:
            state = {}
    due = False
    if state.get("signature") != signature:
        state = {"signature": signature, "first_seen": now_s, "notified_at": None}
        due = immediate
    else:
        notified_at = state.get("notified_at")
        if notified_at:
            due = now_dt - datetime.fromisoformat(str(notified_at)) >= timedelta(hours=24)
        else:
            due = now_dt - datetime.fromisoformat(str(state["first_seen"])) >= timedelta(hours=1)
    if due:
        state["notified_at"] = now_s
    store.set_plugin_meta(meta_key, json.dumps(state, sort_keys=True))
    return due


def _clear_notice(meta_key: str) -> None:
    if store.get_plugin_meta(meta_key):
        store.set_plugin_meta(meta_key, "")


try:
    from . import hermes_tingle_store as store
    from .hermes_tingle_config import calendar_workstream_aliases, load_settings, local_timezone
except ImportError:  # pragma: no cover - test harness imports files as top-level modules
    import hermes_tingle_store as store
    from hermes_tingle_config import calendar_workstream_aliases, load_settings, local_timezone

MAX_EMAILS = 100
MAX_EMAIL_COMPLETION_CHECKS = 25
MAX_EMAIL_REPLY_CHECKS = 25
MAX_GMAIL_HISTORY_PAGES = 10
MAX_GMAIL_HISTORY_IDS = 5000
MAX_PENDING_EMAILS_PER_RUN = 100
GMAIL_RECONCILE_INTERVAL = timedelta(days=7)
GMAIL_RECONCILE_LOOKBACK_DAYS = 30
GMAIL_HISTORY_FAILURE_RESET_THRESHOLD = 3
MAX_CALENDARS = 20
MAX_CHAT_SESSIONS_PER_CHAT = 5
MAX_CHAT_MESSAGES_PER_CHAT = 50
MAX_CHAT_READS_PER_CHAT = 200
MAX_CHAT_MESSAGES_TOTAL = 100
MAX_CHAT_EXCERPT_CHARS = 500
# Only other people's/user-channel messages are candidate context; assistant,
# system and tool rows are never ingested.
CHAT_ALLOWED_ROLES = frozenset({"user"})
MAX_EVENTS = 60
MAX_CALENDAR_EVENT_PAGES = 10
MAX_CALENDAR_EVENT_PAGE_SIZE = 100
MAX_SNIPPET_CHARS = 320
MAX_EVENT_DESCRIPTION_CHARS = 700
MAX_GMAIL_BODY_CHARS = 5000
MAX_AVAILABILITY_DAYS = 7
MAX_AVAILABILITY_CANDIDATES = 10




class SourceReadError(RuntimeError):
    def __init__(self, message, *, kind=None):
        super().__init__(message)
        self.kind = kind  # Closed helper classification, never provider text.


def _workspace_scripts() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "skills" / "productivity" / "google-workspace" / "scripts"


def _run_json(argv: list[str], *, timeout: int = 25) -> Any:
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SourceReadError("Google Workspace connection timed out; authorization was not determined") from exc
    except OSError as exc:
        raise SourceReadError("Google Workspace helper could not start; check the local installation") from exc
    if result.returncode != 0:
        # Classify locally; never echo a provider body, credential, path or email.
        clue = (result.stderr or "").casefold()
        kind = 'transient'
        if any(x in clue for x in ('timeout', 'timed out', 'connection reset', 'temporary failure', 'network is unreachable')):
            reason = 'connection timed out or failed on the network; authorization was not determined'
        elif any(x in clue for x in ('ratelimit', 'rate limit', '429')):
            reason = 'request rate limited; try again later'
        elif any(x in clue for x in ('invalid_grant', 'unauthorized', 'invalid credentials', '401', 'insufficient scope')):
            kind = 'auth'
            reason = 'authorization could not be verified (401/auth); check the existing OAuth connection and scopes'
        elif re.search(r'(?<![0-9])404(?![0-9])|\bnotfound\b', clue):
            kind = 'not_found'
            reason = 'resource not found (404)'
        elif 'modulenotfounderror' in clue:
            reason = 'helper dependency missing (ModuleNotFoundError); check the local installation'
        else:
            reason = 'helper failed; the cause is not confirmed'
        raise SourceReadError('Google Workspace ' + reason, kind=kind)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SourceReadError("Google Workspace returned malformed JSON") from exc



_BATCH_ID = re.compile(r'^[A-Za-z0-9_-]{1,256}$')
_BATCH_HEADERS = {
    'standing': {'from','to','subject','date'},
    'lookback': {'from','to','subject','date','list-id','list-unsubscribe','auto-submitted'},
}


def _metadata_batch(account, ids, profile, *, check, timeout=25):
    """One fixed metadata chunk. Caller enforces date eligibility before this call.

    Authority is rechecked even on helper failure. Returns validated content or
    closed per-ID errors; raw provider/CLI error bodies never reach this result.
    """
    if (profile not in _BATCH_HEADERS or not isinstance(ids,list) or not 1<=len(ids)<=20
            or any(not isinstance(mid,str) or not _BATCH_ID.fullmatch(mid) for mid in ids)
            or len(set(ids))!=len(ids)):
        raise SourceReadError('Invalid bounded metadata request')
    if not isinstance(account,str) or '@' not in account or any(c.isspace() for c in account):
        raise SourceReadError('Verified metadata account required')
    check()
    script=Path(__file__).resolve().parent/'scripts'/'gws_batch_bridge.py'
    request=json.dumps({'mode':'metadata','userId':account,'ids':ids,'profile':profile})
    try:
        result=_run_json([sys.executable,str(script),request],timeout=timeout)
    finally:
        check()
    if (not isinstance(result,dict) or set(result)!={'messages','errors','http_calls','inner_calls'}
            or type(result['http_calls']) is not int or result['http_calls']!=1
            or type(result['inner_calls']) is not int or result['inner_calls']!=len(ids)
            or not isinstance(result['messages'],list) or not isinstance(result['errors'],list)):
        raise SourceReadError('Invalid metadata batch response')
    found={}
    for item in result['messages']:
        if not isinstance(item,dict):
            raise SourceReadError('Invalid metadata entry')
        mid=item.get('id'); thread=item.get('threadId'); stamp=item.get('internalDate')
        part=item.get('payload'); labels=item.get('labelIds',[])
        if (mid not in ids or mid in found or set(item)-{'id','threadId','internalDate','labelIds','snippet','payload'}
                or not isinstance(thread,str) or not _BATCH_ID.fullmatch(thread)
                or not isinstance(stamp,str) or not stamp.isascii() or not stamp.isdigit()
                or not isinstance(labels,list) or any(not isinstance(v,str) for v in labels)
                or ('snippet' in item and not isinstance(item['snippet'],str))
                or not isinstance(part,dict) or set(part)!={'headers'} or not isinstance(part['headers'],list)):
            raise SourceReadError('Metadata identity/date/field mismatch')
        for header in part['headers']:
            if (not isinstance(header,dict) or set(header)!={'name','value'}
                    or not isinstance(header['name'],str) or not isinstance(header['value'],str)
                    or header['name'].casefold() not in _BATCH_HEADERS[profile]):
                raise SourceReadError('Unexpected metadata header')
        found[mid]=item
    for error in result['errors']:
        if (not isinstance(error,dict) or set(error)!={'id','status','kind'}
                or error['id'] not in ids or error['id'] in found
                or type(error['status']) is not int or not (error['status']==0 or 400<=error['status']<=599)
                or error['kind'] not in ('not_found','auth','rate_limit','transient')
                or (error['kind']=='not_found')!=(error['status']==404)
                or (error['status']==401 and error['kind']!='auth')
                or (error['kind']=='auth' and error['status'] not in (401,403))
                or (error['kind']=='rate_limit' and error['status'] not in (403,429))
                or (error['status']==429 and error['kind']!='rate_limit')):
            raise SourceReadError('Invalid metadata error response')
        found[error['id']]=SourceReadError('Gmail metadata '+error['kind'],kind=error['kind'])
    if set(found)!=set(ids):
        raise SourceReadError('Incomplete metadata batch identity set')
    check()
    return found

def _google_api(*args: str, timeout: int = 25) -> Any:
    script = _workspace_scripts() / "google_api.py"
    if not script.is_file():
        raise SourceReadError("Google Workspace skill is not installed in this profile")
    return _run_json([sys.executable, str(script), *args], timeout=timeout)


def _gws_bridge(*args: str, timeout: int = 25) -> Any:
    script = _workspace_scripts() / "gws_bridge.py"
    if not script.is_file():
        raise SourceReadError("Google Workspace bridge is not installed in this profile")
    return _run_json([sys.executable, str(script), *args], timeout=timeout)


_gmail_account = ContextVar("tingle_standing_gmail_account", default=None)


def _check_gmail_binding(binding):
    import hermes_constants
    if (Path(hermes_constants.get_hermes_home()).resolve() != binding[3] or load_settings() != binding[4]):
        raise SourceReadError("Profile or policy changed during Gmail operation")
    store.assert_source_grant("email")
    if store.source_generation("email") != binding[2]:
        raise SourceReadError("Email consent changed during read")


def _gmail_bridge(*args, **kwargs):
    binding = _gmail_account.get()
    if binding is not None:
        _check_gmail_binding(binding)
        args = list(args)
        if len(args) >= 2 and args[-2] == "--params":
            params = json.loads(args[-1])
            if params.get("userId") == "me":
                params["userId"] = binding[0]
                args[-1] = json.dumps(params)
    result = _gws_bridge(*args, **kwargs)
    if binding is not None:
        _check_gmail_binding(binding)
    return result


def _account_bound(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        if _gmail_account.get() is not None:
            return fn(*args, **kwargs)
        if not store.source_enabled("email"):
            raise SourceReadError("email source is disabled by opt-in settings")
        import hermes_constants
        home = Path(hermes_constants.get_hermes_home()).resolve()
        policy = load_settings()
        generation = store.source_generation("email")
        store.assert_source_grant("email")
        address, key = body_account_identity()
        _check_gmail_binding((address,key,generation,home,policy))
        old = store.get_plugin_meta("gmail_account_binding")
        try:
            binding = json.loads(old) if old else None
            if binding is not None and not isinstance(binding,dict):
                raise ValueError()
        except (ValueError,TypeError):
            raise SourceReadError("Stored Gmail account provenance is invalid; explicitly disable and re-enable email") from None
        if binding and binding.get("generation") == generation and binding.get("account") != key:
            raise SourceReadError("Gmail account changed; explicitly disable and re-enable email before scanning the new account")
        if fn.__name__ == "read_email" and binding != {"generation": generation, "account": key}:
            raise ValueError("A recent account-bound standing scan candidate is required; legacy eligibility refused")
        if binding != {"generation": generation, "account": key}:
            # Unqualified legacy caches cannot be adopted as this account's evidence.
            # Items and shared read-attempt audit are retained, never reassigned.
            conn = store._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                if not binding or binding.get("account") != key:
                    legacy = conn.execute("SELECT 1 FROM plugin_meta WHERE key IN ('gmail_history_id','gmail_last_full_reconcile_at') UNION ALL SELECT 1 FROM gmail_pending_messages LIMIT 1").fetchone()
                    if legacy:
                        conn.execute("INSERT OR REPLACE INTO plugin_meta VALUES('gmail_coverage_gap','unprovable_account_migration')")
                    for table in ("gmail_pending_messages", "gmail_unreadable", "email_candidates"):
                        conn.execute("DELETE FROM " + table)
                    conn.execute("DELETE FROM source_seen WHERE source_type='email'")
                    conn.execute("DELETE FROM plugin_meta WHERE key IN ('gmail_history_id','gmail_last_full_reconcile_at')")
                conn.execute("INSERT OR REPLACE INTO plugin_meta VALUES(?,?)", ("gmail_account_binding", json.dumps({"generation":generation,"account":key})))
                conn.commit()
            finally:
                conn.close()
        token = _gmail_account.set((address, key, generation, home, policy))
        try:
            result = fn(*args, **kwargs)
            _check_gmail_binding(_gmail_account.get())
            return result
        finally:
            _gmail_account.reset(token)
    return wrapped


def gmail_ref(account_key, thread, anchor=""):
    if not re.fullmatch(r"[a-f0-9]{64}", str(account_key or "")):
        raise ValueError("Verified email account provenance required")
    return "gmail:" + account_key + "/" + thread + ("#" + anchor if anchor else "")


def _matching_gmail_ref(ref):
    account = _gmail_account.get()
    return bool(account and str(ref).startswith("gmail:" + account[1] + "/"))


def select_calendars(calendars: list[dict[str, Any]], settings: dict | None = None) -> list[dict[str, str]]:
    """Apply an explicitly configured exclusion list, never a personal default."""
    settings = settings if settings is not None else load_settings()
    exclusions = settings.get("calendar_exclude_substrings")
    if not isinstance(exclusions, list) or any(not isinstance(x, str) or not x.strip() for x in exclusions):
        raise ValueError("Tingle config requires explicit calendar exclusions")
    workstreams = settings.get("workstream_exclude_values")
    if not isinstance(workstreams, list) or any(not isinstance(x, str) or not x.strip() for x in workstreams):
        raise ValueError("Tingle config requires explicit workstream exclusions")
    aliases = calendar_workstream_aliases(settings)
    selected = []
    for calendar in calendars:
        summary = str(calendar.get("summary", "")).strip()
        calendar_id = str(calendar.get("id", "")).strip()
        if not summary or not calendar_id or any(x.casefold() in summary.casefold() for x in exclusions):
            continue
        workstream = next((value.strip() for value in workstreams if value.strip().casefold() in summary.casefold()), "")
        if not workstream:
            workstream = next((value for alias, value in aliases.items() if alias in summary.casefold()), "other")
        selected.append({"id": calendar_id, "summary": summary, "workstream": workstream})
    return selected


def normalize_calendar_event(event: dict[str, Any], calendar_name: str, calendar_id: str = "", *, workstream: str = "other", meeting_opt_in: bool = False) -> dict[str, Any] | None:
    """Return a bounded, untrusted event record, omitting cancelled/declined events.

    Organizer/count context is part of the Calendar field scope: without
    the explicit context permission the record carries only the user's own RSVP,
    and the fetch itself asks for nothing more. With it, the record is scoped
    to an attendee count and the organizer identity - never a full list of
    other people's names or addresses."""
    if str(event.get("status", "")).casefold() == "cancelled":
        return None
    summary = str(event.get("summary", "(no title)")).strip()
    attendees = event.get("attendees") if isinstance(event.get("attendees"), list) else []
    self_attendee = next((a for a in attendees if isinstance(a, dict) and a.get("self")), None)
    response = str((self_attendee or {}).get("responseStatus", "")).casefold()
    if response == "declined" or "🚫" in summary:
        return None
    unconfirmed = response in {"needsaction", "tentative"} or "⏳" in summary or "🤔" in summary
    start_obj = event.get("start") if isinstance(event.get("start"), dict) else {}
    end_obj = event.get("end") if isinstance(event.get("end"), dict) else {}
    item = {
        "source_type": "calendar",
        "source_id": f"{calendar_id}:{event.get('id','')}"[:256] if calendar_id else str(event.get("id", ""))[:256],
        "source_version": str(event.get("updated", ""))[:100],
        "source_url": str(event.get("htmlLink", ""))[:2048],
        "calendar": calendar_name[:120],
        "workstream": workstream,
        "title": summary[:240],
        "start": str(start_obj.get("dateTime", start_obj.get("date", "")))[:100],
        "end": str(end_obj.get("dateTime", end_obj.get("date", "")))[:100],
        "location": str(event.get("location", ""))[:240],
        "description": str(event.get("description", ""))[:MAX_EVENT_DESCRIPTION_CHARS],
        "unconfirmed": unconfirmed,
        "untrusted_source": True,
    }
    if meeting_opt_in:
        other_attendees = [a for a in attendees if isinstance(a, dict) and not a.get("self")]
        if other_attendees:
            organizer = event.get("organizer") if isinstance(event.get("organizer"), dict) else {}
            organizer_label = str(organizer.get("displayName") or organizer.get("email") or "")[:240]
            item["meeting"] = {"attendee_count": len(other_attendees), "organizer": organizer_label}
    return item



def calendar_account_identity() -> str:
    """Resolve primary through Calendar scope only, never borrow Gmail access."""
    import hashlib
    value = _gws_bridge('calendar', 'calendars', 'get', '--params',
                        json.dumps({'calendarId': 'primary', 'fields': 'id'}))
    identity = value.get('id') if isinstance(value, dict) else None
    if not isinstance(identity, str) or not identity.strip() or len(identity) > 1024:
        raise SourceReadError('Verified Calendar primary identity unavailable; no event content read')
    return hashlib.sha256(identity.strip().casefold().encode()).hexdigest()


def _calendar_identity_check():
    """Account switches within a grant fail closed; existing unbound grants bind
    only from this first verified check, never retroactively to prior records."""
    import hermes_constants
    store.assert_source_grant('calendar')
    generation = store.source_generation('calendar')
    home = Path(hermes_constants.get_hermes_home()).resolve()
    try:
        policy = load_settings()
    except ValueError as exc:
        raise SourceReadError('Tingle config missing or invalid; Calendar access disabled') from exc
    key = calendar_account_identity()
    store.assert_source_grant('calendar')
    if (store.source_generation('calendar') != generation
            or Path(hermes_constants.get_hermes_home()).resolve() != home or load_settings() != policy):
        raise SourceReadError('Calendar consent, profile or policy changed during identity check')
    expected = {'generation': generation, 'account': key}
    raw = store.get_plugin_meta('calendar_account_binding')
    try:
        old = json.loads(raw) if raw else None
        if old is not None and not isinstance(old, dict):
            raise ValueError()
    except (ValueError, TypeError):
        raise SourceReadError('Stored Calendar identity invalid; disable and re-enable Calendar') from None
    if old and old.get('generation') == generation and old.get('account') != key:
        raise SourceReadError('Calendar account changed; disable and explicitly re-enable Calendar before reading it')
    if old != expected:
        conn = store._connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            grant = conn.execute("SELECT enabled,generation FROM hermes_tingle_source_optins WHERE kind='calendar'").fetchone()
            if not grant or not grant['enabled'] or grant['generation'] != generation:
                raise SourceReadError('Calendar consent changed during identity binding')
            # Prior account-unbound markers cannot suppress this account's events.
            if not old or old.get('account') != key:
                conn.execute("DELETE FROM source_seen WHERE source_type='calendar'")
            conn.execute('INSERT OR REPLACE INTO plugin_meta VALUES(?,?)', ('calendar_account_binding', json.dumps(expected)))
            conn.commit()
        finally:
            conn.close()
    return (key, generation, home, policy)


def _calendar_bound(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        _calendar_optin_guard()
        initial = _calendar_identity_check()
        result = fn(*args, **kwargs)
        if _calendar_identity_check() != initial:
            raise SourceReadError('Calendar account, policy or consent changed during read; result discarded')
        return result
    return wrapped

@_calendar_bound
def _read_calendars() -> list[dict[str, Any]]:
    try:
        settings = load_settings()
    except ValueError as exc:
        raise SourceReadError("Tingle config is missing or invalid; calendar access disabled") from exc
    calendars: list[dict[str, Any]] = []
    page_token = None
    seen_tokens: set[str] = set()
    for _ in range(MAX_CALENDAR_EVENT_PAGES):
        params = {"maxResults": 250, "fields": "items(id,summary,primary,accessRole),nextPageToken"}
        if page_token:
            params["pageToken"] = page_token
        payload = _gws_bridge("calendar", "calendarList", "list", "--params", json.dumps(params))
        items = payload.get("items", []) if isinstance(payload, dict) else []
        calendars.extend(select_calendars(items, settings))
        page_token = payload.get("nextPageToken") if isinstance(payload, dict) else None
        if not page_token or len(calendars) > MAX_CALENDARS:
            break
        # Fail closed on a misbehaving bridge: a repeated page token would
        # otherwise page forever.
        if not isinstance(page_token, str) or page_token in seen_tokens:
            raise SourceReadError("Calendar list returned a repeated page token; coverage is incomplete")
        seen_tokens.add(page_token)
    else:
        raise SourceReadError("Calendar list exceeded the page limit; coverage remains incomplete")
    return calendars


class _CalendarEvents(list):
    """Candidates plus whether a bounded read left possible events unvisited."""
    incomplete = False

    def __init__(self):
        super().__init__()
        # Same fetched records, not another provider read; bounded independently
        # of new-candidate pagination so old markers cannot erase pair context.
        self.transition_context = []
        self.transition_context_incomplete = False


@_calendar_bound
def _read_calendar_events(calendar: dict[str, str], now: datetime, days: int, *, meeting_opt_in: bool | None = None) -> list[dict[str, Any]]:
    # The collector reads its own consent: attendee context is fetched only
    # when the Calendar field scope permits it. (Parameter is an override
    # for tests; None reads the live opt-in.)
    if meeting_opt_in is None:
        meeting_opt_in = store.calendar_context_enabled()
    fields = "items(id,summary,description,location,status,updated,htmlLink,start,end,attendees(self,responseStatus)),nextPageToken"
    if meeting_opt_in:
        fields = "items(id,summary,description,location,status,updated,htmlLink,start,end,attendees,organizer),nextPageToken"
    params = {
        "calendarId": calendar["id"],
        "timeMin": now.astimezone(timezone.utc).isoformat(),
        "timeMax": (now + timedelta(days=days)).astimezone(timezone.utc).isoformat(),
        "maxResults": MAX_CALENDAR_EVENT_PAGE_SIZE,
        "singleEvents": True,
        "orderBy": "startTime",
        "fields": fields,
    }
    result = _CalendarEvents()
    seen_tokens: set[str] = set()
    for _ in range(MAX_CALENDAR_EVENT_PAGES):
        payload = _gws_bridge("calendar", "events", "list", "--params", json.dumps(params))
        if not isinstance(payload, dict) or not isinstance(payload.get("items", []), list):
            raise SourceReadError("Calendar returned an incomplete event page")
        items = payload.get("items", [])
        if any(not isinstance(event, dict) for event in items):
            raise SourceReadError("Calendar returned an incomplete event page")
        token = payload.get("nextPageToken")
        if "nextPageToken" in payload:
            if not isinstance(token, str) or not token or any(c.isspace() for c in token) or token in seen_tokens:
                raise SourceReadError("Calendar returned an invalid page token")
            seen_tokens.add(token)
        for event in items[:MAX_CALENDAR_EVENT_PAGE_SIZE]:
            item = normalize_calendar_event(event, calendar["summary"], calendar["id"], workstream=calendar.get("workstream", "other"), meeting_opt_in=meeting_opt_in)
            if item is not None:
                if len(result.transition_context) < MAX_EVENTS:
                    result.transition_context.append(item)
                else:
                    result.transition_context_incomplete = True
            if item is not None and not store.source_reviewed("calendar", item["source_id"], item["source_version"]):
                result.append(item)
                if len(result) >= MAX_EVENTS:
                    result.incomplete = True
                    return result
        if len(items) > MAX_CALENDAR_EVENT_PAGE_SIZE:
            result.incomplete = True
            return result
        if "nextPageToken" not in payload:
            return result
        params["pageToken"] = token
    result.incomplete = True
    return result


def _parse_clock(value: str) -> time:
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        raise ValueError("candidate_starts must use local HH:MM times")
    hour_text, minute_text = value.split(":", 1)
    if not hour_text.isdigit() or not minute_text.isdigit():
        raise ValueError("candidate_starts must use local HH:MM times")
    hour, minute = int(hour_text), int(minute_text)
    if hour > 23 or minute > 59:
        raise ValueError("candidate_starts must use valid local HH:MM times")
    return time(hour, minute)


def _event_datetime(value: str, local_zone) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=local_zone)
        if dt.utcoffset() != dt.replace(fold=1).utcoffset():
            raise ValueError("event time is ambiguous or nonexistent")
    return dt.astimezone(timezone.utc)


def _all_day_means_absence(event: dict[str, Any], calendar_name: str) -> bool | None:
    calendar = calendar_name.casefold()
    title = str(event.get("summary", "")).casefold()
    if "birthday" in calendar or "holiday" in calendar:
        return None
    markers = ("trip", "travel", "vacation", "urlaub", "away", "out of office", "abwesenheit")
    return True if "trip" in calendar or any(marker in title for marker in markers) else False


def _calendar_optin_guard() -> None:
    if not store.source_enabled("calendar"):
        raise SourceReadError("calendar source is disabled by opt-in settings")


@store.serialized("sources")
@_calendar_bound
def check_calendar_availability(
    date_value: str,
    candidate_starts: list[str] | None = None,
    duration_minutes: int = 30,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Check specific local start times across bounded read-only configured calendars."""
    _calendar_optin_guard()
    settings = load_settings()
    local_zone = ZoneInfo(settings["timezone"])
    if not isinstance(date_value, str):
        raise ValueError("date must be YYYY-MM-DD in the configured timezone")
    try:
        target_day = date.fromisoformat(date_value)
    except ValueError as exc:
        raise ValueError("date must be YYYY-MM-DD in the configured timezone") from exc
    if target_day.isoformat() != date_value:
        raise ValueError("date must be YYYY-MM-DD in the configured timezone")

    current = now or datetime.now(local_zone)
    if current.tzinfo is None:
        current = current.replace(tzinfo=local_zone)
    current = current.astimezone(local_zone)
    if target_day < current.date() or target_day > current.date() + timedelta(days=MAX_AVAILABILITY_DAYS):
        raise ValueError(f"date must be between today and {MAX_AVAILABILITY_DAYS} days ahead in the configured timezone")
    if isinstance(duration_minutes, bool) or not isinstance(duration_minutes, int) or not 5 <= duration_minutes <= 180:
        raise ValueError("duration_minutes must be an integer from 5 to 180")
    if candidate_starts is None:
        candidate_starts = []
    if not isinstance(candidate_starts, list) or len(candidate_starts) > MAX_AVAILABILITY_CANDIDATES:
        raise ValueError(f"candidate_starts must be a list of at most {MAX_AVAILABILITY_CANDIDATES} local HH:MM times")
    parsed_starts = [(_parse_clock(value), value) for value in candidate_starts]

    day_start = datetime.combine(target_day, time.min, tzinfo=local_zone)
    day_end = day_start + timedelta(days=1)
    day_start_utc = day_start.astimezone(timezone.utc)
    day_end_utc = day_end.astimezone(timezone.utc)
    window_start = datetime.combine(target_day, time.fromisoformat(settings["availability_start"]), tzinfo=local_zone)
    window_end = datetime.combine(target_day, time.fromisoformat(settings["availability_end"]), tzinfo=local_zone)
    if any(bound.utcoffset() != bound.replace(fold=1).utcoffset() for bound in (window_start, window_end)):
        raise ValueError("availability window is ambiguous or nonexistent in the configured timezone")
    search_start = window_start.astimezone(timezone.utc)
    search_end = window_end.astimezone(timezone.utc)
    if search_end <= search_start:
        raise ValueError("availability window has invalid bounds in the configured timezone")
    candidate_intervals = []
    for clock, label in parsed_starts:
        local_start = datetime.combine(target_day, clock, tzinfo=local_zone)
        alternate = local_start.replace(fold=1)
        if local_start.utcoffset() != alternate.utcoffset():
            raise ValueError("candidate start is ambiguous or nonexistent in the configured timezone")
        start = local_start.astimezone(timezone.utc)
        end = start + timedelta(minutes=duration_minutes)
        if end > day_end_utc:
            raise ValueError("candidate call must end before midnight in the configured timezone")
        if target_day == current.date() and start <= current.astimezone(timezone.utc):
            raise ValueError("candidate start time has already passed in the configured timezone")
        candidate_intervals.append((label, start, end))

    errors: list[str] = []
    blocks: list[tuple[datetime, datetime, str]] = []
    all_day_uncertain = False
    try:
        calendars = _read_calendars()
    except SourceReadError:
        calendars = []
        errors.append("Calendar list could not be read; availability is incomplete.")
    if not calendars and not errors:
        errors.append("No accessible configured calendars were found.")
    if len(calendars) > MAX_CALENDARS:
        errors.append(f"More than {MAX_CALENDARS} configured calendars are accessible; availability is incomplete.")
        calendars = calendars[:MAX_CALENDARS]

    event_count = 0
    for calendar in calendars:
        page_token = None
        seen_pages = set()
        page_count = 0
        while True:
            if page_count >= 20 or (page_token and page_token in seen_pages):
                errors.append('Calendar pagination stopped at a repeated token or page bound; availability is incomplete.')
                break
            if page_token:
                seen_pages.add(page_token)
            page_count += 1
            params = {
                "calendarId": calendar["id"],
                "timeMin": day_start.isoformat(),
                "timeMax": day_end.isoformat(),
                "singleEvents": True,
                "orderBy": "startTime",
                "maxResults": MAX_EVENTS + 1,
                "fields": "items(id,summary,status,transparency,start,end,attendees(self,responseStatus)),nextPageToken",
            }
            if page_token:
                params["pageToken"] = page_token
            try:
                payload = _gws_bridge("calendar", "events", "list", "--params", json.dumps(params))
            except SourceReadError:
                errors.append("A configured calendar could not be read; availability is incomplete.")
                break
            if not isinstance(payload, dict) or not isinstance(payload.get("items", []), list):
                errors.append("Calendar returned an incomplete event list; availability is incomplete.")
                break
            items = payload.get("items", [])
            event_count += len(items)
            if event_count > MAX_EVENTS:
                errors.append(f"More than {MAX_EVENTS} events fall on this date; availability is incomplete.")
                break
            for event in items:
                if str(event.get("status", "")).casefold() == "cancelled":
                    continue
                if str(event.get("transparency", "")).casefold() == "transparent":
                    continue
                attendees = event.get("attendees") if isinstance(event.get("attendees"), list) else []
                self_attendee = next((a for a in attendees if isinstance(a, dict) and a.get("self")), None)
                response = str((self_attendee or {}).get("responseStatus", "")).casefold()
                if response == "declined":
                    continue
                state = "possible" if response in {"needsaction", "tentative"} or str(event.get("status", "")).casefold() == "tentative" else "busy"
                start_obj = event.get("start") if isinstance(event.get("start"), dict) else {}
                end_obj = event.get("end") if isinstance(event.get("end"), dict) else {}
                if start_obj.get("date") and end_obj.get("date"):
                    start_day = date.fromisoformat(start_obj["date"])
                    end_day = date.fromisoformat(end_obj["date"])
                    if not (start_day <= target_day < end_day):
                        continue
                    all_day_status = _all_day_means_absence(event, calendar["summary"])
                    if all_day_status is True:
                        blocks.append((day_start_utc, day_end_utc, state))
                    elif all_day_status is False:
                        all_day_uncertain = True
                    continue
                try:
                    start = _event_datetime(str(start_obj.get("dateTime", "")), local_zone)
                    end = _event_datetime(str(end_obj.get("dateTime", "")), local_zone)
                except (TypeError, ValueError):
                    errors.append("An event has incomplete time data; availability is incomplete.")
                    continue
                if end <= start:
                    errors.append("An event has invalid time bounds; availability is incomplete.")
                    continue
                if start < day_end_utc and end > day_start_utc:
                    blocks.append((max(start, day_start_utc), min(end, day_end_utc), state))
            page_token = payload.get("nextPageToken")
            if not page_token:
                break
            if event_count >= MAX_EVENTS:
                errors.append(f"More than {MAX_EVENTS} events may fall on this date; availability is incomplete.")
                break

    blocks.sort(key=lambda item: (item[0], item[1]))
    merged: list[tuple[datetime, datetime, str]] = []
    for start, end, state in blocks:
        if merged and start <= merged[-1][1]:
            prev_start, prev_end, prev_state = merged[-1]
            merged[-1] = (prev_start, max(prev_end, end), "busy" if "busy" in {prev_state, state} else "possible")
        else:
            merged.append((start, end, state))

    candidate_results = []
    for label, start, end in candidate_intervals:
        conflicts = [
            {"start": left.astimezone(local_zone).strftime("%H:%M"), "end": right.astimezone(local_zone).strftime("%H:%M"), "state": state}
            for left, right, state in merged if start < right and end > left
        ]
        if conflicts:
            available: bool | None = False
        elif errors or all_day_uncertain:
            available = None
        else:
            available = True
        candidate_results.append({
            "start": label,
            "end": end.astimezone(local_zone).strftime("%H:%M"),
            "available": available,
            "conflicts": conflicts,
        })

    free_windows = []
    if not errors and not all_day_uncertain:
        cursor = search_start
        for start, end, _state in merged:
            if end <= search_start or start >= search_end:
                continue
            start, end = max(start, search_start), min(end, search_end)
            if start - cursor >= timedelta(minutes=duration_minutes):
                free_windows.append({"start": cursor.astimezone(local_zone).strftime("%H:%M"), "end": start.astimezone(local_zone).strftime("%H:%M")})
            cursor = max(cursor, end)
        if search_end - cursor >= timedelta(minutes=duration_minutes):
            free_windows.append({"start": cursor.astimezone(local_zone).strftime("%H:%M"), "end": search_end.astimezone(local_zone).strftime("%H:%M")})

    return {
        "date": target_day.isoformat(),
        "timezone": local_zone.key,
        "duration_minutes": duration_minutes,
        "coverage_complete": not errors and not all_day_uncertain,
        "all_day_uncertain": all_day_uncertain,
        "candidate_results": candidate_results,
        "free_windows": free_windows,
        "errors": errors,
    }


def _read_recent_email(lookback_days: int) -> list[dict[str, Any]]:
    lookback_days = max(1, min(int(lookback_days), 30))
    query = f"in:anywhere newer_than:{lookback_days}d -in:trash -in:spam -in:drafts"
    messages = _google_api("gmail", "search", query, "--max", str(MAX_EMAILS))
    if not isinstance(messages, list):
        raise SourceReadError("Gmail search returned an unexpected result")
    result = []
    for message in messages[:MAX_EMAILS]:
        message_id = str(message.get("id", ""))[:256]
        if not message_id:
            continue
        thread_id = str(message.get("threadId", ""))[:256]
        labels = {str(label).upper() for label in message.get("labels", [])}
        direction = "sent" if "SENT" in labels else "received"
        mailbox_state = "inbox" if "INBOX" in labels else "sent" if direction == "sent" else "archived"
        result.append({
            "source_type": "email",
            "source_id": message_id,
            "source_version": message_id,
            "thread_id": thread_id or message_id,
            "direction": direction,
            "mailbox_state": mailbox_state,
            "source_ref": f"gmail:{thread_id or message_id}",
            "source_url": f"https://mail.google.com/mail/u/0/#all/{thread_id or message_id}",
            "from": str(message.get("from", ""))[:300],
            "to": str(message.get("to", ""))[:300],
            "subject": str(message.get("subject", ""))[:500],
            "date": str(message.get("date", ""))[:100],
            "snippet": str(message.get("snippet", ""))[:MAX_SNIPPET_CHARS],
            "untrusted_source": True,
        })
    return result


@store.serialized("sources")
def _stage_reconciliation_pages(lookback_days: int, *, now: datetime) -> None:
    """Queue IDs from complete pages; never advance the history cursor here."""
    if not store.source_enabled("email"):
        raise store.SourceOptInRevoked("email source is disabled")
    cap = store.source_lookback_cap("email")
    days = max(1, min(int(lookback_days), 30, cap if cap is not None else 30))
    query = f"in:anywhere newer_than:{days}d -in:trash -in:spam -in:drafts"
    token = None
    seen_tokens: set[str] = set()
    for _ in range(MAX_GMAIL_HISTORY_PAGES):
        if not store.source_enabled("email"):
            raise store.SourceOptInRevoked("email source is disabled")
        params: dict[str, Any] = {"userId": "me", "q": query, "maxResults": 500,
                                  "fields": "messages(id),nextPageToken"}
        if token:
            params["pageToken"] = token
        payload = _gmail_bridge("gmail", "users", "messages", "list", "--params", json.dumps(params))
        if (not isinstance(payload, dict) or set(payload) - {"messages", "nextPageToken"}
                or not isinstance(payload.get("messages", []), list)):
            raise SourceReadError("Gmail reconciliation returned an incomplete page")
        ids = []
        for message in payload.get("messages", []):
            message_id = message.get("id") if isinstance(message, dict) else None
            if not isinstance(message_id, str) or not message_id or len(message_id) > 256 or any(c.isspace() for c in message_id):
                raise SourceReadError("Gmail reconciliation returned an invalid message ID")
            ids.append(message_id)
        store.stage_gmail_sync(history_id=None, message_ids=ids, now=now)
        if "nextPageToken" not in payload:
            return
        token = payload["nextPageToken"]
        if not isinstance(token, str) or not token or any(c.isspace() for c in token) or token in seen_tokens:
            raise SourceReadError("Gmail reconciliation returned an invalid page token")
        seen_tokens.add(token)
    raise SourceReadError("Gmail reconciliation exceeded the page limit; coverage remains incomplete")


# --- Chat collector (default ingestion of known chats, interface-injected) --
# The plugin reaches chat history only through Hermes' OWN retained session
# store - messages Hermes already received - never through a live messaging
# account. Both interfaces are injected by operator wiring (the plugin ships
# none); without them the review reports no_history_interface and reads
# nothing. Once the chat source is enabled, every chat this profile has seen
# is in scope by default; the owner's exclusion list (chat_source_exclusions)
# removes individual chats. Recorded legacy per-chat consents keep their chats
# known. Every read is bounded by the global chat source lookback cap (default
# 7 days); an excluded chat is never touched.
#
# Injected contracts:
#   session_resolver(platform: str, chat_id: str) -> iterable of session ids
#   message_reader(session_id: str) -> iterable of message dicts with
#       "role" (str), "content" (str), "timestamp" (epoch seconds, float)
_chat_session_resolver: Any = None
_chat_message_reader: Any = None

# Exact partial-coverage disclosure carried on every chat review: Hermes'
# public session API lists live sessions by platform/chat/thread route. The
# resolver can read up to MAX_CHAT_SESSIONS_PER_CHAT routes per chat, including
# separate topics; the collector applies MAX_CHAT_READS_PER_CHAT across those
# sessions and reads current segments only. Ended sessions, prior lineage and
# compaction-archived rows are not covered, so a chat review never implies full history.
CHAT_COVERAGE_NOTE = (
    f"up to {MAX_CHAT_SESSIONS_PER_CHAT} active session routes per chat (including separate threads/topics), "
    "current segment only; "
    f"up to {MAX_CHAT_READS_PER_CHAT} message reads per chat; "
    "ended sessions, earlier lineage segments and compaction-archived history are not covered")


def set_chat_history_interfaces(session_resolver: Any, message_reader: Any) -> None:
    """Operator wiring: inject the Hermes session-history read interfaces.
    Both must be callables; passing None for either unwires the collector."""
    global _chat_session_resolver, _chat_message_reader
    if session_resolver is None or message_reader is None:
        _chat_session_resolver = None
        _chat_message_reader = None
        return
    if not callable(session_resolver) or not callable(message_reader):
        raise ValueError("chat history interfaces must be callable")
    _chat_session_resolver = session_resolver
    _chat_message_reader = message_reader


@store.serialized("sources")
def _collect_chat_matches(*, now: datetime, discovery_enabled: bool = False) -> dict[str, Any]:
    """Collect task-relevant leads from the profile's chats by default.

    The default update lane collects only active-title-token matches and
    reads nothing without active items. Explicit discovery adds at most twenty
    nonmatched owner-DM excerpts to the SAME traversal, after reserving update
    selection space. Only user-channel roles are considered. Reads are lazy and bounded (never
    materialize a full history): at most MAX_CHAT_SESSIONS_PER_CHAT sessions
    per chat, MAX_CHAT_READS_PER_CHAT raw reads per chat,
    MAX_CHAT_MESSAGES_PER_CHAT collected per chat, MAX_CHAT_MESSAGES_TOTAL
    overall; truncation is always disclosed as an incomplete bounded review.
    Scope: every chat this profile has seen, minus the owner's exclusion list
    (recorded legacy per-chat consents keep their chats known). The window is
    the global chat source lookback cap, default DEFAULT_CHAT_LOOKBACK_DAYS.
    Excerpts are capped and untrusted; only the separate consented private evaluator may receive them."""
    if not store.source_enabled("chat"):
        raise store.SourceOptInRevoked("chat source is disabled")
    review: dict[str, Any] = {"enabled": True, "review_complete": True, "incomplete_reasons": [],
                              "matches": [], "scanned": 0, "chats": 0,
                              "coverage": CHAT_COVERAGE_NOTE}
    discovery_refs = set()
    discovery_leads = []
    legacy_leads = []  # Same traversal; broad ordinary text must not starve local cues.
    discovery = None
    assess = None
    review_module = None
    update_counts = {}
    if discovery_enabled:
        review['discovery'] = {'matches': [], 'review_complete': False,
                               'incomplete_reasons': ['bounded_live_history_only']}
        try:
            try:
                from . import hermes_tingle_discovery as discovery
            except ImportError:
                import hermes_tingle_discovery as discovery
            try:
                from . import hermes_tingle_review as review_module
            except ImportError:
                import hermes_tingle_review as review_module
            try:
                from . import hermes_tingle_discovery_assess as assess
            except ImportError:
                import hermes_tingle_discovery_assess as assess
            discovery_bindings = discovery.eligible_chat_bindings()
            discovery_refs = set(discovery_bindings)
        except Exception:
            # Preserve the title-token lane if private discovery setup fails.
            discovery = None
            review_module = None
            discovery_bindings = {}
            discovery_refs = set()
            review['discovery']['incomplete_reasons'].append('read_error')
    else:
        discovery_bindings = {}
    tokens: dict[str, set[str]] = {}
    bounded_items = store.list_items(status="active", limit=100)
    if store.list_items(status="active", limit=1, offset=len(bounded_items)):
        review['review_complete'] = False
        review['incomplete_reasons'].append('active_item_terms_bounded_to_100')
    for item in bounded_items:
        for token in re.findall(r"[A-Za-z0-9]{4,}", str(item.get("title", "")).casefold()):
            tokens.setdefault(token,set()).add(str(item.get("id", "")))

    def note_incomplete(reason):
        # Without discovery this is the legacy shared collector: every coverage
        # concern is top-level. With discovery enabled, a title-token lane error
        # stays shared while a discovery-only error stays in the private lane so
        # it cannot wake the shared review model.
        if not discovery_enabled:
            review["review_complete"] = False
            if reason not in review["incomplete_reasons"]:
                review["incomplete_reasons"].append(reason)
            return
        if tokens:
            review["review_complete"] = False
            if reason not in review["incomplete_reasons"]:
                review["incomplete_reasons"].append(reason)
        private = review.setdefault('discovery', {'matches': [], 'review_complete': False,
                                                   'incomplete_reasons': []})
        private['review_complete'] = False
        if reason not in private['incomplete_reasons']:
            private['incomplete_reasons'].append(reason)

    in_scope = store.chats_in_scope()
    if not in_scope:
        note_incomplete("no_known_chats")
        return review
    if _chat_session_resolver is None or _chat_message_reader is None:
        note_incomplete("no_history_interface")
        review["chats"] = len(in_scope)
        return review
    if not tokens and not discovery_refs:
        # Nothing can be task-relevant: do not read anyone's messages at all.
        review["chats"] = len(in_scope)
        review["skipped"] = "no_active_items"
        return review

    cap = store.source_lookback_cap("chat")
    days = int(cap) if cap is not None else store.DEFAULT_CHAT_LOOKBACK_DAYS
    total = 0

    def mark_bounded() -> None:
        if tokens:
            review["review_complete"] = False
            if "bounded" not in review["incomplete_reasons"]:
                review["incomplete_reasons"].append("bounded")
        if discovery_enabled:
            discovery_review = review.setdefault('discovery', {'matches': [], 'review_complete': False,
                                                               'incomplete_reasons': []})
            discovery_review['review_complete'] = False
            if 'bounded' not in discovery_review['incomplete_reasons']:
                discovery_review['incomplete_reasons'].append('bounded')

    for chat_ref in in_scope:
        if total >= MAX_CHAT_MESSAGES_TOTAL:
            mark_bounded()
            break
        if not tokens and chat_ref not in discovery_refs:
            continue
        platform, _, chat_id = chat_ref.partition(":")
        cutoff = (now - timedelta(days=days)).timestamp()
        try:
            # Lazy: islice never materializes more than MAX+1 session ids.
            sessions = list(itertools.islice(
                iter(_chat_session_resolver(platform, chat_id)), MAX_CHAT_SESSIONS_PER_CHAT + 1))
        except Exception:
            note_incomplete("read_error")
            continue
        if len(sessions) > MAX_CHAT_SESSIONS_PER_CHAT:
            mark_bounded()
            sessions = sessions[:MAX_CHAT_SESSIONS_PER_CHAT]
        review["chats"] += 1
        collected = 0
        reads = 0
        chat_discovery_leads = []
        stop_chat = False

        def safe_message_iter(iterable):
            nonlocal stop_chat
            try:
                yield from iterable
            except Exception:
                note_incomplete("read_error")
                stop_chat = True

        for session_id in sessions:
            if stop_chat or total >= MAX_CHAT_MESSAGES_TOTAL:
                break
            try:
                message_iter = safe_message_iter(iter(_chat_message_reader(session_id)))
            except Exception:
                note_incomplete("read_error")
                stop_chat = True
                break
            if discovery_enabled:
                message_iter = itertools.islice(message_iter, MAX_CHAT_READS_PER_CHAT - reads)
            for message in message_iter:
                reads += 1
                if reads > MAX_CHAT_READS_PER_CHAT:
                    mark_bounded()
                    stop_chat = True
                    break
                if not isinstance(message, dict):
                    continue
                if str(message.get("role", "")).casefold() not in CHAT_ALLOWED_ROLES:
                    continue
                ts = message.get("timestamp")
                if isinstance(ts, bool) or not isinstance(ts, (int, float)):
                    continue  # undated messages are never guessed into the window
                if ts < cutoff:
                    continue
                content = str(message.get("content", "")).strip()
                if not content:
                    continue
                if collected >= MAX_CHAT_MESSAGES_PER_CHAT or total >= MAX_CHAT_MESSAGES_TOTAL:
                    mark_bounded()
                    stop_chat = True
                    break
                folded = content.casefold()
                matched = sorted({item_id for token, item_ids in tokens.items() if token in folded for item_id in item_ids})
                if not matched:
                    if (discovery is not None and assess is not None and review_module is not None and chat_ref in discovery_refs and
                            isinstance(session_id, str) and session_id and
                            isinstance(message.get('content'), str) and
                            ts <= now.timestamp() and discovery.prefilter(message.get('content'))):
                        try:
                            lead = discovery.lead(chat_ref, session_id, ts, content,
                                                  pinned_owner=discovery_bindings[chat_ref]['owner'],
                                                  pinned_binding=discovery_bindings[chat_ref]['binding'])
                            if (not _case_item_excluded({'title': content, 'source_ref': lead['source_locator']}) and
                                    not assess.evaluated(lead, now=now) and
                                    not any(m['source_locator'] == lead['source_locator']
                                            for m in chat_discovery_leads)):
                                chat_discovery_leads.append(lead)
                        except ValueError:
                            pass  # A private scope failure must not discard title-token updates.
                    continue  # non-matches never enter the update lane
                candidate = {
                    "chat_ref": chat_ref,
                    "role": str(message.get("role", ""))[:40],
                    "excerpt": content[:MAX_CHAT_EXCERPT_CHARS],
                    "timestamp": float(ts),
                    "item_ids": matched[:5],
                    "untrusted_source": True,
                }
                try:
                    from .hermes_tingle_review import attach
                except ImportError:
                    from hermes_tingle_review import attach
                filtered=attach('chat',{'matches':[candidate]},now=now)
                review['unchanged_reviewed']=review.get('unchanged_reviewed',0)+filtered['unchanged_reviewed']
                if not filtered['matches']:
                    continue
                review['matches'].extend(filtered['matches'])
                collected += 1
                total += 1
                review["scanned"] += 1
                update_counts[chat_ref] = collected
            if discovery_enabled and reads >= MAX_CHAT_READS_PER_CHAT:
                mark_bounded()
                stop_chat = True
            if stop_chat:
                break
        if discovery_enabled and chat_discovery_leads:
            chat_room = max(0, MAX_CHAT_MESSAGES_PER_CHAT - collected)
            legacy_leads.extend([lead for lead in chat_discovery_leads if lead.get('_local_label')][:chat_room])
            eligible = chat_discovery_leads[:chat_room]
            if len(chat_discovery_leads) > chat_room and 'shared_budget' not in review['discovery']['incomplete_reasons']:
                review['discovery']['incomplete_reasons'].append('shared_budget')
            global_room = max(0, MAX_CHAT_MESSAGES_TOTAL - len(discovery_leads))
            discovery_leads.extend(eligible[:global_room])
            if len(eligible) > global_room and 'candidate_cap' not in review['discovery']['incomplete_reasons']:
                review['discovery']['incomplete_reasons'].append('candidate_cap')
    if discovery is not None and assess is not None:
        legacy = []
        # Alternative selections, not additive read budgets. Only one is used:
        # broad private evaluation OR the pre-existing conservative local lane.
        for candidate in legacy_leads:
            if len(legacy) >= min(discovery.MAX_SUGGESTIONS, MAX_CHAT_MESSAGES_TOTAL - total):
                break
            try:
                assess.revalidate(candidate)
            except ValueError:
                continue
            legacy.append(candidate)
        review['discovery']['_legacy_matches'] = legacy
        selected = []
        for candidate in discovery_leads:
            if total >= MAX_CHAT_MESSAGES_TOTAL:
                if 'shared_budget' not in review['discovery']['incomplete_reasons']:
                    review['discovery']['incomplete_reasons'].append('shared_budget')
                break
            if len(selected) >= discovery.MAX_EXCERPTS:
                if 'candidate_cap' not in review['discovery']['incomplete_reasons']:
                    review['discovery']['incomplete_reasons'].append('candidate_cap')
                break
            ref = candidate['chat_ref']
            if update_counts.get(ref, 0) >= MAX_CHAT_MESSAGES_PER_CHAT:
                if 'shared_budget' not in review['discovery']['incomplete_reasons']:
                    review['discovery']['incomplete_reasons'].append('shared_budget')
                continue
            try:
                assess.revalidate(candidate)
            except ValueError:
                if 'scope_changed' not in review['discovery']['incomplete_reasons']:
                    review['discovery']['incomplete_reasons'].append('scope_changed')
                discovery_refs.discard(ref)
                continue
            selected.append(candidate)
            total += 1
            update_counts[ref] = update_counts.get(ref, 0) + 1
        review['discovery']['matches'] = selected
    return review


def _read_gmail_profile_history_id() -> str:
    payload = _gmail_bridge(
        "gmail", "users", "getProfile", "--params",
        json.dumps({"userId": "me", "fields": "historyId"}),
    )
    history_id = payload.get("historyId") if isinstance(payload, dict) else None
    if not isinstance(history_id, str) or not history_id.isascii() or not history_id.isdigit() or len(history_id) > 256:
        raise SourceReadError("Gmail profile did not return a history ID")
    return history_id


def _read_gmail_history(start_history_id: str) -> tuple[list[str], str]:
    start_history_id = str(start_history_id or "").strip()
    if not start_history_id or not start_history_id.isascii() or not start_history_id.isdigit():
        raise SourceReadError("Gmail has an invalid saved history ID")
    message_ids: list[str] = []
    page_token: str | None = None
    latest_history_id = start_history_id
    seen_tokens: set[str] = set()
    for _page in range(MAX_GMAIL_HISTORY_PAGES):
        params: dict[str, Any] = {
            "userId": "me",
            "startHistoryId": start_history_id,
            "historyTypes": ["messageAdded"],
            "maxResults": 500,
        }
        if page_token:
            params["pageToken"] = page_token
        payload = _gmail_bridge("gmail", "users", "history", "list", "--params", json.dumps(params))
        if not isinstance(payload, dict) or "error" in payload:
            raise SourceReadError("Gmail history returned an unexpected result")
        history = payload.get("history", [])
        if not isinstance(history, list):
            raise SourceReadError("Gmail history returned an incomplete change list")
        next_history_id = payload.get("historyId")
        if (not isinstance(next_history_id, str) or not next_history_id.isascii()
                or not next_history_id.isdigit() or len(next_history_id) > 256
                or int(next_history_id) < int(latest_history_id)):
            raise SourceReadError("Gmail history returned an invalid or regressing cursor")
        latest_history_id = next_history_id
        for record in history:
            if not isinstance(record, dict) or not isinstance(record.get("messagesAdded"), list) or not record["messagesAdded"]:
                raise SourceReadError("Gmail history returned an incomplete change record")
            added = record["messagesAdded"]
            for entry in added:
                message = entry.get("message", {}) if isinstance(entry, dict) else {}
                message_id = message.get("id") if isinstance(message, dict) else None
                if not isinstance(message_id, str) or not message_id or len(message_id) > 256 or any(c.isspace() for c in message_id):
                    raise SourceReadError("Gmail history returned an invalid message ID")
                message_ids.append(message_id)
                if len(message_ids) > MAX_GMAIL_HISTORY_IDS:
                    raise SourceReadError("Gmail history exceeded the bounded pending-message limit")
        if "nextPageToken" not in payload:
            page_token = None
            break
        page_token = payload["nextPageToken"]
        if not isinstance(page_token, str) or not page_token or any(c.isspace() for c in page_token) or page_token in seen_tokens:
            raise SourceReadError("Gmail history returned an invalid page token")
        seen_tokens.add(page_token)
    if page_token:
        raise SourceReadError("Gmail history exceeded the bounded pagination limit")
    return list(dict.fromkeys(message_ids)), latest_history_id


def _gmail_source_parts(source_ref: str) -> tuple[str, str]:
    raw = str(source_ref or "").strip()
    if not raw.startswith("gmail:"):
        return "", ""
    remainder = raw[6:]
    if "/" in remainder:
        account, remainder = remainder.split("/", 1)
        if not re.fullmatch(r"[a-f0-9]{64}", account):
            return "", ""
    thread_id, separator, anchor_id = remainder.partition("#")
    if (not thread_id or len(thread_id)>256 or len(anchor_id)>256
            or not re.fullmatch(r"[A-Za-z0-9_-]+",thread_id)
            or (separator and not re.fullmatch(r"[A-Za-z0-9_-]+",anchor_id))):
        return "", ""
    return thread_id, anchor_id if separator else ""


def _normalize_gmail_message(payload: dict[str, Any], message_id: str = "") -> dict[str, Any]:
    actual_id = str(payload.get("id", message_id)).strip()[:256]
    thread_id = str(payload.get("threadId", "")).strip()[:256] or actual_id
    labels = {str(label).upper() for label in payload.get("labelIds", [])}
    headers = payload.get("payload", {}).get("headers", []) if isinstance(payload.get("payload"), dict) else []
    header_map = {
        str(header.get("name", "")).casefold(): str(header.get("value", ""))
        for header in headers if isinstance(header, dict)
    }
    direction = "sent" if "SENT" in labels else "received"
    mailbox_state = "inbox" if "INBOX" in labels else "sent" if direction == "sent" else "archived"
    return {
        "source_type": "email",
        "source_id": actual_id,
        "source_version": (_gmail_account.get()[1] + ":" + actual_id) if _gmail_account.get() else actual_id,
        "thread_id": thread_id,
        "direction": direction,
        "mailbox_state": mailbox_state,
        "source_ref": gmail_ref(_gmail_account.get()[1], thread_id) if _gmail_account.get() else f"gmail:{thread_id}",
        "source_url": f"https://mail.google.com/mail/u/0/#all/{thread_id}",
        "from": header_map.get("from", "")[:300],
        "to": header_map.get("to", "")[:300],
        "subject": header_map.get("subject", "")[:500],
        "date": header_map.get("date", "")[:100],
        "internal_date_ms": str(payload.get("internalDate", "")),
        "snippet": str(payload.get("snippet", ""))[:MAX_SNIPPET_CHARS],
        "_excluded": bool(labels & {"TRASH", "SPAM", "DRAFT"}),
        "untrusted_source": True,
    }


class GmailMessageNotFoundError(SourceReadError):
    """The provider explicitly reported the message as gone (deleted/inaccessible)."""


def _is_not_found_error(exc: Exception) -> bool:
    kind = getattr(exc, 'kind', None)
    if kind is not None:
        return kind == 'not_found'
    text = str(exc).casefold()
    return "404" in text or "not found" in text or "notfound" in text


def _classify_read_error(exc: Exception) -> str:
    """Coarse error class for honest alerting: a 404 means the provider cannot find the
    message through this account now (deleted, short-lived draft, or otherwise
    unavailable - the response alone does not say which); auth markers mean the
    connection lost authorization; anything else is an unknown transient failure."""
    kind = getattr(exc, 'kind', None)
    if kind is not None:
        return 'auth' if kind == 'auth' else 'transient'
    text = str(exc).casefold()
    if "401" in text or "403" in text or "unauthorized" in text or "forbidden" in text or "invalid_grant" in text or "credential" in text:
        return "auth"
    return "transient"


class EmailOutsideScope(SourceReadError):
    """Provider timestamp is outside the grant; never read headers/snippet/body."""


def _email_timestamp_preflight(message_id: str, *, now: datetime | None = None, account_user: str | None = None) -> int | None:
    cap = store.source_lookback_cap("email")
    if cap is None:
        return None
    minimal = _gmail_bridge("gmail", "users", "messages", "get", "--params", json.dumps({
        "userId": account_user or "me", "id": message_id, "format": "minimal", "fields": "internalDate"}))
    try:
        timestamp = int(minimal["internalDate"])
        stamp = datetime.fromtimestamp(timestamp/1000, tz=timezone.utc)
    except (KeyError, TypeError, ValueError, OverflowError):
        raise SourceReadError("message date unavailable; cannot enforce the email lookback cap") from None
    if stamp < (now or datetime.now(timezone.utc)) - timedelta(days=cap):
        raise EmailOutsideScope("message is older than the configured email lookback cap")
    return timestamp


@store.serialized("sources")
def _read_email_message(message_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    if not store.source_enabled("email"):
        raise store.SourceOptInRevoked("email source is disabled")
    message_id = str(message_id or "").strip()
    if not message_id or len(message_id) > 256 or any(char.isspace() for char in message_id):
        raise ValueError("invalid Gmail message ID")
    try:
        _email_timestamp_preflight(message_id, now=now)
        payload = _gmail_bridge(
            "gmail", "users", "messages", "get", "--params",
            json.dumps({
                "userId": "me", "id": message_id, "format": "metadata",
                "metadataHeaders": ["From", "To", "Subject", "Date"],
                "fields": "id,threadId,labelIds,internalDate,snippet,payload(headers)",
            }),
        )
    except SourceReadError as exc:
        if _is_not_found_error(exc):
            raise GmailMessageNotFoundError(str(exc)) from exc
        raise
    if not isinstance(payload, dict) or payload.get("id") != message_id:
        raise SourceReadError("Gmail returned incomplete message metadata")
    labels = payload.get("labelIds", [])
    part = payload.get("payload")
    thread_id = payload.get("threadId")
    if (not isinstance(thread_id, str) or not thread_id or len(thread_id) > 256
            or any(c.isspace() for c in thread_id)
            or not isinstance(labels, list) or any(not isinstance(label, str) for label in labels)
            or not isinstance(part, dict) or not isinstance(part.get("headers"), list)
            or any(not isinstance(header, dict) for header in part.get("headers", []))):
        raise SourceReadError("Gmail returned incomplete message metadata")
    return _normalize_gmail_message(payload, message_id)



def _pending_email_metadata(ids, *, now, metrics=None):
    """Batch active queue metadata only; retain per-ID preflight and error semantics."""
    binding=_gmail_account.get()
    if binding is None:
        raise SourceReadError('Account-bound metadata collection required')
    check=lambda:_check_gmail_binding(binding)
    for offset in range(0,len(ids),20):
        chunk=ids[offset:offset+20]
        eligible=[];outcomes={};dates={}
        for mid in chunk:
            check()
            try:
                dates[mid]=_email_timestamp_preflight(mid,now=now)
                eligible.append(mid)
            except (SourceReadError,ValueError) as exc:
                outcomes[mid]=exc
            check()
        if eligible:
            if metrics is not None:
                metrics['metadata_http_attempts']+=1
                metrics['metadata_inner_attempts']+=len(eligible)
            try:
                results=_metadata_batch(binding[0],eligible,'standing',check=check)
            except SourceReadError as exc:
                check()
                results={mid:exc for mid in eligible}
            for mid in eligible:
                value=results[mid]
                if isinstance(value,SourceReadError):
                    outcomes[mid]=value
                elif dates[mid] is not None and value['internalDate']!=str(dates[mid]):
                    outcomes[mid]=SourceReadError('Metadata date changed after preflight')
                else:
                    outcomes[mid]=_normalize_gmail_message(value,mid)
        for mid in chunk:
            check()
            yield mid,outcomes[mid]

def _gmail_history_failure_count() -> int:
    conn = store._connect()
    try:
        row = conn.execute("SELECT value FROM plugin_meta WHERE key='gmail_history_failures'").fetchone()
        return int(row[0]) if row and str(row[0]).isdigit() else 0
    finally:
        conn.close()


def _set_gmail_history_failures(count: int) -> None:
    store.set_plugin_meta("gmail_history_failures", str(int(count)))


@store.serialized("sources")
@_account_bound
def _collect_email_candidates(*, now: datetime, lookback_days: int) -> dict[str, Any]:
    """Use Gmail history deltas, durable pending IDs, and a weekly bounded reconciliation."""
    if not store.source_enabled("email"):
        raise store.SourceOptInRevoked("email source is disabled")
    cap = store.source_lookback_cap("email")
    lookback_days = min(30, max(1, int(lookback_days)), cap if cap is not None else 30)
    current = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
    errors: list[str] = []
    # Automatic safe recovery: transient quarantines get bounded second chances
    # without any user queue management; auth/not-found stay parked.
    store.auto_retry_quarantined_gmail(now=current)
    state = store.gmail_sync_state()
    history_id = str(state.get("history_id") or "").strip()
    last_full = state.get("last_full_reconcile_at")
    last_full_dt = None
    if last_full:
        try:
            last_full_dt = datetime.fromisoformat(str(last_full).replace("Z", "+00:00"))
            if last_full_dt.tzinfo is None:
                last_full_dt = last_full_dt.replace(tzinfo=timezone.utc)
            last_full_dt = last_full_dt.astimezone(timezone.utc)
        except ValueError:
            last_full_dt = None
    # Gmail history has no historical-date filter. Under an explicit cap,
    # use provider-filtered reconciliation on every poll, never broad deltas.
    grant = next((row for row in store.list_source_optins() if row["kind"] == "email"), {})
    grant_started = None
    try:
        grant_started = datetime.fromisoformat(str(grant.get("enabled_at") or "").replace("Z", "+00:00"))
        if grant_started.tzinfo is None:
            grant_started = grant_started.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    coverage_origin = last_full_dt or grant_started
    unknown_initial_coverage = coverage_origin is None
    coverage_gap = ((cap is not None or not history_id) and coverage_origin is not None
                    and current - coverage_origin > timedelta(days=lookback_days))
    full_scan = cap is not None or not history_id or last_full_dt is None or current - last_full_dt >= GMAIL_RECONCILE_INTERVAL
    sync_mode = "bootstrap" if not history_id else "reconcile"

    def full_reconcile(*, window_days: int, mode: str, reset_cursor: bool = False) -> None:
        nonlocal sync_mode
        profile_history_id = ""
        try:
            profile_history_id = _read_gmail_profile_history_id()
        except SourceReadError:
            errors.append("Gmail profile history ID could not be read.")
        _stage_reconciliation_pages(window_days, now=current)
        message_ids = []
        if mode == "reconcile_after_history_error":
            if reset_cursor and profile_history_id:
                staged = store.stage_gmail_sync(
                    history_id=profile_history_id,
                    message_ids=[],
                    full_reconcile_at=current,
                    expected_history_id=history_id,
                    now=current,
                )
                if staged is False:
                    errors.append("Gmail cursor changed during recovery reconciliation; discovered IDs remain queued.")
                else:
                    store.set_plugin_meta("gmail_coverage_gap", "unprovable_history_reset")
                    errors.append("Gmail history was reset after repeated failures; the consented-window scan covers recent changes, but older unreviewed changes may be missed.")
            else:
                errors.append("Gmail history is unavailable; the consented-window scan cannot prove coverage of older changes. Cursor unchanged.")
            sync_mode = mode
            return
        if cap is None and history_id and mode != "reconcile_after_history_error":
            message_ids, profile_history_id = _read_gmail_history(history_id)
        staged = store.stage_gmail_sync(
            history_id=profile_history_id or None,
            message_ids=message_ids,
            full_reconcile_at=current if profile_history_id else None,
            expected_history_id=history_id,
            now=current,
        )
        if staged is False:
            errors.append("Gmail cursor changed during reconciliation; discovered IDs remain queued.")
        sync_mode = mode

    if full_scan:
        try:
            window_days = min(30, max(1, int(lookback_days)))
            full_reconcile(window_days=window_days, mode="bootstrap" if not history_id else "reconcile")
        except SourceReadError as exc:
            errors.append(f"Gmail: {exc}")
            if history_id and cap is None:
                try:
                    changed_ids, latest_history_id = _read_gmail_history(history_id)
                except SourceReadError:
                    errors.append("Gmail incremental history could not be checked; the previous cursor remains active.")
                else:
                    staged = store.stage_gmail_sync(history_id=latest_history_id, message_ids=changed_ids,
                                                    expected_history_id=history_id, now=current)
                    if staged is False:
                        errors.append("Gmail cursor changed during the scan; discovered IDs remain queued.")
                    else:
                        sync_mode = "history"
    else:
        try:
            changed_ids, latest_history_id = _read_gmail_history(history_id)
        except SourceReadError:
            failures = _gmail_history_failure_count() + 1
            _set_gmail_history_failures(failures)
            errors.append("Gmail incremental history failed; attempting a consented-window reconciliation.")
            try:
                full_reconcile(
                    window_days=min(GMAIL_RECONCILE_LOOKBACK_DAYS, lookback_days),
                    mode="reconcile_after_history_error",
                    reset_cursor=failures >= GMAIL_HISTORY_FAILURE_RESET_THRESHOLD,
                )
            except SourceReadError as exc:
                errors.append(f"Gmail recovery reconciliation failed; the previous cursor remains active: {exc}")
        else:
            if _gmail_history_failure_count():
                _set_gmail_history_failures(0)
            staged = store.stage_gmail_sync(history_id=latest_history_id, message_ids=changed_ids,
                                            expected_history_id=history_id, now=current)
            if staged is False:
                errors.append("Gmail cursor changed during the scan; discovered IDs remain queued.")
            sync_mode = "history"

    pending = store.pending_gmail_message_ids(limit=MAX_PENDING_EMAILS_PER_RUN + 1, now=current)
    if len(pending) > MAX_PENDING_EMAILS_PER_RUN:
        errors.append(
            f"Gmail: more than {MAX_PENDING_EMAILS_PER_RUN} messages await review; this run will process only the first {MAX_PENDING_EMAILS_PER_RUN}."
        )
    emails: list[dict[str, Any]] = []
    clear_ids: list[str] = []
    retry_kinds: dict[str, str] = {}
    gone_ids: list[str] = []
    binding = _gmail_account.get()
    unread=[]
    for source_id in pending[:MAX_PENDING_EMAILS_PER_RUN]:
        reviewed_version = binding[1]+":"+source_id
        if store.source_reviewed('email',source_id,reviewed_version):
            clear_ids.append(source_id)
        else:
            unread.append(source_id)
    acquisition_attempts={'metadata_http_attempts':0,'metadata_inner_attempts':0}
    for source_id,message in _pending_email_metadata(unread,now=current,metrics=acquisition_attempts):
        if isinstance(message,Exception):
            if isinstance(message,EmailOutsideScope):
                store.set_plugin_meta("coverage_email_aged_queue_gap", "queued_candidate_expired")
                clear_ids.append(source_id)
            elif isinstance(message,GmailMessageNotFoundError) or _is_not_found_error(message):
                gone_ids.append(source_id)
            else:
                retry_kinds[source_id]=_classify_read_error(message)
            continue
        if message.get("_excluded"):
            clear_ids.append(source_id)
            continue
        normalized = dict(message)
        normalized.pop("_excluded", None)
        emails.append(normalized)
    if clear_ids:
        store.clear_gmail_pending(clear_ids)
    diagnostics: list[str] = []
    if gone_ids:
        store.record_gmail_not_found(gone_ids, now=current)
        diagnostics.append(
            f"Gmail: the provider reported {len(gone_ids)} changed message(s) not found (404) through this account; "
            "that does not say whether they were deleted, short-lived drafts, or otherwise unavailable. "
            "Only their message IDs are known - not sender or subject - so their importance cannot be judged. "
            "They are preserved with their evidence and can be requeued with the retry_unreadable action."
        )
    if retry_kinds:
        quarantined_kinds: list[str] = []
        retried_kinds: list[str] = []
        for kind in sorted(set(retry_kinds.values())):
            outcome = store.defer_gmail_pending(
                [sid for sid, k in retry_kinds.items() if k == kind], error_kind=kind, now=current)
            retried_kinds.extend([kind] * len(outcome["retried"]))
            quarantined_kinds.extend([kind] * len(outcome["quarantined"]))
        if retried_kinds:
            breakdown = ", ".join(f"{kind}: {retried_kinds.count(kind)}" for kind in sorted(set(retried_kinds)))
            diagnostics.append(
                f"Gmail: metadata for {len(retried_kinds)} changed message(s) could not be read ({breakdown}); "
                "their IDs remain queued with backoff for retry."
            )
        if quarantined_kinds:
            breakdown = ", ".join(f"{kind}: {quarantined_kinds.count(kind)}" for kind in sorted(set(quarantined_kinds)))
            diagnostics.append(
                f"Gmail: {len(quarantined_kinds)} message(s) stayed unreadable across "
                f"{store.MAX_GMAIL_METADATA_ATTEMPTS} attempts ({breakdown}) and are quarantined; "
                "review for them is blocked pending recovery - automatic recovery retries transient "
                "quarantines after a cool-down; requeue anytime with the retry_unreadable action."
            )
    quarantined = store.quarantined_gmail_messages()
    if quarantined and not retry_kinds and not gone_ids:
        kinds = ", ".join(sorted({str(q["last_error_kind"]) for q in quarantined if q["last_error_kind"]})) or "unknown"
        diagnostics.append(
            f"Gmail review is incomplete: {len(quarantined)} quarantined message(s) remain unreadable "
            f"(last error kind: {kinds}); they keep their state until automatic or explicit recovery."
        )
    try:
        verbose = bool(load_settings().get("verbose_diagnostics"))
    except ValueError:
        verbose = False
    if verbose:
        # Opt-in operator detail: technical error kinds, counts and recovery plumbing each cycle.
        errors.extend(diagnostics)
    else:
        # Quiet by default: internal errors never bother the user. Persistent materially
        # degraded coverage (auth failures or transient quarantines that exhausted their
        # automatic second chances) gets ONE plain-language notice, repeated only when the
        # degraded set changes. A 404-only quarantine is routine and stays silent here.
        parked = [q for q in quarantined
                  if q["last_error_kind"] == "auth"
                  or (q["last_error_kind"] == "transient"
                      and int(q.get("quarantine_cycles") or 0) >= store.MAX_GMAIL_QUARANTINE_CYCLES)]
        if parked:
            kinds = sorted({str(q["last_error_kind"]) for q in parked})
            if _notice_due("gmail_degraded_notice", ",".join(kinds), now=current, immediate=True):
                if "auth" in kinds:
                    errors.append(
                        "Gmail: some messages could not be checked - the connection may need a refresh in "
                        "the Hermes settings. Affected messages are kept aside and picked up once it works again."
                    )
                else:
                    errors.append(
                        "Gmail: a few messages have not been readable for a while, even after automatic retries. "
                        "They are kept aside safely and retries continue automatically - nothing you need to do. "
                        "If this sticks around, refreshing the Gmail connection in the Hermes settings usually helps."
                    )
        else:
            _clear_notice("gmail_degraded_notice")
    for item in emails:
        item["source_generation"] = store.source_generation("email")
    store.register_email_candidates([str(item["source_id"]) for item in emails], now=current, account_key=_gmail_account.get()[1], policy_key=store.source_policy_key(_gmail_account.get()[4]))
    remaining = store.pending_gmail_message_ids(limit=MAX_PENDING_EMAILS_PER_RUN + 1, now=current)
    # Structural incomplete-review contract: the agent must never infer all-clear from
    # quiet output. review_complete is false whenever any staged ID is unreviewed,
    # regardless of verbosity; diagnostics visibility never changes it.
    incomplete_reasons: list[str] = []
    if errors:
        incomplete_reasons.append("provider_or_sync_failure")
    if unknown_initial_coverage:
        store.set_plugin_meta("gmail_coverage_gap", "unprovable_initial_coverage")
    if coverage_gap:
        # Do not widen the grant. Persist the historical gap until acknowledged
        # explicitly; a successful bounded poll cannot prove the missing interval.
        store.set_plugin_meta("gmail_coverage_gap", "unprovable_elapsed_gap")
    gap_kind = store.get_plugin_meta("gmail_coverage_gap")
    if gap_kind:
        incomplete_reasons.append(gap_kind)
        if _notice_due("gmail_coverage_notice", gap_kind, now=current, immediate=True):
            reason = {
                "unprovable_elapsed_gap": "the review paused longer than its consented recent window",
                "unprovable_history_reset": "history recovery reset the cursor and cannot prove coverage of older changes",
                "unprovable_initial_coverage": "the original monitoring start could not be verified",
                "unprovable_account_migration": "legacy scan caches had no verified account identity and were invalidated; their historical coverage is unverified",
            }.get(gap_kind, "historical coverage could not be verified")
            errors.append(f"Gmail: {reason}. Current-window results do not verify that historical interval. Review that interval yourself; afterward use `/tingle source gap-reviewed email` to record that you checked it. No lookback was widened.")
    aged_gap = store.get_plugin_meta("coverage_email_aged_queue_gap")
    if aged_gap:
        incomplete_reasons.append(aged_gap)
        if _notice_due("gmail_aged_queue_notice", aged_gap, now=current, immediate=True):
            errors.append("Gmail: a queued candidate aged outside the consented window before review. No older metadata/body was read. Coverage remains incomplete across later scans and regrants; review the gap yourself, then use `/tingle source gap-reviewed email`. No lookback was widened.")
    if quarantined:
        incomplete_reasons.append("quarantined_unreadable")
    if store.pending_gmail_total():
        incomplete_reasons.append("pending_retry")
    return {
        "emails": emails,
        "errors": errors,
        "diagnostics": diagnostics,
        "sync_mode": sync_mode,
        "acquisition_attempts": acquisition_attempts,
        "pending_count": len(remaining),
        "quarantined_count": len(quarantined),
        "review_complete": not incomplete_reasons,
        "incomplete_reasons": incomplete_reasons,
        "coverage_origin": coverage_origin.isoformat() if coverage_origin else None,
        "coverage_scope": "consented recent window; historical gaps remain unverified until separately reviewed",
    }


def _guard_thread_lookback(thread_id: str) -> None:
    cap = store.source_lookback_cap("email")
    if cap is None:
        return
    payload = _gmail_bridge("gmail", "users", "threads", "get", "--params", json.dumps({
        "userId": "me", "id": thread_id, "format": "minimal", "fields": "id,messages(internalDate)"}))
    try:
        messages = payload["messages"]
        cutoff = (datetime.now(timezone.utc) - timedelta(days=cap)).timestamp()*1000
        if not isinstance(messages, list) or not messages or payload.get("id") != thread_id:
            raise ValueError()
        if any(int(message["internalDate"]) < cutoff for message in messages):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise SourceReadError("tracked thread is outside the consented window or its dates are unavailable; not read") from None


@store.serialized("sources")
@_account_bound
def _read_email_completion_signals() -> tuple[list[dict[str, str]], list[str]]:
    """Check metadata only for tracked email threads with an explicit Trash=done rule."""
    if not store.source_enabled("email"):
        raise store.SourceOptInRevoked("email source is disabled")
    from bisect import bisect_right
    items = []
    offset = 0
    seen_ids = set()
    while True:
        page = store.list_items(status="active", limit=100, offset=offset)
        if not isinstance(page, list) or any(not isinstance(item, dict) or not isinstance(item.get("id"), str) for item in page):
            return [], ["Gmail: source-deletion item list was incomplete; no checks performed."]
        page_ids = [item["id"] for item in page]
        if len(set(page_ids)) != len(page_ids) or seen_ids.intersection(page_ids):
            return [], ["Gmail: source-deletion item list changed during pagination; no checks performed."]
        seen_ids.update(page_ids)
        items.extend(page)
        if len(page) < 100:
            break
        offset += len(page)
    eligible = sorted((item for item in items
        if item.get("source_type") == "email"
        and item.get("completion_signal") == "source_email_trashed"), key=lambda item: item["id"])
    signals: list[dict[str, str]] = []
    errors: list[str] = []
    if len(eligible) > MAX_EMAIL_COMPLETION_CHECKS:
        errors.append(f"Gmail: source-deletion checks are bounded to {MAX_EMAIL_COMPLETION_CHECKS} tracked threads; remaining items rotate into later reviews.")
    conn = store._connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT value FROM plugin_meta WHERE key=?", ("trash_review_cursor",)).fetchone()
        start = bisect_right([item["id"] for item in eligible], row[0]) if row else 0
        selected = (eligible[start:] + eligible[:start])[:MAX_EMAIL_COMPLETION_CHECKS]
        if selected:
            conn.execute("INSERT INTO plugin_meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", ("trash_review_cursor", selected[-1]["id"]))
        conn.commit()
    finally:
        conn.close()
    for item in selected:
        source_ref = str(item.get("source_ref", ""))
        if not _matching_gmail_ref(source_ref):
            errors.append(f"Gmail: account provenance unavailable or different for {item['id']}; no completion check performed.")
            continue
        thread_id, _anchor_id = _gmail_source_parts(source_ref)
        if not thread_id:
            errors.append(f"Gmail: source-deletion check skipped for {item['id']} because its thread reference is invalid.")
            continue
        try:
            _guard_thread_lookback(thread_id)
            payload = _gmail_bridge(
                "gmail", "users", "threads", "get", "--params",
                json.dumps({
                    "userId": "me",
                    "id": thread_id,
                    "format": "metadata",
                    "fields": "id,messages(id,labelIds)",
                }),
            )
        except SourceReadError:
            errors.append(f"Gmail: source-deletion status could not be checked for {item['id']}.")
            continue
        messages = payload.get("messages", []) if isinstance(payload, dict) else []
        if (not isinstance(payload, dict) or payload.get("id") != thread_id
                or not isinstance(messages, list) or not messages
                or any(not isinstance(message, dict)
                       or not isinstance(message.get("id"), str) or not message["id"]
                       or not isinstance(message.get("labelIds"), list)
                       or any(not isinstance(label, str) for label in message["labelIds"])
                       for message in messages)):
            errors.append(f"Gmail: source-deletion status was incomplete for {item['id']}.")
            continue
        if all(
            isinstance(message, dict)
            and "TRASH" in {str(label).upper() for label in message.get("labelIds", [])}
            for message in messages
        ):
            signals.append({
                "item_id": str(item["id"]),
                "thread_id": thread_id,
                "state": "all_messages_trashed",
            })
    return signals, errors


def all_due_items(*, now: datetime) -> list[dict[str, Any]]:
    """Read all due records, including with the current FT-01 store API."""
    import inspect
    if "all_due" in inspect.signature(store.due_items).parameters or any(
            p.kind == p.VAR_KEYWORD for p in inspect.signature(store.due_items).parameters.values()):
        return store.due_items(now=now, all_due=True)
    # Frozen rollback baseline has no sweeps; invoke its public API, then read
    # the uncapped batch. Current stores always take the branch above.
    store.due_items(now=now)
    now_s = store._stamp(now, field="now")
    conn = store._connect()
    try:
        return [dict(row) for row in conn.execute("""SELECT * FROM hermes_tingle_items
            WHERE status IN ('open','waiting','blocked') AND next_check_at<=?
            AND (snoozed_until IS NULL OR snoozed_until<=?)
            ORDER BY next_check_at,due_at,id""", (now_s, now_s)).fetchall()]
    finally:
        conn.close()


def _review_due_batch(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Rotate by ID without changing reminders, including when checks repeatedly fail."""
    from bisect import bisect_right
    reply = []
    other = []
    for item in items:
        target = reply if (item.get("source_type") == "email"
                           and item.get("completion_signal") in {"source_email_reply_received", "source_email_checkpoint"}) else other
        target.append(item)
    errors = []
    conn = store._connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        def select(group, key, limit):
            group = sorted(group, key=lambda item: item["id"])
            if not group or limit <= 0:
                return []
            def deadline(item):
                try:
                    value = item.get("due_at")
                    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
                except (TypeError, AttributeError, ValueError, OverflowError):
                    return None
            dated = [(deadline(item), item) for item in group]
            urgent = [item for _, item in sorted(((dt, item) for dt, item in dated if dt is not None),
                                                  key=lambda pair: (pair[0], pair[1]["id"]))[:limit // 2]]
            urgent_ids = {item["id"] for item in urgent}
            rotation = [item for item in group if item["id"] not in urgent_ids]
            row = conn.execute("SELECT value FROM plugin_meta WHERE key=?", (key,)).fetchone()
            start = bisect_right([item["id"] for item in rotation], row[0]) if row else 0
            rotated = (rotation[start:] + rotation[:start])[:limit - len(urgent)]
            if rotated:
                conn.execute("INSERT INTO plugin_meta(key,value) VALUES (?,?) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, rotated[-1]["id"]))
            return urgent + rotated
        email_batch = select(reply, "due_review_reply_cursor", MAX_EMAIL_REPLY_CHECKS)
        other_batch = select(other, "due_review_other_cursor", 50 - len(email_batch))
        conn.commit()
    finally:
        conn.close()
    if len(reply) > len(email_batch):
        errors.append(f"Gmail: reply checks are bounded to {MAX_EMAIL_REPLY_CHECKS} due tracked threads; remaining items rotate into later reviews.")
    if len(other) > len(other_batch):
        errors.append("Due review is bounded to 50 items; remaining items rotate into later reviews.")
    return email_batch + other_batch, errors


@store.serialized("sources")
@_account_bound
def _read_email_reply_checks(due_items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Verify only due reply-watch items by fetching thread metadata, never message bodies."""
    if not store.source_enabled("email"):
        raise store.SourceOptInRevoked("email source is disabled")
    eligible = [
        item for item in due_items
        if item.get("source_type") == "email"
        and item.get("completion_signal") == "source_email_reply_received"
    ]
    checks: list[dict[str, Any]] = []
    errors: list[str] = []
    if len(eligible) > MAX_EMAIL_REPLY_CHECKS:
        errors.append(f"Gmail: reply checks are bounded to {MAX_EMAIL_REPLY_CHECKS} due tracked threads.")
    for item in eligible[:MAX_EMAIL_REPLY_CHECKS]:
        item_id = str(item.get("id", ""))
        if not _matching_gmail_ref(item.get("source_ref", "")):
            errors.append(f"Gmail: account provenance unavailable or different for {item_id}; no reply check performed.")
            continue
        thread_id, anchor_id = _gmail_source_parts(str(item.get("source_ref", "")))
        if not thread_id or not anchor_id:
            errors.append(f"Gmail: reply status for {item_id} was not checked because its sent-message anchor is missing.")
            continue
        try:
            _guard_thread_lookback(thread_id)
            payload = _gmail_bridge(
                "gmail", "users", "threads", "get", "--params",
                json.dumps({
                    "userId": "me",
                    "id": thread_id,
                    "format": "metadata",
                    "fields": "id,messages(id,labelIds,internalDate,snippet,payload(headers))",
                }),
            )
        except SourceReadError:
            errors.append(f"Gmail: reply status could not be checked for {item_id}; no follow-up reminder was issued.")
            continue
        messages = payload.get("messages", []) if isinstance(payload, dict) else []
        if (not isinstance(messages, list) or not messages
                or payload.get("id") != thread_id
                or any(not isinstance(m, dict) or not isinstance(m.get("id"), str) or not m["id"].strip()
                       or not isinstance(m.get("labelIds"), list)
                       or any(not isinstance(label, str) or not label for label in m["labelIds"])
                       for m in messages)
                or len({m["id"] for m in messages}) != len(messages)):
            errors.append(f"Gmail: reply status was incomplete for {item_id}; no follow-up reminder was issued.")
            continue
        anchor = next((m for m in messages if m["id"] == anchor_id), None)
        if anchor is None or "SENT" not in {str(label).upper() for label in anchor.get("labelIds", [])}:
            errors.append(f"Gmail: sent-message anchor could not be verified for {item_id}; no follow-up reminder was issued.")
            continue
        try:
            anchor_time = int(str(anchor.get("internalDate", "")))
        except (TypeError, ValueError):
            errors.append(f"Gmail: sent-message date could not be verified for {item_id}; no follow-up reminder was issued.")
            continue
        replies: list[tuple[int, dict[str, Any]]] = []
        invalid_inbound_timestamp = False
        for message in messages:
            if not isinstance(message, dict) or str(message.get("id", "")) == anchor_id:
                continue
            labels = {str(label).upper() for label in message.get("labelIds", [])}
            if "SENT" in labels or "SPAM" in labels or "DRAFT" in labels:
                continue
            try:
                message_time = int(str(message.get("internalDate", "")))
            except (TypeError, ValueError):
                invalid_inbound_timestamp = True
                continue
            if message_time > anchor_time:
                replies.append((message_time, message))
            elif message_time == anchor_time:
                invalid_inbound_timestamp = True
        if invalid_inbound_timestamp:
            errors.append(f"Gmail: an inbound message date was incomplete or ambiguous for {item_id}; no follow-up reminder was issued.")
            continue
        if not replies:
            checks.append({"item_id": item_id, "thread_id": thread_id, "state": "no_reply"})
            continue
        _reply_time, reply = max(replies, key=lambda value: value[0])
        reply_headers = reply.get("payload", {}).get("headers", []) if isinstance(reply.get("payload"), dict) else []
        reply_headers = [header for header in reply_headers if isinstance(header, dict)]
        header_map = {
            str(header.get("name", "")).casefold(): str(header.get("value", ""))
            for header in reply_headers
        }
        checks.append({
            "item_id": item_id,
            "thread_id": thread_id,
            "state": "reply_received",
            "reply": {
                "source_id": str(reply.get("id", ""))[:256],
                "date": header_map.get("date", "")[:100],
                "from": header_map.get("from", "")[:300],
                "snippet": str(reply.get("snippet", ""))[:MAX_SNIPPET_CHARS],
            },
        })
        reply_id = str(reply.get("id", "")).strip()[:256]
        if reply_id:
            store.register_email_candidates([reply_id], account_key=_gmail_account.get()[1])
    return checks, errors


@store.serialized("sources")
@_account_bound
def _read_email_checkpoint_checks_raw(items, *, recovery=False):
    """Bounded fresh thread metadata. A change is evidence to interpret, never done.

    Receipts cover exact metadata + source grant + item revision; reservation rechecks.
    No source text or receipt is persisted; source excerpts remain in ordinary host history.
    """
    checks, errors = [], []
    if not store.source_enabled('email'):
        return [], ['Gmail: checkpoint checks unavailable while email source is disabled.'] if items else []
    for item in items[:MAX_EMAIL_REPLY_CHECKS]:
        if item.get('completion_signal') != 'source_email_checkpoint':
            continue
        if not _matching_gmail_ref(item.get('source_ref', '')):
            errors.append('Gmail: checkpoint account could not be verified; no chase prepared.')
            continue
        thread, anchor = _gmail_source_parts(item['source_ref'])
        if not thread or not anchor:
            errors.append('Gmail: checkpoint source anchor missing; no chase prepared.')
            continue
        try:
            _guard_thread_lookback(thread)
            payload = _gmail_bridge('gmail', 'users', 'threads', 'get', '--params', json.dumps({
                'userId': 'me', 'id': thread, 'format': 'metadata',
                'fields': 'id,messages(id,labelIds,internalDate,snippet,payload(headers))'}))
            messages = payload['messages']
            if (payload.get('id') != thread or not isinstance(messages, list) or not messages
                    or len(messages) > 100 or any(not isinstance(m, dict) for m in messages)):
                raise ValueError()
            ids = [m['id'] for m in messages]
            if (len(set(ids)) != len(ids) or anchor not in ids
                    or any(not isinstance(mid, str) or not mid for mid in ids)):
                raise ValueError()
            for message in messages:
                labels = message['labelIds']
                if (not isinstance(labels, list)
                        or any(not isinstance(label, str) for label in labels)
                        or int(message['internalDate']) <= 0):
                    raise ValueError()
            try:
                from .hermes_tingle import _excluded_work
            except ImportError:
                from hermes_tingle import _excluded_work
            if _excluded_work({'title': item.get('title', ''), 'summary': item.get('summary', ''),
                               'source_ref': item['source_ref'] + ' ' + ' '.join(str(m.get('snippet', '')) for m in messages)}):
                raise ValueError()  # Current policy wins over a retained tracking plan.
            anchor_time = int(next(m['internalDate'] for m in messages if m['id'] == anchor))
            if any(m['id'] != anchor and int(m['internalDate']) == anchor_time for m in messages):
                raise ValueError()  # Same-time chronology is ambiguous.
            if recovery and (len(messages) > 4 or any({'TRASH', 'SPAM', 'DRAFT'} & set(m['labelIds']) for m in messages)):
                raise ValueError()
            current = [m for m in messages if int(m['internalDate']) > anchor_time
                       and not {'TRASH', 'SPAM', 'DRAFT'} & set(m['labelIds'])]
            fingerprint = hashlib.sha256(json.dumps({
                'source': item['source_ref'], 'revision': item['reminder_revision'],
                'generation': store.source_generation('email'),
                'messages': messages}, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            view = []
            for m in sorted(current, key=lambda m: int(m['internalDate']))[-4:]:
                view.append({'source_id': m['id'], 'date': str(m['internalDate']),
                             'direction': 'sent_by_owner' if 'SENT' in m['labelIds'] else 'received',
                             'snippet': str(m.get('snippet', ''))[:MAX_SNIPPET_CHARS]})
                if not recovery:
                    store.register_email_candidates([m['id']], account_key=_gmail_account.get()[1])
            if recovery:
                view = [{'source_id':m['id'], 'date':datetime.fromtimestamp(int(m['internalDate'])/1000, timezone.utc).isoformat(),
                         'direction':'sent_by_owner' if 'SENT' in m['labelIds'] else 'received'}
                        for m in sorted(messages,key=lambda m:int(m['internalDate']),reverse=True)]
            checks.append({'item_id': item['id'], 'receipt': fingerprint,
                           'state': 'thread_changed' if current else 'no_thread_update',
                           'messages': view, 'partial': len(current) > 4,
                           'note': 'Thread-only evidence; does not establish what happened outside email or complete the goal.'})
        except (SourceReadError, ValueError, KeyError, TypeError, OverflowError):
            errors.append('Gmail: checkpoint context was incomplete or inaccessible; no chase prepared.')
    if len(items) > MAX_EMAIL_REPLY_CHECKS:
        errors.append('Gmail: checkpoint batch is bounded; remaining items await a later review.')
    return checks, errors


def _read_email_checkpoint_checks(items, *, recovery=False):
    """Check every constituent within the existing thread budget, never one thread for a case."""
    selected=[]; groups={}; errors=[]
    for item in items[:MAX_EMAIL_REPLY_CHECKS]:
        detail=store.get_item(item['id']) or item
        members=detail.get('case_members') or [item]
        if recovery and len(members)>1:
            errors.append('Gmail: merged cases require review of every source, not single-thread replan.')
            continue
        if len(selected)+len(members)>MAX_EMAIL_REPLY_CHECKS:
            errors.append('Gmail: constituent source budget reached; merged case chase deferred.')
            continue
        groups[item['id']]=members
        selected.extend(members)
    checks,failures=_read_email_checkpoint_checks_raw(selected,recovery=recovery)
    errors.extend(failures)
    by_id={c['item_id']:c for c in checks}; out=[]
    for iid,ms in groups.items():
        if not all(m['id'] in by_id for m in ms):
            errors.append('Gmail: not every case source could be checked; no case chase prepared.')
            continue
        if len(ms)==1:
            out.append(by_id[iid]);continue
        parts=[by_id[m['id']] for m in ms]
        receipt=hashlib.sha256(json.dumps({'case':iid,'members':[(m['id'],m['reminder_revision'],by_id[m['id']]['receipt']) for m in ms]},sort_keys=True).encode()).hexdigest()
        out.append({'item_id':iid,'receipt':receipt,
                    'state':'thread_changed' if any(c['state']=='thread_changed' for c in parts) else 'no_thread_update',
                    'partial':any(c['partial'] for c in parts),
                    'members':parts,
                    'messages':[dict(message,case_member=c['item_id']) for c in parts for message in c['messages']],
                    'note':'All listed case sources checked within the same existing budget. Interpret every changed member before one shared chase; no completion is inferred.'})
    if len(items)>MAX_EMAIL_REPLY_CHECKS: errors.append('Gmail: checkpoint batch bounded; later cases await review.')
    return out,errors


def _case_item_excluded(item):
    try:
        from .hermes_tingle import _excluded_work
    except ImportError:
        from hermes_tingle import _excluded_work
    return _excluded_work(item)


def _retirement_evidence(snapshot: dict[str, Any]) -> tuple[set[str], dict[str, str]]:
    """Content-free per-item evidence keys; a repeated unchanged signal keeps its key."""
    grouped: dict[str, list[str]] = {}

    def add(item_id: Any, *parts: Any) -> None:
        if not isinstance(item_id, str) or not re.fullmatch(r"(?:HT|OL)-[A-F0-9]{6}", item_id):
            return
        token = "|".join(str(part)[:128] for part in parts if part not in (None, ""))
        if token:
            grouped.setdefault(item_id.upper(), []).append(token)

    for row in snapshot.get("tracked_email_context", []) or []:
        if isinstance(row, dict):
            add(row.get("item_id"), "email-context",
                *(message.get("source_id") for message in row.get("messages", []) if isinstance(message, dict)))
    for row in snapshot.get("email_completion_signals", []) or []:
        if isinstance(row, dict):
            add(row.get("item_id"), "email-completion", row.get("state", ""))
    for row in snapshot.get("email_reply_checks", []) or []:
        if isinstance(row, dict) and row.get("state") != "no_reply":
            reply = row.get("reply") or {}
            add(row.get("item_id"), "reply", row.get("state", ""), reply.get("source_id", ""))
    for row in snapshot.get("email_checkpoint_checks", []) or []:
        if isinstance(row, dict) and row.get("state") != "no_thread_update":
            message_keys = []
            for message in row.get("messages", []) or []:
                if isinstance(message, dict):
                    message_keys.append("/".join(str(message.get(key, ""))[:128]
                                                  for key in ("source_id", "date", "direction")))
            add(row.get("item_id"), "checkpoint", row.get("state", ""), *sorted(message_keys))
    chat_review = snapshot.get("chat_review")
    if isinstance(chat_review, dict):
        for row in chat_review.get("matches", []) or []:
            if isinstance(row, dict):
                for item_id in row.get("item_ids", []) or []:
                    add(item_id, "chat", row.get("chat_ref", ""), row.get("role", ""),
                        row.get("timestamp", ""))
    keys = {
        item_id: hashlib.sha256("|".join(sorted(parts)).encode()).hexdigest()[:64]
        for item_id, parts in grouped.items()
    }
    return set(keys), keys


def _filter_retired_from_snapshot(snapshot: dict[str, Any], retired_ids: set[str]) -> None:
    """Remove just-retired items from same-run evidence before the agent sees it."""
    def strip_rows(value: Any) -> Any:
        if isinstance(value, dict):
            if (str(value.get("id", "")).upper() in retired_ids
                    or str(value.get("item_id", "")).upper() in retired_ids):
                return None
            cleaned = {key: strip_rows(item) for key, item in value.items()}
            return {key: item for key, item in cleaned.items() if item is not None}
        if isinstance(value, list):
            return [item for item in (strip_rows(row) for row in value) if item is not None]
        return value

    for key in ("due_items", "tracked_email_context", "email_completion_signals",
                "email_reply_checks", "email_checkpoint_checks", "case_merge_context"):
        if key in snapshot:
            snapshot[key] = strip_rows(snapshot[key])
    chat_review = snapshot.get("chat_review")
    if isinstance(chat_review, dict) and isinstance(chat_review.get("matches"), list):
        # The context-review receipt binds the whole item-id scope. Dropping one
        # ID would mismatch both the displayed scope and the stored receipt.
        chat_review["matches"] = [
            row for row in chat_review["matches"]
            if not isinstance(row, dict)
            or not any(str(item_id).upper() in retired_ids
                       for item_id in (row.get("item_ids") or []))
        ]


def _source_reviews_complete(snapshot: dict[str, Any]) -> bool:
    """Soft retirement is safe only when enabled sources completed this review."""
    if snapshot.get("errors"):
        return False
    for key in ("email_sync", "chat_review", "calendar_review"):
        review = snapshot.get(key)
        if isinstance(review, dict) and review.get("enabled") is True:
            if review.get("review_complete") is not True:
                return False
    return True


def _apply_retirement(snapshot: dict[str, Any], *, now: datetime,
                      source_epoch: dict[str, int | None] | None = None) -> None:
    evidence_ids, evidence_keys = _retirement_evidence(snapshot)
    retired_ids = set(store.auto_close_stale_items(
        now=now, protected_ids=evidence_ids, protected_evidence=evidence_keys,
        allow_soft_close=_source_reviews_complete(snapshot), source_epoch=source_epoch))
    if retired_ids:
        _filter_retired_from_snapshot(snapshot, retired_ids)


def collect_snapshot(*, now: datetime | None = None, lookback_days: int = 14, upcoming_days: int = 7,
                     discovery_enabled: bool = False) -> dict[str, Any]:
    """Read only new/unreviewed candidates and due loops; never persists source bodies."""
    try:
        snapshot_settings = load_settings()
    except ValueError:
        snapshot = {
            "email_sync": {"sync_mode": "unavailable", "pending_count": None},
            "due_items": [], "emails": [], "tracked_email_context": [],
            "email_completion_signals": [], "email_reply_checks": [],
            "calendar_events": [], "calendar_names": [], "approved_feedback_rules": [],
            "errors": ["Gmail and Calendar: profile configuration is missing or invalid; no sources were checked."],
            "untrusted_source_data": True,
        }
        # Only the daily review owns retirement. Model-triggered scan_sources
        # with invalid config must not backfill or enforce the hard cap.
        if discovery_enabled:
            try:
                # The hard cap is source-independent. Backfill dates and enforce
                # only the one-year limit without reading sources or soft-closing.
                store.auto_close_stale_items(
                    now=now or datetime.now(timezone.utc), allow_soft_close=False)
            except Exception:
                snapshot["errors"].append("Local tracker retirement state could not be updated.")
        return snapshot
    snapshot_policy_key = store.source_policy_key(snapshot_settings)
    # Capture the grant epoch before any source read. The sweep fences evidence
    # writes against this snapshot so a mid-review revoke/purge cannot be undone.
    collection_epoch = store.source_epoch_snapshot()
    now = now or datetime.now(timezone.utc)
    lookback_days = max(1, min(int(lookback_days), 30))
    upcoming_days = max(1, min(int(upcoming_days), 30))
    errors: list[str] = []
    diagnostics: list[str] = []
    emails: list[dict[str, Any]] = []
    scanned_emails: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    calendars: list[dict[str, str]] = []
    calendar_context = []
    calendar_context_incomplete = False

    email_sync: dict[str, Any] = {"sync_mode": "unavailable", "pending_count": 0,
                                 "review_complete": False, "incomplete_reasons": ["unavailable"]}
    chat_review: dict[str, Any] = {"enabled": store.source_enabled("chat"),
                                   "review_complete": True, "incomplete_reasons": [],
                                   "matches": [], "scanned": 0, "chats": 0}
    if chat_review["enabled"]:
        try:
            chat_review = _collect_chat_matches(now=now, discovery_enabled=discovery_enabled)
        except Exception:
            chat_review["review_complete"] = False
            chat_review["incomplete_reasons"].append("read_error")
            diagnostics.append("Chat history: the chat scan failed; coverage is incomplete.")
    else:
        chat_review["review_complete"] = False
        chat_review["incomplete_reasons"] = ["disabled"]
    for kind, review in (("chat", chat_review),):
        key = kind + "_coverage_notice"
        reasons = sorted(set(review.get("incomplete_reasons") or []))
        if review.get("enabled") and reasons:
            diagnostics.append(kind + " coverage incomplete: " + ",".join(reasons))
            if _notice_due(key, ",".join(reasons), now=now, immediate=True):
                errors.append(kind.capitalize() + ": enabled-source coverage is incomplete (" + ", ".join(reasons) + "). This is not an all-clear. Later polls retry within the same bounds; the notice repeats at most daily while this persists.")
        else:
            _clear_notice(key)
    email_enabled = store.source_enabled("email")
    email_sync["enabled"] = email_enabled
    email_cap = store.source_lookback_cap("email")
    if email_cap is not None:
        lookback_days = min(lookback_days, email_cap)
    if email_enabled:
        try:
            email_sync = _collect_email_candidates(now=now, lookback_days=lookback_days)
            email_sync["enabled"] = True
            scanned_emails = email_sync["emails"]
            emails = scanned_emails
            errors.extend(email_sync["errors"])
            diagnostics.extend(email_sync.get("diagnostics", []))
        except SourceReadError as exc:
            errors.append(f"Gmail: {exc}")
        except store.SourceOptInRevoked:
            errors.append("Gmail: source opt-in was revoked during collection; nothing was staged.")

        email_completion_signals: list[dict[str, str]] = []
        try:
            email_completion_signals, completion_errors = _read_email_completion_signals()
            errors.extend(completion_errors)
        except Exception:
            errors.append("Gmail: tracked email source status could not be checked.")
    else:
        email_completion_signals = []
        email_sync = {"enabled": False, "sync_mode": "disabled", "pending_count": 0,
                      "review_complete": False, "incomplete_reasons": ["disabled"]}

    calendar_enabled = store.source_enabled("calendar")
    calendar_generation = store.source_generation("calendar") if calendar_enabled else None
    calendar_kinds: set[str] = set()
    calendar_review: dict[str, Any] = {"enabled": calendar_enabled, "review_complete": True, "incomplete_reasons": []}
    if not calendar_enabled:
        calendar_kinds.add("disabled")
        calendar_review["review_complete"] = False
        calendar_review["incomplete_reasons"] = ["disabled"]
    try:
        if calendar_enabled and type(calendar_generation) is not int:
            raise SourceReadError("Calendar grant generation unavailable; no event candidates accepted")
        try:
            workstream_exclusions = {value.strip().casefold() for value in load_settings()["workstream_exclude_values"]} if calendar_enabled else set()
        except ValueError as exc:
            raise SourceReadError("Tingle config is missing or invalid; calendar access disabled") from exc
        calendars = _read_calendars() if calendar_enabled else []
        if calendar_enabled and not calendars:
            errors.append("Calendar: no accessible configured calendars were found.")
            calendar_kinds.add("no_accessible_calendars")
        if len(calendars) > MAX_CALENDARS:
            diagnostics.append(f"Calendar: bounded scan covers the first {MAX_CALENDARS} configured calendars only.")
            calendar_kinds.add("bounded")
        for calendar in calendars[:MAX_CALENDARS]:
            if any(str(calendar.get(field, "")).strip().casefold() in workstream_exclusions for field in ("summary", "workstream")):
                continue
            try:
                items = _read_calendar_events(calendar, now, upcoming_days)
                context = getattr(items, 'transition_context', items)
                remaining = max(0, MAX_EVENTS - len(calendar_context))
                calendar_context.extend(dict(item, source_generation=calendar_generation)
                                        for item in context[:remaining])
                if len(context) > remaining or getattr(items, 'transition_context_incomplete', False):
                    calendar_context_incomplete = True
                if getattr(items, "incomplete", False):
                    diagnostics.append(f"Calendar: bounded event scan incomplete for {calendar['summary']}.")
                    calendar_kinds.add("incomplete")
                for item in items:
                    if store.source_reviewed("calendar", item["source_id"], item["source_version"]):
                        continue
                    item["source_generation"] = calendar_generation
                    events.append(item)
                    if len(events) >= MAX_EVENTS:
                        break
                if len(events) >= MAX_EVENTS:
                    diagnostics.append(f"Calendar: bounded scan reached {MAX_EVENTS} candidate events.")
                    calendar_kinds.add("bounded")
                    break
            except SourceReadError:
                diagnostics.append(f"Calendar read failed for {calendar['summary']}.")
                calendar_kinds.add("read_failed")
    except SourceReadError:
        diagnostics.append("Calendar list failed; check the existing Google Workspace connection and scopes.")
        calendar_kinds.add("list_failed")
    # Routine calendar source errors follow the same quiet default: diagnostics only, plus
    # one deduplicated plain-language notice when the same failure persists across cycles.
    if calendar_enabled and calendar_kinds:
        if _notice_due("calendar_degraded_notice", ",".join(sorted(calendar_kinds)), now=now, immediate=False):
            errors.append(
                "Calendar: some calendars could not be checked just now. Retries continue automatically; "
                "if this repeats, the Google connection in the Hermes settings may need attention."
            )
    elif calendar_enabled:
        _clear_notice("calendar_degraded_notice")
    calendar_review = {
        "enabled": calendar_enabled,
        "coverage_scope": "upcoming events from this collection time only; no past monitoring interval was checked",
        "historical_coverage_verified": False,
        "review_complete": not calendar_kinds,
        "incomplete_reasons": sorted(calendar_kinds),
    }

    loops, due_errors = _review_due_batch(all_due_items(now=now))
    errors.extend(due_errors)
    deadline_catchup = store.deadline_catchup_state(now=now)
    if not deadline_catchup["complete"]:
        errors.append("Hard-deadline catch-up is incomplete; earlier missed windows are being recorded in bounded batches. Later polls continue without skipping windows.")
    email_reply_checks: list[dict[str, Any]] = []
    if email_enabled:
        try:
            email_reply_checks, reply_errors = _read_email_reply_checks(loops)
            errors.extend(reply_errors)
        except Exception:
            errors.append("Gmail: waiting-for-reply status could not be checked.")

    email_checkpoint_checks = []
    if email_enabled:
        checkpoint_items = [item for item in loops if item.get('completion_signal') == 'source_email_checkpoint']
        email_checkpoint_checks, checkpoint_errors = _read_email_checkpoint_checks(checkpoint_items)
        errors.extend(checkpoint_errors)

    scanned_by_thread: dict[str, list[dict[str, Any]]] = {}
    for email in scanned_emails:
        scanned_by_thread.setdefault(str(email.get("thread_id", "")), []).append(email)
    tracked_email_context = []
    active_items = store.list_items(status="active", limit=100)
    if email_enabled and store.list_items(status="active", limit=1, offset=len(active_items)):
        errors.append("Gmail: tracked email context is bounded to 100 active items; items beyond that bound were not checked.")
    for item in active_items if email_enabled else []:
        if item.get("source_type") != "email":
            continue
        thread_id, _anchor_id = _gmail_source_parts(str(item.get("source_ref", "")))
        thread_messages = [m for m in scanned_by_thread.get(thread_id, []) if str(item.get("source_ref", "")).split("#",1)[0] == m.get("source_ref") and re.match(r"^gmail:[a-f0-9]{64}/", str(m.get("source_ref", "")))] if thread_id else []
        if thread_messages:
            tracked_email_context.append({
                "item_id": str(item["id"]),
                "title": str(item.get("title", "")),
                "summary": str(item.get("summary", "")),
                "status": str(item.get("status", "")),
                "completion_signal": str(item.get("completion_signal", "")),
                "thread_id": thread_id,
                "messages": thread_messages,
            })
    try:
        if load_settings().get("verbose_diagnostics"):
            # Opt-in operator detail: diagnostics join user-facing errors (email adds its own).
            errors.extend(d for d in diagnostics if d not in errors)
    except ValueError:
        pass
    try:
        from . import hermes_tingle_debug as debug
    except ImportError:
        import hermes_tingle_debug as debug
    debug.safe_record('snapshot', dict(emails=len(emails), calendar_events=len(events),
        due_items=len(loops), errors=len(errors), checkpoint_checks=len(email_checkpoint_checks),
        reply_checks=len(email_reply_checks), case_items=min(len(active_items), 12),
        chat_matches=len(chat_review.get('matches') or [])))
    calendar_transition_snapshot = calendar_transitions.build(
        calendar_context, now=now, timezone_name=load_settings()['timezone'],
        coverage_complete=calendar_review['review_complete'] and not calendar_context_incomplete)
    if calendar_enabled and calendar_generation is not None:
        calendar_transition_snapshot.update(store.prepare_calendar_transition_pairs(
            calendar_transition_snapshot['events'], calendar_transition_snapshot['pairs'],
            source_generation=calendar_generation, expected_policy_key=snapshot_policy_key, now=now))
    else:
        calendar_transition_snapshot.update({
            'suppressed_pair_count': 0,
            'untracked_pair_count': len(calendar_transition_snapshot['pairs']) if calendar_enabled else 0,
        })
    snapshot = {
        "generated_at": now.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "lookback_days": lookback_days,
        "upcoming_days": upcoming_days,
        "source_generations": {kind: store.source_generation(kind) for kind in ("email", "calendar")},
        "email_sync": email_sync,
        "deadline_catchup": deadline_catchup,
        "due_items": loops,
        "emails": emails,
        "tracked_email_context": tracked_email_context,
        "case_merge_context": {
            "items":[{k:item.get(k) for k in ('id','title','owner','status','next_check_at','case_merge')}
                     | {'summary':str(item.get('summary',''))[:300]} for item in active_items[:12] if not _case_item_excluded(item)],
            "bounded":len(active_items)>12,
            "note":"Local stored summaries only, not current source proof. Consider same current outcome, never just a shared topic. Model proposals require actual owner-DM preview; no automatic merges."},
        "email_completion_signals": email_completion_signals,
        "email_reply_checks": email_reply_checks,
        "email_checkpoint_checks": email_checkpoint_checks,
        "calendar_events": events,
        "calendar_names": [c["summary"] for c in calendars],
        "calendar_review": calendar_review,
        "calendar_transitions": calendar_transition_snapshot,
        "chat_review": chat_review,
        "errors": errors,
        "diagnostics": diagnostics,
        "approved_feedback_rules": store.approved_feedback_rules(),
        "untrusted_source_data": True,
    }
    # The daily copied helper enables discovery. Model-triggered scan_sources keeps
    # discovery disabled, so it cannot retire items as a side effect.
    if discovery_enabled:
        _apply_retirement(snapshot, now=now, source_epoch=collection_epoch)
    return snapshot


@store.serialized("sources")
@_account_bound
def read_email(message_id: str) -> dict[str, Any]:
    if not store.source_enabled("email"):
        raise SourceReadError("email source is disabled by opt-in settings")
    import hermes_constants
    original_home = Path(hermes_constants.get_hermes_home()).resolve()
    original_policy = load_settings()
    def check_binding():
        if Path(hermes_constants.get_hermes_home()).resolve() != original_home or load_settings() != original_policy:
            raise SourceReadError("Profile or policy changed during body read")
        store.assert_source_grant("email")
    check_binding()
    message_id = str(message_id or "").strip()
    if not message_id or len(message_id) > 256 or any(ch.isspace() for ch in message_id):
        raise ValueError("invalid Gmail message ID")
    # Validate current standing eligibility without recording anything before
    # the minimal account-identity read; identity failure leaves both ledgers intact.
    stamp = store._now()
    eligibility = store._connect()
    try:
        candidate = eligibility.execute("SELECT 1 FROM email_candidates WHERE source_id=? AND account_key=? AND generation=? AND policy_key=? AND registered_at>=?",
                                        (message_id,_gmail_account.get()[1],store.source_generation("email"),store.source_policy_key(original_policy),(stamp-timedelta(hours=24)).isoformat(timespec='seconds'))).fetchone()
        prior = eligibility.execute("SELECT 1 FROM email_body_reads WHERE source_id=? AND read_at>=?",
                                    (message_id,(stamp-timedelta(days=14)).isoformat(timespec='seconds'))).fetchone()
        if not candidate or prior:
            raise ValueError("email body reads require a recent scan candidate and are limited to once per message ID during the14-day source window")
    finally:
        eligibility.close()
    try:
        from . import hermes_tingle_body_audit as body_audit
    except ImportError:
        import hermes_tingle_body_audit as body_audit
    check_binding()
    account_user, account_key = body_account_identity()
    check_binding()
    if not body_audit.claim(message_id, account_key, origin="standing"):
        raise ValueError("Email body recently reserved/read during the shared14-day dedupe window; body unavailable")
    if not store.claim_email_body_read(message_id, account_key=account_key):
        raise ValueError("Email candidate changed; shared body reservation retained conservatively")
    check_binding()
    _email_timestamp_preflight(message_id, account_user=account_user)
    check_binding()
    payload = _read_bound_email(message_id, account_user)
    check_binding()
    if not isinstance(payload, dict) or payload.get("id") != message_id:
        raise SourceReadError("Gmail returned an unexpected message result")
    return {
        "id": str(payload.get("id", message_id)),
        "thread_id": str(payload.get("threadId", "")),
        "source_ref": gmail_ref(account_key,str(payload.get("threadId", "")),message_id),
        "from": str(payload.get("from", ""))[:300],
        "to": str(payload.get("to", ""))[:300],
        "subject": str(payload.get("subject", ""))[:500],
        "date": str(payload.get("date", ""))[:100],
        "body": str(payload.get("body", ""))[:MAX_GMAIL_BODY_CHARS],
        "untrusted_source": True,
    }


@store.serialized("sources")
def prepare_reminder_batch(item_ids: list[str], *, expected_versions: dict[str, int] | None = None, expected_email_checks: dict[str, str] | None = None, checkpoint_next_checks: dict[str, str | None] | None = None) -> list[dict[str, Any]]:
    """Reverify reply waits at the actual tool entry point, not from stale prose."""
    if not isinstance(item_ids,list) or not 1 <= len(item_ids) <= 100 or len(set(item_ids)) != len(item_ids):
        raise ValueError("Select 1-100 distinct reminder item IDs")
    items = [store.get_item(item_id) for item_id in item_ids]
    if any(item is None for item in items):
        raise ValueError("reminder item not found")
    if (not isinstance(expected_versions, dict) or set(expected_versions) != set(item_ids)
            or any(type(expected_versions.get(item['id'])) is not int or expected_versions[item['id']] != item['reminder_revision'] for item in items)):
        raise ValueError("Copy expected_versions for every item from the reviewed snapshot; missing or stale revisions refused")
    waits = [item for item in items if item.get("completion_signal") == "source_email_reply_received"]
    if waits:
        if not store.source_enabled("email"):
            raise SourceReadError("email source disabled; reply waits cannot be verified")
        checks, errors = _read_email_reply_checks(waits)
        verified = {check["item_id"] for check in checks if check.get("state") == "no_reply"}
        if errors or any(item["id"] not in verified for item in waits):
            raise SourceReadError("reply wait not verified as no_reply; no reminder batch prepared")
    checkpoints = [item for item in items if item.get('completion_signal') == 'source_email_checkpoint']
    if checkpoints:
        if not store.source_enabled('email'):
            raise SourceReadError('email source disabled; checkpoints cannot be verified')
        checks, errors = _read_email_checkpoint_checks(checkpoints)
        verified = {check['item_id']: check['receipt'] for check in checks}
        if (errors or not isinstance(expected_email_checks, dict)
                or any(expected_email_checks.get(item['id']) != verified.get(item['id'])
                       or item['id'] not in verified for item in checkpoints)):
            raise SourceReadError('email thread changed or was not checked; review a fresh snapshot before reminding')
    if checkpoints and (not isinstance(checkpoint_next_checks, dict)
                        or any(item['id'] not in checkpoint_next_checks for item in checkpoints)):
        raise ValueError('choose each next contextual review time or explicitly mark it unknown')
    kwargs = {'checkpoint_next_checks': checkpoint_next_checks} if checkpoints else {}
    return store.mark_notified(item_ids, expected_versions=expected_versions, **kwargs)


def source_capabilities() -> set[str]:
    """Local file/interface presence only. Never contacts an account."""
    available = set()
    if (_workspace_scripts() / "gws_bridge.py").is_file():
        available.update(("email", "calendar"))
    if callable(_chat_session_resolver) and callable(_chat_message_reader):
        available.add("chat")
    return available


def source_offers() -> str:
    available = source_capabilities()
    parts = []
    if "email" in available:
        parts.append("Google Workspace helper installed (account connection not checked). "
                     "After owner opt-in, Tingle can review email and calendars/meetings if the account is connected.")
    if "chat" in available:
        parts.append("A retained chat-history interface is wired; owner opt-in is still required.")
    if parts:
        parts.append("Fresh installs start with email, calendar and chat on from first-run setup; /tingle sources shows grants. "
                     "Enable runs a minimal connection check after consent.")
    return " ".join(parts + [source_privacy_notice()]) if parts else ""


def probe_source(kind: str) -> str | None:
    """Only call after an explicit authenticated owner enable command.

    Discards probe metadata; it does not import source content or claim full scopes.
    """
    if kind not in source_capabilities():
        if kind == "chat":
            raise SourceReadError(
                "Chat history isn't wired into this Hermes runtime, so no grant changed. "
                "Ask your operator to update the Tingle runtime, then retry `/tingle source enable chat`; "
                "this isn't a chat-account sign-in issue.")
        raise SourceReadError("Source capability is not installed/wired in this profile; no grant changed.")
    try:
        if kind == "email":
            return body_account_identity()[1]
        elif kind == "calendar":
            return calendar_account_identity()
        # Chat checks interface presence only, never reads conversation contents.
    except SourceReadError as exc:
        raise SourceReadError("Source probe failed; helper is installed but account/scopes could not be verified. "
                              "Check the connection in Hermes, then explicitly re-enable. No grant changed.") from exc


def source_privacy_notice() -> str:
    return ("Connection checks do not read email messages or event details. Google may refresh its saved sign-in on this profile; "
            "chat checks access without reading messages. Later source text and tracked details may be sent to your "
            "configured AI provider and appear in Hermes history. Turning a source off stops future reads, but "
            "can't pull back content already sent or saved.")


def body_account_identity() -> tuple[str, str]:
    """Minimal identity read after source consent, hashed for profile-local audit."""
    import hashlib
    value = _gws_bridge("gmail", "users", "getProfile", "--params",
                        json.dumps({"userId": "me", "fields": "emailAddress"}))
    address = value.get("emailAddress") if isinstance(value, dict) else None
    if not isinstance(address, str) or "@" not in address or len(address) > 320:
        raise SourceReadError("Gmail account identity unavailable; body read refused")
    return address.strip(), hashlib.sha256(address.strip().casefold().encode()).hexdigest()


def _mime_headers(payload: dict) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise SourceReadError("Malformed MIME payload")
    headers = payload.get('headers', [])
    if not isinstance(headers, list) or any(not isinstance(h, dict) or
            not isinstance(h.get('name'), str) or not isinstance(h.get('value'), str) for h in headers):
        raise SourceReadError("Malformed MIME headers")
    return {h['name'].lower(): h['value'] for h in headers}


def _list_mail_flagged(payload: dict) -> bool:
    """Privacy gating for list mail: ANY case-insensitive instance of a nonempty
    List-Id/List-Unsubscribe header suppresses body reads. A later empty or
    duplicate value never clears an earlier flag, and malformed header entries
    are treated as flagged; gating never relies on last-wins normalization."""
    if not isinstance(payload, dict):
        return True
    headers = payload.get('headers', [])
    if not isinstance(headers, list):
        return True
    for header in headers:
        if (not isinstance(header, dict) or not isinstance(header.get('name'), str)
                or not isinstance(header.get('value'), str)):
            return True
        if header['name'].casefold() in ('list-id', 'list-unsubscribe') and header['value'].strip():
            return True
    return False


def inline_email_text(payload: dict, limit: int) -> str:
    """Bounded shared text extraction. Full payloads may include MIME attachments.

    No attachment endpoint is used. Filename-bearing subtrees are not retained.
    HTML fallback stays untrusted text; it is never rendered or executed.
    """
    import base64
    queue = [payload]
    plain, html = [], []
    visited = 0
    while queue and sum(map(len, plain)) < limit:
        node = queue.pop(0)
        visited += 1
        if visited > 100:
            break
        _mime_headers(node)  # Validate nested containers, not only the root.
        body = node.get('body', {})
        children = node.get('parts', [])
        if not isinstance(body, dict) or not isinstance(children, list) or any(not isinstance(v, dict) for v in children):
            raise SourceReadError("Malformed MIME body/parts")
        encoded = body.get('data')
        if encoded is not None and not isinstance(encoded, str):
            raise SourceReadError("Malformed inline email data")
        if node.get('filename'):
            continue
        if node.get('mimeType') in ('text/plain', 'text/html', None) and isinstance(encoded, str):
            try:
                target = html if node.get('mimeType') == 'text/html' else plain
                target.append(base64.b64decode(encoded + '=' * (-len(encoded) % 4), altchars=b'-_', validate=True).decode(errors='replace')[:limit])
            except (ValueError, TypeError):
                raise SourceReadError("Malformed inline email body") from None
        queue.extend(children[:50])
    return '\n'.join(plain or html)[:limit]


def _read_bound_email(message_id: str, account_user: str) -> dict:
    """Full-body path pins Gmail userId to provider identity, never the mutable me alias."""
    data = _gmail_bridge("gmail", "users", "messages", "get", "--params", json.dumps({
        "userId": account_user, "id": message_id, "format": "full",
        "fields": "id,threadId,payload"}))
    if not isinstance(data, dict) or data.get("id") != message_id or not isinstance(data.get("payload"), dict):
        raise SourceReadError("Gmail full-body identity/structure mismatch")
    headers = _mime_headers(data['payload'])
    text = inline_email_text(data['payload'], MAX_GMAIL_BODY_CHARS)
    return {'id':message_id,'threadId':str(data.get('threadId','')),'from':headers.get('from',''),
            'to':headers.get('to',''),'subject':headers.get('subject',''),'date':headers.get('date',''),
            'body':text}


@store.serialized("sources")
@_account_bound
def checkpoint_replan_context(item):
    """Explicit one-item recovery only. Existing body reservations are content-free writes."""
    checks, errors = _read_email_checkpoint_checks([item], recovery=True)
    if errors or len(checks) != 1:
        raise SourceReadError('Replan blocked: current thread context is inaccessible, excluded or exceeds four messages.')
    messages = []
    for m in checks[0]['messages']:
        thread, _ = _gmail_source_parts(item['source_ref'])
        _email_timestamp_preflight(m['source_id'])
        metadata = _gmail_bridge('gmail','users','messages','get','--params',json.dumps({
            'userId':'me','id':m['source_id'],'format':'metadata',
            'fields':'id,threadId,labelIds,internalDate,snippet,payload(headers)'}))
        if (not isinstance(metadata,dict) or metadata.get('id') != m['source_id']
                or metadata.get('threadId') != thread or not isinstance(metadata.get('labelIds'),list)
                or any(not isinstance(label,str) for label in metadata['labelIds'])
                or {'TRASH','SPAM','DRAFT'} & {label.upper() for label in metadata['labelIds']}
                or not isinstance(metadata.get('payload'),dict)
                or not isinstance(metadata['payload'].get('headers'),list)
                or _list_mail_flagged(metadata['payload'])):
            raise SourceReadError('Replan blocked: fresh body metadata is excluded or incomplete.')
        try:
            from .hermes_tingle import _excluded_work
        except ImportError:
            from hermes_tingle import _excluded_work
        headers = _mime_headers(metadata['payload'])
        if _excluded_work({'title':item['title'],'summary':item['summary'],
                'source_ref':item['source_ref']+' '+str(metadata.get('snippet',''))+' '+json.dumps(headers)}):
            raise SourceReadError('Replan blocked: current source policy excludes body context.')
        store.register_email_candidates([m['source_id']],account_key=_gmail_account.get()[1])
        try:
            email = read_email(m['source_id'])
        except (ValueError, SourceReadError):
            raise SourceReadError('Replan blocked: eligible body context unavailable or recently read; existing read limits were not bypassed.') from None
        thread, _ = _gmail_source_parts(item['source_ref'])
        if email.get('thread_id') != thread or not email.get('body') or len(email['body']) >= MAX_GMAIL_BODY_CHARS:
            raise SourceReadError('Replan blocked: body context missing, truncated or from a different thread.')
        messages.append({'n':len(messages)+1,'date':m['date'],'direction':m['direction'],'text':email['body']})
    record = {'id':'C1','context_complete':True,'omitted_older_messages':0,'messages':messages}
    return record, checks[0]['receipt']
