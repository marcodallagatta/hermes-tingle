#!/usr/bin/env python3
"""Bounded Gmail batch primitives using the existing workspace token bridge.

Default dates mode requests only IDs/internalDate, retaining existing retries.
Fixed metadata modes request only the existing standing/look-back field/header
profiles, one HTTP attempt only. No bodies, attachments or arbitrary methods.
No token is passed in arguments. Caller owns authority/date/budget boundaries.
"""
import json
import os
import re
import secrets
import sys
import time
from collections import Counter
import urllib.error
import urllib.request
from pathlib import Path

BATCH_URL = "https://gmail.googleapis.com/batch/gmail/v1"
MAX_IDS = 20
MAX_ATTEMPTS = 3
RETRY_STATUS = {429, 503}
FIELDS = "id%2CinternalDate"


def _boundary():
    return "tingle-batch-" + secrets.token_hex(8)


def _hermes_home() -> Path:
    """Resolve the Hermes home without importing host modules.

    The plugin host's interpreter may not have hermes-agent on sys.path, so
    derive the home from HERMES_HOME or this script's installed location.
    """
    configured = os.environ.get("HERMES_HOME")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[3]


def _token():
    scripts = _hermes_home() / "skills" / "productivity" / "google-workspace" / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import gws_bridge
    return gws_bridge.get_valid_token()


def build_body(user_id, ids, boundary):
    parts = []
    for mid in ids:
        request = (
            f"GET /gmail/v1/users/{user_id}/messages/{mid}"
            f"?format=minimal&fields={FIELDS} HTTP/1.1\r\n"
            "Host: gmail.googleapis.com\r\n\r\n"
        )
        parts.append(
            f"--{boundary}\r\nContent-Type: application/http\r\n"
            f"Content-Transfer-Encoding: binary\r\n\r\n{request}\r\n"
        )
    return ("".join(parts) + f"--{boundary}--\r\n").encode("utf-8")


def _parts(raw, ids):
    """Keep successful parts; failures carry only allowlisted status diagnostics."""
    text = raw.decode("utf-8", errors="replace")
    matches = list(re.finditer(r"HTTP/1\.1 (\d{3})[^\r\n]*", text))
    if len(matches) != len(ids):
        raise ValueError("Gmail batch returned an unexpected number of parts")
    out = []
    for index, (expected, match) in enumerate(zip(ids, matches)):
        status = int(match.group(1))
        if status != 200:
            out.append({"id": expected, "status": status})
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sep = text.find("\r\n\r\n", match.end(), end)
        if sep < 0:
            raise ValueError("Gmail batch part malformed")
        try:
            item, _ = json.JSONDecoder().raw_decode(text[sep + 4:end].lstrip())
        except json.JSONDecodeError:
            raise ValueError("Gmail batch part malformed") from None
        if not isinstance(item, dict) or item.get("id") != expected:
            raise ValueError("Gmail batch message identity mismatch")
        stamp = item.get("internalDate")
        if not isinstance(stamp, str) or not stamp.isdigit() or len(stamp) < 10:
            raise ValueError("Gmail batch date missing or untrustworthy")
        out.append({"id": expected, "internalDate": stamp})
    return out


def _diagnostic(statuses, total):
    labels = {0: "transport failure", 503: "503 Service Unavailable", 429: "429 rate limited"}
    counts = Counter(statuses)
    return "Gmail batch date gaps: " + "; ".join(
        f"{count}/{total} parts returned {labels.get(status, str(status))}"
        for status, count in sorted(counts.items()))


def parse_response(raw, ids, boundary):
    del boundary  # The server chooses its own boundary.
    parts = _parts(raw, ids)
    failures = [part['status'] for part in parts if 'status' in part]
    if failures:
        raise ValueError(_diagnostic(failures, len(ids)))
    return parts


def batch_dates(user_id, ids, *, token=None, opener=None, sleep=time.sleep, clock=time.monotonic):
    """One bounded chunk, retrying only transient failed IDs, never good parts.

    The caller checks authority/budgets between chunks. No provider error body
    reaches diagnostics, since it can contain account information or tokens.
    """
    if not isinstance(ids, list) or not ids or len(ids) > MAX_IDS:
        raise ValueError("Gmail batch needs 1 to 20 message IDs")
    token = _token() if token is None else token
    opener = opener or urllib.request.urlopen
    pending, verified, failures = list(ids), {}, {}
    calls = inner_calls = 0
    deadline = clock() + 20
    for attempt in range(MAX_ATTEMPTS):
        if attempt:
            delay = 0.25 * (2 ** (attempt - 1))
            if clock() + delay >= deadline:
                break
            sleep(delay)
        remaining = deadline - clock()
        if remaining <= 0:
            break
        boundary = _boundary()
        request = urllib.request.Request(
            BATCH_URL, data=build_body(user_id, pending, boundary),
            headers={"Content-Type": f"multipart/mixed; boundary={boundary}",
                     "Authorization": f"Bearer {token}"})
        calls += 1
        inner_calls += len(pending)
        try:
            with opener(request, timeout=min(8, remaining)) as response:
                parts = _parts(response.read(), pending)
        except urllib.error.HTTPError as exc:
            parts = [{"id": mid, "status": exc.code} for mid in pending]
        except (urllib.error.URLError, TimeoutError, OSError):
            parts = [{"id": mid, "status": 0} for mid in pending]
        retry = []
        for part in parts:
            mid = part['id']
            if 'internalDate' in part:
                verified[mid] = part
                failures.pop(mid, None)
            else:
                failures[mid] = part['status']
                if part['status'] in RETRY_STATUS:
                    retry.append(mid)
        if not retry:
            break
        pending = retry
    return {"dates": [verified[mid] for mid in ids if mid in verified],
            "unverified": len(ids) - len(verified), "calls": calls, "inner_calls": inner_calls,
            "diagnostic": _diagnostic(list(failures.values()), len(ids)) if failures else ""}



# Fixed metadata profiles only. This path never requests bodies or attachments.
METADATA_HEADERS = {
    'standing': ('From', 'To', 'Subject', 'Date'),
    'lookback': ('From', 'To', 'Subject', 'Date', 'List-Id', 'List-Unsubscribe', 'Auto-Submitted'),
}
METADATA_FIELDS = 'id,threadId,labelIds,internalDate,snippet,payload(headers)'
_METADATA_ID = re.compile(r'^[A-Za-z0-9_-]{1,256}$')


def build_metadata_body(user_id, ids, boundary, profile):
    from urllib.parse import urlencode, quote
    if profile not in METADATA_HEADERS:
        raise ValueError('Unknown fixed metadata profile')
    if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_IDS:
        raise ValueError('Metadata batch needs 1 to 20 unique IDs')
    if any(not isinstance(mid, str) or not _METADATA_ID.fullmatch(mid) for mid in ids) or len(set(ids)) != len(ids):
        raise ValueError('Invalid metadata message ID')
    if not isinstance(user_id, str) or '@' not in user_id or any(c.isspace() for c in user_id):
        raise ValueError('Metadata batch needs a verified account')
    query = urlencode({'format':'metadata', 'fields':METADATA_FIELDS,
                       'metadataHeaders':METADATA_HEADERS[profile]}, doseq=True)
    parts=[]
    for mid in ids:
        path=f'/gmail/v1/users/{quote(user_id, safe="")}/messages/{mid}?{query}'
        parts.append(f'--{boundary}\r\nContent-Type: application/http\r\n\r\n'
                     f'GET {path} HTTP/1.1\r\nHost: gmail.googleapis.com\r\n\r\n')
    return (''.join(parts)+f'--{boundary}--\r\n').encode()


def _metadata_error_kind(status, payload):
    error=payload.get('error', {}) if isinstance(payload,dict) else {}
    reasons={entry.get('reason') for entry in error.get('errors', [])
             if isinstance(entry,dict) and isinstance(entry.get('reason'),str)} if isinstance(error,dict) and isinstance(error.get('errors',[]),list) else set()
    if status==404:
        return 'not_found'
    if status==401 or (status==403 and reasons & {'authError','insufficientPermissions'}):
        return 'auth'
    if status==429 or (status==403 and reasons & {'rateLimitExceeded','userRateLimitExceeded'}):
        return 'rate_limit'
    return 'transient'


def parse_metadata_response(raw, ids, content_type):
    from email import policy
    from email.parser import BytesParser
    if not isinstance(content_type,str) or '\r' in content_type or '\n' in content_type:
        raise ValueError('Invalid metadata response content type')
    outer=BytesParser(policy=policy.default).parsebytes(
        b'Content-Type: '+content_type.encode('ascii')+b'\r\nMIME-Version: 1.0\r\n\r\n'+raw)
    if not outer.is_multipart() or outer.defects:
        raise ValueError('Malformed metadata multipart response')
    parts=list(outer.iter_parts())
    if len(parts)!=len(ids):
        raise ValueError('Unexpected metadata response part count')
    messages,errors=[],[]
    for expected,part in zip(ids,parts):
        if part.get_content_type()!='application/http' or part.defects:
            raise ValueError('Malformed metadata response part')
        inner=part.get_payload(decode=True)
        if not isinstance(inner,bytes):
            raise ValueError('Malformed metadata response payload')
        head,sep,body=inner.partition(b'\r\n\r\n')
        match=re.match(rb'HTTP/1\.[01] ([0-9]{3})(?:[ \r\n]|$)',head)
        if not sep or not match:
            raise ValueError('Malformed metadata HTTP part')
        status=int(match.group(1))
        try:
            payload=json.loads(body)
        except (ValueError,UnicodeError):
            if status==200:
                raise ValueError('Malformed metadata JSON') from None
            payload={}
        if status!=200:
            errors.append({'id':expected,'status':status,'kind':_metadata_error_kind(status,payload)})
            continue
        if (not isinstance(payload,dict) or payload.get('id')!=expected
                or set(payload)-{'id','threadId','labelIds','internalDate','snippet','payload'}):
            raise ValueError('Metadata identity or field mismatch')
        messages.append(payload)
    return {'messages':messages,'errors':errors}


def batch_metadata(user_id, ids, profile, *, token=None, opener=None):
    # Exactly one attempt. Retry policy belongs to the existing host state machine.
    boundary=_boundary()
    body=build_metadata_body(user_id,ids,boundary,profile)
    token=_token() if token is None else token
    opener=opener or urllib.request.urlopen
    request=urllib.request.Request(BATCH_URL,data=body,headers={
        'Content-Type':f'multipart/mixed; boundary={boundary}', 'Authorization':f'Bearer {token}'})
    try:
        with opener(request,timeout=20) as response:
            raw=response.read(3_000_001)
            if len(raw)>3_000_000:
                raise ValueError('Metadata response exceeds byte limit')
            result=parse_metadata_response(raw,ids,response.headers.get('Content-Type'))
    except urllib.error.HTTPError as exc:
        result={'messages':[],'errors':[{'id':mid,'status':exc.code,
                'kind':_metadata_error_kind(exc.code,{})} for mid in ids]}
    except (urllib.error.URLError,TimeoutError,OSError):
        result={'messages':[],'errors':[{'id':mid,'status':0,'kind':'transient'} for mid in ids]}
    return dict(result,http_calls=1,inner_calls=len(ids))

def main():
    payload = json.loads(sys.argv[1])
    user_id = payload.get("userId")
    ids = payload.get("ids")
    if not isinstance(user_id, str) or "@" not in user_id:
        raise ValueError("Gmail batch needs a verified account")
    mode = payload.get('mode', 'dates')
    if mode == 'metadata':
        if set(payload) - {'userId', 'ids', 'mode', 'profile'}:
            raise ValueError('Unsupported metadata request parameters')
        print(json.dumps(batch_metadata(user_id, ids, payload.get('profile'))))
    elif mode == 'dates':
        print(json.dumps(batch_dates(user_id, ids)))
    else:
        raise ValueError('Unsupported fixed batch mode')


if __name__ == "__main__":
    main()
