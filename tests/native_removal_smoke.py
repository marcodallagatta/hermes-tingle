import json,os,shutil,sys,tempfile
from pathlib import Path
home=Path(tempfile.mkdtemp(prefix='tingle-removal-native-'))
os.environ['HERMES_HOME']=str(home)
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import argparse
parser=argparse.ArgumentParser();parser.add_argument('--keep-data',action='store_true');keep=parser.parse_args().keep_data
import hermes_tingle_resources as r
import hermes_tingle_removal as remove
from cron import jobs
from hermes_cli.plugins_cmd_git import _write_install_metadata,_read_install_metadata
# Disposable local native manager tree, synthetic policy and inactive scheduler.
plugin=home/'plugins'/'hermes-tingle';plugin.mkdir(parents=True)
(plugin/'plugin.yaml').write_text('name: hermes-tingle\nversion: "0.9.0"\ndescription: Synthetic removal rehearsal\n')
(plugin/'__init__.py').write_text('def register(ctx): pass\n')
_write_install_metadata({'hermes-tingle':{'source':'synthetic-local-fixture'}})
(home/'hermes-tingle.json').write_text('{"synthetic":true}')
r.record(home/'hermes-tingle.json',home=home)
scripts=home/'scripts';scripts.mkdir()
helper=scripts/'hermes_tingle_due.py';helper.write_text('print(\'synthetic\')\n');r.record(helper,home=home)
for namespace in r.NAMES:
    import sqlite3
    root=home/'plugin-data'/namespace;root.mkdir(parents=True)
    c=sqlite3.connect(root/'data.db');c.execute('create table synthetic(x)');c.commit();c.close();r.record(root,home=home,directory=True)
with jobs.use_cron_store(home):
    job=jobs.create_job(prompt=None,schedule='30m',name='[S] Hermes Tingle deadline nudges',script='hermes_tingle_due.py',no_agent=True)
    other=jobs.create_job(prompt='unrelated synthetic',schedule='60m',name='other synthetic')
r.record_job(home,job)
# Actual process/gateway quiescence check is enabled, actual strict Cron adapter,
# actual native manager subprocess. No network, real accounts, gateway or firing.
plan=remove.inventory(home)
result=remove.remove(home,plan,confirmation='REMOVE '+plan['token'],writers_stopped=True,adopt_legacy=True,keep_data=keep)
assert result['removed']
with jobs.use_cron_store(home):
    assert jobs.get_job(job['id']) is None and jobs.get_job(other['id']) is not None
assert 'hermes-tingle' not in _read_install_metadata()
assert not plugin.exists() and (home/'hermes-tingle.json').exists()==keep
assert not (home/'.tingle-removal-recovery').exists() and not (home/r.MARKER).exists()
for namespace in r.NAMES:assert (home/'plugin-data'/namespace).exists()==keep
if not keep:
    # A clean reinstall has no inherited source grants, tracked items or budgets.
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
    from tingle_init import write_policy,DEFAULTS
    import hermes_tingle_store as store
    write_policy(home,{**DEFAULTS,'delivery':'telegram:42'},None)
    assert store.list_items()==[] and not any(row['enabled'] for row in store.list_source_optins())
print(json.dumps({'result':'PASS','home':str(home),'native_manager_metadata_absent':True,'exact_owned_job_removed':True,'unrelated_job_retained':True,'default_namespace_delete_or_explicit_keep':True,'keep_data':keep,'clean_reinstall_checked':not keep}))
shutil.rmtree(home)
