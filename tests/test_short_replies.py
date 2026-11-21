"""Bounded default replies and safe diagnostic classification. Synthetic only."""
import re
import subprocess
from types import SimpleNamespace

import pytest
import hermes_tingle as plugin
import hermes_tingle_lookback as lb
import hermes_tingle_sources as sources
from test_lookback import env
from test_lookback_quality import FakeLlm, seed, strong, assess_scan


@pytest.mark.parametrize('diagnostic,expected,absent', [
    ('Token refresh failed (network): timed out', 'network', 'OAuth'),
    ('HTTP 401 invalid_grant', 'authorization', 'network'),
    ('HTTP 403 rateLimitExceeded', 'rate limited', 'OAuth'),
    ('HTTP 403 domainPolicy', 'not confirmed', 'OAuth'),
    ('Private payload in unknown error', 'not confirmed', 'Private'),
])
def test_safe_helper_classification(monkeypatch, diagnostic, expected, absent):
    monkeypatch.setattr(sources.subprocess, 'run', lambda *a, **k: SimpleNamespace(
        returncode=1, stdout='', stderr=diagnostic+' secret-token=sensitive customer@example.invalid'))
    with pytest.raises(sources.SourceReadError) as e:
        sources._run_json(['helper'])
    text=str(e.value)
    assert expected in text and absent not in text
    assert 'sensitive' not in text and 'example.invalid' not in text


def test_timeout_never_claims_oauth_failure_or_retries(monkeypatch):
    calls=[]
    def run(*a, **k):
        calls.append(1)
        raise subprocess.TimeoutExpired('helper',25)
    monkeypatch.setattr(sources.subprocess,'run',run)
    with pytest.raises(sources.SourceReadError) as e:
        sources._run_json(['helper'])
    assert 'timed out' in str(e.value) and 'OAuth' not in str(e.value)
    assert len(calls)==1


def test_compact_default_keeps_detail_explicit(env):
    sid=seed(env,strong(2))
    assess_scan(env,sid,FakeLlm())
    conn=lb.connect();row=conn.execute('SELECT * FROM scans WHERE id=?',(sid,)).fetchone()
    short=lb._summary_text(conn,row);conn.close()
    full=plugin.handle_command(env[1],f'lookback results {sid}',source=env[0])
    assert len(short)<len(full) and 'unverified' in short and 'Nothing added' in short
    assert 'Evidence:' not in short and 'Ask:' not in short
    assert 'lookback review '+sid in short and 'Ask:' in full and 'Could you review the revised contract?' in full


def test_compact_partial_and_failed_states_stay_visible(env):
    sid=seed(env,strong(2))
    assess_scan(env,sid,FakeLlm())
    conn=lb.connect()
    conn.execute("UPDATE scans SET coverage='Partial/failed scan',error_detail='connection timed out' WHERE id=?",(sid,))
    a=lb._latest_assessment(conn,sid)
    conn.execute('UPDATE assessments SET failed_calls=1,assessed=1 WHERE id=?',(a['id'],))
    conn.commit();row=conn.execute('SELECT * FROM scans WHERE id=?',(sid,)).fetchone()
    out=lb._summary_text(conn,row);conn.close()
    assert 'incomplete' in out and 'network' in out and '1 model request failed; 1 conversation was not assessed.' in out
    assert 'permission changed' not in out


def test_topic_icons_are_closed_and_neutral_for_unknowns():
    from hermes_tingle_topic import safe_icon
    assert safe_icon('📄')=='📄'
    assert safe_icon('arbitrary text')=='📌'
    assert safe_icon(None)=='📌'


def test_short_title_cannot_inject_lines_markup_or_links():
    text=lb.short_title('<b>hi</b>\n[click](https://synthetic.invalid/x) `quote`\u202e '+('x'*200))
    assert '\n' not in text and 'https' not in text and '[' not in text and '<b>' not in text
    assert '\u202e' not in text and len(text)<=72


def test_default_caps_titles_at_five_and_keeps_remainder(env):
    sid=seed(env,strong(8))
    assess_scan(env,sid,FakeLlm())
    conn=lb.connect(); row=conn.execute('SELECT * FROM scans WHERE id=?',(sid,)).fetchone()
    out=lb._summary_text(conn,row); conn.close()
    assert out.count('Review the revised contract')==5
    assert 'Plus 3 more.' in out and '8 possible follow-ups' in out
    assert len(out.encode('utf-16-le'))//2 < 1400


@pytest.mark.parametrize('diagnostic,gone,kind', [
    ('HTTP 404 notFound', True, 'transient'),
    ('HTTP 401 invalid credentials', False, 'auth'),
    ('invalid_grant', False, 'auth'),
    ('HTTP 403 rateLimitExceeded', False, 'transient'),
    ('HTTP 403 domainPolicy', False, 'transient'),
    ('ModuleNotFoundError: missing synthetic module', False, 'transient'),
    ('Token refresh failed on network', False, 'transient'),
    ('Unknown read error', False, 'transient'),
])
def test_safe_diagnostics_preserve_downstream_error_contracts(monkeypatch, diagnostic, gone, kind):
    monkeypatch.setattr(sources.subprocess, 'run', lambda *a, **k: SimpleNamespace(
        returncode=1, stdout='', stderr=diagnostic+' secret-token=sensitive'))
    with pytest.raises(sources.SourceReadError) as e:
        sources._run_json(['helper'])
    assert sources._is_not_found_error(e.value) is gone
    assert sources._classify_read_error(e.value)==kind
    assert 'sensitive' not in str(e.value)
