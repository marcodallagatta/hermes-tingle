"""Mixed-thread list-mail suppression regression reversal."""
import pytest
import base64
import json
from datetime import timedelta
import hermes_tingle as plugin
import hermes_tingle_lookback as lb
import hermes_tingle_sources as sources
from test_lookback import run, env


@pytest.mark.parametrize('order',[['m1','m2'],['m2','m1']])
def test_flagged_list_lead_body_read_through_another_lead_thread(env, monkeypatch,order):
    stamp=env[4]; calls=[]
    def bridge(*args):
        p=json.loads(args[-1]); calls.append((args[2],p))
        if args[2]=='getProfile': return {'emailAddress':'owner@example.invalid'}
        if args[2]=='threads': return {'id':'t1','messages':[{'id':mid} for mid in order]}
        if args[3]=='list': return {'messages':[{'id':mid} for mid in order]}
        mid=p['id']
        if p['format']=='minimal': return {'id':mid,'internalDate':stamp}
        if p['format']=='full': return {'id':mid,'threadId':'t1','internalDate':stamp,'payload':{'mimeType':'text/plain','body':{'data':base64.urlsafe_b64encode(('body '+mid).encode()).decode()}}}
        headers=[{'name':'Subject','value':'Personal'}]
        if mid=='m2' and 'List-Id' in p['metadataHeaders']: headers.append({'name':'List-Id','value':'news.example.invalid'})
        return {'id':mid,'threadId':'t1','internalDate':stamp,'labelIds':['INBOX'],'snippet':'hello','payload':{'headers':headers}}
    monkeypatch.setattr(sources,'_gws_bridge',bridge)
    text=plugin.handle_command(env[1],'lookback start 7',source=env[0]); print(text)
    conn=lb.connect(); rows=[json.loads(r[0]) for r in conn.execute('SELECT payload FROM candidates')]; conn.close()
    noise=next(p for p in rows if p['message_id']=='m2')
    calls_full=[p['id'] for kind,p in calls if p.get('format')=='full']
    print('noise status:',noise['context_status'],'full bodies:',calls_full)
    assert 'List-mail noise' in noise['context_status'] and 'm2' not in calls_full
    assert calls_full==['m1']
