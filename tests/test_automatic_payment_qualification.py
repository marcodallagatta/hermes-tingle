"""Synthetic contracts/plumbing only, never a real-model payment judgment benchmark."""
import json
import pytest
import hermes_tingle_assess as assess
from test_lookback import env
from test_lookback_quality import seed, FakeLlm, verdict, assess_scan, asid
import hermes_tingle_lookback as lb


@pytest.mark.parametrize('text,disposition,action', [
    ('We will collect the invoice amount automatically on the collection date. No action is needed.', 'not_actionable', ''),
    ('Der Rechnungsbetrag wird automatisch per Lastschrift eingezogen. Sie brauchen nichts zu tun.', 'not_actionable', ''),
    ('Il pagamento avverra tramite addebito automatico. Non occorre effettuare un bonifico.', 'not_actionable', ''),
    ('Automatic collection failed. Please update your payment details to settle the balance.', 'action_needed', 'Update payment details after failed collection'),
    ('Your mandate was cancelled. Please choose a payment method for the outstanding invoice.', 'action_needed', 'Choose payment method for outstanding invoice'),
    ('Insufficient funds prevented collection. Please fund your account before our next attempt.', 'action_needed', 'Fund account before the next collection attempt'),
    ('Please pay this outstanding invoice by the due date. No automatic collection is arranged.', 'action_needed', 'Pay outstanding invoice'),
    ('Payment methods include direct debit. See the attached invoice for details.', 'possible', ''),
    ('Collection initially failed. Later update: the full payment was collected successfully; nothing remains due.', 'not_actionable', ''),
    ('Payment is collected automatically. Please still send the missing signed service agreement.', 'action_needed', 'Send missing signed service agreement'),
])
def test_scripted_payment_dispositions_follow_existing_pipeline(env, text, disposition, action):
    """The fake supplies the judgment: this checks transport and qualification, not correctness of AI."""
    sid = seed(env, [dict(snippet=text, subject='Synthetic utility statement', date=lb.now().isoformat())])
    llm = FakeLlm(lambda c: verdict(c['id'], disposition, text if action else '', action=action))
    assess_scan(env, sid, llm)
    assert len(llm.calls) == 1
    assert text in json.loads(llm.calls[0]['input'][0]['text'])['conversations'][0]['messages'][0]['text']
    assert llm.calls[0]['json_schema'] == assess.SCHEMA
    assert llm.calls[0]['max_tokens'] == assess.MAX_TOKENS
    instructions = llm.calls[0]['instructions']
    for contract in ('A due date or amount alone does not establish a manual payment task',
                     'confirms this charge will be collected automatically',
                     'failed or declined collection', 'cancelled mandate', 'insufficient funds',
                     'explicitly requested owner intervention', 'generic payment-method footer',
                     'use possible rather than inventing one', 'across the supplied messages and languages'):
        assert contract in instructions
    conn = lb.connect()
    aid = asid(sid)
    rows = conn.execute('SELECT action FROM assessment_results WHERE assessment_id=?', (aid,)).fetchall()
    qualified = conn.execute('SELECT qualified FROM assessments WHERE id=?', (aid,)).fetchone()[0]
    conn.close()
    assert qualified == int(bool(action))
    assert len(rows) == int(bool(action))
    if action:
        assert rows[0]['action'] == action


def test_instruction_does_not_add_a_payment_keyword_filter():
    assert 'not payment keywords' in assess.INSTRUCTIONS
    assert 'Only direction=received' in assess.INSTRUCTIONS
    assert 'Do not infer facts, deadlines or dates' in assess.INSTRUCTIONS
