"""Actual onboarding command reply, synthetic scan/model and local items only."""
import json
import pytest
import hermes_tingle as plugin
import hermes_tingle_lookback as lb
import hermes_tingle_onboarding as onboarding
import hermes_tingle_store as store
from test_lookback import env
from test_onboarding_flow import _yes
from test_lookback_quality import FakeLlm,default_decide


def completion(env,monkeypatch,*,partial=False,uncertain=False,count=6):
    def decide(c):
        result=default_decide(c);result['topic_emoji']='📄';return result
    _yes(env[1],FakeLlm(decide))
    original=lb.auto_accept_qualified
    def accept(*args,**kwargs):
        added,considered,skipped,unknown=original(*args,**kwargs)
        titles=['Review contract','Pay invoice','Book flight','Schedule appointment','Renew pass','Send document']
        for title in titles[len(added):count]:
            icons=['📄','💶','🧳','🩺','🎟️','🪪']
            added.append(store.add_item(title=title,topic_emoji=icons[len(added)])['id'])
        if uncertain:unknown=added[:2]
        return added,considered,skipped,unknown
    monkeypatch.setattr(lb,'auto_accept_qualified',accept)
    if partial:
        scan=lb._run_scan
        def run(*args,**kwargs):
            sid=scan(*args,**kwargs)
            conn=lb.connect();conn.execute("UPDATE scans SET coverage='Partial coverage: cap' WHERE id=?",(sid,));conn.commit();conn.close()
            return sid
        monkeypatch.setattr(lb,'_run_scan',run)
    return plugin.handle_command(env[1],'',source=env[0])


@pytest.mark.parametrize('partial',[False,True])
def test_actual_completion_is_line_based_and_bounded(env,monkeypatch,partial):
    out=completion(env,monkeypatch,partial=partial,uncertain=True)
    lines=out.splitlines()
    assert out.count('`/tingle list`')==1
    assert 'Review: `/tingle list`' in lines
    ids=store.onboarding_state()['items']
    expected=onboarding._titles(ids,limit=5,decorated=True).splitlines()
    assert len(expected)==6 and all(line in lines for line in expected)
    assert 'Plus 1 more.' in lines
    assert len([line for line in lines if 'Possible time limit' in line])==1
    assert 'No extra reminder set' in out and 'unverified' in out
    assert '<ISO' not in out and 'expiry at' not in out
    assert 'LC-' not in out and 'Ask:' not in out and 'Evidence:' not in out
    assert len(out)<1200
    assert 'Coverage is incomplete' not in out
    assert 'Partial import' not in out
    assert 'Onboarding import done' in out


def test_title_line_cannot_break_into_extra_items(env,monkeypatch):
    item=store.add_item(title='<b>Review</b>\n[contract](https://synthetic.invalid/x)\u202e test')['id']
    out=onboarding._titles([item],decorated=True)
    assert out=='📌 Review contract test'


def test_prompt_has_no_host_question_prefix_and_keeps_consent(env):
    captured=[]
    env[1].ask_choice=lambda question,choices:captured.append((question,choices)) or 'no'
    plugin.handle_command(env[1],'',source=env[0])
    question,choices=captured[0]
    assert question.startswith(f'👋🧝 Welcome to Tingle v{plugin.VERSION}.') and '❓' not in question
    for fact in ('headers, snippets and selected bodies','possibly cloud and billed',
                 'cannot cap or see the cost','host retries','shared profile',
                 '7 days','history and backups','unverified one-off the day before a claimed expiry','45 days',
                 'No standing source settings change','A lookback scan'):
        assert fact in question
    assert [c['value'] for c in choices]==['yes','no']
    assert 'lookback help' not in question and 'Exact limits:' not in question
    assert 'Before you choose:' in question


@pytest.mark.parametrize('icon',['🩺','💶','📄','🧳','🪪','📦','🏠','📅','invalid','📌'])
def test_retained_model_icon_is_display_only_and_safely_bounded(env,icon):
    item=store.add_item(title='Synthetic item',summary='Private retained summary',topic_emoji=icon)
    before=store.get_item(item['id'])
    out=onboarding._titles([item['id']],decorated=True)
    assert out==('📌' if icon=='invalid' else icon)+' Synthetic item'
    assert store.get_item(item['id'])==before


@pytest.mark.parametrize('planned', [False, True])
def test_import_preserves_timing_without_default_diagnostic_tail(env, monkeypatch, planned):
    from test_lookback_quality import ASK
    def decide(conv):
        value = default_decide(conv)
        if planned:
            value['review_plan'] = dict(actor='me', outcome='Review the synthetic form',
                basis='inferred', anchor_message=1, after_days=5, timing_quote='')
        return value
    llm = FakeLlm(decide)
    _yes(env[1], llm)
    out = plugin.handle_command(env[1], '', source=env[0])
    ids = store.onboarding_state()['items']
    assert ids and len(llm.calls) == 1
    assert 'need context/timing review' not in out
    assert 'no reminder scheduled for those yet' not in out
    for item_id in ids:
        item = store.get_item(item_id)
        assert bool(item['next_check_at']) == planned
        before = store.get_item(item_id)
        listing = plugin.handle_command(env[1], 'list', source=env[0])
        assert 'Review timing needed' not in listing
        detail = plugin.handle_command(env[1], 'timing ' + item_id, source=env[0])
        assert ('Thread check:' in detail) == planned
        if not planned:
            assert 'No reminder scheduled' in detail and 'replan ' + item_id in detail
        assert store.get_item(item_id) == before


def test_timing_details_are_owner_dm_only(env):
    from types import SimpleNamespace
    item = store.add_item(title='Synthetic private item')
    for sender in [None, SimpleNamespace(platform='telegram', user_id='999', chat_id='999', chat_type='dm', is_bot=False),
                   SimpleNamespace(platform='telegram', user_id='42', chat_id='42', chat_type='group', is_bot=False)]:
        out = plugin.handle_command(env[1], 'timing ' + item['id'], source=sender)
        assert 'Synthetic private item' not in out
        assert 'This item does not use' not in out


def test_network_refresh_timeout_keeps_redacted_accurate_reason():
    text = lb.failure_reason('token refresh timed out SECRET-provider-diagnostic')
    assert 'network' in text and 'authorization' not in text and 'SECRET' not in text
