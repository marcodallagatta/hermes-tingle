"""Single Calendar source migration: preserve old field grants, never infer a wider one."""
import json
import sqlite3
from types import SimpleNamespace
import pytest
import hermes_tingle_store as store
import hermes_tingle_sources as sources
import hermes_tingle as plugin
from test_lookback import env


def legacy_state(calendar,meeting,*,valid=True):
    conn=store._connect()
    conn.execute('ALTER TABLE hermes_tingle_source_optins DROP COLUMN calendar_context')
    for kind,enabled in [('calendar',calendar),('meeting',meeting)]:
        conn.execute('UPDATE hermes_tingle_source_optins SET enabled=?,enabled_by=?,enabled_at=?,generation=12 WHERE kind=?',
                     (enabled,'synthetic owner' if valid else '', '2026-11-16T09:00:00+00:00' if valid else None,kind))
    conn.commit();conn.close()


@pytest.mark.parametrize('calendar,meeting',[(False,False),(True,False),(False,True),(True,True)])
def test_legacy_scope_matrix_migrates_once_without_widening(env,calendar,meeting):
    legacy_state(calendar,meeting)
    grants={r['kind']:r for r in store.list_source_optins()}
    assert set(grants)=={'calendar','email','document','chat'}
    assert bool(grants['calendar']['enabled'])==calendar
    assert bool(grants['calendar']['calendar_context'])==meeting
    assert store.calendar_context_enabled()==(calendar and meeting)
    before=grants['calendar'].copy()
    assert next(r for r in store.list_source_optins() if r['kind']=='calendar')==before
    assert 'meeting' not in sources.source_capabilities()


def test_invalid_legacy_permission_is_not_promoted(env):
    legacy_state(True,True,valid=False)
    grants={r['kind']:r for r in store.list_source_optins()}
    assert not grants['calendar']['enabled'] and not grants['calendar']['calendar_context']
    assert not store.calendar_context_enabled()


def test_existing_basic_calendar_fetch_remains_self_only(env,monkeypatch):
    from datetime import datetime,timezone
    legacy_state(True,False)
    calls=[]
    monkeypatch.setattr(sources,'_gws_bridge',lambda *a:calls.append(json.loads(a[-1])) or {'items':[]})
    sources._read_calendar_events({'id':'c1','summary':'Synthetic'},datetime.now(timezone.utc),7)
    assert len(calls)==1 and 'attendees(self,responseStatus)' in calls[0]['fields']
    assert 'organizer' not in calls[0]['fields']


def test_explicit_calendar_enable_includes_context_without_separate_prompt(env,monkeypatch):
    sender,ctx,*_=env
    legacy_state(True,False)
    calls=[]
    ctx.ask_choice=lambda q,ch:pytest.fail('Calendar includes context without a separate toggle')
    monkeypatch.setattr(sources,'probe_source',lambda kind:calls.append(kind))
    out=plugin.handle_command(ctx,'source enable calendar',source=sender)
    assert 'opted in' in out and store.calendar_context_enabled() and calls==['calendar']
    listing=plugin.handle_command(ctx,'sources',source=sender)
    assert 'organizer and attendee count' in listing and '`meeting`:' not in listing
    print('\n--- single Calendar source after enable\n'+listing)


def test_calendar_enable_without_choice_interface_uses_single_explicit_command(env,monkeypatch):
    legacy_state(False,False)
    monkeypatch.setattr(sources,'probe_source',lambda kind:None)
    out=plugin.handle_command(None,'source enable calendar',source=env[0])
    assert 'opted in' in out and store.calendar_context_enabled()


def test_existing_full_calendar_regrant_does_not_ask_again(env,monkeypatch):
    legacy_state(True,True)
    monkeypatch.setattr(sources,'probe_source',lambda kind:None)
    env[1].ask_choice=lambda q,ch:pytest.fail('existing field scope needs no repeated prompt')
    out=plugin.handle_command(env[1],'source enable calendar 7',source=env[0])
    assert 'opted in' in out and store.calendar_context_enabled()


def test_calendar_revocation_clears_both_scopes_and_generations(env):
    legacy_state(True,True)
    before=store.source_generation('calendar')
    conn=store._connect();conn.execute("INSERT INTO source_seen(source_type,source_id,source_version,reviewed_at) VALUES('calendar','c1','v1','2026-11-16T09:00:00+00:00')");conn.commit();conn.close()
    store.disable_source('calendar')
    assert not store.source_enabled('calendar') and not store.calendar_context_enabled()
    conn=store._connect();row=conn.execute("SELECT * FROM hermes_tingle_source_optins WHERE kind='calendar'").fetchone()
    assert row['generation']>before and row['calendar_context']==0
    assert conn.execute("SELECT enabled FROM hermes_tingle_source_optins WHERE kind='meeting'").fetchone()[0]==0
    assert not conn.execute("SELECT * FROM source_seen WHERE source_type='calendar'").fetchall();conn.close()
    store.enable_source('calendar',enabled_by='synthetic owner')
    assert store.source_enabled('calendar') and store.calendar_context_enabled()


def test_legacy_command_enable_never_aliases_to_wider_calendar(env,monkeypatch):
    legacy_state(False,True)
    monkeypatch.setattr(sources,'probe_source',lambda kind:pytest.fail('no alias probe'))
    out=plugin.handle_command(env[1],'source enable meeting',source=env[0])
    assert 'part of Calendar' in out and not store.source_enabled('calendar')
    with pytest.raises(ValueError):store.enable_source('meeting',enabled_by='synthetic owner')


def test_legacy_disable_narrows_only_calendar_context(env):
    legacy_state(True,True)
    out=plugin.handle_command(env[1],'source disable meeting',source=env[0])
    assert 'existing event permission is unchanged' in out
    assert store.source_enabled('calendar') and not store.calendar_context_enabled()


def test_consent_change_during_probe_aborts_enable(env,monkeypatch):
    legacy_state(True,False)
    def probe(kind):
        store.disable_source('calendar')
    monkeypatch.setattr(sources,'probe_source',probe)
    out=plugin.handle_command(env[1],'source enable calendar',source=env[0])
    assert 'consent changed' in out or 'Source consent changed' in out
    assert not store.calendar_context_enabled()


def test_migration_retains_items_and_source_audit_evidence(env):
    item=store.add_item(title='Synthetic task',summary='Keep this item')
    legacy_state(True,True)
    store.list_source_optins()
    assert store.get_item(item['id'])['title']=='Synthetic task'
    conn=store._connect();legacy=conn.execute("SELECT * FROM hermes_tingle_source_optins WHERE kind='meeting'").fetchone();conn.close()
    assert legacy['enabled_by']=='synthetic owner' and legacy['generation']==12


def test_field_narrowing_purges_review_state_and_invalidates_generation(env):
    legacy_state(True,True)
    before=store.source_generation('calendar')
    conn=store._connect();conn.execute("INSERT INTO source_seen(source_type,source_id,source_version,reviewed_at) VALUES('calendar','c1','v1','2026-11-16T09:00:00+00:00')");conn.commit();conn.close()
    store.enable_source('calendar',enabled_by='synthetic owner',calendar_context=False)
    assert not store.calendar_context_enabled() and store.source_generation('calendar')>before
    conn=store._connect();assert not conn.execute("SELECT * FROM source_seen WHERE source_type='calendar'").fetchall();conn.close()


def test_invalidated_existing_grant_cannot_keep_context_on_regrant(env):
    legacy_state(True,True)
    assert store.calendar_context_enabled()
    conn=store._connect();conn.execute("UPDATE hermes_tingle_source_optins SET enabled_by='' WHERE kind='calendar'");conn.commit();conn.close()
    assert not store.calendar_context_enabled()
    store.enable_source('calendar',enabled_by='synthetic owner',calendar_context=False)
    assert not store.calendar_context_enabled()
