"""Synthetic noise filtering and scan-sized acceptance, no provider/live account access."""
import json
import pytest
from test_lookback import env, run
from test_review_journeys import three
from test_lookback_edit_draft import first_lead
import hermes_tingle as plugin
import hermes_tingle_lookback as lb
import hermes_tingle_store as store


def noise_payload(**changes):
    return dict(subject='Payment receipt', snippet='For your information only. No action is required.',
                automated=True, context_status='List-mail noise flag: body not read', thread_context=[], **changes)


def replace_payload(lead, **changes):
    conn=lb.connect()
    data=json.loads(conn.execute('SELECT payload FROM candidates WHERE id=?',(lead,)).fetchone()[0])
    data.update(changes)
    conn.execute('UPDATE candidates SET payload=? WHERE id=?',(json.dumps(data),lead))
    conn.commit(); conn.close()


def test_noise_hidden_before_pagination_but_recoverable_by_id(three):
    sender,ctx,scan,ids=first_lead(three)
    replace_payload(ids[0], **noise_payload())
    listing=plugin.handle_command(ctx,f'lookback list {scan}',source=sender)
    assert ids[0] not in listing and '1 noisy match kept out' in listing
    assert f'lookback suppressed {scan}' in listing
    hidden=plugin.handle_command(ctx,f'lookback suppressed {scan}',source=sender)
    assert ids[0] in hidden and 'Payment receipt' in hidden
    assert 'Payment receipt' in plugin.handle_command(ctx,f'lookback view {ids[0]}',source=sender)
    seen=[]
    ctx.ask_choice=lambda q,ch: seen.append(q) or 'stop'
    plugin.handle_command(ctx,f'lookback review {scan}',source=sender)
    assert all('Payment receipt' not in q for q in seen)
    assert not store.list_items()
    conn=lb.connect(); assert conn.execute('SELECT state FROM candidates WHERE id=?',(ids[0],)).fetchone()[0]=='pending';conn.close()
    print('\n--- filtered list\n'+listing+'\n--- private suppressed list\n'+hidden)


@pytest.mark.parametrize('changes',[
    {'snippet':'No action is required for this receipt. Please pay the outstanding fee.'},
    {'subject':'Re: Payment receipt'},
    {'automated':False},
    {'context_status':'Bounded context. Incomplete: body previously read'},
    {'context_status':'Body/thread cap reached; context incomplete'},
    {'subject':'Please review payment receipt'},
    {'snippet':'Please approve the payment.'},
    {'snippet':'Informazione sulla ricevuta, per favore rispondere.'},
    {'thread_context':[{'body':"I will send the form by Friday."}]},
])
def test_ambiguous_or_action_bearing_candidates_not_suppressed(changes):
    data=noise_payload();data.update(changes)
    assert not lb._suppression_reason(data)


def test_legacy_no_automation_and_missing_context_never_hidden():
    assert not lb._suppression_reason({'subject':'Newsletter','context_status':'Body/thread cap reached'})


def test_draft_restores_suppressed_lead_to_normal_review(three):
    sender,ctx,scan,ids=first_lead(three)
    replace_payload(ids[0],**noise_payload())
    lb.propose_edit('telegram:42',ids[0],'Review my receipt','Owner choice','-')
    assert ids[0] in plugin.handle_command(ctx,f'lookback list {scan}',source=sender)


def test_track_all_excludes_noise_skipped_other_scan_and_cancel_is_noop(three):
    sender,ctx,scan,ids=first_lead(three)
    replace_payload(ids[0],**noise_payload())
    plugin.handle_command(ctx,f'lookback skip {ids[1]}',source=sender)
    conn=lb.connect(); c=conn.execute('SELECT * FROM candidates WHERE id=?',(ids[2],)).fetchone()
    conn.execute('INSERT INTO candidates(id,scan_id,generation,payload) VALUES(?,?,?,?)',('LC-FFFF','LB-OTHER',c['generation'],c['payload']));conn.commit();conn.close()
    prompts=[]
    ctx.ask_choice=lambda q,ch: prompts.append(q) or 'cancel'
    assert 'nothing added' in plugin.handle_command(ctx,f'lookback track-all {scan}',source=sender)
    assert ids[2] in prompts[0] and ids[0] not in prompts[0] and ids[1] not in prompts[0] and 'LC-FFFF' not in prompts[0]
    assert not store.list_items()
    ctx.ask_choice=lambda q,ch:'next' if any(c['value']=='next' for c in ch) else 'accept'
    out=plugin.handle_command(ctx,f'lookback track-all {scan}',source=sender)
    assert '1 of 1 added' in out and len(store.list_items())==1


def test_track_all_six_leads_full_pages_one_final_confirmation(three):
    sender,ctx,scan,ids=first_lead(three)
    conn=lb.connect(); original=conn.execute('SELECT * FROM candidates LIMIT 1').fetchone()
    for i in range(3):
        data=json.loads(original['payload']);data['source_ref']=data['source_ref'].replace('#m1',f'#x{i}').replace('#m2',f'#x{i}').replace('#m3',f'#x{i}')
        conn.execute('INSERT INTO candidates(id,scan_id,generation,payload) VALUES(?,?,?,?)',(f'LC-FFF{i}',scan,1,json.dumps(data)))
    conn.commit();conn.close()
    prompts=[]; confirmations=[]
    def ask(q,ch):
        prompts.append(q)
        if any(c['value']=='next' for c in ch):return 'next'
        confirmations.append(q);return 'accept'
    ctx.ask_choice=ask
    out=plugin.handle_command(ctx,f'lookback track-all {scan}',source=sender)
    assert '6 of 6 added' in out and len(store.list_items())==6
    assert len(prompts)>1 and len(confirmations)==1
    assert sum(q.count('Title:') for q in prompts)==6
    assert max(lb._units(q) for q in prompts)<1800
    print('\n--- full six-lead preview pages\n'+'\n\n'.join(prompts))
    print('\n--- final bulk result\n'+out)


def test_pages_are_lossless_unicode_bounded_and_cancel_prevents_add(three):
    text=('🦋<&Title: summary' * 500)
    pages=lb._preview_pages(text)
    assert ''.join(pages)==text and all(lb._units(p)<=lb.PREVIEW_UNITS for p in pages)
    sender,ctx,scan,ids=first_lead(three)
    picks=iter(['next','cancel'])
    for lead in ids:lb.propose_edit('telegram:42',lead,'x'*180,'y'*900,'-')
    seen=[]
    def ask(q,ch):
        pick=next(picks);seen.append(q+'\nOWNER PICK: '+pick);return pick
    ctx.ask_choice=ask
    result=plugin.handle_command(ctx,f'lookback track-all {scan}',source=sender)
    print('\n--- cancelled exact preview journey\n'+'\n\n'.join(seen)+'\n'+result)
    assert 'Cancelled' in result
    assert not store.list_items()


def test_track_all_partial_commit_failure_reports_other_successes(three):
    sender,ctx,scan,ids=first_lead(three)
    def ask(q,ch):
        if any(c['value']=='next' for c in ch):return 'next'
        plugin.handle_command(ctx,f'lookback skip {ids[1]}',source=sender)
        return 'accept'
    ctx.ask_choice=ask
    out=plugin.handle_command(ctx,f'lookback track-all {scan}',source=sender)
    assert '2 of 3 added' in out and ids[1] in out and len(store.list_items())==2


def test_expired_scan_and_changed_consent_block_track_all(three,monkeypatch):
    sender,ctx,scan,ids=first_lead(three)
    ctx.ask_choice=lambda q,ch:'next' if any(c['value']=='next' for c in ch) else 'accept'
    monkeypatch.setattr(lb,'epoch',lambda:999)
    out=plugin.handle_command(ctx,f'lookback track-all {scan}',source=sender)
    assert 'consent epoch' in out and not store.list_items()


def test_scan_automatically_filters_positive_metadata_without_more_reads(env,monkeypatch):
    import hermes_tingle_sources as sources
    original=sources._gws_bridge
    def bridge(*args):
        result=original(*args)
        params=json.loads(args[-1])
        if params.get('format')=='metadata':
            result['snippet']='For your information only. No action is required.'
            result['payload']['headers']=[{'name':'Subject','value':'Payment receipt'},
                                          {'name':'List-Id','value':'receipts.example.invalid'}]
        return result
    monkeypatch.setattr(sources,'_gws_bridge',bridge)
    out=plugin.handle_command(env[1],'lookback start 7',source=env[0])
    assert '1 routine matches kept out' in out and 'track-all' not in out
    assert not any(p.get('format')=='full' for a,p in env[2])
    assert not store.list_items()
    assert not store.list_items()
    print('\n--- all-noise scan completion\n'+out)


def test_pagination_filters_first_and_review_counts_whole_scan(three):
    sender,ctx,scan,ids=first_lead(three)
    conn=lb.connect(); original=conn.execute('SELECT * FROM candidates LIMIT 1').fetchone()
    for i in range(12):
        conn.execute('INSERT INTO candidates(id,scan_id,generation,payload) VALUES(?,?,?,?)',
                     (f'LC-AAA{i:02X}',scan,1,original['payload']))
    conn.commit();conn.close()
    for i in range(10):replace_payload(f'LC-AAA{i:02X}',**noise_payload())
    out=plugin.handle_command(ctx,f'lookback list {scan}',source=sender)
    assert '3 conversations' in out and 'More:' not in out and '10 noisy matches' in out
    for i in range(10):assert f'LC-AAA{i:02X}' not in out
    # More than a review-walk page: finishing ten does not claim the scan is done.
    for i in range(10):replace_payload(f'LC-AAA{i:02X}',automated=False)
    ctx.ask_choice=lambda q,ch:'skip'
    out=plugin.handle_command(ctx,f'lookback review {scan}',source=sender)
    assert '3 skipped' in out and '1 still waiting' in out


def test_track_all_expired_scan_never_prompts_or_adds(three):
    from datetime import timedelta
    sender,ctx,scan,ids=first_lead(three)
    conn=lb.connect();conn.execute('UPDATE scans SET expires=? WHERE id=?',((lb.now()-timedelta(seconds=1)).isoformat(),scan));conn.commit();conn.close()
    prompts=[];ctx.ask_choice=lambda q,ch:prompts.append(q) or 'accept'
    assert 'expired' in plugin.handle_command(ctx,f'lookback track-all {scan}',source=sender)
    assert not prompts and not store.list_items()


def test_suppressed_expiry_and_nonowner_do_not_leak(three):
    sender,ctx,scan,ids=first_lead(three)
    replace_payload(ids[0],**noise_payload())
    sender.user_id='other'
    out=plugin.handle_command(ctx,f'lookback suppressed {scan}',source=sender)
    assert 'Payment receipt' not in out and ids[0] not in out


@pytest.mark.parametrize('snippet',[
    'No action is required unless you did not make this purchase.',
    'No action is needed if you recognize this transaction.',
    'No action required, except disputed purchases.',
    'For your information only. However, contact us about unrecognized activity.',
    'No action required until your current booking ends.',
    'No action required when the payment is correct.',
    'No action is required. Otherwise, call the bank.',
])
def test_conditional_no_action_is_not_suppressed(snippet):
    data=noise_payload();data['snippet']=snippet
    assert not lb._suppression_reason(data)
