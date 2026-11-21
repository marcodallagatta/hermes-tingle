"""Explicit fake receipt evidence for tests of post-delivery scheduling.

These helpers must never be autouse: no test may silently convert model output
or a flush into delivery. End-to-end uncertainty tests do not use them.
"""
def confirmed_reminder(store, item_ids, *, now=None):
    prepared = store.mark_notified(item_ids, now=now)
    token = prepared[0]['receipt_command'].split()[-1]
    assert store.acknowledge_alert_emissions(token, now=now) == 1
    return [store.get_item(item_id) for item_id in item_ids]


def review_current_source(store, kind, source_id, source_version='', **kwargs):
    # This helper intentionally represents a review of the CURRENT scan grant.
    return store.mark_source_reviewed(kind, source_id, source_version,
                                     source_generation=store.source_generation(kind), **kwargs)


def capture_reservation(monkeypatch, store, target):
    original = store.record_alert_emission
    def record(ids, **kwargs):
        seed_outbox_items(store, ids, now=kwargs.get('now'))
        conn = store._connect()
        try:
            selected = kwargs.get('expected_versions') or {}
            kwargs['expected_versions'] = {item_id: selected[item_id] if selected.get(item_id) is not None else conn.execute('SELECT reminder_revision FROM hermes_tingle_items WHERE id=?', (item_id,)).fetchone()[0] for item_id in ids}
        finally:
            conn.close()
        batch = original(ids, **kwargs)
        target.extend(ids)
        return batch
    monkeypatch.setattr(store, 'record_alert_emission', record)


def seed_outbox_items(store, item_ids, *, now=None):
    """Explicit legacy unit-test fixture, not production behavior.

    Some renderer/outbox tests use fixed synthetic IDs without capture. Give
    those IDs real due rows before exercising the stricter reservation API.
    New end-to-end regressions must use add_item and actual entrypoints instead.
    Never reset an existing row's schedule, budget, status, or revision.
    """
    stamp = store._stamp(now or store._now())
    conn = store._connect()
    try:
        for item_id in item_ids:
            conn.execute("INSERT OR IGNORE INTO hermes_tingle_items(id,title,next_check_at,created_at,updated_at) VALUES(?,?,?,?,?)",
                         (item_id.upper(), 'Synthetic outbox fixture', stamp, stamp, stamp))
        conn.commit()
    finally:
        conn.close()
    return item_ids


def bind_synthetic_email(store):
    """Explicit account provenance for preexisting unit-test caches, never production migration."""
    import json
    store.set_plugin_meta('gmail_account_binding', json.dumps({
        'generation':store.source_generation('email'),
        'account':'a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf'}))


def mock_pending_reader(monkeypatch, read_one):
    """Explicit collector-seam fake. Preserves per-ID outcomes, not transport proof."""
    import hermes_tingle_sources as sources
    def read(ids, *, now, metrics=None):
        for mid in ids:
            try:
                yield mid,read_one(mid,now=now)
            except (sources.SourceReadError,ValueError) as exc:
                yield mid,exc
    monkeypatch.setattr(sources,'_pending_email_metadata',read)
