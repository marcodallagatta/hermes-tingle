"""Synthetic regression reproducer: PASS verifies confirmation holds no mutation fence."""
import json, os, pathlib, sqlite3, subprocess, sys, threading, types
from tempfile import TemporaryDirectory
repo=pathlib.Path(os.environ['TINGLE_REPO']).resolve(strict=True)
sys.path.insert(0,str(repo))
local=threading.local()
constants=types.ModuleType('hermes_constants')
constants.get_hermes_home=lambda:pathlib.Path(getattr(local,'home',os.environ.get('HERMES_HOME','')))
sys.modules['hermes_constants']=constants
pkg=types.ModuleType('plugins');pkg.__path__=[]
storage=types.ModuleType('plugins.plugin_storage')
def plugin_db(name):
 root=constants.get_hermes_home()/'plugin-data'/name
 root.mkdir(parents=True,exist_ok=True)
 return sqlite3.connect(root/'data.db')
storage.plugin_db=plugin_db;sys.modules['plugins']=pkg;sys.modules['plugins.plugin_storage']=storage
import setup_jobs, hermes_tingle_store as store
if len(sys.argv)>1:
 store.list_items();print('same-profile worker read completed');sys.exit(0)
with TemporaryDirectory(dir=os.environ['TMPDIR'],prefix='lock-scope-') as tmp:
 root=pathlib.Path(tmp);a=root/'a';b=root/'b';a.mkdir();b.mkdir()
 config={'timezone':'Etc/UTC','daily_schedule':'0 9 * * *','deadline_poll_minutes':30,'date_only_time':'09:00','availability_start':'09:00','availability_end':'18:00','delivery':'telegram:123','calendar_exclude_substrings':[],'workstream_exclude_values':[],'source_exclude_substrings':[],'ticket_exclude_patterns':[]}
 for p in (a,b):(p/'hermes-tingle.json').write_text(json.dumps(config))
 entered=threading.Event();release=threading.Event();done=threading.Event();result=[]
 class Context:
  def ask_choice(self,*args): entered.set();assert release.wait(10);return 'cancel'
 def setup():
  local.home=a;result.append(setup_jobs._activate_jobs(Context()))
 def other_profile():
  local.home=b
  try: store.list_items();result.append('other profile list completed')
  finally: done.set()
 t=threading.Thread(target=setup);t.start();assert entered.wait(3)
 u=threading.Thread(target=other_profile);u.start();assert done.wait(3),'cross-profile blocked on interactive confirmation'
 env={**os.environ,'HERMES_HOME':str(a)}
 p=subprocess.run([sys.executable,__file__,'worker'],text=True,capture_output=True,env=env,timeout=5)
 assert p.returncode==0,(p.stdout,p.stderr)
 print(p.stdout.strip());print('different-profile list proceeds while setup waits: YES')
 release.set();t.join(5);u.join(5);assert not t.is_alive() and not u.is_alive() and done.is_set(),result
 assert any('cancelled' in x for x in result)
 print('after cancellation other-profile list completes: YES')
