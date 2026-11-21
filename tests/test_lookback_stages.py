"""Temporary stage diagnostics. Synthetic mail and scripted model only, never a live-quality claim."""
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_lookback import env, run, snapshot
from test_lookback_quality import seed, strong, verdict, FakeLlm, FailingOn, assess_scan, ASK
from test_lookback_automatic import noise_payload
import hermes_tingle_lookback as lb
import hermes_tingle as plugin
import hermes_tingle_assess as assess
import hermes_tingle_store as store


def rows(scan):
    conn = lb.connect()
    result = conn.execute('SELECT * FROM candidates WHERE scan_id=? ORDER BY id', (scan,)).fetchall()
    conn.close()
    return result


def codes(scan):
    return [r['stage'] for r in rows(scan)]


def trace(env, scan, fragment='Synthetic topic'):
    return plugin.handle_command(env[1], f'lookback trace {scan} {fragment}', source=env[0])


def test_staging_and_old_rows_are_distinct(env):
    scan, candidate = run(env)
    assert candidate['stage'] == lb.STAGE_STAGED
    assert 'staged' in trace(env, scan['id'], 'Review request')
    conn = lb.connect()
    conn.execute("UPDATE candidates SET stage=''"); conn.commit(); conn.close()
    assert 'not recorded' in trace(env, scan['id'], 'Review request')


def test_model_stages_explain_every_returned_and_missing_verdict(env):
    scan = seed(env, strong(7))
    class Model:
        def complete_structured(self, **kw):
            convs = json.loads(kw['input'][0]['text'])['conversations']
            result = [verdict(c['id'], 'action_needed', ASK, 1, 'Review') for c in convs]
            result[0] = verdict(convs[0]['id'], 'possible')
            result[1] = verdict(convs[1]['id'], 'not_actionable')
            result[2] = verdict(convs[2]['id'], 'action_needed', ASK, 1, 'Review', resolved=True)
            result[3] = verdict(convs[3]['id'], 'action_needed', 'Invented quote not present here', 1, 'Review')
            result[4]['evidence_message'] = 'wrong type'
            return SimpleNamespace(parsed={'results': result[:-1]})
    before = snapshot()
    assess_scan(env, scan, Model())
    assert codes(scan).count(lb.STAGE_MODEL_NOT_ACTION) == 3
    assert codes(scan).count(lb.STAGE_PICK_UNSUPPORTED) == 1
    assert codes(scan).count(lb.STAGE_NO_VERDICT) == 2
    assert codes(scan).count(lb.STAGE_PICK_LISTED) == 1
    assert snapshot() == before


def test_failure_unsent_cap_and_counts_are_separate(env):
    scan = seed(env, strong(90))
    assess_scan(env, scan, FailingOn({2, 3}))
    result = codes(scan)
    assert result.count(lb.STAGE_PICK_LISTED) == 12
    assert result.count(lb.STAGE_REQUEST_FAILED) == 24
    assert result.count(lb.STAGE_REQUEST_NOT_SENT) == 36
    assert result.count(lb.STAGE_CAPPED) == 18
    assert len(result) == 90 and all(code in lb.STAGES for code in result)
    text = plugin.handle_command(env[1], f'lookback list {scan} details', source=env[0])
    assert 'request_failed 24' in text and 'request_not_sent 36' in text and 'capped 18' in text


def test_eligibility_and_suppression_codes_do_not_change_selection(env):
    scan = seed(env, strong(1) + [noise_payload(), dict(subject='Owned', outgoing=True), dict(subject='Drafted')])
    conn = lb.connect(); conn.execute("UPDATE candidates SET draft='{}' WHERE id='LC-00000003'")
    conn.commit(); conn.close()
    llm = FakeLlm(); assess_scan(env, scan, llm)
    assert codes(scan) == [lb.STAGE_PICK_LISTED, lb.STAGE_SUPPRESSED, lb.STAGE_SKIPPED_OWNER_LAST, lb.STAGE_SKIPPED_DRAFT]
    assert len(json.loads(llm.calls[0]['input'][0]['text'])['conversations']) == 1


def test_group_members_share_code_and_tally_is_explicitly_rows(env):
    scan = seed(env, strong(3))
    conn = lb.connect()
    row = conn.execute("SELECT payload FROM candidates WHERE id='LC-00000000'").fetchone()
    data = json.loads(row['payload']); data['thread_id'] = 't1'
    conn.execute("UPDATE candidates SET payload=? WHERE id='LC-00000000'", (json.dumps(data),))
    conn.commit(); conn.close()
    llm = FakeLlm(); assess_scan(env, scan, llm)
    assert codes(scan) == [lb.STAGE_PICK_LISTED] * 3
    assert len(json.loads(llm.calls[0]['input'][0]['text'])['conversations']) == 2
    assert 'messages, not conversation totals' in plugin.handle_command(env[1], f'lookback list {scan} details', source=env[0])


def test_changed_content_does_not_get_listed(env):
    scan = seed(env, strong(1))
    def hook(_):
        conn = lb.connect(); conn.execute("UPDATE candidates SET draft_rev=draft_rev+1")
        conn.commit(); conn.close()
    assess_scan(env, scan, FakeLlm(hook=hook))
    assert codes(scan) == [lb.STAGE_PICK_CHANGED]


def test_exact_skip_terms_have_their_own_code(env, monkeypatch):
    scan = seed(env, strong(1))
    monkeypatch.setattr(store, 'approved_triage_rules', lambda: ['[skip: contract]'])
    assess_scan(env, scan, FakeLlm())
    assert codes(scan) == [lb.STAGE_PICK_SKIPPED_BY_RULE]


def test_accept_clears_payload_and_stage_does_not_leak_into_items(env):
    scan = seed(env, strong(1))
    assess_scan(env, scan, FakeLlm())
    conn = lb.connect(); c = rows(scan)[0]
    lb._commit_lead(conn, env[0], lb.binding(env[0]), c['id'], c['generation'], 'Review', 'Synthetic item', '-', None)
    conn.close()
    c = rows(scan)[0]
    assert c['stage'] == lb.STAGE_ACCEPTED and c['payload'] == '{}' and c['draft'] == ''
    assert 'inconclusive' in trace(env, scan)
    item = store.list_items()[0]
    assert 'stage' not in item and 'pick_listed' not in json.dumps(item)


def test_trace_is_local_content_free_capped_and_read_only(env):
    scan = seed(env, strong(14))
    assess_scan(env, scan, FakeLlm())
    before = [tuple(r) for r in rows(scan)], len(env[2]), snapshot()
    out = trace(env, scan)
    assert '14 staged candidates match' in out and 'First 10 shown of 14' in out
    assert sum(line.startswith('LC-') for line in out.splitlines()) == 10
    assert ASK not in out and 'Synthetic topic' not in out and 'source_ref' not in out
    assert before == ([tuple(r) for r in rows(scan)], len(env[2]), snapshot())
    assert 'at least 3' in trace(env, scan, 'ab')
    assert 'inconclusive' in trace(env, scan, 'not present')
    env[0].user_id = 'visitor'
    out = trace(env, scan)
    assert 'LC-' not in out and 'pick_listed' not in out
    env[0].user_id = '42'; env[0].chat_type = 'group'
    assert 'LC-' not in trace(env, scan)


@pytest.mark.parametrize('cleanup', ['discard', 'expire'])
def test_stage_data_vanishes_with_existing_retention(env, cleanup):
    scan = seed(env, strong(1)); assess_scan(env, scan, FakeLlm())
    if cleanup == 'discard':
        plugin.handle_command(env[1], f'lookback discard {scan}', source=env[0])
    else:
        conn = lb.connect(); conn.execute('UPDATE scans SET expires=?', ((lb.now()-timedelta(seconds=1)).isoformat(),))
        conn.commit(); lb.expire(conn); conn.close()
    assert not rows(scan)
    assert 'LC-' not in trace(env, scan)


def test_no_match_does_not_reduce_causes_to_two(env):
    scan = seed(env, strong(1))
    out = trace(env, scan, 'nonmatching')
    assert 'inconclusive' in out and 'words differ' in out and 'never have reached staging' in out
    assert 'already been handled' in out and 'Nothing else was checked' in out


def test_second_run_replaces_prior_codes_not_stale_success(env):
    scan = seed(env, strong(1)); assess_scan(env, scan, FakeLlm())
    assert codes(scan) == [lb.STAGE_PICK_LISTED]
    assess_scan(env, scan, FailingOn({1}))
    assert codes(scan) == [lb.STAGE_REQUEST_FAILED]


def test_schema_upgrade_preserves_old_candidates_without_inventing_stage(env):
    scan, candidate = run(env)
    conn = lb.connect()
    conn.execute('ALTER TABLE candidates DROP COLUMN stage')
    conn.commit(); conn.close()
    conn = lb.connect()
    row = conn.execute('SELECT * FROM candidates WHERE id=?', (candidate['id'],)).fetchone()
    assert row['stage'] == '' and row['payload'] == candidate['payload']
    conn.close()
    assert 'not recorded' in trace(env, scan['id'], 'Review request')


def test_cancel_leaves_stage_unrecorded_and_no_trace_body_search(env):
    scan = seed(env, strong(1))
    assess_scan(env, scan, FakeLlm(), answer='cancel')
    assert codes(scan) == ['']
    assert 'inconclusive' in trace(env, scan, 'revised contract')


def test_skip_clears_searchable_content_without_retaining_new_index(env):
    scan = seed(env, strong(1)); assess_scan(env, scan, FakeLlm())
    plugin.handle_command(env[1], 'lookback skip LC-00000000', source=env[0])
    assert rows(scan)[0]['payload'] == '{}'
    assert 'inconclusive' in trace(env, scan)
