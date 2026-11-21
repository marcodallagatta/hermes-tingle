"""Broader recognition (open processes, expiring benefits, adviser threads) and owner triage rules.

All mail is synthetic. The fake model proves what is sent and what is accepted; it does not measure real
model quality, which needs a live, separately authorized check."""
import json
from datetime import timedelta
from test_lookback import env
from test_lookback_quality import FakeLlm, seed, verdict, assess_scan
import hermes_tingle_assess as assess
import hermes_tingle_lookback as lb
import hermes_tingle as plugin
import hermes_tingle_store as store

PROCESS = 'Your claim is open and we still need the signed form to continue.'
EXPIRY = 'Your voucher of 10 credits is valid until 30 November 2026.'
ADVISER = 'Please send the updated figures so we can file the next step.'


def _sent(llm):
    return json.loads(llm.calls[0]['input'][0]['text'])


def test_instructions_cover_process_expiry_and_adviser_threads_without_loosening_evidence():
    text = assess.INSTRUCTIONS
    assert 'open process' in text and 'explicit expiry' in text and 'professional adviser' in text
    assert 'whole supplied thread' in text
    # Evidence rules are unchanged: received messages only, exact quotes, no invented dates.
    assert 'Only direction=received' in text and 'Do not infer facts, deadlines or dates' in text


def test_process_and_expiry_picks_qualify_only_with_an_exact_received_quote(env):
    scan = seed(env, [dict(snippet=PROCESS), dict(snippet=EXPIRY), dict(snippet='Weekly offers just for you')])
    quotes = {PROCESS: 'we still need the signed form', EXPIRY: 'valid until 30 November 2026'}
    def decide(conv):
        text = conv['messages'][0]['text']
        for full, quote in quotes.items():
            if full in text:
                return verdict(conv['id'], 'action_needed', quote, 1, 'Follow up')
        return verdict(conv['id'], 'action_needed', 'invented quote that is not present', 1, 'x')
    out = assess_scan(env, scan, FakeLlm(decide))
    conn = lb.connect(); a = lb._latest_assessment(conn, scan); conn.close()
    assert a['considered'] == 3 and a['qualified'] == 2
    assert 'we still need the signed form' in out or 'valid until 30 November 2026' in out
    assert not store.list_items()  # Assessment alone never adds.


def test_thread_with_owner_reply_last_is_assessed_when_a_received_message_is_staged(env):
    now = lb.now()
    scan = seed(env, [dict(snippet=ADVISER, thread_id='tx', date=(now - timedelta(days=2)).isoformat()),
                      dict(snippet='Thanks, I will look into it', outgoing=True, thread_id='tx',
                           date=now.isoformat()),
                      dict(snippet='Only the owner wrote here', outgoing=True, thread_id='ty', date=now.isoformat())])
    llm = FakeLlm(); assess_scan(env, scan, llm)
    convs = _sent(llm)['conversations']
    assert len(convs) == 1
    assert [m['direction'] for m in convs[0]['messages']] == ['sent_by_owner', 'received']


def test_rule_add_saves_an_approved_triage_rule_and_it_reaches_the_model(env):
    out = plugin.handle_command(env[1], 'rule add never track vouchers or coupons', source=env[0])
    assert 'Saved `FB-' in out and 'standing preference' in out and 'rare miss' in out
    rules = store.approved_feedback_rules()
    assert [(r['rule_text'], r['category']) for r in rules] == [('never track vouchers or coupons', 'triage')]
    assert 'never track vouchers or coupons' in plugin.handle_command(env[1], 'rules', source=env[0])
    scan = seed(env, [dict(snippet=EXPIRY)])
    llm = FakeLlm(); assess_scan(env, scan, llm)
    assert _sent(llm)['owner_rules'] == ['never track vouchers or coupons']
    # Rules persist for the next scan too.
    scan2 = seed(env, [dict(snippet=EXPIRY)])
    llm2 = FakeLlm(); assess_scan(env, scan2, llm2)
    assert _sent(llm2)['owner_rules'] == ['never track vouchers or coupons']


def test_dismissing_a_rule_stops_it_and_no_rules_sends_no_field(env):
    plugin.handle_command(env[1], 'rule add ignore newsletters', source=env[0])
    rule_id = store.approved_feedback_rules()[0]['id']
    assert 'Dismissed' in plugin.handle_command(env[1], f'rule dismiss {rule_id}', source=env[0])
    assert store.approved_feedback_rules() == []
    scan = seed(env, [dict(snippet=EXPIRY)])
    llm = FakeLlm(); assess_scan(env, scan, llm)
    assert 'owner_rules' not in _sent(llm)


def test_rule_add_needs_a_verified_owner_and_bounded_text(env):
    assert store.approved_feedback_rules() == []
    assert 'Usage' in plugin.handle_command(env[1], 'rule add', source=env[0])
    assert 'under 200' in plugin.handle_command(env[1], 'rule add ' + 'x' * 201, source=env[0])
    assert plugin.handle_command(env[1], 'rule add ignore everything', source=None) != ''
    assert store.approved_feedback_rules() == []


def test_only_steering_categories_reach_the_model(env):
    other = store.capture_feedback_candidate('Prefer concise reminders.', category='formatting')
    store.resolve_feedback_candidate(other['id'], 'approved', decided_by=('telegram', '42', '42'), evidence='test')
    plugin.handle_command(env[1], 'rule add skip promotions', source=env[0])
    scan = seed(env, [dict(snippet=EXPIRY)])
    llm = FakeLlm(); assess_scan(env, scan, llm)
    assert _sent(llm)['owner_rules'] == ['skip promotions']


def test_rule_text_is_bounded_before_it_is_sent():
    assert assess.clean_rules(['  a  rule ', '', 5, 'x' * 500])[0] == 'a rule'
    assert all(len(r) <= assess.RULE_CHARS for r in assess.clean_rules(['x' * 500]))
    assert len(assess.clean_rules([f'r{i}' for i in range(50)])) == assess.RULES_MAX


def test_limits_are_enforced_at_capture_and_approval_not_dropped_silently(env):
    import pytest
    with pytest.raises(ValueError):
        store.capture_feedback_candidate('x' * 201, category='triage')
    for i in range(store.TRIAGE_RULES_MAX):
        assert 'Saved' in plugin.handle_command(env[1], f'rule add synthetic preference {i}', source=env[0])
    out = plugin.handle_command(env[1], 'rule add one more preference', source=env[0])
    assert 'already active' in out and 'Nothing was saved' in out
    assert len(store.approved_feedback_rules(limit=100)) == store.TRIAGE_RULES_MAX
    # A chat-captured pending rule cannot be approved past the limit either.
    pending = store.capture_feedback_candidate('another preference', category='triage')
    reply = plugin.handle_command(env[1], f"rule approve {pending['id']}", source=env[0])
    assert 'dismiss one first' in reply
    assert len(store.approved_feedback_rules(limit=100)) == store.TRIAGE_RULES_MAX
    scan = seed(env, [dict(snippet=EXPIRY)])
    llm = FakeLlm(); assess_scan(env, scan, llm)
    assert len(_sent(llm)['owner_rules']) == store.TRIAGE_RULES_MAX


def test_chat_captured_triage_rule_is_pending_until_the_owner_approves(env):
    row = store.capture_feedback_candidate('never track vouchers', category='triage')
    scan = seed(env, [dict(snippet=EXPIRY)])
    llm = FakeLlm(); assess_scan(env, scan, llm)
    assert 'owner_rules' not in _sent(llm)
    assert 'Approved' in plugin.handle_command(env[1], f"rule approve {row['id']}", source=env[0])
    scan2 = seed(env, [dict(snippet=EXPIRY)])
    llm2 = FakeLlm(); assess_scan(env, scan2, llm2)
    assert _sent(llm2)['owner_rules'] == ['never track vouchers']


def test_usage_skill_tells_the_model_how_to_capture_preferences_honestly():
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent / 'skills' / 'usage' / 'SKILL.md').read_text()
    assert 'feedback_category="triage"' in text and '/tingle rule add' in text
    assert 'occasional miss' in text and 'At most 20' in text


def test_instructions_are_generous_but_keep_junk_and_evidence_rules():
    text = assess.INSTRUCTIONS
    assert 'Be generous' in text and 'obvious junk' in text
    assert 'Prefer possible over action_needed' not in text
    assert 'Do not infer facts, deadlines or dates' in text


def test_partial_context_pick_is_added_and_says_so(env):
    scan = seed(env, [dict(snippet=ASK_LONG)])
    assess_scan(env, scan, FakeLlm())
    conn = lb.connect(); a = lb._latest_assessment(conn, scan)
    partial = conn.execute('SELECT partial FROM assessment_results WHERE assessment_id=?', (a['id'],)).fetchone()['partial']
    conn.close()
    assert partial == 1
    added, considered, skipped, _uncertain = lb.auto_accept_qualified(env[0], lb.binding(env[0]), scan)
    assert len(added) == 1 and skipped == 0
    item = store.get_item(added[0])
    assert 'Only part of this thread was read' in json.dumps(item)


ASK_LONG = 'Could you review the revised contract? ' + 'x' * 5000


def test_skip_terms_are_enforced_deterministically_and_counted(env):
    out = plugin.handle_command(env[1], 'rule add never track vouchers [skip: voucher, coupon]', source=env[0])
    assert 'always left out' in out and '`voucher`' in out
    scan = seed(env, [dict(snippet=EXPIRY, subject='Your Voucher expires soon'), dict(snippet=PROCESS)])
    quotes = {'valid until 30 November 2026': EXPIRY, 'we still need the signed form': PROCESS}
    def decide(conv):
        text = conv['messages'][0]['text']
        for quote, full in quotes.items():
            if full in text:
                return verdict(conv['id'], 'action_needed', quote, 1, 'Use the benefit' if full == EXPIRY else 'Send the form')
        return verdict(conv['id'])
    reply = assess_scan(env, scan, FakeLlm(decide))
    conn = lb.connect(); a = lb._latest_assessment(conn, scan); conn.close()
    assert a['qualified'] == 1 and a['excluded'] == 1  # The model qualified both; the skip term removed one.
    assert '1 matched your skip rules and were left out' in reply
    assert 'send the form' in reply.lower()


def test_rule_without_skip_terms_is_labelled_as_model_judgment(env):
    out = plugin.handle_command(env[1], 'rule add ignore marketing', source=env[0])
    assert 'applies it by judgment' in out and '[skip:' in out
    assert assess.skip_terms(['ignore marketing', 'x [skip: ab]']) == []


def test_chat_captured_general_rule_also_steers_scans_and_counts_toward_the_limit(env):
    row = store.capture_feedback_candidate('ignore promotional mail', category='general')
    plugin.handle_command(env[1], f"rule approve {row['id']}", source=env[0])
    scan = seed(env, [dict(snippet=EXPIRY)])
    llm = FakeLlm(); assess_scan(env, scan, llm)
    assert _sent(llm)['owner_rules'] == ['ignore promotional mail']
    assert store.approved_triage_rules() == ['ignore promotional mail']


def test_skill_explains_exact_skip_words():
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent / 'skills' / 'usage' / 'SKILL.md').read_text()
    assert '[skip: voucher, coupon, gutschein]' in text and 'deterministically' in text


def test_prompt_separates_empty_confirmations_from_documents_with_an_open_step():
    text = assess.INSTRUCTIONS
    assert 'merely delivers a document' in text and 'unpaid amount' in text and 'too short' in text
    assert 'does not count just for its topic' in text
    assert assess.VERSION == 'assess-10'


def test_onboarding_titles_are_flat_and_bounded(env):
    import hermes_tingle_onboarding as ob
    ids = [store.add_item(title=f'Item `{i}`\nwith   markup ' + 'x' * 100, summary='s',
                          source_type='email', source_ref=f'ref-{i}')['id'] for i in range(10)]
    text = ob._titles(ids)
    assert '`' not in text and '\n' not in text and text.endswith('plus 2 more')
    assert max(len(part) for part in text.split(', ')) <= 70


# ---- explicit deadlines and plain-text titles ----
import pytest
import hermes_tingle_onboarding as _ob


def test_stated_expiry_is_not_turned_into_a_due_date_and_the_limit_is_explicit(env):
    # Known limit, kept honest: the quote is evidence only. No date is parsed or invented, so the item
    # keeps the standard default timing and the owner must read the quoted text for the expiry.
    quote = 'Your personal voucher expires on 30 November 2099.'
    scan = seed(env, [dict(snippet=quote, subject='Personal benefit')])
    assess_scan(env, scan, FakeLlm(lambda c: verdict(c['id'], 'action_needed', quote, 1, 'Use your voucher')))
    ids, _, _, _ = lb.auto_accept_qualified(env[0], lb.binding(env[0]), scan)
    item = store.get_item(ids[0])
    assert item['due_at'] is None and 'taken from' not in item['summary']


def test_paired_pipeline_cases_are_not_decided_by_length_or_category(env):
    # Scripted verdicts: the point is that nothing in the plugin drops a pick for being short or for its
    # category; deciding which messages deserve a pick stays with the model (prompt guidance only).
    cases = {
        'Pay 40 EUR by 2026-12-20.': 'action_needed',          # short and concrete
        'Here is the link.': 'possible',                       # short and vague
        'Invoice attached. Payment of 40 EUR is still open.': 'action_needed',   # document with an open step
        'Your test confirmation worked. Nothing else needed.': 'not_actionable',  # empty confirmation
    }
    scan = seed(env, [dict(snippet=t, subject=f'Case {i}') for i, t in enumerate(cases)])
    def decide(conv):
        full = conv['messages'][0]['text']
        text = next(t for t in cases if t in full)
        kind = cases[text]
        return verdict(conv['id'], kind, text if kind == 'action_needed' else '', 1, 'Follow up' if kind == 'action_needed' else '')
    assess_scan(env, scan, FakeLlm(decide))
    ids, considered, _, _ = lb.auto_accept_qualified(env[0], lb.binding(env[0]), scan)
    assert considered == 4 and len(ids) == 2
    titles = sorted(store.get_item(i)['title'] for i in ids)
    assert titles == ['Follow up', 'Follow up']
    assert sorted('Case 0' in store.get_item(i)['summary'] or 'Case 2' in store.get_item(i)['summary'] for i in ids) == [True, True]


@pytest.mark.parametrize('raw,check', [
    ('**Approved** [view](https://example.invalid/x?a=b)', lambda t: t == 'Approved view'),
    ('See https://example.invalid/path now', lambda t: 'example.invalid' not in t and 'link' in t),
    ('<b>Bold</b> &amp; <a href="https://example.invalid">go</a>', lambda t: '<' not in t and '&amp;' not in t),
    ('Line\none\x00two\u202e\u200b end', lambda t: '\n' not in t and '\x00' not in t and '\u202e' not in t),
    ('   ', lambda t: t == 'untitled item'),
    ('x' * 170, lambda t: len(t) == 60),
    ('# _under_ ~~strike~~ > quote | pipe', lambda t: not any(c in t for c in '#_~>|')),
])
def test_titles_are_plain_text(env, raw, check):
    item = store.add_item(title=raw if raw.strip() else 'placeholder', summary='s', source_type='email', source_ref='t-' + str(abs(hash(raw))))
    if not raw.strip():
        text = _ob._titles(['missing-id'])
    else:
        text = _ob._titles([item['id']])
    assert check(text), text


def test_many_titles_are_capped_and_counted(env):
    ids = [store.add_item(title=f'T{i}', summary='s', source_type='email', source_ref=f'm-{i}')['id'] for i in range(12)]
    text = _ob._titles(ids)
    assert text.count(',') == 8 and text.endswith('plus 4 more')
