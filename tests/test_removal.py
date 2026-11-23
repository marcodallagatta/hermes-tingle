"""Removal only touches disposable test fixtures. No real jobs or uninstall."""
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest
from runtime_support import hermes_import
import hermes_tingle_resources as resources
import hermes_tingle_removal as removal
import hermes_tingle_store as store


@pytest.fixture
def fixture(tmp_path,monkeypatch):
    home=tmp_path
    root=home/'plugin-data'/'hermes-tingle';root.mkdir(parents=True)
    conn=sqlite3.connect(root/'data.db');conn.execute('CREATE TABLE test(x)');conn.commit();conn.close()
    resources.record(root,home=home,directory=True)
    config=home/'hermes-tingle.json';resources.record(config,home=home)
    helper=home/'scripts'/'hermes_tingle_due.py';helper.parent.mkdir();helper.write_text('# synthetic helper')
    resources.record(helper,home=home)
    install=home/'plugins'/'hermes-tingle';install.mkdir(parents=True);(install/'plugin.yaml').write_text('name: hermes-tingle')
    unrelated=home/'unrelated';unrelated.write_text('keep this')
    jobs={'owned':{'id':'owned','name':'Tingle','script':'hermes_tingle_due.py','enabled':True},
          'other':{'id':'other','name':'unrelated','script':'other.py','enabled':True}}
    resources.record_job(home,jobs['owned'])
    calls=[]
    def pause(job_id):calls.append(('pause',job_id));jobs[job_id]['enabled']=False;return dict(jobs[job_id])
    def delete(job_id):calls.append(('remove',job_id));return jobs.pop(job_id,None) is not None
    api={'list':lambda:list(jobs.values()),'get':lambda job_id:dict(jobs[job_id]) if job_id in jobs else None,'pause':pause,'remove':delete,'gateway':lambda home:None}
    monkeypatch.setattr(removal,'verify_quiescent',lambda *a:None)
    return home,jobs,api,calls


def run(fixture,**kwargs):
    home,jobs,api,calls=fixture
    plan=removal.inventory(home,api)
    return removal.remove(home,plan,confirmation='REMOVE '+plan['token'],writers_stopped=True,adopt_legacy=True,api=api,**kwargs)


def manually_remove_cron_jobs(fixture):
    """Test setup: model the operator's separate native Hermes cleanup step."""
    home,jobs,api,calls=fixture
    plan=removal.inventory(home,api)
    for job in plan['jobs']:
        assert api['remove'](job['id'])
    calls.clear()  # Subsequent assertions cover Tingle's remover only.


def run_after_manual_cron_cleanup(fixture,**kwargs):
    manually_remove_cron_jobs(fixture)
    return run(fixture,**kwargs)


def mutate_at_final_quiescence(monkeypatch,change):
    """Inject a concurrent change after recovery setup but before resource deletion."""
    original=removal.verify_quiescent
    count=0
    def verify(home,api):
        nonlocal count
        count+=1
        if count==2:change()
        return original(home,api)
    monkeypatch.setattr(removal,'verify_quiescent',verify)


def test_prompt_refresh_updates_only_exact_previously_owned_job_record(tmp_path):
    before = {
        'id':'review-1', 'name':'Tingle daily review', 'prompt':'old prompt',
        'script':'hermes_tingle_snapshot.py', 'schedule':'0 9 * * *',
        'timezone':'Europe/Berlin', 'enabled':True, 'deliver':'telegram:42',
    }
    after = {**before, 'prompt':'new prompt'}
    resources.record_job(tmp_path, before)
    assert resources.refresh_recorded_job(tmp_path, before, after) is True
    stored = resources.read(tmp_path)['jobs']['review-1']['definition']
    assert stored == resources.job_definition(after)


def test_prompt_refresh_does_not_adopt_or_overwrite_drifted_job_record(tmp_path):
    before = {'id':'review-1', 'name':'Tingle daily review', 'prompt':'old prompt'}
    after = {**before, 'prompt':'new prompt'}
    resources.record_job(tmp_path, before)
    changed = {**before, 'schedule':'0 10 * * *'}
    resources.record_job(tmp_path, changed)
    drifted = resources.read(tmp_path)['jobs']['review-1']['definition']
    assert resources.refresh_recorded_job(tmp_path, before, after) is False
    assert resources.read(tmp_path)['jobs']['review-1']['definition'] == drifted
    unowned = {'id':'external-job', 'name':'Tingle daily review', 'prompt':'old prompt'}
    assert resources.refresh_recorded_job(tmp_path, unowned, {**unowned, 'prompt':'new prompt'}) is False


def test_prompt_refresh_does_not_adopt_legacy_job_record_missing_authored_fields(tmp_path):
    before = {'id':'review-1','name':'Tingle daily review','prompt':'old',
              'wrap_response':False,'repeat':{'times':None,'completed':3}}
    after = {**before,'prompt':'new'}
    resources.record_job(tmp_path,before)
    manifest=resources.read(tmp_path)
    definition=manifest['jobs']['review-1']['definition']
    definition.pop('wrap_response')
    definition.pop('repeat_times')
    resources.write(tmp_path,manifest)
    assert resources.refresh_recorded_job(tmp_path,before,after) is False
    assert resources.read(tmp_path)['jobs']['review-1']['definition']==definition


def test_prompt_refresh_migrates_legacy_owned_job_record_when_new_fields_are_default(tmp_path):
    before = {'id':'review-1','name':'Tingle daily review','prompt':'old',
              'wrap_response':None,'repeat':{'times':None,'completed':3}}
    after = {**before,'prompt':'new'}
    resources.record_job(tmp_path,before)
    manifest=resources.read(tmp_path)
    definition=manifest['jobs']['review-1']['definition']
    definition.pop('wrap_response')
    definition.pop('repeat_times')
    resources.write(tmp_path,manifest)
    assert resources.refresh_recorded_job(tmp_path,before,after) is True
    assert resources.read(tmp_path)['jobs']['review-1']['definition']==resources.job_definition(after)


def test_removal_refuses_when_tingle_cron_jobs_remain_before_any_cleanup(fixture):

    home,jobs,api,calls=fixture
    plan=removal.inventory(home,api)
    before_jobs=json.loads(json.dumps(jobs))
    before_files={str(p):p.read_bytes() for p in home.rglob('*') if p.is_file()}
    with pytest.raises(ValueError,match='does not change Cron jobs'):
        removal.remove(home,plan,confirmation='REMOVE '+plan['token'],writers_stopped=True,
                       adopt_legacy=True,api=api)
    assert jobs==before_jobs and calls==[]
    assert {str(p):p.read_bytes() for p in home.rglob('*') if p.is_file()}==before_files
    assert (home/'hermes-tingle.json').exists()
    assert (home/'plugins/hermes-tingle').exists()
    assert (home/'plugin-data/hermes-tingle/data.db').exists()
    assert not (home/resources.MARKER).exists()


def test_default_delete_only_owned_inventory_leaves_plugin_for_hermes(fixture):
    home,jobs,api,calls=fixture
    api['metadata_absent']=lambda:False
    result=run_after_manual_cron_cleanup(fixture)
    assert result['cleanup_completed'] and result['plugin_uninstall_required'] and not result['kept_data']
    assert list(jobs)==['other'] and jobs['other']['enabled']
    for p in ('hermes-tingle.json','scripts/hermes_tingle_due.py','plugin-data/hermes-tingle',resources.MANIFEST,resources.LOCK,resources.MARKER):assert not (home/p).exists()
    assert (home/'plugins/hermes-tingle/plugin.yaml').exists() and not api['metadata_absent']()
    assert (home/'unrelated').read_text()=='keep this'
    assert calls==[]


def test_tingle_data_cleanup_leaves_manager_install_for_explicit_hermes_remove(fixture,monkeypatch):
    home,jobs,api,calls,*_=fixture
    manually_remove_cron_jobs(fixture)
    api['metadata_absent']=lambda:False
    invoked=[]
    def native_remove(*args,**kwargs):
        invoked.append((args,kwargs))
        return subprocess.CompletedProcess(args,0,'','')
    monkeypatch.setattr(subprocess,'run',native_remove)
    plan=removal.inventory(home,api)
    result=removal.remove(home,plan,confirmation='REMOVE '+plan['token'],writers_stopped=True,
                          adopt_legacy=True,api=api)
    assert result['cleanup_completed'] and result['plugin_uninstall_required']
    assert invoked==[] and (home/'plugins/hermes-tingle').exists()
    assert not api['metadata_absent']()


def test_keep_is_explicit_but_requires_manual_cron_cleanup(fixture):
    home,jobs,*_=fixture
    result=run_after_manual_cron_cleanup(fixture,keep_data=True)
    assert result['kept_data'] and result['plugin_uninstall_required'] and 'owned' not in jobs
    assert (home/'plugin-data/hermes-tingle/data.db').exists() and (home/'hermes-tingle.json').exists()
    assert (home/'plugins/hermes-tingle/plugin.yaml').exists()
    assert not (home/'scripts/hermes_tingle_due.py').exists()
    assert not (home/resources.MARKER).exists()


def test_inventory_only_does_not_change_files_or_jobs(fixture):
    home,jobs,api,*_=fixture
    before={str(p):p.read_bytes() for p in home.rglob('*') if p.is_file()}
    plan=removal.inventory(home,api)
    assert plan and before=={str(p):p.read_bytes() for p in home.rglob('*') if p.is_file()}
    assert jobs['owned']['enabled']


def test_legacy_needs_explicit_adoption_not_identical_name_or_bytes(fixture):
    home,jobs,api,calls=fixture
    manually_remove_cron_jobs(fixture)
    (home/resources.MANIFEST).unlink()
    plan=removal.inventory(home,api)
    with pytest.raises(ValueError,match='adoption'):
        removal.remove(home,plan,confirmation='REMOVE '+plan['token'],writers_stopped=True,api=api)
    assert not calls and (home/'plugins/hermes-tingle').exists()


@pytest.mark.parametrize('bad',['symlink','hardlink','changed'])
def test_unsafe_or_changed_helper_never_silently_deleted(fixture,bad):
    home,jobs,api,calls=fixture
    manually_remove_cron_jobs(fixture)
    plan=removal.inventory(home,api)
    path=home/'scripts/hermes_tingle_due.py'
    if bad=='changed':path.write_text('operator changed helper')
    else:
        path.unlink()
        if bad=='symlink':path.symlink_to(home/'unrelated')
        else:__import__('os').link(home/'unrelated',path)
    with pytest.raises(ValueError):
        removal.remove(home,plan,confirmation='REMOVE '+plan['token'],writers_stopped=True,adopt_legacy=True,api=api)
    assert not calls and (home/'unrelated').exists()


def test_busy_job_is_never_paused_or_deleted_by_tingle_remover(fixture):
    home,jobs,api,calls=fixture
    jobs['owned']['fire_claim']={'by':'active'}
    with pytest.raises(ValueError,match='does not change Cron jobs'):run(fixture)
    assert jobs['owned']['enabled'] and 'owned' in jobs and calls==[]
    assert not (home/resources.MARKER).exists() and (home/'hermes-tingle.json').exists()
    store._connect().close()


def test_unknown_namespace_file_needs_independent_adoption(fixture):
    home,jobs,api,*_=fixture
    extra=home/'plugin-data/hermes-tingle/operator-note.txt';extra.write_text('not plugin-created')
    plan=removal.inventory(home,api)
    assert plan['resources'][str(extra.relative_to(home))]['ownership']=='needs explicit adoption'


def test_wrong_profile_or_confirmation_has_no_effect(fixture):
    home,jobs,api,calls=fixture
    plan=removal.inventory(home,api)
    with pytest.raises(ValueError):removal.remove(home,plan,confirmation='REMOVE guessed',writers_stopped=True,api=api)
    assert not calls and jobs['owned']['enabled']


def test_open_database_creation_registers_only_actual_canonical_namespace(tmp_path,monkeypatch):
    def native(name):
        root=tmp_path/'plugin-data'/name;root.mkdir(parents=True,exist_ok=True)
        return sqlite3.connect(root/'data.db')
    monkeypatch.setattr(store,'plugin_db',native)
    for name in resources.NAMES:
        store.open_database(name).close()
    data=resources.read(tmp_path)
    assert set(data['directories'])=={'plugin-data/'+name for name in resources.NAMES}
    (tmp_path/resources.MARKER).write_text('{}')
    with pytest.raises(ValueError,match='maintenance'):store.open_database('hermes-tingle')


def test_recovery_bundle_survives_tingle_data_failure_then_retry(fixture,monkeypatch):
    home,jobs,api,calls=fixture
    manually_remove_cron_jobs(fixture)
    helper=home/'scripts/hermes_tingle_due.py';original=helper.read_text()
    plan=removal.inventory(home,api)
    mutate_at_final_quiescence(monkeypatch,lambda:helper.write_text('synthetic concurrent change'))
    with pytest.raises(ValueError,match='incomplete') as failure:
        removal.remove(home,plan,confirmation='REMOVE '+plan['token'],writers_stopped=True,adopt_legacy=True,api=api)
    assert 'python '+str(home/'.tingle-removal-recovery'/'tingle_remove.py')+' -p <profile-name>' in str(failure.value)
    root=home/'.tingle-removal-recovery'
    assert root.exists() and (root/'tingle_remove.py').exists()
    assert (home/resources.MARKER).exists() and (home/'plugins/hermes-tingle/plugin.yaml').exists()
    helper.write_text(original)
    monkeypatch.setattr(removal,'verify_quiescent',lambda *a:None)
    result=run(fixture)
    assert result['cleanup_completed'] and result['plugin_uninstall_required'] and not root.exists()
    assert (home/'plugins/hermes-tingle/plugin.yaml').exists()


def test_other_profiles_and_manager_backups_are_not_guessed_owned(fixture):
    home,jobs,api,*_=fixture
    unrelated=home/'profiles'/'other'/'hermes-tingle.json';unrelated.parent.mkdir(parents=True);unrelated.write_text('private other profile')
    backup=home/'plugins-backup'/'hermes-tingle-deadbeef';backup.mkdir(parents=True);(backup/'plugin.yaml').write_text('name: hermes-tingle')
    plan=removal.inventory(home,api)
    assert 'profiles/other/hermes-tingle.json' not in plan['resources']
    assert plan['resources']['plugins-backup/hermes-tingle-deadbeef']['ownership']=='needs explicit adoption'
    assert run_after_manual_cron_cleanup(fixture)['cleanup_completed'] and unrelated.exists() and not backup.exists()


def test_resource_changes_during_removal_are_not_deleted(fixture,monkeypatch):
    home,jobs,api,calls=fixture
    manually_remove_cron_jobs(fixture)
    plan=removal.inventory(home,api)
    path=home/'scripts/hermes_tingle_due.py'
    mutate_at_final_quiescence(monkeypatch,lambda:path.write_text('changed concurrently'))
    with pytest.raises(ValueError,match='changed after preview'):
        removal.remove(home,plan,confirmation='REMOVE '+plan['token'],writers_stopped=True,adopt_legacy=True,api=api)
    assert path.read_text()=='changed concurrently'


def test_changed_model_or_route_is_not_silently_owned(fixture):
    home,jobs,api,*_=fixture
    jobs['owned']['provider']='changed-provider'
    jobs['owned']['origin']={'platform':'changed','chat_id':'other'}
    plan=removal.inventory(home,api)
    assert next(j for j in plan['jobs'] if j['id']=='owned')['ownership']=='needs explicit adoption'


def test_legacy_job_manifest_with_new_fields_at_defaults_remains_owned(fixture):
    home,jobs,api,*_=fixture
    manifest=resources.read(home)
    definition=manifest['jobs']['owned']['definition']
    definition.pop('wrap_response')
    definition.pop('repeat_times')
    resources.write(home,manifest)
    plan=removal.inventory(home,api)
    assert next(job for job in plan['jobs'] if job['id']=='owned')['ownership']=='created'


def test_legacy_job_manifest_with_changed_new_fields_needs_adoption(fixture):
    home,jobs,api,*_=fixture
    manifest=resources.read(home)
    definition=manifest['jobs']['owned']['definition']
    definition.pop('wrap_response')
    definition.pop('repeat_times')
    resources.write(home,manifest)
    jobs['owned']['wrap_response']=False
    jobs['owned']['repeat']={'times':3}
    plan=removal.inventory(home,api)
    assert next(job for job in plan['jobs'] if job['id']=='owned')['ownership']=='needs explicit adoption'


def test_native_adapter_inventory_does_not_scaffold_missing_cron_state(tmp_path):
    hermes_import('gateway.status')
    import hermes_tingle_removal_runtime as adapter
    before=set(tmp_path.rglob('*'))
    api=adapter.runtime(tmp_path)
    assert not {'pause','remove','transaction'} & set(api)
    assert api['list']()==[]
    assert set(tmp_path.rglob('*'))==before


def test_native_adapter_rejects_ambiguous_cron_store_without_writes(tmp_path):
    hermes_import('gateway.status')
    import hermes_tingle_removal_runtime as adapter
    root=tmp_path/'cron';root.mkdir()
    path=root/'jobs.json';path.write_bytes(b'{"jobs":[],"jobs":[]}')
    before=set(tmp_path.rglob('*'))
    api=adapter.runtime(tmp_path)
    with pytest.raises(ValueError,match='duplicate JSON key'):
        api['list']()
    assert path.read_bytes()==b'{"jobs":[],"jobs":[]}'
    assert set(tmp_path.rglob('*'))==before


def test_native_adapter_rejects_syntactically_malformed_json_without_writes(tmp_path):
    hermes_import('gateway.status')
    import hermes_tingle_removal_runtime as adapter
    root=tmp_path/'cron';root.mkdir()
    path=root/'jobs.json';raw=b'{"jobs":['
    path.write_bytes(raw)
    api=adapter.runtime(tmp_path)
    with pytest.raises(json.JSONDecodeError):
        api['list']()
    assert path.read_bytes()==raw


def test_native_adapter_rejects_nonstandard_json_constants_without_writes(tmp_path):
    hermes_import('gateway.status')
    import hermes_tingle_removal_runtime as adapter
    root=tmp_path/'cron';root.mkdir()
    path=root/'jobs.json';raw=b'{"jobs":[{"id":"N2","name":"Tingle daily review","script":"x","enabled":true,"priority":NaN}]}'
    path.write_bytes(raw)
    api=adapter.runtime(tmp_path)
    with pytest.raises(ValueError,match='non-standard JSON constant'):
        api['list']()
    assert path.read_bytes()==raw


def test_native_adapter_rejects_nonstring_name_or_script(tmp_path):
    hermes_import('gateway.status')
    import hermes_tingle_removal_runtime as adapter
    root=tmp_path/'cron';root.mkdir()
    path=root/'jobs.json'
    for bad in (b'{"jobs":[{"id":"N1","name":["Tingle daily review"],"script":"x","enabled":true}]}',
                b'{"jobs":[{"id":"N2","name":"Tingle daily review","script":["x"],"enabled":true}]}'):
        path.write_bytes(bad)
        api=adapter.runtime(tmp_path)
        with pytest.raises(ValueError,match='Malformed Cron records'):
            api['list']()
        assert path.read_bytes()==bad


def test_native_adapter_rejects_duplicate_cron_ids_without_writes(tmp_path):
    hermes_import('gateway.status')
    import hermes_tingle_removal_runtime as adapter
    root=tmp_path/'cron';root.mkdir()
    path=root/'jobs.json'
    raw=b'{"jobs":[{"id":"N1","name":"Tingle daily review","script":"x","enabled":true},{"id":"N1","name":"unrelated","script":"y","enabled":true}]}'
    path.write_bytes(raw)
    api=adapter.runtime(tmp_path)
    with pytest.raises(ValueError,match='Duplicate Cron IDs'):
        api['list']()
    assert path.read_bytes()==raw


def test_namespace_database_writer_blocks_without_deleting_data(fixture):
    home,jobs,api,*_=fixture
    manually_remove_cron_jobs(fixture)
    conn=sqlite3.connect(home/'plugin-data/hermes-tingle/data.db')
    conn.execute('BEGIN IMMEDIATE')
    try:
        with pytest.raises(ValueError,match='locked'):run(fixture)
        assert (home/'hermes-tingle.json').exists() and list(jobs)==['other']
    finally:conn.close()
    assert run(fixture)['cleanup_completed']


def test_stale_native_metadata_is_reported_for_manual_hermes_cleanup(fixture):
    home,jobs,api,calls=fixture
    manually_remove_cron_jobs(fixture)
    shutil.rmtree(home/'plugins/hermes-tingle')
    api['metadata_absent']=lambda:False
    result=run(fixture)
    assert result['cleanup_completed'] and result['plugin_uninstall_required']
    assert not api['metadata_absent']() and not (home/resources.MARKER).exists()


def test_absolute_named_helper_job_is_legacy_lead_not_automatic_ownership(fixture):
    home,jobs,api,*_=fixture
    jobs['legacy']={'id':'legacy','name':'Renamed legacy','script':str(home/'scripts/hermes_tingle_snapshot.py'),'enabled':True}
    plan=removal.inventory(home,api)
    assert next(j for j in plan['jobs'] if j['id']=='legacy')['ownership']=='needs explicit adoption'


def test_provenance_introspection_failure_closes_connection(monkeypatch):
    class Conn:
        closed=False
        def execute(self,*args):raise sqlite3.OperationalError('synthetic unavailable pragma')
        def close(self):self.closed=True
    conn=Conn();monkeypatch.setattr(store,'plugin_db',lambda name:conn)
    with pytest.raises(sqlite3.OperationalError):store.open_database('hermes-tingle')
    assert conn.closed


def test_mountpoint_inside_namespace_is_not_traversed_or_deleted(fixture,monkeypatch):
    home,jobs,api,calls=fixture
    root=home/'plugin-data/hermes-tingle'
    original=removal.os.path.ismount
    monkeypatch.setattr(removal.os.path,'ismount',lambda path:Path(path)==root or original(path))
    with pytest.raises(ValueError,match='Mounted resource'):removal.inventory(home,api)
    assert not calls and root.exists()


@pytest.mark.parametrize('relative,kind',[
    ('.tingle-resources-crash','data'),('.tingle-init-crash','data'),
    ('scripts/.hermes_tingle_snapshot.py.crash','helper'),('scripts/.hermes_tingle_due.py.crash','helper'),
])
def test_known_crash_temporary_files_are_unproven_inventory_then_removed(fixture,relative,kind):
    home,jobs,api,*_=fixture
    path=home/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('private synthetic temporary')
    plan=removal.inventory(home,api)
    assert plan['resources'][relative]['ownership']=='needs explicit adoption'
    assert plan['resources'][relative]['kind']==kind
    assert run_after_manual_cron_cleanup(fixture)['cleanup_completed'] and not path.exists()


def test_keep_retains_private_policy_manifest_orphans_but_not_helper_code(fixture):
    home,jobs,api,*_=fixture
    private=home/'.tingle-resources-crash';private.write_text('private route')
    code=home/'scripts/.hermes_tingle_due.py.crash';code.write_text('code')
    assert run_after_manual_cron_cleanup(fixture,keep_data=True)['cleanup_completed']
    assert private.exists() and (home/resources.MANIFEST).exists() and not code.exists()


# Regression probes from the independent removal review.
def test_install_tree_is_left_for_manual_hermes_removal(fixture):
 home,jobs,api,calls=fixture
 manually_remove_cron_jobs(fixture)
 plan=removal.inventory(home,api)
 install=home/'plugins/hermes-tingle'
 assert plan['plugin_installed'] and 'plugins/hermes-tingle' not in plan['resources']
 extra=install/'operator-added-private-notes.txt'
 extra.write_text('manager-owned install content')
 result=run(fixture)
 assert result['cleanup_completed'] and result['plugin_uninstall_required']
 assert extra.read_text()=='manager-owned install content' and install.exists()


def test_config_replaced_after_preview_is_not_deleted(fixture,monkeypatch):
 home,jobs,api,calls=fixture
 manually_remove_cron_jobs(fixture)
 plan=removal.inventory(home,api)
 original=home/'hermes-tingle.json';saved=original.read_text()
 def change():original.rename(home/'held-original-config');original.write_text(saved)
 mutate_at_final_quiescence(monkeypatch,change)
 # Same bytes, but inode differs. Destructive deletion must fail.
 with pytest.raises(ValueError):
  removal.remove(home,plan,confirmation='REMOVE '+plan['token'],writers_stopped=True,adopt_legacy=True,api=api)
 assert original.exists()


def test_directory_replacement_not_silently_removed(fixture,monkeypatch):
 home,jobs,api,calls=fixture
 manually_remove_cron_jobs(fixture)
 root=home/'plugin-data/hermes-tingle';empty=root/'empty';empty.mkdir();resources.record(empty,home=home,directory=True)
 plan=removal.inventory(home,api)
 def change():empty.rename(home/'held-original-directory');empty.mkdir()
 mutate_at_final_quiescence(monkeypatch,change)
 with pytest.raises(ValueError):
  removal.remove(home,plan,confirmation='REMOVE '+plan['token'],writers_stopped=True,adopt_legacy=True,api=api)
 assert empty.exists()

def test_wizard_maintenance_marker_appearing_after_entry_blocks_write(tmp_path,synthetic_settings,monkeypatch):
 import importlib.util
 spec=importlib.util.spec_from_file_location('remove_init_probe',__import__('pathlib').Path(__file__).parents[1]/'tools/tingle_init.py');wizard=importlib.util.module_from_spec(spec);spec.loader.exec_module(wizard)
 path=tmp_path/wizard.NAME;before=path.read_bytes()
 # Running wizard passed first entry check; remover installs its cooperative fence while wizard waits.
 original=resources.active
 def fence(home):
  original(home)
  (tmp_path/resources.MARKER).write_text('{}')
 monkeypatch.setattr(resources,'active',fence)
 with pytest.raises(ValueError,match='maintenance'):
  wizard.write_policy(tmp_path,{**synthetic_settings,'delivery':'telegram:999'},before)
 assert path.read_bytes()==before

def test_new_tingle_job_created_during_cleanup_not_false_success(fixture,monkeypatch):
 home,jobs,api,calls=fixture
 manually_remove_cron_jobs(fixture)
 plan=removal.inventory(home,api)
 def changed():jobs['new-owned']={'id':'new-owned','name':'[S] Hermes Tingle deadline nudges','script':'hermes_tingle_due.py','enabled':True}
 mutate_at_final_quiescence(monkeypatch,changed)
 with pytest.raises(ValueError,match='incomplete'):
  removal.remove(home,plan,confirmation='REMOVE '+plan['token'],writers_stopped=True,adopt_legacy=True,api=api)
 assert 'new-owned' in jobs


def test_new_helper_during_cleanup_is_not_deleted_or_reported_success(fixture,monkeypatch):
    home,jobs,api,calls=fixture
    manually_remove_cron_jobs(fixture)
    helper=home/'scripts/hermes_tingle_snapshot.py'
    plan=removal.inventory(home,api)
    mutate_at_final_quiescence(monkeypatch,lambda:helper.write_text('new operator code'))
    with pytest.raises(ValueError,match='new unapproved'):
        removal.remove(home,plan,confirmation='REMOVE '+plan['token'],writers_stopped=True,adopt_legacy=True,api=api)
    assert helper.exists() and (home/resources.MARKER).exists()


def test_mutation_waiter_cannot_resume_after_completed_removal(fixture,monkeypatch):
    import threading
    home,jobs,api,calls=fixture
    manually_remove_cron_jobs(fixture)
    entered=threading.Event();errors=[];original=resources.active
    def active(path):original(path);entered.set()
    monkeypatch.setattr(resources,'active',active)
    def writer():
        try:
            with resources.mutation(home):pytest.fail('stale writer resumed')
        except ValueError as exc:errors.append(str(exc))
    with resources.locked(home):
        t=threading.Thread(target=writer);t.start();assert entered.wait(3)
        assert run(fixture)['cleanup_completed']
    t.join(3)
    assert errors and 'maintenance' in errors[0]


def test_replaced_valid_manifest_not_deleted_using_old_preview(fixture,monkeypatch):
 home,jobs,api,calls=fixture
 manually_remove_cron_jobs(fixture)
 plan=removal.inventory(home,api)
 manifest=home/resources.MANIFEST
 def change():
  data=json.loads(manifest.read_text());data['operator_note']='new private annotation not in preview'
  manifest.rename(home/'held-original-manifest');manifest.write_text(json.dumps(data))
 mutate_at_final_quiescence(monkeypatch,change)
 with pytest.raises(ValueError,match='incomplete'):
  removal.remove(home,plan,confirmation='REMOVE '+plan['token'],writers_stopped=True,adopt_legacy=True,api=api)
 assert manifest.exists()



@pytest.mark.parametrize('name',[resources.MANIFEST,resources.LOCK,resources.MARKER,'.tingle-removal-recovery/hermes_tingle_resources.py'])
def test_workflow_file_replacement_with_same_bytes_is_preserved(fixture,name,monkeypatch):
    home,jobs,api,calls=fixture
    manually_remove_cron_jobs(fixture)
    plan=removal.inventory(home,api)
    def changed():
        path=home/name;payload=path.read_bytes()
        path.rename(home/('held-'+path.name));path.write_bytes(payload)
    mutate_at_final_quiescence(monkeypatch,changed)
    with pytest.raises(ValueError,match='Workflow resource changed'):
        removal.remove(home,plan,confirmation='REMOVE '+plan['token'],writers_stopped=True,adopt_legacy=True,api=api)
    assert (home/name).exists() and (home/resources.MARKER).exists()


def test_same_inode_manifest_content_change_is_preserved(fixture,monkeypatch):
    home,jobs,api,calls=fixture
    manually_remove_cron_jobs(fixture)
    plan=removal.inventory(home,api)
    def changed():
        path=home/resources.MANIFEST;data=json.loads(path.read_text());data['note']='new private annotation';path.write_text(json.dumps(data))
    mutate_at_final_quiescence(monkeypatch,changed)
    with pytest.raises(ValueError,match='Workflow resource changed'):
        removal.remove(home,plan,confirmation='REMOVE '+plan['token'],writers_stopped=True,adopt_legacy=True,api=api)
    assert 'new private annotation' in (home/resources.MANIFEST).read_text()


@pytest.mark.parametrize('name', [
    'Tingle daily review', 'Tingle nudges',
    '[SA] Hermes Tingle daily review', '[S] Hermes Tingle deadline nudges',
    ' Tingle daily review ',
])
def test_renamed_and_legacy_display_names_still_inventory_without_auto_adoption(fixture, name):
    home,jobs,api,calls=fixture
    jobs['stable-renamed-id'] = {'id':'stable-renamed-id','name':name,'enabled':False,
                                 'script':None,'prompt':'synthetic retained definition'}
    before=json.loads(json.dumps(jobs))
    plan=removal.inventory(home,api)
    entry=next(job for job in plan['jobs'] if job['id']=='stable-renamed-id')
    assert entry['ownership']=='needs explicit adoption'
    assert jobs==before and calls==[]
