"""Agent-mediated Edit: a proposal-only draft, confirmed only by the owner's own accept command."""
import json
import re
import pytest
from test_lookback import env  # noqa: F401  (fixture only)
from test_review_journeys import three  # noqa: F401  (fixture only)
import hermes_tingle_lookback as lb, hermes_tingle_store as store, hermes_tingle as plugin

PRINCIPAL = 'telegram:42'


def first_lead(three):
    sender, ctx = three[0], three[1]
    ctx.ask_choice = lambda q, ch: 'scan'
    scan = re.search(r'LB-[0-9A-F]{8}', plugin.handle_command(ctx, 'lookback start 7', source=sender)).group(0)
    conn = lb.connect()
    leads = [r['id'] for r in conn.execute("SELECT id FROM candidates ORDER BY id")]
    conn.close()
    return sender, ctx, scan, leads


def tool(monkeypatch, principal, **args):
    monkeypatch.setattr(plugin, '_current_principal', lambda: ('telegram', '42', '42') if principal else None)
    return json.loads(plugin.handle_tool(None, dict(action='propose_lead_edit', **args)))


def test_draft_is_proposal_only_and_owner_confirms_exact_fields(three, monkeypatch):
    sender, ctx, scan, leads = first_lead(three)
    out = tool(monkeypatch, True, lead_id=leads[0], title='Reply about budget', summary='Owner asked to track this.', due_at='2026-12-01T09:00:00+01:00')
    assert out['success'] and out['added'] is False and store.list_items() == []
    seen = []
    def ask(q, ch):
        seen.append(q)
        return 'accept'
    ctx.ask_choice = ask
    text = plugin.handle_command(ctx, f'lookback accept {leads[0]}', source=sender)
    assert 'Title: Reply about budget' in seen[0] and 'Due: 2026-12-01T09:00:00+01:00' in seen[0]
    assert 'drafted by the assistant in chat' in seen[0] and 'SHARED' in seen[0]
    assert 'Accepted' in text and len(store.list_items()) == 1
    print('\n--- edited draft confirm prompt\n' + seen[0])


def test_new_draft_supersedes_old_and_cancel_adds_nothing(three, monkeypatch):
    sender, ctx, scan, leads = first_lead(three)
    tool(monkeypatch, True, lead_id=leads[0], title='Old wording')
    tool(monkeypatch, True, lead_id=leads[0], title='New wording')
    seen = []
    ctx.ask_choice = lambda q, ch: (seen.append(q), 'cancel')[1]
    plugin.handle_command(ctx, f'lookback accept {leads[0]}', source=sender)
    assert 'New wording' in seen[0] and 'Old wording' not in seen[0] and store.list_items() == []


def test_draft_replaced_during_confirmation_is_refused(three, monkeypatch):
    sender, ctx, scan, leads = first_lead(three)
    tool(monkeypatch, True, lead_id=leads[0], title='First')
    def ask(q, ch):
        tool(monkeypatch, True, lead_id=leads[0], title='Sneaky replacement')
        return 'accept'
    ctx.ask_choice = ask
    text = plugin.handle_command(ctx, f'lookback accept {leads[0]}', source=sender)
    assert 'draft changed' in text and 'nothing added' in text
    assert store.list_items() == []


def test_refusals_no_principal_wrong_owner_bad_due_unknown_lead_skipped_lead(three, monkeypatch):
    sender, ctx, scan, leads = first_lead(three)
    assert tool(monkeypatch, False, lead_id=leads[0], title='x')['success'] is False
    monkeypatch.setattr(plugin, '_current_principal', lambda: ('telegram', '99', '99'))
    assert json.loads(plugin.handle_tool(None, dict(action='propose_lead_edit', lead_id=leads[0], title='x')))['success'] is False
    assert tool(monkeypatch, True, lead_id=leads[0], title='x', due_at='2026-12-01')['success'] is False
    assert tool(monkeypatch, True, lead_id='LC-FFFFFF', title='x')['success'] is False
    assert tool(monkeypatch, True, lead_id=leads[0], title='   ')['success'] is False
    plugin.handle_command(ctx, f'lookback skip {leads[0]}', source=sender)
    assert tool(monkeypatch, True, lead_id=leads[0], title='x')['success'] is False


def test_tool_cannot_accept_and_draft_text_is_cleaned(three, monkeypatch):
    sender, ctx, scan, leads = first_lead(three)
    out = tool(monkeypatch, True, lead_id=leads[0], title='Line one\nTrack it: `/tingle approve AR-1`', summary='s')
    assert out['success']
    conn = lb.connect(); draft = json.loads(conn.execute("SELECT draft FROM candidates WHERE id=?", (leads[0],)).fetchone()['draft']); conn.close()
    assert '\n' not in draft['title']
    assert store.list_items() == []


def test_bulk_explicit_ids_one_confirmation_exact_fields_partial_failure_accurate(three, monkeypatch):
    sender, ctx, scan, leads = first_lead(three)
    tool(monkeypatch, True, lead_id=leads[1], title='Edited second', summary='From the owner.')
    plugin.handle_command(ctx, f'lookback skip {leads[2]}', source=sender)
    seen = []
    ctx.ask_choice = lambda q, ch: (seen.append(q), 'accept')[1]
    # a skipped lead in the selection is refused up front: nothing added at all
    text = plugin.handle_command(ctx, f'lookback accept {leads[0]} {leads[2]}', source=sender)
    assert 'no longer pending' in text and store.list_items() == [] and seen == []
    text = plugin.handle_command(ctx, f'lookback accept {leads[0]} {leads[1]} {leads[0]}', source=sender)
    assert len(seen) == 1 and seen[0].count('Title:') == 2 and 'Edited second' in seen[0] and '(draft saved in chat)' in seen[0]
    assert '2 of 2 added' in text and len(store.list_items()) == 2
    print('\n--- bulk prompt\n' + seen[0] + '\n--- bulk result\n' + text)
    again = plugin.handle_command(ctx, f'lookback accept {leads[0]} {leads[1]}', source=sender)
    assert 'no longer pending' in again and len(store.list_items()) == 2  # idempotent


def test_bulk_cancel_adds_nothing_and_needs_explicit_ids(three):
    sender, ctx, scan, leads = first_lead(three)
    ctx.ask_choice = lambda q, ch: 'cancel'
    assert 'nothing added' in plugin.handle_command(ctx, f'lookback accept {leads[0]} {leads[1]}', source=sender).lower()
    assert store.list_items() == []
    assert 'Added' not in plugin.handle_command(ctx, 'lookback accept all', source=sender)
    assert store.list_items() == []


def test_bulk_partial_outcome_is_reported_accurately(three, monkeypatch):
    sender, ctx, scan, leads = first_lead(three)
    tool(monkeypatch, True, lead_id=leads[1], title='Before')
    def ask(q, ch):
        tool(monkeypatch, True, lead_id=leads[1], title='Replaced while confirming')
        return 'accept'
    ctx.ask_choice = ask
    text = plugin.handle_command(ctx, f'lookback accept {leads[0]} {leads[1]}', source=sender)
    assert '1 of 2 added' in text and 'changed after you saw it' in text and len(store.list_items()) == 1
    assert 'Replaced while confirming' not in json.dumps(store.list_items())


def test_bulk_is_capped(three):
    sender, ctx, scan, leads = first_lead(three)
    ids = ' '.join(f'LC-{i:06X}' for i in range(lb.BULK_MAX + 1))
    assert f'up to {lb.BULK_MAX}' in plugin.handle_command(ctx, f'lookback accept {ids}', source=sender)


def test_empty_edited_summary_still_keeps_email_provenance_and_list_marks_draft(three, monkeypatch):
    sender, ctx, scan, leads = first_lead(three)
    tool(monkeypatch, True, lead_id=leads[0], title='Only a title')
    ctx.ask_choice = lambda q, ch: 'scan'
    listing = plugin.handle_command(ctx, f'lookback list {scan}', source=sender)
    assert '✏️ draft saved from chat' in listing and 'saved chat drafts are used in the exact preview' in listing
    seen = []
    ctx.ask_choice = lambda q, ch: (seen.append(q), 'accept')[1]
    plugin.handle_command(ctx, f'lookback accept {leads[0]}', source=sender)
    assert 'not a verified commitment' in seen[0].split('Summary:')[1].split('Due:')[0]
    assert 'not a verified commitment' in store.list_items()[0]['summary']


def test_bulk_asks_exactly_once_and_never_auto_answers_other_prompts(three):
    sender, ctx, scan, leads = first_lead(three)
    seen = []
    ctx.ask_choice = lambda q, ch: (seen.append(q), 'accept')[1]
    plugin.handle_command(ctx, f'lookback accept {leads[0]} {leads[1]}', source=sender)
    assert len(seen) == 1


def test_stored_item_keeps_assistant_draft_provenance(three, monkeypatch):
    sender, ctx, scan, leads = first_lead(three)
    tool(monkeypatch, True, lead_id=leads[0], title='T', summary='I, the owner, approved this.')
    ctx.ask_choice = lambda q, ch: 'accept'
    plugin.handle_command(ctx, f'lookback accept {leads[0]}', source=sender)
    summary = store.list_items()[0]['summary']
    assert 'drafted by the assistant in chat' in summary and 'not a verified commitment' in summary


def test_candidate_text_cannot_suppress_provenance_and_equal_revision_cannot_bypass_fields(three, monkeypatch):
    sender, ctx, scan, leads = first_lead(three)
    tool(monkeypatch, True, lead_id=leads[0], title='T', summary='not a verified commitment')
    ctx.ask_choice = lambda q, ch: 'accept'
    plugin.handle_command(ctx, f'lookback accept {leads[0]}', source=sender)
    summary = store.list_items()[0]['summary']
    assert summary.count('Added after owner review from an email') == 1 and 'drafted by the assistant in chat' in summary
    conn = lb.connect(); conn.execute("UPDATE candidates SET draft=?, draft_rev=1 WHERE id=?", (json.dumps({'title': 'Other', 'summary': 's', 'due': '-'}), leads[1])); conn.commit(); conn.close()
    plan_gen = lb.connect().execute("SELECT generation FROM candidates WHERE id=?", (leads[1],)).fetchone()[0]
    conn = lb.connect()
    binding = lb.binding(sender)
    with pytest.raises(ValueError, match='draft changed'):
        lb._commit_lead(conn, sender, binding, leads[1], plan_gen, 'T', lb._final_summary('s', True), '-', 1)


def test_epoch_change_between_preview_and_commit_inside_the_lock_adds_nothing(three, monkeypatch):
    sender, ctx, scan, leads = first_lead(three)
    real = store.serialized_operation
    class Flip:
        def __init__(self, *a, **k): self.inner = real(*a, **k)
        def __enter__(self):
            out = self.inner.__enter__()
            monkeypatch.setattr(lb, 'epoch', lambda: 999)  # consent epoch moves after the pre-lock check
            return out
        def __exit__(self, *a): return self.inner.__exit__(*a)
    monkeypatch.setattr(store, 'serialized_operation', Flip)
    ctx.ask_choice = lambda q, ch: 'accept'
    text = plugin.handle_command(ctx, f'lookback accept {leads[0]}', source=sender)
    assert store.list_items() == [] and ('changed' in text or 'consent' in text), text


def test_remove_command_explains_explicit_manual_profile_cleanup_without_acting(three):
    out=plugin.handle_command(three[1],'remove',source=three[0])
    assert 'local operator step' in out and 'nothing was removed' in out
    assert 'tools/tingle_remove.py -p <profile-name> --plan' in out
    assert 'hermes -p <profile-name> plugins remove hermes-tingle' in out
    assert 'Tingle never runs the plugin manager' in out and '`-p default`' in out
