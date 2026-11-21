"""Owner stop control for a running model assessment. All mail and the model are synthetic.

A scripted fake model stands in for the host facade: these tests prove the stop
command's authority gates, the atomic stop/finish race, prompt return while a
request is in flight, the no-writes-after-cancel rule and the truthful exit
summary. They do NOT measure how well a real model judges mail."""
import json
import threading
from types import SimpleNamespace

import pytest
from test_lookback import env, run, snapshot
from test_lookback_quality import FakeLlm, seed, strong, assess_scan
import hermes_tingle_lookback as lb
import hermes_tingle as plugin
import hermes_tingle_store as store


def stop(env, sender=None):
    return plugin.handle_command(env[1], 'lookback stop', source=sender or env[0])


def db_state(scan):
    conn = lb.connect()
    row = conn.execute('SELECT assess_state, assess_runs FROM scans WHERE id=?', (scan,)).fetchone()
    assessments = conn.execute('SELECT COUNT(*) c FROM assessments').fetchone()['c']
    results = conn.execute('SELECT COUNT(*) c FROM assessment_results').fetchone()['c']
    pending = conn.execute("SELECT COUNT(*) c FROM candidates WHERE state='pending'").fetchone()['c']
    conn.close()
    return dict(row, assessments=assessments, results=results, pending=pending)


class CancellingLlm(FakeLlm):
    """Fake model that runs the real stop command from inside request `on_call`."""

    def __init__(self, env, on_call, **kw):
        super().__init__(**kw)
        self.env, self.on_call, self.stop_reply = env, on_call, None

    def complete_structured(self, **kw):
        if len(self.calls) + 1 == self.on_call and self.stop_reply is None:
            self.stop_reply = plugin.handle_command(self.env[1], 'lookback stop', source=self.env[0])
        return super().complete_structured(**kw)


@pytest.mark.parametrize('change', [{'user_id': 'other'}, {'chat_type': 'group'}, {'is_bot': True}])
def test_stop_requires_a_verified_owner_dm_and_changes_nothing(env, change):
    scan = seed(env, strong(3))
    before = snapshot()
    for k, v in change.items():
        setattr(env[0], k, v)
    out = stop(env)
    assert 'stop' not in out.casefold() or 'direct message' in out or 'person' in out or 'owner' in out
    assert 'Stopping' not in out
    assert snapshot() == before


def test_stop_without_a_running_review_is_truthful(env):
    seed(env, strong(2))
    assert stop(env) == 'No model review is running right now; nothing was stopped.'


def test_stop_between_batches_sends_nothing_more_and_stores_nothing(env):
    scan = seed(env, strong(13))  # Two batches: 12 conversations, then 1.
    llm = CancellingLlm(env, on_call=1)
    out = assess_scan(env, scan, llm)
    assert llm.stop_reply is not None and 'Stopping the current model review' in llm.stop_reply
    assert len(llm.calls) == 1  # The second batch was never sent.
    assert 'Stopped. 12 assessed, 1 left unassessed; no further calls will start.' in out
    state = db_state(scan)
    assert state['assess_state'].startswith('cancelled') and state['assess_runs'] == 1
    assert state['assessments'] == 0 and state['results'] == 0 and state['pending'] == 13
    assert not store.list_items()


def test_one_already_gated_request_is_the_bound_and_nothing_after_it_is_sent(env):
    # Stop lands as batch 2 of 3 starts, inside its pre-send gate window: that one
    # already-approved request proceeds, its result is discarded, and batch 3 never goes.
    scan = seed(env, strong(25))  # Three batches: 12, 12, then 1.
    llm = CancellingLlm(env, on_call=2)
    out = assess_scan(env, scan, llm)
    assert len(llm.calls) == 2  # The gated request ran; the batch gated after the stop did not.
    assert 'Stopped. 24 assessed, 1 left unassessed; no further calls will start.' in out
    state = db_state(scan)
    assert state['assess_state'].startswith('cancelled')
    assert state['assessments'] == 0 and state['results'] == 0 and state['pending'] == 25
    assert not store.list_items()


def test_in_flight_result_is_discarded_and_the_store_transaction_rolls_back(env):
    scan = seed(env, strong(3))  # One batch: stop lands while its request is in flight.
    llm = CancellingLlm(env, on_call=1)
    out = assess_scan(env, scan, llm)
    assert 'Stopped. 3 assessed, 0 left unassessed; no further calls will start.' in out
    assert 'nothing was added' in out
    state = db_state(scan)
    assert state['assess_state'].startswith('cancelled')
    assert state['assessments'] == 0 and state['results'] == 0 and state['pending'] == 3
    assert not store.list_items()


def test_stop_during_a_blocked_request_returns_promptly_and_the_worker_exits_cleanly(env):
    scan = seed(env, strong(3))
    entered, release = threading.Event(), threading.Event()
    outcome = {}

    class BlockedLlm(FakeLlm):
        def complete_structured(self, **kw):
            entered.set()
            assert release.wait(timeout=30), 'test harness never released the blocked request'
            return super().complete_structured(**kw)

    def work():
        outcome['reply'] = assess_scan(env, scan, BlockedLlm())

    worker = threading.Thread(target=work)
    worker.start()
    assert entered.wait(timeout=30), 'worker never reached the model request'
    ack = stop(env)  # Must return while the request is still blocked.
    assert 'Stopping the current model review' in ack
    release.set()
    worker.join(timeout=30)
    assert not worker.is_alive()
    # The blocked request completed after the stop landed, so its three conversations were
    # assessed but their results are discarded at the final gate: nothing is stored.
    assert 'Stopped. 3 assessed, 0 left unassessed; no further calls will start.' in outcome['reply']
    state = db_state(scan)
    assert state['assess_state'].startswith('cancelled')
    assert state['assessments'] == 0 and state['results'] == 0 and state['pending'] == 3


def test_a_completed_run_reports_finished_to_a_later_stop_and_keeps_results(env):
    scan = seed(env, strong(3))
    out = assess_scan(env, scan, FakeLlm())
    assert 'Stopped.' not in out
    state = db_state(scan)
    assert state['assess_state'] == 'done' and state['assessments'] == 1 and state['results'] == 3
    assert stop(env) == 'No model review is running right now; nothing was stopped.'
    assert db_state(scan) == state  # Results stay intact.


def test_repeated_stop_reports_the_recorded_stop(env):
    scan = seed(env, strong(3))
    llm = CancellingLlm(env, on_call=1)
    assess_scan(env, scan, llm)
    assert db_state(scan)['assess_state'].startswith('cancelled')
    again = stop(env)
    assert 'already recorded' in again and 'no further calls will start' in again


def test_a_fresh_scan_starts_after_the_stopped_worker_exits(env):
    scan = seed(env, strong(3))
    llm = CancellingLlm(env, on_call=1)
    assess_scan(env, scan, llm)
    assert db_state(scan)['assess_state'].startswith('cancelled')
    text = plugin.handle_command(env[1], 'lookback start 7', source=env[0])
    assert 'scan failed or revoked' not in text
    conn = lb.connect()
    scans = conn.execute('SELECT id, assess_state FROM scans ORDER BY created').fetchall()
    conn.close()
    assert len(scans) == 2 and scans[1]['id'] != scan
    assert not str(scans[1]['assess_state']).startswith('cancelled')
