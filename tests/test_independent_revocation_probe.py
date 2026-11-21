from types import SimpleNamespace
import setup_jobs, hermes_tingle_store as store, hermes_tingle_sources as sources

def test_probe3_revocation_race_real(synthetic_settings, monkeypatch):
    monkeypatch.setattr(setup_jobs, '_first_run_sources_pristine', lambda: True)
    def probe(kind):
        if kind == 'email':
            store.disable_source('email')  # concurrent owner revocation during the probe
        return 'b' * 64
    monkeypatch.setattr(sources, 'probe_source', probe)
    ctx = SimpleNamespace(ask_choice=lambda q, ch: 'all')
    setup_jobs._offer_first_run_sources(ctx, lambda: None, local=False)
    assert store.source_enabled('email') is False, "RACE: mid-probe revocation was overwritten"
