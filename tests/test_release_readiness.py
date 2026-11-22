from delivery_fakes import bind_synthetic_email
from delivery_fakes import seed_outbox_items
"""Release regressions: real entry points and deterministic synthetic interleavings.

No mailbox, gateway, live profile, or third party is involved. File-backed SQLite
is deliberate: it exercises the same cross-thread/process lock mechanism as cron.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading

import pytest

import hermes_tingle as plugin
import hermes_tingle_store as store
import hermes_tingle_sources as sources
import hermes_tingle_wiring as wiring
import due_alerts

_REAL_POPEN = subprocess.Popen
NOW = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)
OWNER = ("telegram", "424242", "424242")


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.db"
    def connect(_name):
        conn = sqlite3.connect(path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn
    monkeypatch.setattr(store, "plugin_db", connect)
    return path


def grant():
    store.enable_source("email", enabled_by="synthetic owner")
    bind_synthetic_email(store)


def scalar(sql):
    conn = store._connect()
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()




def test_due_helper_runs_hard_deadline_recurrence_and_document_sweeps(db):
    hard = store.add_item(title="Synthetic deadline", hard_deadline=True, due_at=NOW-timedelta(days=1))
    recurring = store.add_item(title="Synthetic cancellation", hard_deadline=True,
        due_at=NOW-timedelta(days=1), recur_interval=1, recur_unit="year", recur_lead_days=7)
    paperwork = store.add_item(title="Synthetic paperwork", next_check_at=NOW)
    store.add_document(paperwork["id"], name="Synthetic certificate", valid_until=(NOW-timedelta(days=1)).isoformat())
    sources.all_due_items(now=NOW)
    assert store.get_item(hard["id"])["status"] == "missed"
    assert store.get_item(recurring["id"])["due_at"].startswith("2027-")
    assert store.get_item(paperwork["id"])["documents"][0]["state"] == "expired"
    assert len(store.list_items(status="missed")) == 2
    assert scalar("SELECT count(*) FROM recurrence_completions") == 2
    sources.all_due_items(now=NOW)
    assert scalar("SELECT count(*) FROM recurrence_completions") == 2


def test_scheduled_script_runs_sweeps_not_just_direct_store_methods(db, capsys):
    item = store.add_item(title="Synthetic old hard deadline", hard_deadline=True,
                          due_at=datetime.now(timezone.utc)-timedelta(days=1))
    due_alerts.main()
    assert store.get_item(item["id"])["status"] == "missed"
    # Missed is unresolved and must be visible, never silently hidden.
    output = capsys.readouterr().out
    assert "Synthetic old hard deadline" in output and "Status: missed" in output


def test_source_snapshot_runs_sweeps_with_all_sources_off(db):
    item = store.add_item(title="Synthetic deadline", hard_deadline=True, due_at=NOW-timedelta(days=1))
    sources.collect_snapshot(now=NOW)
    assert store.get_item(item["id"])["status"] == "missed"


@pytest.mark.parametrize("cursor,stale", [(False,False),(True,True),(True,False)])
def test_bounded_grant_queries_only_scoped_pages_never_broad_history(db, monkeypatch, cursor, stale):
    store.enable_source("email", lookback_days=1, enabled_by="synthetic owner")
    bind_synthetic_email(store)
    if cursor:
        store.stage_gmail_sync(history_id="100", message_ids=[], full_reconcile_at=NOW-timedelta(days=8 if stale else 1))
    queries=[]
    def bridge(*args, **kwargs):
        params=json.loads(args[-1])
        if "getProfile" in args:
            return {"historyId":"101"}
        assert "history" not in args, "history cannot be date-filtered"
        queries.append(params["q"])
        if "pageToken" not in params:
            return {"messages":[],"nextPageToken":"synthetic-next"}
        return {"messages":[]}
    monkeypatch.setattr(sources,"_gws_bridge",bridge)
    result=sources._collect_email_candidates(now=NOW,lookback_days=30)
    assert len(queries)==2 and all("newer_than:1d" in q for q in queries)
    assert result["emails"]==[]


def test_narrowing_unbounded_grant_clears_all_email_state_and_keeps_items(db):
    grant()
    item=store.add_item(title="Synthetic tracked item",source_type="email",source_ref="gmail:synthetic-thread")
    store.stage_gmail_sync(history_id="100",message_ids=["synthetic-id"])
    store.record_gmail_not_found(["synthetic-id"])
    store.set_plugin_meta("gmail_history_failures","3")
    store.enable_source("email",lookback_days=1,enabled_by="synthetic owner")
    for table in ("gmail_pending_messages","gmail_unreadable","email_candidates","email_body_reads"):
        assert scalar(f"SELECT count(*) FROM {table}")==0
    assert scalar("SELECT count(*) FROM plugin_meta WHERE key GLOB 'gmail_*'")==0
    assert store.get_item(item["id"])["source_ref"]=="gmail:synthetic-thread"


@pytest.mark.parametrize("writer", ["stage","unreadable","candidate","body","review","meta"])
def test_every_retained_email_writer_refuses_completed_revocation(db, writer):
    grant()
    store.stage_gmail_sync(history_id="100",message_ids=["synthetic-id"])
    store.disable_source("email")
    actions={
        "stage":lambda:store.stage_gmail_sync(history_id="101",message_ids=["synthetic-late"]),
        "unreadable":lambda:store.record_gmail_not_found(["synthetic-late"]),
        "candidate":lambda:store.register_email_candidates(["synthetic-late"], account_key='a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf'),
        "body":lambda:store.claim_email_body_read("synthetic-late", account_key='a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf'),
        "review":lambda:store.mark_source_reviewed("email","synthetic-late"),
        "meta":lambda:store.set_plugin_meta("gmail_degraded_notice","synthetic"),
    }
    if writer=="review":
        assert actions[writer]() is False
    else:
        with pytest.raises(store.SourceOptInRevoked):actions[writer]()
    for table in ("gmail_pending_messages","gmail_unreadable","email_candidates","email_body_reads","source_seen"):
        assert scalar(f"SELECT count(*) FROM {table}")==0


@pytest.mark.parametrize("regrant",[False,True])
def test_check_write_interleaving_cannot_repopulate_after_revoke_or_regrant(db,monkeypatch,regrant):
    grant()
    original=store.source_enabled
    def interleave(kind):
        enabled=original(kind)
        store.disable_source(kind)
        if regrant:store.enable_source(kind,lookback_days=1,enabled_by="synthetic new grant")
        return enabled
    monkeypatch.setattr(store,"source_enabled",interleave)
    with pytest.raises(store.SourceOptInRevoked):
        store.stage_gmail_sync(history_id="100",message_ids=["synthetic-late"])
    assert scalar("SELECT count(*) FROM gmail_pending_messages")==0


def test_inflight_source_scan_finishes_before_revocation_returns_and_is_purged(db,monkeypatch):
    grant()
    entered=threading.Event();release=threading.Event();revoking=threading.Event();revoked=threading.Event()
    @store.serialized("sources")
    def scan():
        entered.set(); assert release.wait(5)
        store.stage_gmail_sync(history_id="100",message_ids=["synthetic-inflight"])
    def revoke():
        revoking.set();store.disable_source("email");revoked.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        scanning=pool.submit(scan);assert entered.wait(5)
        revocation=pool.submit(revoke);assert revoking.wait(5)
        assert not revoked.is_set()
        release.set();scanning.result(5);revocation.result(5)
    assert revoked.is_set() and not store.source_enabled("email")
    assert scalar("SELECT count(*) FROM gmail_pending_messages")==0


def test_file_lock_serializes_separate_processes(db, tmp_path):
    # The child uses exactly the same public serialization context, but a fresh
    # interpreter and SQLite connection. No parent's thread lock is shared.
    script='''import sys,sqlite3
import runtime_stubs
runtime_stubs.install()
import hermes_tingle_store as s
s.plugin_db=lambda _name:sqlite3.connect(sys.argv[1])
print('ready',flush=True)
with s.serialized_operation('sources'):
 print('entered',flush=True)
'''
    env={"PATH":"/usr/bin:/bin","HOME":str(tmp_path),"HERMES_HOME":str(tmp_path),
         "PYTHONPATH":str(Path(__file__).resolve().parents[1])+":"+str(Path(__file__).resolve().parent)}
    with store.serialized_operation("sources"):
        child=_REAL_POPEN([sys.executable,"-c",script,str(db)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env)
        assert child.stdout.readline().strip()=="ready"
        # Lock is still held here; child cannot have entered the critical section.
        import select
        assert not select.select([child.stdout],[],[],0.1)[0]
    out,err=child.communicate(timeout=10)
    assert child.returncode==0,err
    assert out.strip()=="entered"


def test_old_unconfirmed_batch_stays_silent_without_due_item(db,monkeypatch,capsys):
    store.record_alert_emission(seed_outbox_items(store, ["HT-ABC123"], now=datetime.now(timezone.utc)-timedelta(days=1)),now=datetime.now(timezone.utc)-timedelta(days=1))
    monkeypatch.setattr(sources,"all_due_items",lambda **kwargs:[])
    assert due_alerts.main()==0
    assert capsys.readouterr().out==""
    assert scalar("SELECT count(*) FROM alert_emissions")==1
    assert scalar("SELECT count(*) FROM alert_emissions WHERE surfaced_at IS NULL AND acked_at IS NULL")==1
    assert due_alerts.main()==0 and capsys.readouterr().out==""














def test_fresh_and_ambiguous_upgrade_sources_off_explicit_grants_preserved(db):
    assert all(not r["enabled"] for r in store.list_source_optins())
    store.enable_source("chat",lookback_days=2,enabled_by="synthetic owner")
    conn=store._connect()
    conn.execute("UPDATE hermes_tingle_source_optins SET enabled=1,enabled_by='migration:install-default' WHERE kind='email'")
    conn.execute("INSERT INTO gmail_unreadable VALUES('synthetic-id','not_found',1,'t','t','')")
    conn.commit();conn.close()
    rows={r["kind"]:r for r in store.list_source_optins()}
    assert not rows["email"]["enabled"] and rows["chat"]["enabled"]
    assert scalar("SELECT count(*) FROM gmail_unreadable")==0


def test_legacy_alias_works_through_tool_commands_and_relations(db):
    item=store.add_item(title="Synthetic old reference")
    conn=store._connect();conn.execute("UPDATE hermes_tingle_items SET id='OL-ABC123' WHERE id=?",(item["id"],));conn.commit();conn.close()
    migrated=store.get_item("OL-ABC123")
    assert migrated["id"].startswith("HT-")
    assert store.get_item(migrated["id"])["id"]==migrated["id"]
    store.add_document("OL-ABC123",name="Synthetic alias document")
    assert store.get_item("OL-ABC123")["documents"][0]["name"]=="Synthetic alias document"
    result=json.loads(plugin.handle_tool(None,{"action":"update","item_id":"OL-ABC123","title":"Synthetic renamed"}))
    assert result["success"],result
    assert "Synthetic renamed" in plugin.handle_command(None,"done OL-ABC123")
    assert store.get_item("OL-ABC123")["status"]=="done"


def test_repeated_parallel_initialization_and_connections(db):
    for _ in range(12):
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures=[pool.submit(store.list_items) for i in range(8)]
            assert all(f.result(10)==[] for f in futures)
    assert scalar("PRAGMA integrity_check")=="ok"
    assert scalar("SELECT count(*) FROM hermes_tingle_items")==0


def test_failed_schema_initialization_closes_connection(db,monkeypatch):
    class Connection:
        row_factory=None;closed=False
        def execute(self,*args):
            return [(0,'main','')] if args[0]=='PRAGMA database_list' else None
        def close(self):self.closed=True
    conn=Connection()
    monkeypatch.setattr(store,"plugin_db",lambda _:conn)
    monkeypatch.setattr(store,"ensure_schema",lambda _:(_ for _ in ()).throw(sqlite3.OperationalError("synthetic initialization failure")))
    with pytest.raises(sqlite3.OperationalError):store._connect()
    assert conn.closed

@pytest.mark.parametrize("age_days,timestamp",[(10,"old"),(0,"missing"),(0,"invalid")])
def test_aged_pending_and_forged_date_never_reach_rich_metadata(db,monkeypatch,age_days,timestamp):
    store.enable_source("email",lookback_days=1,enabled_by="synthetic owner")
    bind_synthetic_email(store)
    store.stage_gmail_sync(history_id="100",message_ids=["synthetic-aged"],full_reconcile_at=NOW-timedelta(days=2))
    requests=[]
    def bridge(*args,**kwargs):
        params=json.loads(args[-1]);requests.append((args,params))
        if "getProfile" in args:return {"historyId":"101"}
        if "list" in args:return {"messages":[]}
        assert params.get("format")=="minimal", "rich metadata requested before timestamp admission"
        if timestamp=="missing":return {}
        if timestamp=="invalid":return {"internalDate":"not-a-timestamp"}
        return {"internalDate":str(int((NOW-timedelta(days=age_days)).timestamp()*1000))}
    monkeypatch.setattr(sources,"_gws_bridge",bridge)
    result=sources._collect_email_candidates(now=NOW,lookback_days=1)
    assert result["emails"]==[]
    assert not any(p.get("format")=="metadata" for a,p in requests)
    if timestamp=="old":assert store.pending_gmail_message_ids()==[]


def test_metadata_preflight_uses_provider_not_forged_sender_date(db,monkeypatch):
    store.enable_source("email",lookback_days=1,enabled_by="synthetic owner")
    bind_synthetic_email(store)
    reads=[]
    def bridge(*args,**kwargs):
        params=json.loads(args[-1]);reads.append(params)
        if params.get("format")=="minimal":
            return {"internalDate":str(int((datetime.now(timezone.utc)-timedelta(days=10)).timestamp()*1000))}
        return {"id":"synthetic-old","threadId":"synthetic-thread","labelIds":["INBOX"],
                "internalDate":"0","snippet":"OUT OF SCOPE SYNTHETIC",
                "payload":{"headers":[{"name":"Date","value":"Mon, 16 Nov 2026 12:00:00 +0000"}]}}
    monkeypatch.setattr(sources,"_gws_bridge",bridge)
    with pytest.raises(sources.EmailOutsideScope):sources._read_email_message("synthetic-old")
    assert len(reads)==1 and reads[0]["fields"]=="internalDate"


def test_requeued_quarantine_ages_out_before_content_read(db,monkeypatch):
    store.enable_source("email",lookback_days=1,enabled_by="synthetic owner")
    bind_synthetic_email(store)
    store.stage_gmail_sync(history_id="100",message_ids=["synthetic-aged"])
    store.record_gmail_not_found(["synthetic-aged"])
    store.retry_quarantined_gmail()
    def bridge(*args,**kwargs):
        params=json.loads(args[-1])
        if "getProfile" in args:return {"historyId":"101"}
        if "list" in args:return {"messages":[]}
        assert params.get("format")=="minimal"
        return {"internalDate":str(int((NOW-timedelta(days=10)).timestamp()*1000))}
    monkeypatch.setattr(sources,"_gws_bridge",bridge)
    assert sources._collect_email_candidates(now=NOW,lookback_days=1)["emails"]==[]
    assert store.pending_gmail_message_ids()==[]
    assert scalar("SELECT count(*) FROM gmail_unreadable")==0






@pytest.mark.parametrize("field,value",[("enabled_by",""),("enabled_at",None),("enabled_at","")])
def test_inconsistent_enabled_rows_are_not_grants(db,field,value):
    grant()
    conn=store._connect()
    conn.execute(f"UPDATE hermes_tingle_source_optins SET {field}=? WHERE kind='email'",(value,))
    conn.commit();conn.close()
    assert not store.source_enabled("email")


def test_minimal_timestamp_404_is_quarantined_not_transient(db,monkeypatch):
    store.enable_source("email",lookback_days=1,enabled_by="synthetic owner")
    bind_synthetic_email(store)
    store.stage_gmail_sync(history_id="100",message_ids=["synthetic-missing"])
    def bridge(*args,**kwargs):
        params=json.loads(args[-1])
        if "getProfile" in args:return {"historyId":"101"}
        if "list" in args:return {"messages":[]}
        assert params.get("format")=="minimal"
        raise sources.SourceReadError("synthetic 404 notFound")
    monkeypatch.setattr(sources,"_gws_bridge",bridge)
    result=sources._collect_email_candidates(now=NOW,lookback_days=1)
    assert result["emails"]==[]
    quarantined=store.quarantined_gmail_messages()
    assert len(quarantined)==1 and quarantined[0]["last_error_kind"]=="not_found"
    assert scalar("SELECT count(*) FROM gmail_unreadable")==1


def test_bounded_success_fetches_one_timestamp_then_rich_metadata(db,monkeypatch):
    store.enable_source("email",lookback_days=1,enabled_by="synthetic owner")
    bind_synthetic_email(store)
    store.stage_gmail_sync(history_id="100",message_ids=["synthetic-current"])
    formats=[]
    def bridge(*args,**kwargs):
        params=json.loads(args[-1])
        if "getProfile" in args:return {"historyId":"101"}
        if "list" in args:return {"messages":[]}
        formats.append(params["format"])
        if params["format"]=="minimal":return {"internalDate":str(int(NOW.timestamp()*1000))}
        return {"id":"synthetic-current","threadId":"synthetic-thread","labelIds":["INBOX"],
                "internalDate":str(int(NOW.timestamp()*1000)),"snippet":"synthetic current",
                "payload":{"headers":[]}}
    monkeypatch.setattr(sources,"_gws_bridge",bridge)
    def helper(argv, **kwargs):
        request=json.loads(argv[-1])
        assert request['mode']=='metadata' and request['profile']=='standing'
        metadata=bridge('gmail','users','messages','get','--params',json.dumps({'format':'metadata'}))
        return {'messages':[metadata],'errors':[],'http_calls':1,'inner_calls':1}
    monkeypatch.setattr(sources,'_run_json',helper)
    result=sources._collect_email_candidates(now=NOW,lookback_days=1)
    assert len(result["emails"])==1
    assert formats==["minimal","metadata"]
