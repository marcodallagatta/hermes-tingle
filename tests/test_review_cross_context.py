"""Account-provenance regression reversal."""
import json
import hermes_tingle as plugin
import hermes_tingle_lookback as lb
import hermes_tingle_sources as sources
import hermes_tingle_store as store
from test_lookback import run, env


def test_lookback_A_item_linked_to_unrelated_standing_B_thread(env, monkeypatch):
    scan,c=run(env)
    assert 'Accepted' in plugin.handle_command(env[1], f'lookback accept {c["id"]} "A item" "A summary" "-"',source=env[0])
    store.enable_source('email',lookback_days=7,enabled_by='synthetic standing consent')
    store.disable_source('calendar')
    stamp=str(int(lb.now().timestamp()*1000))
    calls=[]
    def bridge(*args):
        p=json.loads(args[-1]); calls.append((args[2],p))
        if args[2]=='getProfile': return {'historyId':'100','emailAddress':'b@example.invalid'}
        if args[2]=='messages' and args[3]=='list': return {'messages':[{'id':'b-unrelated'}]}
        if args[2]=='messages' and p.get('format')=='minimal': return {'id':'b-unrelated','internalDate':stamp}
        if args[2]=='messages' and p.get('format')=='metadata': return {'id':'b-unrelated','threadId':'t1','internalDate':stamp,'labelIds':['INBOX'],'snippet':'private B unrelated','payload':{'headers':[{'name':'Subject','value':'Unrelated B'}]}}
        raise AssertionError((args,p))
    monkeypatch.setattr(sources,'_gws_bridge',bridge)
    import importlib.util
    spec=importlib.util.spec_from_file_location('real_identity',sources.__file__)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    module.SourceReadError=sources.SourceReadError
    monkeypatch.setattr(module,'_gws_bridge',bridge)
    monkeypatch.setattr(sources,'body_account_identity',module.body_account_identity)
    result=sources.collect_snapshot()
    print('cross-account tracked context:', result['tracked_email_context'])
    assert result['tracked_email_context']==[]
