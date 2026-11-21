"""Synthetic source-relative contextual timing; no real mail or model."""
from datetime import datetime, timezone
import copy
import pytest
import hermes_tingle_checkpoint as cp
from test_lookback import env

NOW = datetime(2026, 11, 18, 12, tzinfo=timezone.utc)
TEXT = 'Processing takes three weeks. Please wait for the decision.'
RECORD = {'context_complete': True, 'messages': [{'n': 1, 'date': '2026-11-10T12:00:00+00:00'}]}
EVIDENCE = {1: (TEXT, 'received')}
PLAN = dict(actor='other', outcome='Wait for the application decision', basis='stated',
            anchor_message=1, after_days=21, timing_quote='Processing takes three weeks.')


def test_processing_estimate_is_source_relative_not_import_relative():
    out = cp.validate(PLAN, RECORD, EVIDENCE, now=NOW)
    assert out['review_at'] == '2026-12-01T12:00:00+00:00'
    assert out['actor'] == 'other' and not out['review_needed']
    assert TEXT not in str(out) and 'timing_quote' not in out
    assert 'due_at' not in out


def test_overdue_processing_window_is_check_now_not_restart_wait():
    record = copy.deepcopy(RECORD); record['messages'][0]['date'] = '2026-10-01T12:00:00+00:00'
    assert cp.validate(PLAN, record, EVIDENCE, now=NOW)['review_at'] == NOW.isoformat(timespec='seconds')


@pytest.mark.parametrize('change', [
    {'actor': 'unknown'}, {'basis': 'unknown'}, {'timing_quote': 'a fabricated wait'},
    {'anchor_message': 7}, {'after_days': -1}, {'after_days': True}, {'outcome': ''},
])
def test_unsupported_plans_remain_unknown_without_fixed_default(change):
    assert cp.validate(dict(PLAN, **change), RECORD, EVIDENCE, now=NOW) == cp.unknown()


def test_partial_context_never_auto_schedules():
    assert cp.validate(PLAN, dict(RECORD, context_complete=False), EVIDENCE, now=NOW) == cp.unknown()


def test_unknown_authorship_or_missing_timezone_cannot_anchor():
    assert cp.validate(PLAN, RECORD, {1: (TEXT, 'unknown')}, now=NOW) == cp.unknown()
    record = copy.deepcopy(RECORD); record['messages'][0]['date'] = '2026-11-10'
    assert cp.validate(PLAN, record, EVIDENCE, now=NOW) == cp.unknown()


def test_contextual_inference_is_labeled_not_quote_verified():
    out = cp.validate(dict(PLAN, basis='inferred', timing_quote=''), RECORD, EVIDENCE, now=NOW)
    assert out['basis'] == 'inferred' and out['review_at']


def test_actual_assessment_dates_keep_offset_with_millisecond_sources():
    import hermes_tingle_assess as assess
    record, evidence = assess.conversation_record('C1', [dict(date='2026-11-10T12:00:00.123000+00:00',
        text=TEXT, direction='received')],automated=False,acquisition_complete=True)
    assert record['messages'][0]['date']=='2026-11-10T12:00:00+00:00'
    assert cp.validate(PLAN, record, evidence, now=NOW)['review_at']=='2026-12-01T12:00:00+00:00'


@pytest.mark.parametrize('proposal,record,expected', [
    (None, RECORD, 'missing_plan'),
    ({}, RECORD, 'malformed_plan'),
    (PLAN, dict(RECORD, context_complete=False, omitted_older_messages=2), 'omitted_messages'),
    (PLAN, dict(RECORD, context_complete=False, messages=[dict(RECORD['messages'][0],shortened=True)]), 'clipped_context'),
    (PLAN, dict(RECORD, context_complete=False), 'incomplete_acquisition'),
    (dict(PLAN,after_days=True),RECORD,'invalid_fields'),
    (dict(PLAN,actor='unknown',basis='unknown'),RECORD,'explicit_unknown'),
    (dict(PLAN,anchor_message=7),RECORD,'invalid_anchor'),
    (dict(PLAN,timing_quote='fabricated'),RECORD,'unsupported_timing_quote'),
    (PLAN,dict(RECORD,messages=[{'n':1,'date':'undated'}]),'invalid_source_date'),
    (PLAN,RECORD,'scheduled'),
])
def test_diagnostic_codes_are_content_free(proposal,record,expected):
    plan,reason=cp.diagnose(proposal,record,EVIDENCE,now=NOW)
    assert reason==expected
    assert TEXT not in str(plan) and 'timing_quote' not in plan
    assert (not plan['review_needed']) == (reason=='scheduled')


def test_missing_plan_is_not_a_valid_actionable_qualification():
    import hermes_tingle_assess as assess
    from test_lookback_quality import verdict, ASK
    result=verdict('C1','action_needed',ASK,1,'Review the synthetic form')
    evidence={'C1':{1:(ASK,'received')}}
    for bad,reason in [(None,'missing_plan'),({},'malformed_plan')]:
        changed=dict(result)
        if bad is None: changed.pop('review_plan')
        else: changed['review_plan']=bad
        out=assess.validate({'results':[changed]},evidence)['C1']
        assert not out['supported'] and out['plan_protocol_fault']==reason
    assert assess.validate({'results':[result]},evidence)['C1']['supported']


def test_missing_plan_import_is_not_added_or_reported_as_full_success(env):
    import hermes_tingle as plugin
    import hermes_tingle_store as store
    import hermes_tingle_lookback as lb
    from test_lookback_quality import FakeLlm,default_decide
    def missing(conv):
        value=default_decide(conv);value.pop('review_plan');return value
    env[1].llm=FakeLlm(decide=missing)
    env[1].ask_choice=lambda *a:'yes'
    out=plugin.handle_command(env[1],'',source=env[0])
    assert 'timing plans were missing or malformed' in out
    assert not store.list_items()
    conn=lb.connect();a=conn.execute('SELECT * FROM assessments').fetchone();conn.close()
    assert a['plan_faults']>0 and a['assessed']<a['considered'] and a['qualified']==0
