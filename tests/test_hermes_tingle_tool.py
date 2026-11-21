from delivery_fakes import bind_synthetic_email
from delivery_fakes import seed_outbox_items
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hermes_tingle_store as store
import hermes_tingle_sources
import pytest
import hermes_tingle
from schemas import HERMES_TINGLE


class _FakePlatform:
    def __init__(self, value="telegram"):
        self.value = value


class _FakeSource:
    """Minimal SessionSource stand-in for command-dispatch sender checks."""

    def __init__(self, *, user_id="424242", chat_type="dm", is_bot=False, platform="telegram",
                 chat_id="1001"):
        self.user_id = user_id
        self.chat_type = chat_type
        self.is_bot = is_bot
        self.platform = _FakePlatform(platform)
        self.chat_id = chat_id


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "hermes-tingle.db"

    def connect(_name: str):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(store, "plugin_db", connect)
    store.enable_source("email", enabled_by="synthetic fixture consent")
    bind_synthetic_email(store)
    store.enable_source("calendar", enabled_by="synthetic fixture consent")
    return store


@pytest.mark.parametrize("field", ["title", "summary"])
def test_configured_ticket_pattern_excludes_matching_content(isolated_db, field):
    args = {"action": "add", "title": "Example reminder", field: "Discuss TASK-42"}
    result = json.loads(hermes_tingle.handle_tool(None, args))
    assert result["success"] and result.get("skipped") is True
    assert isolated_db.list_items() == []


@pytest.mark.parametrize('field', ['title', 'why'])
def test_excluded_ticket_cannot_be_added_through_goal_step(isolated_db, field):
    call = lambda **args: json.loads(hermes_tingle.handle_tool(None, args))
    goal = call(action='add', title='Personal task')['item']['id']
    step = {'title': 'Collect document', 'why': 'Needed for goal'}
    step[field] = 'Check TASK-42'
    result = call(action='add_step', item_id=goal, **step)
    assert not result['success']
    assert not isolated_db.get_item(goal).get('steps')


def test_update_cannot_introduce_excluded_work_reference(isolated_db):
    call = lambda **args: json.loads(hermes_tingle.handle_tool(None, args))
    item = call(action='add', title='Personal follow-up', source_type='manual')['item']
    result = call(action='update', item_id=item['id'], summary='Check TASK-42')
    assert result['success'] is False
    assert isolated_db.get_item(item['id'])['summary'] != 'Check TASK-42'


def test_lifecycle_shows_order_and_wait_completion_never_closes_goal(isolated_db):
    call = lambda **args: json.loads(hermes_tingle.handle_tool(None, args))
    goal = call(action="add", title="Secure the document", summary="Needed for filing")["item"]
    wait = call(action="add", title="Wait for reply", status="waiting", owner="other", source_type="email", source_ref="gmail:thread#sent", completion_signal="source_email_reply_received", next_check_at="2026-12-01")['item']
    first = call(action="add_step", item_id=goal['id'], title="Send request", why="Ask for the document")
    assert first['success']
    assert call(action="add_step", item_id=goal['id'], title="Wait for answer", why="Need their response", wait_item_id=wait['id'])['success']
    assert call(action="add_step", item_id=goal['id'], title="File document", why="Finish filing")['success']
    text = hermes_tingle.handle_command(None, "list")
    assert "Send request" in text and "Ask for the document" in text
    assert not call(action="complete", item_id=goal['id'])['success']
    assert call(action="complete_step", item_id=goal['id'], step_position=1)['success']
    text = hermes_tingle.handle_command(None, "list")
    assert "Wait for answer" in text and "Need their response" in text
    assert call(action="complete", item_id=wait['id'])['success']
    assert isolated_db.get_item(goal['id'])['status'] == 'open'
    text = hermes_tingle.handle_command(None, "list")
    assert "File document" in text and "Finish filing" in text
    assert call(action="complete_step", item_id=goal['id'], step_position=3)['success']
    assert isolated_db.get_item(goal['id'])['status'] == 'open'
    assert call(action="complete", item_id=goal['id'])['success']


def test_lifecycle_rejects_ambiguous_links_and_closure_paths(isolated_db):
    call = lambda **args: json.loads(hermes_tingle.handle_tool(None, args))
    goal = call(action='add', title='Goal')['item']['id']
    second = call(action='add', title='Other goal')['item']['id']
    wait = call(action='add', title='Wait', status='waiting', owner='other', source_type='email', source_ref='gmail:t#s', completion_signal='source_email_reply_received', next_check_at='2026-12-01')['item']['id']
    assert call(action='add_step', item_id=goal, title='Wait', why='Need reply', wait_item_id=wait)['success']
    assert not call(action='add_step', item_id=second, title='Wait again', why='Need reply', wait_item_id=wait)['success']
    assert not call(action='add_step', item_id=wait, title='Nested', why='No nested goals')['success']
    assert not call(action='add_step', item_id=second, title='Self', why='No cycles', wait_item_id=second)['success']
    assert not call(action='complete_step', item_id=goal, step_position=1)['success']
    assert not call(action='update', item_id=goal, status='done')['success']
    assert not call(action='dismiss', item_id=goal)['success']
    assert not call(action='update', item_id=wait, completion_signal='')['success']


def test_lifecycle_cannot_dismiss_completed_wait_under_closed_goal(isolated_db):
    call = lambda **args: json.loads(hermes_tingle.handle_tool(None, args))
    goal = call(action='add', title='Goal')['item']['id']
    wait = call(action='add', title='Wait', status='waiting', owner='other', source_type='email', source_ref='gmail:t#s', completion_signal='source_email_reply_received', next_check_at='2026-12-01')['item']['id']
    assert call(action='add_step', item_id=goal, title='Wait', why='Need reply', wait_item_id=wait)['success']
    assert call(action='complete', item_id=wait)['success']
    assert call(action='complete', item_id=goal)['success']
    assert not call(action='dismiss', item_id=wait)['success']
    assert isolated_db.get_item(wait)['status'] == 'done'
    assert isolated_db.get_item(goal)['steps'][0]['complete'] is True


def test_lifecycle_preserves_distinct_sources_and_rejects_conflicting_duplicate(isolated_db):
    call = lambda **args: json.loads(hermes_tingle.handle_tool(None, dict(review_plan=dict(actor='unknown', outcome='', basis='unknown', review_at=None, review_needed=True), **args)))
    a = call(action='add', title='Get paperwork', source_type='email', source_ref='gmail:'+'a'*64+'/a#anchor')['item']
    b = call(action='add', title='Get paperwork', source_type='email', source_ref='gmail:'+'a'*64+'/b#anchor')['item']
    assert a['id'] != b['id']
    conflicting = call(action='add', title='Different responsibility', source_type='email', source_ref='gmail:'+'a'*64+'/a#anchor')
    assert not conflicting['success']
    same_title_other_context = call(action='add', title='Get paperwork', summary='Different request', source_type='email', source_ref='gmail:'+'a'*64+'/a#anchor')
    assert not same_title_other_context['success']
    assert isolated_db.get_item(a['id'])['source_ref'] == 'gmail:'+'a'*64+'/a#anchor'
    assert isolated_db.get_item(b['id'])['source_ref'] == 'gmail:'+'a'*64+'/b#anchor'


def test_lifecycle_baseline_reopen_preserves_steps_and_unique_source(isolated_db, tmp_path):
    import importlib.util
    call = lambda **args: json.loads(hermes_tingle.handle_tool(None, args))
    goal = call(action='add', title='Goal', source_type='conversation', source_ref='session:goal')['item']
    wait = call(action='add', title='Reply', status='waiting', owner='other', source_type='email', source_ref='gmail:thread#sent', completion_signal='source_email_reply_received', next_check_at='2026-12-01')['item']
    assert call(action='add_step', item_id=goal['id'], title='Wait', why='Await reply', wait_item_id=wait['id'])['success']
    db_path = tmp_path / 'hermes-tingle.db'
    before = sqlite3.connect(db_path)
    steps = before.execute('SELECT * FROM hermes_tingle_steps').fetchall()
    index = before.execute("SELECT sql FROM sqlite_master WHERE name='idx_hermes_tingle_items_source_ref'").fetchone()[0]
    before.close()
    spec = importlib.util.spec_from_file_location('baseline_tingle_store', Path(__file__).resolve().parent / 'fixtures' / 'baseline_store.py')
    old = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(old)
    old.plugin_db = lambda _: sqlite3.connect(db_path)
    assert old.get_item(goal['id'])['title'] == 'Goal'
    assert old.get_item(wait['id'])['source_ref'] == 'gmail:thread#sent'
    after = sqlite3.connect(db_path)
    assert after.execute('SELECT * FROM hermes_tingle_steps').fetchall() == steps
    assert after.execute("SELECT sql FROM sqlite_master WHERE name='idx_hermes_tingle_items_source_ref'").fetchone()[0] == index
    with pytest.raises(sqlite3.IntegrityError):
        after.execute("UPDATE hermes_tingle_items SET source_type='email', source_ref='gmail:thread#sent' WHERE id=?", (goal['id'],))
    after.close()
    assert call(action='get', item_id=goal['id'])['item']['steps'][0]['wait_item_id'] == wait['id']


def test_lifecycle_command_handles_guard_and_paginates(isolated_db):
    goal = isolated_db.add_item(title='Important goal', due_at='2026-01-01')
    for i in range(25):
        isolated_db.add_item(title=f'Other item {i:02d}')
    isolated_db.add_step(goal['id'], title='First action', why='Reason')
    assert 'finish all goal steps' in hermes_tingle.handle_command(None, f"done {goal['id']}").lower()
    first = hermes_tingle.handle_command(None, 'list')
    second = hermes_tingle.handle_command(None, 'list 2')
    assert 'First action' in first and 'Reason' in first
    assert 'page 2' in first.lower()
    assert 'Other item' in second and goal['id'] not in second
    page = json.loads(hermes_tingle.handle_tool(None, {'action': 'list', 'limit': 10, 'offset': 10}))
    assert page['success'] and len(page['items']) == 10 and page['has_more']


def test_tool_add_then_list_returns_cross_session_item(isolated_db):
    added = json.loads(hermes_tingle.handle_tool(None, {
        "action": "add",
        "title": "Send the signed form",
        "source_type": "conversation",
        "source_ref": "session:old:message:3",
        "due_at": "2026-11-20",
        "completion_signal": "source_email_trashed",
    }))
    listed = json.loads(hermes_tingle.handle_tool(None, {"action": "list"}))

    assert added["success"] is True
    assert added["item"]["id"].startswith("HT-")
    assert listed["items"][0]["id"] == added["item"]["id"]
    assert listed["items"][0]["completion_signal"] == "source_email_trashed"


def test_tool_list_exposes_profile_timezone_to_the_model(tmp_path, synthetic_settings, isolated_db):
    (tmp_path / "hermes-tingle.json").write_text(json.dumps({**synthetic_settings, "timezone": "Asia/Tokyo"}))
    result = json.loads(hermes_tingle.handle_tool(None, {"action": "list"}))
    assert result["timezone"] == "Asia/Tokyo"


def test_tool_adds_context_timed_reply_watch(isolated_db):
    result = json.loads(hermes_tingle.handle_tool(None, {
        "action": "add",
        "title": "Follow up with a colleague about the document",
        "status": "waiting",
        "owner": "other",
        "source_type": "email",
        "source_ref": "gmail:thread-1#sent-message-1",
        "source_url": "https://mail.google.com/mail/u/0/#all/thread-1",
        "completion_signal": "source_email_reply_received",
        "next_check_at": "2026-12-01T09:00:00+01:00",
    }))

    assert result["success"] is True
    item = result["item"]
    assert item["status"] == "waiting"
    assert item["owner"] == "other"
    assert item["completion_signal"] == "source_email_reply_received"
    assert item["source_ref"] == "gmail:thread-1#sent-message-1"
    assert item["next_check_at"] == "2026-12-01T08:00:00+00:00"


def test_tool_does_not_create_configured_work_items(isolated_db):
    result = json.loads(hermes_tingle.handle_tool(None, {
        "action": "add",
        "title": "Update the delivery ticket",
        "source_type": "manual",
        "source_ref": "https://tracker.example.invalid/TASK-123",
    }))

    assert result["success"] is True
    assert result["skipped"] is True
    assert "excluded" in result["reason"]
    assert isolated_db.list_items() == []


@pytest.mark.parametrize("fields", [
    {"workstream": "wOrK AlPhA"},
    {"source_ref": "https://tracker.example.invalid/items/12"},
    {"source_ref": "event:TASK-123"},
    {"source_url": "https://example.invalid/TASK-456"},
    {"source_ref": "private-work:event"},
])
def test_configured_work_exclusions_block_add(isolated_db, fields):
    args = {"action": "add", "title": "Prepare slides", "source_type": "calendar"}
    args.update(fields)
    result = json.loads(hermes_tingle.handle_tool(None, args))
    assert result["success"] is True and result["skipped"] is True
    assert isolated_db.list_items() == []


@pytest.mark.parametrize("invalid", [
    None,
    {"ticket_exclude_patterns": ["["]},
    {"workstream_exclude_values": None},
    {"source_exclude_substrings": [""]},
    {"calendar_exclude_substrings": None},
    {"calendar_workstream_aliases": {"lab": "Not Excluded"}},
    {"calendar_workstream_aliases": {"": "Work Alpha"}},
    {"calendar_workstream_aliases": ["lab"]},
    {"delivery": "telegram:invalid"},
    {"timezone": "Not/A_Timezone"},
    {"daily_schedule": ""},
    {"date_only_time": None},
    {"date_only_time": "24:00"},
    {"date_only_time": "9:10"},
    {"availability_start": "12:00", "availability_end": "12:00"},
    {"availability_start": "22:00", "availability_end": "06:00"},
    {"availability_end": "17:60"},
    {"deadline_poll_minutes": True},
    {"deadline_poll_minutes": 0},
    {"deadline_poll_minutes": 1441},
    {"deadline_poll_minutes": "30"},
])
def test_missing_or_invalid_policy_blocks_add_before_database_write(tmp_path, synthetic_settings, isolated_db, invalid):
    path = tmp_path / "hermes-tingle.json"
    if invalid is None:
        path.unlink()
    else:
        path.write_text(json.dumps({**synthetic_settings, **invalid}))
    result = json.loads(hermes_tingle.handle_tool(None, {"action": "add", "title": "Ordinary reminder"}))
    assert result["success"] is False
    assert isolated_db.list_items() == []


def test_retired_behavior_capture_command_is_unknown_and_read_only(isolated_db):
    result = hermes_tingle.handle_command(None, "behavior status")
    assert "didn't catch that command" in result
    conn = store._connect()
    try:
        assert not conn.execute("SELECT 1 FROM plugin_meta WHERE key='behavior_cases_enabled'").fetchone()
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='private_behavior_cases'").fetchone()
    finally:
        conn.close()


def test_retired_external_consumer_modules_are_not_shipped():
    root = Path(__file__).resolve().parents[1]
    assert not (root / "hermes_tingle_behavior.py").exists()
    assert not (root / "hermes_tingle_diagnostics.py").exists()
    assert not (root / "hermes_tingle_digest.py").exists()


def test_slash_command_marks_item_done_across_sessions(isolated_db):
    item = isolated_db.add_item(title="Call the dentist", now="2026-11-14T10:00:00+01:00")

    result = hermes_tingle.handle_command(None, f"done {item['id']}")

    assert " done:" in result.lower()
    assert f"`{item['id']}`" in result
    assert isolated_db.get_item(item["id"])["status"] == "done"


def test_slash_command_formats_ids_as_copyable_code(isolated_db):
    done_item = isolated_db.add_item(title="Finish the form")
    dismissed_item = isolated_db.add_item(title="Archive the note")
    snoozed_item = isolated_db.add_item(title="Check the reply")

    listed = hermes_tingle.handle_command(None, "")
    done = hermes_tingle.handle_command(None, f"done {done_item['id']}")
    dismissed = hermes_tingle.handle_command(None, f"dismiss {dismissed_item['id']}")
    snoozed = hermes_tingle.handle_command(None, f"snooze {snoozed_item['id']} 2d")

    assert f"`{done_item['id']}`" in listed
    assert f"`{dismissed_item['id']}`" in listed
    assert f"`{snoozed_item['id']}`" in listed
    assert f"`{done_item['id']}`" in done
    assert f"`{dismissed_item['id']}`" in dismissed
    assert f"`{snoozed_item['id']}`" in snoozed


def test_slash_command_formats_dates_in_local_time_without_timezone(isolated_db):
    isolated_db.add_item(
        title="Call a colleague",
        due_at="2026-11-17T16:00:00+00:00",
        now="2026-11-15T10:00:00+01:00",
    )

    listed = hermes_tingle.handle_command(None, "list")

    assert "Tue, 17 November 2026 at 16:00" in listed
    assert "CET" not in listed
    assert "CEST" not in listed
    assert "2026-11-17T16:00:00+00:00" not in listed


@pytest.mark.parametrize("zone, expected", [
    ("America/New_York", "Sun, 8 March 2026 at 03:30"),
    ("Asia/Tokyo", "Sun, 8 March 2026 at 16:30"),
])
def test_slash_command_uses_profile_timezone(tmp_path, synthetic_settings, isolated_db, zone, expected):
    (tmp_path / "hermes-tingle.json").write_text(json.dumps({**synthetic_settings, "timezone": zone}))
    isolated_db.add_item(title="Task", due_at="2026-03-08T07:30:00Z", now="2026-03-01T00:00:00Z")
    assert expected in hermes_tingle.handle_command(None, "list")


def test_slash_command_renders_a_styled_active_list(isolated_db):
    isolated_db.add_item(
        title="Send the signed form",
        status="open",
        due_at="2026-11-17T16:00:00+00:00",
        now="2026-11-15T10:00:00+01:00",
    )
    waiting = isolated_db.add_item(title="Check the reply", status="waiting")
    blocked = isolated_db.add_item(title="Approve the draft", status="blocked")

    listed = hermes_tingle.handle_command(None, "list")

    assert listed.startswith(f"📌 *Hermes Tingle* · v{hermes_tingle.VERSION} — Unresolved commitments")
    assert "📌 *Send the signed form*" in listed
    assert "📌 *Check the reply*" in listed
    assert "📌 *Approve the draft*" in listed
    assert f"`{waiting['id']}` · Waiting" in listed
    assert f"`{blocked['id']}` · Blocked" in listed
    assert "📅 Due: Tue, 17 November 2026 at 16:00" in listed


def test_slash_command_list_omits_thread_check_label(isolated_db):
    """Checkpoint items keep their check time but drop the '(thread check required)' label."""
    isolated_db.add_item(
        title="Review the synthetic form",
        source_type="email",
        source_ref="gmail:a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf/thread#anchor",
        completion_signal="source_email_checkpoint",
        next_check_at="2026-11-21T09:00:00+00:00",
    )

    listed = hermes_tingle.handle_command(None, "list")

    assert "🔎 Check: Sat, 21 November 2026 at 09:00" in listed
    assert "thread check required" not in listed


def test_slash_command_formats_empty_list_with_a_clear_state(isolated_db):
    listed = hermes_tingle.handle_command(None, "list")

    assert "All clear" in listed
    assert "Tell Hermes one real item" in listed
    assert "shared profile context" in listed


def test_slash_command_empty_list_stays_concise(isolated_db):
    listed = hermes_tingle.handle_command(None, "list")

    assert len(listed) < 500
    assert "Google Workspace helper installed" not in listed
    assert "--activate" not in listed and "/tingle setup" not in listed
    assert "/tingle sources" in listed and "/tingle lookback" in listed


def test_all_clear_status_names_the_running_version(isolated_db):
    listed = hermes_tingle.handle_command(None, "list")

    assert "All clear" in listed
    assert f"v{hermes_tingle.VERSION}" in listed


def test_unresolved_status_names_the_running_version(isolated_db):
    isolated_db.add_item(title="Synthetic task", status="open")

    listed = hermes_tingle.handle_command(None, "list")

    assert listed.startswith(f"📌 *Hermes Tingle* · v{hermes_tingle.VERSION} —")


def test_running_version_matches_plugin_manifest():
    """The loaded-module constant and plugin.yaml must move together, or the
    freshness signal would disagree with the installed metadata."""
    manifest = (Path(__file__).resolve().parents[1] / "plugin.yaml").read_text()
    match = re.search(r'^version:\s*"?([0-9]+\.[0-9]+\.[0-9]+)"?\s*$', manifest, re.M)
    assert match, "plugin.yaml carries no semantic version"
    assert hermes_tingle.VERSION == match.group(1)


def test_slash_command_does_not_echo_unparseable_due_dates(isolated_db, monkeypatch):
    monkeypatch.setattr(hermes_tingle.store, "list_items", lambda **kwargs: [
        {"id": "HT-ABC123", "status": "open", "title": "Call a colleague", "due_at": "not-a-date"}
    ])

    listed = hermes_tingle.handle_command(None, "list")

    assert "date unavailable" in listed
    assert "not-a-date" not in listed


def test_tool_reads_calendar_availability_without_mutation(monkeypatch):
    captured = {}
    availability = {"coverage_complete": True, "candidate_results": [{"start": "17:00", "end": "17:30", "available": False, "conflicts": []}], "free_windows": []}

    def fake_check(date_value, candidate_starts, duration_minutes):
        captured.update(date=date_value, starts=candidate_starts, duration=duration_minutes)
        return availability

    monkeypatch.setattr(hermes_tingle_sources, "check_calendar_availability", fake_check)
    result = json.loads(hermes_tingle.handle_tool(None, {
        "action": "check_calendar_availability",
        "date": "2026-11-17",
        "candidate_starts": ["17:00"],
        "duration_minutes": 30,
    }))

    assert result == {"success": True, "availability": availability}
    assert captured == {"date": "2026-11-17", "starts": ["17:00"], "duration": 30}


def test_tool_schema_describes_configured_exclusions_without_personal_defaults():
    assert "configured work exclusions" in HERMES_TINGLE["description"]
    assert "configured" in HERMES_TINGLE["parameters"]["properties"]["workstream"]["description"]


def test_schema_describes_profile_timezone_without_personal_default():
    assert "configured timezone" in HERMES_TINGLE["description"]
    assert "configured timezone" in HERMES_TINGLE["parameters"]["properties"]["date"]["description"]
    assert "configured date-only time" in HERMES_TINGLE["parameters"]["properties"]["due_at"]["description"]
    assert "09:10" not in HERMES_TINGLE["parameters"]["properties"]["due_at"]["description"]


def test_tool_description_requests_copyable_ids_in_telegram():
    assert HERMES_TINGLE["name"] == "hermes_tingle"
    assert "inline code" in HERMES_TINGLE["description"]
    assert "human-readable dates in the configured timezone" in HERMES_TINGLE["description"]
    assert "never output raw ISO timestamps" in HERMES_TINGLE["description"]
    assert "light triage" in HERMES_TINGLE["description"]
    assert "no global body-count cap" in HERMES_TINGLE["description"]
    assert "Never conclude a relevant email has no URL from metadata/snippets alone" in HERMES_TINGLE["description"]
    assert "capture a pending feedback candidate only from a direct user correction" in HERMES_TINGLE["description"]
    assert "only the user's explicit /tingle rule approve|dismiss command decides" in HERMES_TINGLE["description"]
    assert "check_calendar_availability" in HERMES_TINGLE["parameters"]["properties"]["action"]["enum"]
    assert "candidate_starts" in HERMES_TINGLE["parameters"]["properties"]
    assert "date" in HERMES_TINGLE["parameters"]["properties"]
    assert "no Calendar writes" in HERMES_TINGLE["description"]
    assert "matching scheduled call as confirmation, not a conflict" in HERMES_TINGLE["description"]
    assert "completion_signal" in HERMES_TINGLE["parameters"]["properties"]
    completion_values = HERMES_TINGLE["parameters"]["properties"]["completion_signal"]["enum"]
    assert "source_email_reply_received" in completion_values
    assert "per-thread timing from context" in HERMES_TINGLE["parameters"]["properties"]["next_check_at"]["description"]


def test_tool_accepts_ht_and_legacy_alias_prefix():
    assert hermes_tingle._ID_RE.fullmatch("HT-1A2B3C")
    assert hermes_tingle._ID_RE.fullmatch("OL-1A2B3C")


def test_tool_source_scan_returns_bounded_untrusted_snapshot(isolated_db, monkeypatch):
    snapshot = {"emails": [{"source_id": "m-1", "untrusted_source": True}], "calendar_events": [], "errors": []}
    monkeypatch.setattr(hermes_tingle_sources, "collect_snapshot", lambda **kwargs: snapshot)

    result = json.loads(hermes_tingle.handle_tool(None, {"action": "scan_sources", "lookback_days": 14}))

    assert result["success"] is True
    assert result["snapshot"] == snapshot


def test_tool_missing_config_does_not_claim_or_read_email(monkeypatch):
    def missing():
        raise ValueError('Tingle config missing or invalid')

    def forbidden(*args, **kwargs):
        raise AssertionError('email was claimed or fetched without config')

    monkeypatch.setattr(hermes_tingle, 'load_settings', missing)
    monkeypatch.setattr(hermes_tingle.store, 'claim_email_body_read', forbidden)
    monkeypatch.setattr(hermes_tingle_sources, 'read_email', forbidden)
    result = json.loads(hermes_tingle.handle_tool(None, {'action': 'read_email', 'source_id': 'm-1'}))
    assert result['success'] is False
    assert 'config missing' in result['error']


def test_tool_reads_only_requested_email_and_marks_it_untrusted(monkeypatch):
    email = {"id": "m-1", "body": "Please reply by Friday", "untrusted_source": True}
    monkeypatch.setattr(hermes_tingle.store, "claim_email_body_read", lambda source_id: source_id == "m-1")
    monkeypatch.setattr(hermes_tingle_sources, "read_email", lambda message_id: email)

    result = json.loads(hermes_tingle.handle_tool(None, {"action": "read_email", "source_id": "m-1"}))

    assert result["success"] is True
    assert result["email"]["untrusted_source"] is True


def test_tool_rejects_unscanned_email_body_before_fetch(monkeypatch):
    monkeypatch.setattr(hermes_tingle.store, "claim_email_body_read", lambda source_id: False)
    monkeypatch.setattr(hermes_tingle_sources, "_google_api", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not fetch")))

    result = json.loads(hermes_tingle.handle_tool(None, {"action": "read_email", "source_id": "outside-scan"}))

    assert result["success"] is False
    assert "scan candidate" in result["error"]


def test_tool_mark_notified_prepares_but_does_not_confirm_delivery(isolated_db):
    item = isolated_db.add_item(
        title="Reply to landlord",
        next_check_at="2026-11-14T12:00:00+01:00",
        now="2026-11-14T11:00:00+01:00",
    )

    result = json.loads(hermes_tingle.handle_tool(None, {"action": "mark_notified", "item_ids": [item["id"]], 'expected_versions':{i:hermes_tingle.store.get_item(i)['reminder_revision'] for i in [item["id"]]}}))

    assert result["success"] is True
    assert result["items"][0]["reminder_count"] == 0
    assert result["items"][0]["last_notified_at"] is None
    receipt = result["items"][0]["receipt_command"].split()[-1]
    assert isolated_db.acknowledge_alert_emissions(receipt) == 1
    assert isolated_db.get_item(item["id"])["reminder_count"] == 1


def test_tool_feedback_candidates_need_explicit_approval(isolated_db):
    captured = json.loads(hermes_tingle.handle_tool(None, {
        "action": "capture_feedback",
        "feedback_text": "For relevant email threads, don't claim a link is absent from a snippet.",
        "feedback_category": "links",
    }))

    assert captured["success"] is True
    candidate = captured["feedback"]
    assert candidate["status"] == "pending"
    assert isolated_db.approved_feedback_rules() == []

    pending = json.loads(hermes_tingle.handle_tool(None, {"action": "list_feedback"}))
    assert pending["feedback"][0]["id"] == candidate["id"]

    # the model can never approve: the tool refuses and the candidate stays pending
    refused = json.loads(hermes_tingle.handle_tool(None, {
        "action": "resolve_feedback",
        "feedback_id": candidate["id"],
        "feedback_decision": "approved",
    }))
    assert refused["success"] is False
    assert isolated_db.approved_feedback_rules() == []
    assert isolated_db.list_feedback_candidates(status="pending")[0]["id"] == candidate["id"]

    # only the owner's explicit command approves, with the dispatch principal bound
    import json as _json
    from pathlib import Path as _Path
    import hermes_constants
    home = _Path(hermes_constants.get_hermes_home())
    settings = _json.loads((home / "hermes-tingle.json").read_text())
    settings["owner_principals"] = ["telegram:424242"]
    (home / "hermes-tingle.json").write_text(_json.dumps(settings))
    owner = _FakeSource(user_id="424242", chat_type="dm")
    reply = hermes_tingle.handle_command(None, f"rule approve {candidate['id']}", source=owner)
    assert "Approved" in reply
    rule = isolated_db.approved_feedback_rules()[0]
    assert rule["id"] == candidate["id"]
    assert rule["decided_by"] == "telegram:424242:1001"


def test_feedback_schema_keeps_decisions_off_the_model():
    actions = HERMES_TINGLE["parameters"]["properties"]["action"]["enum"]
    assert {"capture_feedback", "list_feedback"} <= set(actions)
    assert "resolve_feedback" not in actions


def test_snooze_command_rejects_an_unknown_id_without_raising(isolated_db):
    result = hermes_tingle.handle_command(None, "snooze HT-1A2B3C 2d")

    assert "not found" in result.lower()


def test_snooze_command_rejects_a_past_date_without_raising(isolated_db):
    item = isolated_db.add_item(title="Water the plants")

    result = hermes_tingle.handle_command(None, f"snooze {item['id']} 2020-01-01T09:00:00+00:00")

    assert "future" in result.lower()
    assert isolated_db.get_item(item["id"])["snoozed_until"] is None


def test_snooze_command_rejects_a_garbage_duration_without_raising(isolated_db):
    item = isolated_db.add_item(title="Water the plants")

    result = hermes_tingle.handle_command(None, f"snooze {item['id']} whenever")

    assert "ISO-8601" in result
    assert isolated_db.get_item(item["id"])["snoozed_until"] is None


def test_snooze_command_rejects_a_closed_item_without_raising(isolated_db):
    item = isolated_db.add_item(title="Water the plants")
    isolated_db.complete_item(item["id"])

    result = hermes_tingle.handle_command(None, f"snooze {item['id']} 2d")

    assert "not found or already closed" in result


def test_snooze_command_rejects_a_zero_duration_without_raising(isolated_db):
    item = isolated_db.add_item(title="Water the plants")

    result = hermes_tingle.handle_command(None, f"snooze {item['id']} 0m")

    assert "future" in result.lower()


def test_snooze_tool_action_reports_errors_as_json(isolated_db):
    item = isolated_db.add_item(title="Water the plants")

    past = json.loads(hermes_tingle.handle_tool(None, {
        "action": "snooze", "item_id": item["id"], "until": "2020-01-01",
    }))
    garbage = json.loads(hermes_tingle.handle_tool(None, {
        "action": "snooze", "item_id": item["id"], "until": "whenever",
    }))
    unknown = json.loads(hermes_tingle.handle_tool(None, {
        "action": "snooze", "item_id": "HT-1A2B3C", "until": "2099-01-01",
    }))

    assert past["success"] is False and "future" in past["error"].lower()
    assert garbage["success"] is False and "ISO-8601" in garbage["error"]
    assert unknown["success"] is False and "not found" in unknown["error"].lower()
    assert isolated_db.get_item(item["id"])["snoozed_until"] is None


def test_successful_reply_does_not_acknowledge_emitted_reminders(isolated_db):
    # Acting on an item is not evidence that any reminder batch arrived: the ID
    # can come from /tingle list. Only an explicit /tingle ack sets acked_at.
    store.record_alert_emission(seed_outbox_items(store, ["HT-F00D11"], now="2020-01-01T00:00:00+00:00"), now="2020-01-01T00:00:00+00:00")
    item = store.add_item(title="Synthetic task")
    result = hermes_tingle.handle_command(None, f"done {item['id']}")
    assert "Marked" in result
    assert len(store.unsurfaced_emissions(older_than="2999-01-01T00:00:00+00:00")) == 1


def test_successful_reply_records_interaction_without_asserting_receipt(isolated_db):
    item = store.add_item(title="Synthetic task")
    hermes_tingle.handle_command(None, f"done {item['id']}")
    conn = store.plugin_db("hermes-tingle")
    row = conn.execute("SELECT value FROM plugin_meta WHERE key='last_command_interaction_at'").fetchone()
    conn.close()
    assert row and row["value"]


def test_explicit_ack_confirms_only_identified_flushed_batch(isolated_db, tmp_path):
    settings_path = tmp_path / "hermes-tingle.json"
    settings = json.loads(settings_path.read_text())
    settings["owner_principals"] = ["telegram:424242"]
    settings_path.write_text(json.dumps(settings))
    source = _FakeSource()
    one = store.record_alert_emission(seed_outbox_items(store, ["HT-F00D11"]))
    two = store.record_alert_emission(seed_outbox_items(store, ["HT-B00B22"]))
    store.mark_emission_flushed(two)
    assert "generic acknowledgment" in hermes_tingle.handle_command(None, "ack", source=source)
    assert store.get_alert_emission(one)["acked_at"] is None
    assert store.get_alert_emission(two)["acked_at"] is None
    token = store.get_alert_emission(two)["receipt_token"]
    assert "Acknowledged that" in hermes_tingle.handle_command(None, "ack " + token, source=source)
    assert store.get_alert_emission(one)["acked_at"] is None
    assert "Nothing changed" in hermes_tingle.handle_command(None, "ack " + token, source=source)


def test_failed_reply_does_not_acknowledge_emitted_reminders(isolated_db):
    store.record_alert_emission(seed_outbox_items(store, ["HT-F00D11"], now="2020-01-01T00:00:00+00:00"), now="2020-01-01T00:00:00+00:00")
    result = hermes_tingle.handle_command(None, "done HT-1A2B3C")
    assert "not found" in result.lower()
    assert len(store.unsurfaced_emissions(older_than="2999-01-01T00:00:00+00:00")) == 1


def test_skip_command_skips_a_goal_step(isolated_db):
    goal = store.add_item(title="Synthetic goal", source_type="manual")
    store.add_step(goal["id"], title="Step one", why="Reason")
    result = hermes_tingle.handle_command(None, f"skip {goal['id']} 1")
    assert "Skipped step 1" in result
    assert store.get_item(goal["id"])["steps"][0]["skipped"] is True
    assert "not found" in hermes_tingle.handle_command(None, f"skip {goal['id']} 9").lower()
    assert "Usage" in hermes_tingle.handle_command(None, f"skip {goal['id']} two")
    assert "look like" in hermes_tingle.handle_command(None, "skip nonsense 1")
