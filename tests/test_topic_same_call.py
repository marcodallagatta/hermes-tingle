"""Same qualification request carries the decorative icon. Synthetic model only."""
import json
import sqlite3
import pytest
import hermes_tingle_assess as assess
import hermes_tingle_lookback as lb
import hermes_tingle_store as store
import hermes_tingle_onboarding as ob
from test_lookback import env
from test_lookback_quality import seed,strong,FakeLlm,verdict,ASK,assess_scan


@pytest.mark.parametrize('icon',['🩺','📄','invalid',None,[], '📄\nleak'])
def test_same_call_emoji_survives_qualification_import_and_render(env,icon):
    sid=seed(env,strong(1));calls=[]
    def decide(c):
        calls.append(c['id'])
        r=verdict(c['id'],'action_needed',ASK,1,'Review contract')
        r['topic_emoji']=icon
        return r
    assess_scan(env,sid,FakeLlm(decide))
    assert len(calls)==1
    conn=lb.connect();row=conn.execute('SELECT topic_emoji FROM assessment_results').fetchone();conn.close()
    expected=icon if isinstance(icon,str) and icon in assess.ICONS else '📌'
    assert row['topic_emoji']==expected
    added,_,_,_=lb.auto_accept_qualified(env[0],lb.binding(env[0]),sid)
    assert len(added)==1 and store.get_item(added[0])['topic_emoji']==expected
    assert ob._titles(added,decorated=True).startswith(expected+' ')
    assert len(calls)==1


def test_schema_asks_emoji_in_existing_call_and_old_results_fall_back():
    required=assess.SCHEMA['properties']['results']['items']['required']
    assert 'topic_emoji' in required and 'same response' in assess.INSTRUCTIONS
    r=verdict('C1','action_needed',ASK,1,'Review contract')
    out=assess.validate({'results':[r]},{'C1':{1:(ASK,'received')}})
    assert out['C1']['supported'] and out['C1']['topic_emoji']=='📌'
    assert assess.BATCH==12 and assess.MAX_CALLS==6 and assess.TIMEOUT==60


def test_legacy_item_column_upgrade_preserves_original_fields(env):
    item=store.add_item(title='Original item',summary='Retained summary',due_at='2027-01-01T12:00:00+00:00')
    path=store._connect().execute('PRAGMA database_list').fetchone()[2]
    conn=sqlite3.connect(path);conn.execute('ALTER TABLE hermes_tingle_items DROP COLUMN topic_emoji');conn.commit();conn.close()
    restored=store.get_item(item['id'])
    assert restored['topic_emoji']=='📌'
    for key in item:
        if key!='topic_emoji':assert restored[key]==item[key]
    assert store.get_item(item['id'])==restored  # idempotent second open


def test_legacy_temporary_results_upgrade_is_additive_and_idempotent(env):
    sid=seed(env,strong(1));assess_scan(env,sid,FakeLlm())
    conn=lb.connect()
    before=[dict(r) for r in conn.execute('SELECT * FROM assessment_results')]
    conn.execute('ALTER TABLE assessment_results DROP COLUMN topic_emoji');conn.commit();conn.close()
    conn=lb.connect();after=[dict(r) for r in conn.execute('SELECT * FROM assessment_results')]
    assert all(r['topic_emoji']=='📌' for r in after)
    assert [{k:v for k,v in r.items() if k!='topic_emoji'} for r in after]==[{k:v for k,v in r.items() if k!='topic_emoji'} for r in before]
    assert conn.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    conn.close()
    conn=lb.connect();assert [dict(r) for r in conn.execute('SELECT * FROM assessment_results')]==after;conn.close()
