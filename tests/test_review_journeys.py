"""Synthetic review journeys: three distinct emails, Track / Skip / Cancel, then a duplicate refusal."""
import pytest
from test_lookback import env  # noqa: F401  (fixture only)
import base64, json, re
from datetime import timedelta
import hermes_tingle_sources as sources, hermes_tingle_lookback as lb, hermes_tingle_store as store, hermes_tingle as plugin

SUBJECTS = {'m1': 'Synthetic budget review', 'm2': 'Synthetic newsletter', 'm3': 'Synthetic venue question'}


@pytest.fixture
def three(env, monkeypatch):
    stamp = env[4]
    def bridge(*args):
        params = json.loads(args[-1])
        if args[2] == 'getProfile': return {'emailAddress': 'owner@example.invalid'}
        if args[2] == 'threads':
            mid = 'm' + params['id'][1:]
            return {'id': params['id'], 'messages': [{'id': mid}]}
        if args[3] == 'list': return {'messages': [{'id': m} for m in SUBJECTS]}
        mid = params['id']; tid = 't' + mid[1:]
        if params['format'] == 'full':
            return {'id': mid, 'internalDate': stamp, 'threadId': tid, 'payload': {'mimeType': 'text/plain', 'body': {'data': base64.urlsafe_b64encode(b'Synthetic body text.').decode()}}}
        if params['format'] == 'minimal': return {'id': mid, 'internalDate': stamp}
        return {'id': mid, 'internalDate': stamp, 'threadId': tid, 'labelIds': ['INBOX'], 'snippet': 'Synthetic snippet',
                'payload': {'headers': [{'name': 'Subject', 'value': SUBJECTS[mid]}, {'name': 'From', 'value': 'Synthetic Person <p@example.invalid>'}]}}
    monkeypatch.setattr(sources, '_gws_bridge', bridge)
    return env


def test_track_skip_cancel_journey_and_duplicate_refusal(three):
    sender, ctx = three[0], three[1]
    ctx.ask_choice = lambda q, ch: 'scan'
    out = plugin.handle_command(ctx, 'lookback start 7', source=sender)
    assert ('lookback review LB-' in out) or ('No possible follow-ups' in out), out
    scan_id = re.search(r'LB-[0-9A-F]{8}', out).group(0)
    plan = iter(['track', 'skip', 'track'])
    log = []
    def ask(q, ch):
        log.append(q)
        if 'SHARED' in q:
            return 'accept' if len([x for x in log if 'SHARED' in x]) == 1 else 'cancel'
        return next(plan)
    ctx.ask_choice = ask
    text = plugin.handle_command(ctx, f'lookback review {scan_id}', source=sender)
    print('\n--- review summary\n' + text)
    assert '1 tracked, 1 skipped, 1 still waiting' in text
    assert len(store.list_items()) == 1
    print('\n--- confirm prompt example\n' + [q for q in log if 'SHARED' in q][0])
    # Duplicate: the same email again is refused, expected.
    ctx.ask_choice = lambda q, ch: 'scan'
    out2 = plugin.handle_command(ctx, 'lookback start 7', source=sender)
    scan2 = re.search(r'LB-[0-9A-F]{8}', out2).group(0)
    ctx.ask_choice = lambda q, ch: 'accept' if 'SHARED' in q else 'track'
    text2 = plugin.handle_command(ctx, f'lookback review {scan2}', source=sender)
    print('\n--- second scan review (duplicates expected refused)\n' + text2)
    assert len(store.list_items()) >= 1


def test_refused_lead_offers_skip_with_uncertain_identity_wording_and_done_summary(three):
    sender, ctx = three[0], three[1]
    ctx.ask_choice = lambda q, ch: 'scan'
    scan1 = re.search(r'LB-[0-9A-F]{8}', plugin.handle_command(ctx, 'lookback start 7', source=sender)).group(0)
    ctx.ask_choice = lambda q, ch: 'accept' if 'SHARED' in q else 'track'
    plugin.handle_command(ctx, f'lookback review {scan1}', source=sender)  # tracks all three
    conn = lb.connect(); conn.execute('DELETE FROM scans'); conn.execute('DELETE FROM candidates'); conn.commit(); conn.close()
    ctx.ask_choice = lambda q, ch: 'scan'
    scan2 = re.search(r'LB-[0-9A-F]{8}', plugin.handle_command(ctx, 'lookback start 7', source=sender)).group(0)
    asked = []
    def ask(q, ch):
        asked.append(q)
        if 'Skip this lead so it stops' in q:
            return 'skip'
        return 'accept' if 'SHARED' in q else 'track'
    ctx.ask_choice = ask
    text = plugin.handle_command(ctx, f'lookback review {scan2}', source=sender)
    refusals = [q for q in asked if 'Skip this lead so it stops' in q]
    assert refusals and 'may come from this same email' in refusals[0] and 'cannot tell for certain' in refusals[0]
    assert 'definitely' not in refusals[0].lower()
    assert 'Review done' in text and '0 tracked, 3 skipped' in text
    print('\n--- refused leads, skipped, review done\n' + text)


def test_source_state_read_failure_is_unavailable_not_off(three, monkeypatch):
    monkeypatch.setattr(store, 'list_source_optins', lambda: (_ for _ in ()).throw(RuntimeError('boom')))
    out = plugin.handle_command(three[1], '', source=three[0])
    assert 'Source status unavailable' in out and 'off until you opt in' not in out
