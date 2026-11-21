"""Synthetic private diagnostics; network/subprocess forbidden by conftest."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import json
import pytest
import hermes_tingle_debug as debug
import hermes_tingle as plugin
import hermes_tingle_store as store
from test_lookback import env
from test_lookback_quality import seed, strong, assess_scan, FailingOn

NOW = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)


def test_default_off_and_no_retained_record():
    assert not debug.enabled()
    debug.record('snapshot', {'emails': 4})
    assert debug.read() == []


@pytest.mark.parametrize('event,counts', [
    ('private subject', {'emails': 1}), ('snapshot', {'title': 1}),
    ('snapshot', {'emails': 'secret body'}), ('snapshot', {'emails': True}),
    ('snapshot', {'emails': -1}), ('snapshot', {'emails': 1000001}),
])
def test_closed_schema_rejects_free_text_and_unbounded_counts(event, counts):
    debug.set_enabled(True)
    with pytest.raises(ValueError):
        debug.record(event, counts)
    assert debug.read() == []


def test_ttl_cap_off_purge_and_read_does_not_prune(monkeypatch):
    debug.set_enabled(True, now=NOW)
    for n in range(205):
        debug.record('snapshot', {'emails': n}, now=NOW)
    assert len(debug.read(limit=200, now=NOW)) == 200
    assert debug.read(now=NOW)[0]['counts']['emails'] == 204
    assert not debug.read(now=NOW + timedelta(days=7))
    conn = store._connect()
    assert conn.execute('SELECT COUNT(*) FROM private_debug_records').fetchone()[0] == 200
    conn.close()
    debug.record('assessment', {'failed_calls': 1}, now=NOW+timedelta(days=7))
    assert len(debug.read(limit=200, now=NOW+timedelta(days=7))) == 1
    debug.purge()
    assert debug.enabled() and not debug.read(now=NOW)
    debug.record('snapshot', {'emails': 1}, now=NOW)
    debug.set_enabled(False)
    assert not debug.enabled() and not debug.read(now=NOW)


def test_fresh_module_read_and_corrupt_record_fail_closed():
    import importlib
    debug.set_enabled(True)
    debug.record('snapshot', {'emails': 2})
    assert importlib.reload(debug).read()[0]['counts'] == {'emails': 2}
    conn = store._connect()
    conn.execute('INSERT INTO private_debug_records(created,event,counts) VALUES(?,?,?)',
                 (datetime.now(timezone.utc).isoformat(), 'snapshot', json.dumps({'subject': 'secret'})))
    conn.commit(); conn.close()
    assert len(debug.read()) == 1


def test_owner_dm_controls_and_no_group_disclosure(env):
    out = plugin.handle_command(env[1], 'debug on', source=env[0])
    assert '7 days' in out and '200' in out and debug.enabled()
    debug.record('snapshot', {'emails': 3})
    for sender in [None, SimpleNamespace(platform='telegram', user_id='999', chat_id='999', chat_type='dm', is_bot=False),
                   SimpleNamespace(platform='telegram', user_id='42', chat_id='42', chat_type='group', is_bot=False)]:
        for mode in ['show', 'off', 'purge']:
            assert 'emails=3' not in plugin.handle_command(env[1], 'debug '+mode, source=sender)
        assert debug.enabled() and debug.read()
    assert 'emails=3' in plugin.handle_command(env[1], 'debug show', source=env[0])
    assert 'deleted' in plugin.handle_command(env[1], 'debug off', source=env[0])
    assert not debug.enabled() and not debug.read()


def test_failed_assessment_records_only_counts_with_no_extra_requests(env):
    debug.set_enabled(True)
    sid = seed(env, strong(36))
    llm = FailingOn({1})
    assess_scan(env, sid, llm)
    assert len(llm.calls) == 3
    rows = debug.read()
    assert len(rows) == 1 and rows[0]['event'] == 'assessment'
    assert rows[0]['counts']['failed_calls'] == 1
    assert rows[0]['counts']['request_failed'] == 12
    assert rows[0]['counts']['assessed'] == 24
    text = json.dumps(rows)
    for forbidden in ['SECRET', 'Synthetic topic', 'LC-', 'LB-', 'AS-', '@', 'HT-']:
        assert forbidden not in text
    assert not store.list_items()


def test_safe_record_storage_failure_never_breaks_tracking(monkeypatch):
    monkeypatch.setattr(debug, 'record', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('secret')))
    assert debug.safe_record('snapshot', {'emails': 1}) is None


def test_snapshot_record_not_added_to_model_payload(monkeypatch):
    import hermes_tingle_sources as sources
    from scripts.source_snapshot import build_agent_input
    for kind in ('email', 'calendar', 'chat'):
        store.disable_source(kind)
    before = sources.collect_snapshot()
    debug.set_enabled(True)
    after = sources.collect_snapshot()
    assert debug.read()[0]['event'] == 'snapshot'
    for snapshot in (before, after):
        assert not any('debug' in key for key in snapshot)
        assert 'private_debug' not in build_agent_input(snapshot)
        assert 'observed counters' not in build_agent_input(snapshot)


def test_failed_or_stopped_runs_do_not_claim_complete_trace(env):
    debug.set_enabled(True)
    # No instrumentation record is itself no proof about a run; status is explicit.
    assert 'not proof' in plugin.handle_command(env[1], 'debug show', source=env[0])
