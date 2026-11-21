"""Task names from existing qualified actions; synthetic mail, no live calls."""
import json
import pytest
import hermes_tingle as plugin
import hermes_tingle_lookback as lb
import hermes_tingle_store as store
from test_lookback import env
from test_lookback_quality import seed, strong, FakeLlm, assess_scan, verdict, ASK


def qualified(env):
    sid = seed(env, [dict(strong(1)[0], subject='Synthetic attachment')])
    llm = FakeLlm(lambda c: verdict(c['id'], 'action_needed', ASK, 1,
                                  'Review the revised contract for the renewal'))
    assess_scan(env, sid, llm)
    conn = lb.connect()
    candidate = conn.execute('SELECT * FROM candidates WHERE scan_id=?', (sid,)).fetchone()
    conn.close()
    return sid, candidate['id'], llm


def test_onboarding_keeps_existing_qualified_action_and_source_subject(env):
    sid, lead, llm = qualified(env)
    added, _, _, _ = lb.auto_accept_qualified(env[0], lb.binding(env[0]), sid)
    item = store.get_item(added[0])
    assert item['title'] == 'Review the revised contract for the renewal'
    assert 'Subject: Synthetic attachment.' in item['summary'] and 'not a verified commitment' in item['summary']
    assert len(llm.calls) == 1


@pytest.mark.parametrize('bulk', [False, True])
def test_review_preview_and_accepted_title_match(env, bulk):
    sid, lead, llm = qualified(env)
    previews = []
    env[1].ask_choice = lambda q, choices: previews.append(q) or 'accept'
    if bulk:
        lb._bulk_accept(env[1], [lead], env[0], lb.binding(env[0]))
    else:
        plugin.handle_command(env[1], f'lookback accept {lead}', source=env[0])
    item = store.list_items()[0]
    assert 'Title: ' + item['title'] in previews[0]
    assert item['title'] == 'Review the revised contract for the renewal'
    assert len(llm.calls) == 1


def test_owner_draft_wins_over_qualified_name(env):
    sid, lead, llm = qualified(env)
    conn = lb.connect()
    conn.execute('UPDATE candidates SET draft=?,draft_rev=1 WHERE id=?',
                 (json.dumps(dict(title='Owner chosen title', summary='Owner detail', due='-')), lead))
    conn.commit(); conn.close()
    env[1].ask_choice = lambda q, choices: 'accept'
    plugin.handle_command(env[1], f'lookback accept {lead}', source=env[0])
    assert store.list_items()[0]['title'] == 'Owner chosen title'
    assert not lb.auto_accept_qualified(env[0], lb.binding(env[0]), sid)[0]
    assert len(llm.calls) == 1


def test_no_assessment_never_guesses_action_from_subject(env):
    sid = seed(env, [dict(strong(1)[0], subject='Synthetic attachment')])
    conn = lb.connect(); candidate = conn.execute('SELECT * FROM candidates').fetchone()
    title, summary, due = lb._candidate_fields(conn, candidate); conn.close()
    assert title.startswith('Review email from ') and title != 'Synthetic attachment'
    assert 'Subject: Synthetic attachment.' in summary and due == '-'


def test_wrong_generation_cannot_supply_title(env):
    sid, lead, llm = qualified(env)
    conn = lb.connect()
    conn.execute('UPDATE assessments SET generation=generation+1 WHERE scan_id=?', (sid,))
    candidate = conn.execute('SELECT * FROM candidates WHERE id=?', (lead,)).fetchone()
    assert lb._candidate_fields(conn, candidate)[0].startswith('Review email from ')
    conn.close()


def test_preparation_does_not_rename_existing_items(env):
    old = store.add_item(title='Existing historical title')
    sid, lead, llm = qualified(env)
    lb.auto_accept_qualified(env[0], lb.binding(env[0]), sid)
    assert store.get_item(old['id']) == old
