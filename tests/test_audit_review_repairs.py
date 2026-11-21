"""Synthetic private-review authorization, grouping, rejection and retention checks."""
import json
from datetime import timedelta
import pytest
from test_lookback import env
from test_lookback_quality import seed,strong,assess_scan,FakeLlm,ASK,verdict
import hermes_tingle_lookback as lb
import hermes_tingle_store as store


@pytest.mark.parametrize('change',['consent','owner','ttl'])
def test_after_wait_never_displays_next_card_when_authority_changes(env,change):
    scan=seed(env,strong(2));prompts=[]
    def ask(text,choices):
        prompts.append(text)
        if change=='consent':
            store.disable_source('email')
        elif change=='owner':
            env[0].user_id='999'
        else:
            conn=lb.connect();conn.execute('UPDATE scans SET expires=? WHERE id=?',((lb.now()-timedelta(seconds=1)).isoformat(),scan));conn.commit();conn.close()
        return 'later'
    env[1].ask_choice=ask
    with pytest.raises(ValueError):lb.review(env[1],env[0],scan)
    assert len(prompts)==1


def test_default_review_keeps_model_shortlist_and_one_page_action_works(env):
    scan=seed(env,strong(2))
    out=assess_scan(env,scan,FakeLlm(lambda c:verdict(c['id'],'action_needed',ASK,1,'Synthetic chosen action') if c['id']=='C1' else verdict(c['id'])))
    assert f'lookback track-top {scan} AS-' in out
    prompts=[]
    env[1].ask_choice=lambda q,ch:prompts.append(q) or 'stop'
    lb.review(env[1],env[0],scan)
    assert len(prompts)==1
    conn=lb.connect();a=lb._latest_assessment(conn,scan);cid=conn.execute('SELECT candidate_id FROM assessment_results WHERE assessment_id=?',(a['id'],)).fetchone()[0];conn.close()
    # Only selected lead, not simply newest recency.
    assert 'Lead 1 of 1' in prompts[0]


def test_refused_auto_accept_is_explicit_not_a_routine_disposition(env,monkeypatch):
    scan=seed(env,strong(2));assess_scan(env,scan,FakeLlm())
    original=lb._commit_lead;calls=[]
    def collision(*a,**kw):
        calls.append(a[3])
        if len(calls)==1:raise ValueError('Synthetic collision')
        return original(*a,**kw)
    monkeypatch.setattr(lb,'_commit_lead',collision)
    stats={}
    added,considered,private,uncertain=lb.auto_accept_qualified(env[0],lb.binding(env[0]),scan,stats=stats)
    assert len(added)==1 and stats=={'qualified':2,'accepted':1,'rejected':1,'unassessed':0}
    assert considered==2 and private==0


def test_sibling_context_retention_matches_narrowed_claim(env):
    scan=seed(env,[dict(snippet=ASK,thread_id='same'),dict(snippet=ASK,thread_id='same')])
    assess_scan(env,scan,FakeLlm())
    lb.auto_accept_qualified(env[0],lb.binding(env[0]),scan)
    conn=lb.connect();rows=conn.execute('SELECT state,payload FROM candidates WHERE scan_id=?',(scan,)).fetchall();conn.close()
    assert any(r['state']=='accepted' and r['payload']=='{}' for r in rows)
    assert any(r['state']=='pending' and ASK in r['payload'] for r in rows)
    assert 'sibling leads may retain shared' in lb.handle(env[1],['help'],env[0])
