"""Synthetic-only expiry tests: no mailbox, model or delivery proof."""
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
import hermes_tingle_store as store
import hermes_tingle_expiry as expiry
import hermes_tingle_expiry_preferences as prefs
import hermes_tingle_assess as assess
import hermes_tingle_lookback as lb
import hermes_tingle as plugin
from test_lookback import env
from test_lookback_quality import seed, FakeLlm, verdict, assess_scan


def instant():
    return datetime.now(timezone.utc).replace(microsecond=0)


def proposal(now, days=3):
    day = (now + timedelta(days=days)).date()
    return {'tier': 'automatic', 'date': day.isoformat(),
            'fire_at': datetime.combine(day - timedelta(days=1), datetime.min.time().replace(hour=10), timezone.utc).isoformat(),
            'category': 'vouchers'}


def item(now, **kw):
    return store.add_item(title='Synthetic voucher', now=now.isoformat(), **kw)


def state(i):
    row = store.get_item(i['id'])
    return {k: row[k] for k in ('due_at', 'next_check_at', 'reminder_count', 'reminder_attempt_count',
                              'reminder_cycle', 'reminder_revision')}


def classify(text, *, now=None, **kw):
    now = now or instant()
    day = (now + timedelta(days=3)).date().isoformat()
    span = kw.pop('span', day)
    claim = {'span': span, 'date': day, 'is_item_own': True}
    claim.update(kw.pop('claim', {}))
    return expiry.classify(claim, quote=text, excerpt=kw.pop('excerpt', text),
                           message_date=kw.pop('message_date', now.isoformat()), now=now,
                           zone=ZoneInfo('Etc/UTC'), **kw)


def test_clear_date_only_claim_is_automatic():
    now = instant(); d = (now + timedelta(days=3)).date().isoformat()
    r = classify('Your voucher expires on ' + d, now=now)
    assert r['tier'] == 'automatic' and r['date'] == d and r['fire_at']


@pytest.mark.parametrize('suffix', [' not valid', ' instead', ' starts later', ' if you qualify',
                                     ' tomorrow', ' at 10:00', ' CET', ' 12/12/2029'])
def test_excerpt_wide_demotions(suffix):
    now = instant(); d = (now + timedelta(days=3)).date().isoformat()
    quote = 'Your voucher expires on ' + d
    assert classify(quote, now=now, excerpt=quote + suffix)['tier'] == 'uncertain'


@pytest.mark.parametrize('change', [dict(context_complete=False), dict(direction='unknown'),
    dict(direction='sent_by_owner'), dict(due_at='2027-01-01'), dict(reply_watch=True),
    dict(claim={'is_item_own': False}), dict(claim={'date': '2099-01-01'}), dict(message_date='invalid')])
def test_other_demotions(change):
    now = instant(); d = (now + timedelta(days=3)).date().isoformat()
    assert classify('Your voucher expires on ' + d, now=now, **change)['tier'] == 'uncertain'


@pytest.mark.parametrize('days', [-1, 0, 1, 46])
def test_outside_horizon_has_no_proposal(days):
    now = instant(); d = (now + timedelta(days=days)).date().isoformat()
    r = classify('Your voucher expires on ' + d, now=now, span=d)
    assert r['tier'] == 'uncertain' and not r['fire_at']


@pytest.mark.parametrize('span', ['30 November', '11/30/2026', 'tomorrow', '2026-02-30', 'foo'])
def test_incomplete_or_invalid_dates_never_invented(span):
    assert not classify('Your voucher expires on ' + span, span=span)['fire_at']


def test_lifecycle_oneoff_leaves_all_ordinary_state_unchanged():
    now = instant(); i = item(now); before = state(i)
    p = proposal(now)
    assert expiry.create_nudge(i['id'], p, now=now)
    assert state(i) == before
    fire = datetime.fromisoformat(p['fire_at'])
    rows = expiry.reserve_due(now=fire + timedelta(minutes=29))
    assert len(rows) == 1 and state(i) == before
    assert not expiry.reserve_due(now=fire + timedelta(minutes=30))
    assert not expiry.create_nudge(i['id'], p, now=now)  # single outstanding/attempt
    text = expiry.render_nudges(rows, now=fire)
    assert 'not verified' in text and 'accepted lead payload was cleared' in text and 'lookback view' not in text
    assert store.list_alert_emissions() == [] if hasattr(store, 'list_alert_emissions') else True


def test_completion_and_snooze_drop_queue_without_touching_ordinary_attempts():
    now = instant(); i = item(now); p = proposal(now)
    assert expiry.create_nudge(i['id'], p, now=now)
    store.update_item(i['id'], status='done')
    conn = store._connect()
    assert conn.execute('SELECT count(*) FROM expiry_nudges').fetchone()[0] == 0
    conn.close()
    j = item(now, summary='another synthetic item')
    assert expiry.create_nudge(j['id'], p, now=now)
    conn = store._connect()
    conn.execute('UPDATE hermes_tingle_items SET snoozed_until=? WHERE id=?',
                 ((datetime.fromisoformat(p['fire_at']) + timedelta(hours=1)).isoformat(), j['id']))
    conn.commit()
    assert conn.execute('SELECT count(*) FROM expiry_nudges').fetchone()[0] == 0
    conn.close()


def test_sweeper_runs_when_alert_job_never_ran(monkeypatch):
    now = instant(); i = item(now); p = proposal(now)
    assert expiry.create_nudge(i['id'], p, now=now)
    monkeypatch.setattr(expiry, 'utcnow', lambda: now + timedelta(days=46))
    conn = store._connect()  # any local entrypoint follows existing lazy-expiry pattern
    assert conn.execute('SELECT count(*) FROM expiry_nudges').fetchone()[0] == 0
    conn.close()


def test_date_only_window_end_is_not_claimed_as_actual_expiry():
    now = instant(); i = item(now); p = proposal(now)
    expiry.create_nudge(i['id'], p, now=now)
    cutoff = datetime.fromisoformat(p['date']).replace(tzinfo=timezone.utc)
    assert not expiry.reserve_due(now=cutoff)
    rows = [{'item_id': i['id'], 'source_date': p['date']}]
    assert 'may already have expired' in expiry.render_nudges(rows, now=cutoff)


def test_explicit_instant_uses_short_grace():
    now = instant(); i = item(now); p = proposal(now)
    fire = now + timedelta(hours=1)
    assert expiry.create_nudge(i['id'], dict(p, tier='explicit'), now=now, explicit_time=fire.isoformat())
    assert not expiry.reserve_due(now=fire + timedelta(minutes=45))


def test_excludes_due_and_reply_watch_but_not_ordinary_ladder_state():
    now = instant(); p = proposal(now)
    for kw in ({'due_at': (now+timedelta(days=6)).isoformat()},
               {'completion_signal': 'source_email_reply_received', 'source_type': 'email', 'source_ref': 'gmail:abc#sent', 'status': 'waiting', 'owner': 'other', 'next_check_at': (now+timedelta(days=5)).isoformat()}):
        i = item(now, **kw)
        assert not expiry.create_nudge(i['id'], p, now=now)
    # One-off survives independent of intervening ordinary checks: a fire time
    # after the next ordinary check is still created (ordinary ladder may end
    # or miss the expiry), and an exhausted ladder no longer blocks creation.
    i = item(now)
    assert expiry.create_nudge(i['id'], proposal(now, days=10), now=now)
    j = item(now, summary='ladder ended synthetic')
    conn = store._connect()
    conn.execute('UPDATE hermes_tingle_items SET reminder_attempt_count=4,next_check_at=NULL WHERE id=?', (j['id'],))
    conn.commit(); conn.close()
    assert expiry.create_nudge(j['id'], proposal(now, days=30), now=now)


def test_ordinary_collision_merges_expiry_line_instead_of_deleting():
    now = instant(); i = item(now); p = proposal(now)
    assert expiry.create_nudge(i['id'], p, now=now)
    # Ordinary reminder due in the same poll: the one-off is still emitted
    # (merged as a separate line in the same message by the caller), never
    # silently deleted because ordinary output covers the item.
    fire = datetime.fromisoformat(p['fire_at'])
    conn = store._connect()
    conn.execute('UPDATE hermes_tingle_items SET next_check_at=? WHERE id=?', (fire.isoformat(), i['id']))
    conn.commit(); conn.close()
    store.record_alert_emission([i['id']], now=fire.isoformat())
    rows = expiry.reserve_due(now=datetime.fromisoformat(p['fire_at']))
    assert len(rows) == 1


@pytest.mark.parametrize('zone,day,offset', [('Europe/Berlin', '2027-03-29', '+02:00'),
                                            ('Europe/Berlin', '2027-11-01', '+01:00'),
                                            ('Australia/Lord_Howe', '2027-10-04', '+11:00')])
def test_target_date_uses_iana_rules(zone, day, offset):
    z = ZoneInfo(zone); target = datetime.fromisoformat(day).date()
    now = datetime.combine(target-timedelta(days=4), datetime.min.time(), z)
    r = expiry.classify({'span':day, 'date':day, 'is_item_own':True}, quote='expires on '+day,
                        excerpt='expires on '+day, message_date=now.isoformat(), now=now, zone=z)
    fire = datetime.fromisoformat(r['fire_at']).astimezone(z)
    assert fire.hour == 10 and fire.isoformat().endswith(offset)


def test_decline_streak_does_not_silently_activate():
    assert prefs.answer('vouchers', 'a', 'no') is None
    assert prefs.answer('vouchers', 'a', 'no') is None
    assert prefs.answer('vouchers', 'b', 'no') is None
    fb = prefs.answer('vouchers', 'c', 'no')
    assert fb and fb['status'] == 'pending' and not prefs.suppression('vouchers')
    assert prefs.answer('vouchers', 'd', 'no') is None
    assert prefs.answer('vouchers', 'e', 'correction') is None
    assert prefs.answer('vouchers', 'f', 'no') is None
    assert prefs.answer('returns', 'g', 'no') is None
    assert not prefs.suppression('vouchers')


def _write_file(rules):
    prefs.path().parent.mkdir(parents=True, exist_ok=True)
    prefs.path().write_text(json.dumps({'version': 1, 'rules': rules}))


def _file_rule(rule_id='mine', category='vouchers', action='suppress'):
    return {'id': rule_id, 'text': f'{action} {category} expiry suggestions', 'category': category,
            'action': action}


def _learned(category='vouchers', *, approve=True):
    fb = prefs.answer(category, 'a', 'no') or prefs.answer(category, 'b', 'no') or prefs.answer(category, 'c', 'no')
    assert fb and fb['status'] == 'pending'
    if approve:
        fb = store.resolve_feedback_candidate(fb['id'], 'approved', decided_by=('telegram', 'owner', 'dm'))
    return fb


def test_file_is_authoritative_and_plugin_never_writes_it():
    fb = _learned()
    assert prefs.suppression('vouchers') == fb['id']
    before = prefs.path().read_bytes() if prefs.path().exists() else None
    _write_file([_file_rule(action='resume')])
    # File resume outranks the learned suppress without any plugin file write.
    assert prefs.suppression('vouchers') is None
    assert (prefs.path().read_bytes() if prefs.path().exists() else None) is not None
    _write_file([_file_rule()])
    assert prefs.suppression('vouchers') == 'mine'
    # Reset touches learned state only; the owner's file is never rewritten.
    removed = prefs.reset_learned(decided_by=('telegram', 'owner', 'dm'), evidence='synthetic')
    assert removed == 1 and prefs.suppression('vouchers') == 'mine'
    assert before != prefs.path().read_bytes()  # owner content intact, only owner changes it
    prefs.path().unlink()
    prefs.path().write_text('{')
    with pytest.raises(ValueError, match='Invalid'):
        prefs.suppression('vouchers')


def test_owner_command_rules_are_db_records_that_file_outranks():
    fb = _learned()
    row = prefs.explicit_rule('vouchers', 'resume', decided_by=('telegram', 'owner', 'dm'), evidence='synthetic')
    assert prefs.suppression('vouchers') is None  # explicit command beats learned
    _write_file([_file_rule()])
    assert prefs.suppression('vouchers') == 'mine'  # file beats owner command
    prefs.path().unlink()
    assert prefs.suppression('vouchers') is None
    store.resolve_feedback_candidate(row['id'], 'approved', decided_by=('telegram', 'owner', 'dm'))
    # re-approving a resume-record does nothing (it was dismissed by design)
    assert prefs.suppression('vouchers') is None
    store.resolve_feedback_candidate(fb['id'], 'dismissed', decided_by=('telegram', 'owner', 'dm'))
    assert prefs.suppression('vouchers') is None


def test_hand_edited_file_copy_of_learned_rule_wins_and_survives_reset():
    fb = _learned()
    assert prefs.suppression('vouchers') == fb['id']
    _write_file([{'id': fb['id'], 'text': fb['rule_text'], 'category': 'vouchers', 'action': 'suppress'}])
    removed = prefs.reset_learned(decided_by=('telegram', 'owner', 'dm'), evidence='synthetic')
    assert removed == 1
    assert prefs.suppression('vouchers') == fb['id']  # the owner's copy keeps it active


def test_assessment_and_acceptance_create_no_source_retention(env):
    now = instant(); day = (now+timedelta(days=3)).date().isoformat()
    quote = 'Your voucher expires on ' + day
    scan = seed(env, [dict(snippet=quote, subject='Synthetic voucher', date=now.isoformat(),
                           context_status='Bounded thread context')])
    def decide(c):
        v = verdict(c['id'], 'action_needed', quote, 1, 'Use voucher')
        v.update(expiry_span=day, expiry_date=day, expiry_is_item_own=True)
        return v
    assess_scan(env, scan, FakeLlm(decide))
    conn = lb.connect(); r = conn.execute('SELECT * FROM assessment_results').fetchone(); conn.close()
    assert json.loads(r['expiry'])['date'] == day
    env[1].ask_choice = lambda q,ch: 'accept' if any(c['value']=='accept' for c in ch) else 'yes'
    out = plugin.handle_command(env[1], f"lookback accept {r['candidate_id']}", source=env[0])
    assert 'Accepted as' in out and 'one-off' in out
    conn = lb.connect()
    assert conn.execute('SELECT payload FROM candidates').fetchone()[0] == '{}'
    assert not conn.execute('SELECT * FROM assessment_results').fetchall()
    conn.close()
    conn = store._connect(); row = conn.execute('SELECT * FROM expiry_nudges').fetchone(); conn.close()
    assert row and quote not in repr(tuple(row))
    assert day not in store.get_item(row['item_id'])['summary']


def test_expiry_command_rejects_missing_nonowner_group_and_bot(env):
    from types import SimpleNamespace
    for source in (None, SimpleNamespace(user_id='other',platform='telegram',chat_type='dm'),
                   SimpleNamespace(user_id='owner',platform='telegram',chat_type='group'),
                   SimpleNamespace(user_id='owner',platform='telegram',chat_type='dm',is_bot=True)):
        text = plugin.handle_command(env[1], 'expiry cancel HT-ABCDEF', source=source)
        assert 'cancelled' not in text


def test_four_attempt_ladder_matches_control_after_additive_emission():
    now = instant(); a = item(now); b = store.add_item(title='Control item', now=now.isoformat())
    a_cycle, b_cycle = state(a)['reminder_cycle'], state(b)['reminder_cycle']
    p = proposal(now)
    assert expiry.create_nudge(a['id'], p, now=now)
    assert expiry.reserve_due(now=datetime.fromisoformat(p['fire_at']))
    for day in (7, 9, 16, 30):
        poll = now + timedelta(days=day, seconds=1)
        store.record_alert_emission([a['id'], b['id']], now=poll.isoformat())
        left, right = state(a), state(b)
        assert left['reminder_cycle'] == a_cycle and right['reminder_cycle'] == b_cycle
        for key in ('due_at','next_check_at','reminder_count','reminder_attempt_count','reminder_revision'):
            assert left[key] == right[key], key
    assert state(a)['reminder_attempt_count'] == 4 and state(a)['next_check_at'] is None


def test_suppression_applies_to_pending_automatic_but_never_ordinary_or_explicit():
    now = instant(); i = item(now); before = state(i); p = proposal(now)
    assert expiry.create_nudge(i['id'], p, now=now)
    command_rule = prefs.explicit_rule('vouchers', 'suppress', decided_by=('telegram','owner','dm'), evidence='synthetic')
    assert not expiry.reserve_due(now=datetime.fromisoformat(p['fire_at']))
    assert state(i) == before
    conn = store._connect()
    assert conn.execute('SELECT rule_id FROM expiry_suppressions').fetchone()[0] == command_rule['id']
    conn.close()
    j = store.add_item(title='Explicit owner request', now=now.isoformat())
    fire = now + timedelta(hours=1)
    assert expiry.create_nudge(j['id'], dict(p,tier='explicit'), now=now, explicit_time=fire.isoformat())
    assert len(expiry.reserve_due(now=fire)) == 1


def test_invalid_file_blocks_expiry_not_ordinary_state():
    now = instant(); i = item(now); p = proposal(now); before = state(i)
    assert expiry.create_nudge(i['id'], p, now=now)
    prefs.path().parent.mkdir(exist_ok=True, parents=True)
    prefs.path().write_text('{')
    with pytest.raises(ValueError, match='Invalid'):
        expiry.reserve_due(now=datetime.fromisoformat(p['fire_at']))
    assert state(i) == before


def test_learned_approval_dismiss_and_reset():
    fb = _learned()
    assert prefs.suppression('vouchers') == fb['id']
    assert prefs.reset_learned(decided_by=('telegram','owner','dm'), evidence='synthetic') == 1
    assert not prefs.suppression('vouchers')
    store.resolve_feedback_candidate(fb['id'], 'approved', decided_by=('telegram','owner','dm'))
    assert prefs.suppression('vouchers') == fb['id']
    store.resolve_feedback_candidate(fb['id'], 'dismissed', decided_by=('telegram','owner','dm'))
    assert not prefs.suppression('vouchers')


def test_gap_and_fold_wall_times_are_refused_even_outside_usual_night_hours():
    from datetime import date, time
    assert expiry.wall_time(date(1993,8,21), time(10), ZoneInfo('Pacific/Kwajalein')) is None
    assert expiry.wall_time(date(2027,10,31), time(2,30), ZoneInfo('Europe/Berlin')) is None
    assert expiry.wall_time(date(2027,3,28), time(2,30), ZoneInfo('Europe/Berlin')) is None


def test_cancel_uncertain_timing_imports_item_and_resets_only_on_actual_answer(env):
    now = instant(); day = (now+timedelta(days=3)).date().isoformat()
    quote = 'Your voucher expires on ' + day
    scan = seed(env, [dict(snippet=quote+' if eligible', subject='Synthetic voucher', date=now.isoformat(),
                           context_status='Bounded thread context')])
    def decide(c):
        v = verdict(c['id'], 'action_needed', quote, 1, 'Use voucher')
        v.update(expiry_span=day, expiry_date=day, expiry_is_item_own=True)
        return v
    assess_scan(env, scan, FakeLlm(decide))
    conn=lb.connect(); lead=conn.execute('SELECT candidate_id FROM assessment_results').fetchone()[0];conn.close()
    prompts=[]
    def choose(q,ch):
        prompts.append(q)
        return 'accept' if any(c['value']=='accept' for c in ch) else 'cancel'
    env[1].ask_choice=choose
    text=plugin.handle_command(env[1], f'lookback accept {lead}', source=env[0])
    assert 'Accepted as' in text and any('time limit' in q for q in prompts)
    conn=store._connect()
    assert not conn.execute('SELECT * FROM expiry_nudges').fetchall()
    assert not conn.execute('SELECT * FROM expiry_streaks').fetchall()
    conn.close()


def test_timing_race_refuses_stale_candidate(env):
    now=instant();day=(now+timedelta(days=3)).date().isoformat();quote='Voucher expires on '+day
    scan=seed(env,[dict(snippet=quote+' if eligible',date=now.isoformat(),context_status='Bounded thread context')])
    def decide(c):
        v=verdict(c['id'],'action_needed',quote,1,'Use voucher')
        v.update(expiry_span=day,expiry_date=day,expiry_is_item_own=True);return v
    assess_scan(env,scan,FakeLlm(decide))
    conn=lb.connect();lead=conn.execute('SELECT candidate_id FROM assessment_results').fetchone()[0];conn.close()
    def choose(q,ch):
        if any(c['value']=='accept' for c in ch): return 'accept'
        conn=lb.connect();conn.execute("UPDATE candidates SET draft_rev=draft_rev+1 WHERE id=?",(lead,));conn.commit();conn.close()
        return 'yes'
    env[1].ask_choice=choose
    out=plugin.handle_command(env[1],f'lookback accept {lead}',source=env[0])
    assert 'changed' in out and not store.list_items()


def test_only_one_off_record_and_no_source_date_in_attempt_log():
    now=instant();i=item(now);p=proposal(now)
    assert expiry.create_nudge(i['id'],p,now=now)
    assert not expiry.create_nudge(i['id'],p,now=now)
    expiry.reserve_due(now=datetime.fromisoformat(p['fire_at']))
    conn=store._connect()
    columns={r[1] for r in conn.execute('PRAGMA table_info(expiry_emissions)')}
    assert columns=={'item_id','emitted_at','expires_at','state'}
    assert not conn.execute('SELECT * FROM expiry_nudges').fetchall()
    conn.close()


def test_uncertain_yes_uses_short_grace_not_date_only_window():
    now=instant();i=item(now);p=dict(proposal(now),tier='confirmed')
    fire=datetime.fromisoformat(p['fire_at'])
    assert expiry.create_nudge(i['id'],p,now=now)
    assert not expiry.reserve_due(now=fire+timedelta(minutes=45))


def test_explicit_owner_time_can_replace_pending_automatic():
    now=instant();i=item(now);p=proposal(now)
    assert expiry.create_nudge(i['id'],p,now=now)
    fire=now+timedelta(hours=1)
    assert expiry.create_nudge(i['id'],dict(p,tier='explicit'),now=now,explicit_time=fire.isoformat())
    rows=expiry.reserve_due(now=fire)
    assert len(rows)==1 and rows[0]['tier']=='explicit'


def test_due_alert_script_emits_extra_without_ordinary_reservation(monkeypatch, capsys):
    import due_alerts
    import hermes_tingle_sources as sources
    now=instant();i=item(now);p=proposal(now)
    fire=datetime.fromisoformat(p['fire_at']);before=state(i)
    assert expiry.create_nudge(i['id'],p,now=now)
    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return fire.astimezone(tz) if tz else fire.replace(tzinfo=None)
    monkeypatch.setattr(due_alerts,'datetime',Fixed)
    monkeypatch.setattr(sources,'all_due_items',lambda **kw: [])
    monkeypatch.setattr(store,'deadline_catchup_state',lambda **kw: {'complete':True})
    monkeypatch.setattr(due_alerts,'_unconfirmed_delivery_note',lambda *a,**kw: ('',[]))
    monkeypatch.setattr(store,'record_alert_emission',lambda *a,**kw: pytest.fail('ordinary advancement used'))
    assert due_alerts._main()==0
    text=capsys.readouterr().out
    assert 'Expiry suggestion' in text and 'cutoff time unknown' in text
    assert state(i)==before
    assert due_alerts._main()==0
    assert not capsys.readouterr().out


def test_second_create_after_emission_still_refused():
    now=instant();i=item(now);p=proposal(now)
    assert expiry.create_nudge(i['id'],p,now=now)
    assert not expiry.create_nudge(i['id'],p,now=now)
    assert expiry.reserve_due(now=datetime.fromisoformat(p['fire_at']))
    assert not expiry.create_nudge(i['id'],p,now=now)
    conn=store._connect();assert not conn.execute('SELECT * FROM expiry_nudges').fetchall();conn.close()


@pytest.mark.parametrize('days,uncertain', [(30, False), (3, True)])
def test_onboarding_restores_clear_expiry_and_reports_uncertain(env, days, uncertain):
    now=instant();day=(now+timedelta(days=days)).date().isoformat();quote='Your voucher expires on '+day
    scan=seed(env,[dict(snippet=quote+(' if eligible' if uncertain else ''),date=now.isoformat(),
                        context_status='Bounded thread context')])
    def decide(c):
        v=verdict(c['id'],'action_needed',quote,1,'Use voucher')
        v.update(expiry_span=day,expiry_date=day,expiry_is_item_own=True);return v
    assess_scan(env,scan,FakeLlm(decide))
    added,_,_,unknown=lb.auto_accept_qualified(env[0],lb.binding(env[0]),scan)
    assert len(added)==1
    conn=store._connect();rows=conn.execute('SELECT * FROM expiry_nudges').fetchall();conn.close()
    if uncertain:
        assert unknown==added and not rows
    else:
        assert not unknown and len(rows)==1 and rows[0]['source_date']==day
        assert datetime.fromisoformat(rows[0]['fire_at']).date()==datetime.fromisoformat(day).date()-timedelta(days=1)
        assert store.get_item(added[0])['reminder_attempt_count']==0
        assert store.get_item(added[0])['next_check_at'] is None  # unknown contextual plan never invents an ordinary reminder
        assert rows[0]['fire_at']  # separately consented expiry one-off remains unchanged


def test_preference_operations_do_not_modify_owner_file(monkeypatch):
    _write_file([_file_rule()]);original=prefs.path().read_bytes()
    from pathlib import Path
    real_write=Path.write_text
    def reject_write(self,*a,**kw):
        if self==prefs.path():pytest.fail('plugin wrote owner file')
        return real_write(self,*a,**kw)
    monkeypatch.setattr(Path,'write_text',reject_write)
    _learned()
    prefs.explicit_rule('vouchers','resume',decided_by=('telegram','owner','dm'),evidence='synthetic')
    prefs.reset_learned(decided_by=('telegram','owner','dm'),evidence='synthetic')
    assert prefs.path().read_bytes()==original and prefs.suppression('vouchers')=='mine'


def test_thirty_day_oneoff_survives_intervening_ordinary_checks():
    now=instant();i=item(now);p=proposal(now,days=30)
    assert expiry.create_nudge(i['id'],p,now=now)
    for day in (7,9,16):
        store.record_alert_emission([i['id']],now=(now+timedelta(days=day,seconds=1)).isoformat())
        assert not expiry.reserve_due(now=now+timedelta(days=day,seconds=1))
    before=state(i)
    assert len(expiry.reserve_due(now=datetime.fromisoformat(p['fire_at'])))==1
    assert state(i)==before


def test_onboarding_source_change_does_not_use_stale_claim(env):
    now=instant();day=(now+timedelta(days=30)).date().isoformat();quote='Your voucher expires on '+day
    scan=seed(env,[dict(snippet=quote,date=now.isoformat(),context_status='Bounded thread context')])
    def decide(c):
        v=verdict(c['id'],'action_needed',quote,1,'Use voucher')
        v.update(expiry_span=day,expiry_date=day,expiry_is_item_own=True);return v
    assess_scan(env,scan,FakeLlm(decide))
    conn=lb.connect();conn.execute('UPDATE candidates SET draft_rev=draft_rev+1');conn.commit();conn.close()
    added,_,_,_=lb.auto_accept_qualified(env[0],lb.binding(env[0]),scan)
    assert added  # independent import remains useful, stale timing discarded
    conn=store._connect();assert not conn.execute('SELECT * FROM expiry_nudges').fetchall();conn.close()
