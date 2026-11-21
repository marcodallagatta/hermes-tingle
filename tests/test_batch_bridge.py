"""Synthetic tests for the one-off look-back date batch reader. No network access."""
import json
from datetime import datetime, timezone

import pytest

import gws_batch_bridge as bb


def _part(mid, stamp, status="200 OK"):
    payload = {"id": mid, "internalDate": stamp}
    return (f"--B\r\nContent-Type: application/http\r\n\r\n"
            f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\n\r\n"
            f"{json.dumps(payload)}\r\n")


def test_build_body_requests_only_ids_and_dates():
    body = bb.build_body("owner@example.invalid", ["m1", "m2"], "B").decode()
    assert body.count("GET /gmail/v1/users/owner@example.invalid/messages/") == 2
    assert "fields=id%2CinternalDate" in body
    for forbidden in ("Subject", "snippet", "payload", "format=full"):
        assert forbidden not in body


def test_hermes_home_without_host_constants(monkeypatch):
    monkeypatch.setenv("HERMES_HOME", "/tmp/tingle-fake-home")
    assert str(bb._hermes_home()) == "/tmp/tingle-fake-home"
    monkeypatch.delenv("HERMES_HOME", raising=False)
    home = bb._hermes_home()
    assert home.is_absolute()
    assert str(home) != "/tmp/tingle-fake-home"


def test_parse_response_maps_ids_in_order():
    raw = (_part("m1", "1763300000000") + _part("m2", "1763300001000") + "--B--\r\n").encode()
    assert bb.parse_response(raw, ["m1", "m2"], "B") == [
        {"id": "m1", "internalDate": "1763300000000"},
        {"id": "m2", "internalDate": "1763300001000"},
    ]


def test_parse_response_handles_server_boundary_and_epilogue():
    raw = ('\r\n--batch_server123\r\nContent-Type: application/http\r\nContent-ID: response-\r\n\r\n'
           'HTTP/1.1 200 OK\r\nContent-Type: application/json; charset=UTF-8\r\nVary: Origin\r\n\r\n'
           '{\n  "id": "m1",\n  "internalDate": "1763300000000"\n}\n'
           '\r\n--batch_server123--\r\n').encode()
    assert bb.parse_response(raw, ['m1'], 'client-boundary') == [
        {'id': 'm1', 'internalDate': '1763300000000'}]


def test_parse_response_rejects_mismatched_id():
    raw = (_part("other", "1000") + "--B--\r\n").encode()
    with pytest.raises(ValueError):
        bb.parse_response(raw, ["m1"], "B")


def test_parse_response_rejects_non_200_part():
    raw = (_part("m1", "1000", status="404 Not Found") + "--B--\r\n").encode()
    with pytest.raises(ValueError):
        bb.parse_response(raw, ["m1"], "B")


def test_batch_dates_rejects_more_than_cap_ids():
    with pytest.raises(ValueError):
        bb.batch_dates("owner@example.invalid", [f"m{i}" for i in range(bb.MAX_IDS + 1)],
                       token="t", opener=lambda *a, **k: pytest.fail("no network expected"))


def test_batch_dates_sends_bearer_header_and_uses_one_request(monkeypatch):
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return (_part("m1", "1763300000000") + "--B--\r\n").encode()

    def opener(req, **kwargs):
        seen["authorization"] = req.get_header("Authorization")
        seen["url"] = req.full_url
        return Resp()

    monkeypatch.setattr(bb, "_boundary", lambda: "B")
    result = bb.batch_dates("owner@example.invalid", ["m1"], token="secret-token", opener=opener)
    assert result == {"dates": [{"id": "m1", "internalDate": "1763300000000"}],
                      "unverified": 0, "calls": 1, "inner_calls": 1, "diagnostic": ""}
    assert seen["authorization"] == "Bearer " + "secret-token"
    assert seen["url"] == bb.BATCH_URL


class Response:
    def __init__(self, raw):
        self.raw = raw
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self):
        return self.raw


def scripted(responses, seen):
    def opener(req, **kwargs):
        seen.append(req.data.decode())
        value = responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return Response(value)
    return opener


def test_mixed_parts_retry_only_failed_ids_then_success():
    seen, sleeps = [], []
    raw = (_part('m1', '1763300000000') + _part('m2', '0', '503 Service Unavailable')).encode()
    result = bb.batch_dates('owner@example.invalid', ['m1', 'm2'], token='t',
        opener=scripted([raw, _part('m2', '1763300001000').encode()], seen), sleep=sleeps.append)
    assert result['unverified'] == 0 and result['calls'] == 2
    assert len(result['dates']) == 2 and sleeps == [0.25]
    assert '/messages/m1?' not in seen[1] and '/messages/m2?' in seen[1]


@pytest.mark.parametrize('status', ['503 Service Unavailable', '429 Too Many Requests'])
def test_exhausted_transient_keeps_verified_subset_and_redacts(status):
    seen, sleeps = [], []
    bad = _part('secret-id', 'private-token@example.invalid', status)
    result = bb.batch_dates('owner@example.invalid', ['m1', 'secret-id'], token='private-token',
        opener=scripted([(_part('m1', '1763300000000') + bad).encode(), bad.encode(), bad.encode()], seen),
        sleep=sleeps.append)
    assert result['dates'] == [{'id': 'm1', 'internalDate': '1763300000000'}]
    assert result['unverified'] == 1 and result['calls'] == 3
    assert sleeps == [0.25, 0.5]
    assert '1/2' in result['diagnostic'] and status[:3] in result['diagnostic']
    assert all(secret not in result['diagnostic'] for secret in ('secret-id', 'private-token', '@'))


@pytest.mark.parametrize('status', ['400 Bad Request', '401 Unauthorized', '403 Forbidden', '404 Not Found'])
def test_nontransient_parts_never_retry(status):
    seen = []
    result = bb.batch_dates('owner@example.invalid', ['m1'], token='t',
        opener=scripted([_part('m1', '0', status).encode()], seen), sleep=lambda _: pytest.fail('no backoff'))
    assert result['calls'] == 1 and result['unverified'] == 1 and not result['dates']


def test_whole_request_503_retry_success():
    import urllib.error
    seen = []
    error = urllib.error.HTTPError(bb.BATCH_URL, 503, 'private error', {}, None)
    result = bb.batch_dates('owner@example.invalid', ['m1'], token='t',
        opener=scripted([error, _part('m1', '1763300000000').encode()], seen), sleep=lambda _: None)
    assert result['calls'] == 2 and result['unverified'] == 0


def test_malformed_success_identity_still_fails_closed():
    with pytest.raises(ValueError, match='identity mismatch'):
        bb.batch_dates('owner@example.invalid', ['m1'], token='t',
            opener=lambda *a, **k: Response(_part('wrong', '1763300000000').encode()))


def test_error_counts_include_all_parts_without_private_payload():
    raw = (_part('m1', '1763300000000') + _part('secret', 'private', '503 Service Unavailable')).encode()
    with pytest.raises(ValueError, match='1/2 parts returned 503') as error:
        bb.parse_response(raw, ['m1', 'secret'], 'B')
    assert 'private' not in str(error.value) and 'secret' not in str(error.value)


def test_wall_clock_bound_stops_before_another_retry():
    seen = []
    times = iter([0, 0, 20])
    result = bb.batch_dates('owner@example.invalid', ['m1'], token='t',
        opener=scripted([_part('m1', '0', '503 Service Unavailable').encode()], seen),
        sleep=lambda _: pytest.fail('deadline prevents sleep'), clock=lambda: next(times))
    assert result['calls'] == 1 and result['unverified'] == 1
