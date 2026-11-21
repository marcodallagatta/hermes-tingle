"""Blind eval runner: bounds, raw preservation and no unapproved calls. Input is invented."""
import importlib.util, json, os, pathlib, sys, types
import pytest
import hermes_tingle_assess as assess

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / 'scripts' / 'synthetic_eval_blind.py'
ALLOWED_KEYS = {'instructions', 'input', 'json_schema', 'schema_name', 'system_prompt', 'max_tokens', 'timeout', 'purpose'}


def load_mod():
    spec = importlib.util.spec_from_file_location('synthetic_eval_blind', SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def inert_host_bootstrap(monkeypatch):
    """Inert hermes_bootstrap for this module only, plus a fresh hop marker.

    The runner imports the host's supported hermes_bootstrap before claiming
    evidence on every actual run; a missing module fails closed on the pinned
    host, so these tests install an explicit inert module. monkeypatch teardown
    restores sys.modules and the environment, so no other test module (including
    real-host tests) is affected. Each test starts without an inherited hop
    marker, like a fresh OS process.
    """
    monkeypatch.setitem(sys.modules, 'hermes_bootstrap', types.ModuleType('hermes_bootstrap'))
    monkeypatch.delenv('TINGLE_EVAL_BOOTSTRAP_HOPS', raising=False)
    yield


class FakeResult:
    def __init__(self, text, parsed):
        self.text, self.parsed, self.provider, self.model, self.usage = text, parsed, 'fake', 'fake-0', None


class FakeLlm:
    def __init__(self, parsed_for_call, fail_at=None):
        self.calls, self.parsed_for_call, self.fail_at = [], parsed_for_call, fail_at

    def complete_structured(self, **kw):
        self.calls.append(kw)
        if self.fail_at is not None and len(self.calls) == self.fail_at:
            raise RuntimeError('boom')
        parsed = self.parsed_for_call(len(self.calls))
        return FakeResult(json.dumps(parsed), parsed)


def install_fake_agent(monkeypatch, llm):
    agent = types.ModuleType('agent')
    plugin_llm = types.ModuleType('agent.plugin_llm')
    plugin_llm.PluginLlm = lambda plugin_id: llm
    agent.plugin_llm = plugin_llm
    monkeypatch.setitem(sys.modules, 'agent', agent)
    monkeypatch.setitem(sys.modules, 'agent.plugin_llm', plugin_llm)


def parsed_for(mod, records, disposition='not_actionable'):
    return {'results': [dict(id=r['id'], disposition=disposition, action='', evidence_message=0,
                             evidence_quote='', why='synthetic', resolved_later=False, uncertainty='',
                             expiry_span='', expiry_date='', expiry_is_item_own=False)
                        for r in records]}


def test_dry_run_two_twelves_and_no_model_call(capsys):
    mod = load_mod()
    records, evidence, digest = mod.load(mod.DEFAULT_INPUT)
    batches, chars = mod.plan(records)
    assert len(records) == 24 and [len(b) for b in batches] == [12, 12] and len(digest) == 64
    assert len(evidence) == 24 and all(isinstance(v, dict) for v in evidence.values())
    sys.argv = ['synthetic_eval_blind.py']
    assert mod.main() == 0
    assert 'Dry run' in capsys.readouterr().out


def test_run_preserves_raw_and_validates_with_production(monkeypatch, tmp_path):
    mod = load_mod()
    records, _, _ = mod.load(mod.DEFAULT_INPUT)
    llm = FakeLlm(lambda n: parsed_for(mod, records[(n - 1) * 12:n * 12]))
    out = tmp_path / 'evidence'
    assert run_main(mod, monkeypatch, llm, out) == 0 and len(llm.calls) == 2
    first = llm.calls[0]
    assert set(first) == ALLOWED_KEYS and first['purpose'] == assess.PURPOSE
    assert first['schema_name'] == assess.SCHEMA_NAME and first['json_schema'] == assess.SCHEMA
    assert first['max_tokens'] == assess.MAX_TOKENS and first['timeout'] == assess.TIMEOUT
    payload = json.loads(first['input'][0]['text'])
    assert [c['id'] for c in payload['conversations']] == [r['id'] for r in records[:12]]
    first_parsed = parsed_for(mod, records[:12])
    assert (out / 'request-1.raw.txt').read_text() == json.dumps(first_parsed)  # first raw output verbatim
    assert (out / 'request-2.raw.txt').exists()
    manifest = json.loads((out / 'manifest.json').read_text())
    assert manifest['requests'][0]['validation'] == 'ok' and manifest['input_sha256']
    assert not any(k in json.dumps(manifest) for k in ('expected', 'answer'))


def test_failed_request_stops_spend_and_records(monkeypatch, tmp_path):
    mod = load_mod()
    llm = FakeLlm(lambda n: parsed_for(mod, []), fail_at=1)
    out = tmp_path / 'evidence'
    assert run_main(mod, monkeypatch, llm, out) == 1 and len(llm.calls) == 1
    manifest = json.loads((out / 'manifest.json').read_text())
    assert manifest['requests'][0]['failure'] == 'RuntimeError' and 'boom' not in (out / 'manifest.json').read_text()


def test_cross_batch_id_is_rejected_and_stops_spend(monkeypatch, tmp_path):
    """A response naming another batch's conversation ID must fail closed, as in production."""
    mod = load_mod()
    records, _, _ = mod.load(mod.DEFAULT_INPUT)
    llm = FakeLlm(lambda n: parsed_for(mod, records[:12]))  # batch 2 answers with batch 1 ids
    out = tmp_path / 'evidence'
    assert run_main(mod, monkeypatch, llm, out) == 1 and len(llm.calls) == 2
    manifest = json.loads((out / 'manifest.json').read_text())  # persisted after the failure, not only at end
    assert manifest['requests'][0]['validation'] == 'ok'
    assert manifest['requests'][1]['validation'] == 'rejected: malformed assessment'
    assert (out / 'request-2.raw.txt').exists()  # returned raw preserved even when rejected


def test_manifest_persists_after_each_success(monkeypatch, tmp_path):
    mod = load_mod()
    records, _, _ = mod.load(mod.DEFAULT_INPUT)
    llm = FakeLlm(lambda n: parsed_for(mod, records[(n - 1) * 12:n * 12]), fail_at=2)
    out = tmp_path / 'evidence'
    assert run_main(mod, monkeypatch, llm, out) == 1 and len(llm.calls) == 2
    manifest = json.loads((out / 'manifest.json').read_text())
    assert len(manifest['requests']) == 2
    assert manifest['requests'][0]['validation'] == 'ok' and manifest['requests'][1]['failure'] == 'RuntimeError'


def run_main(mod, monkeypatch, llm, out, token=None):
    install_fake_agent(monkeypatch, llm)
    # token is accepted for regression compatibility: the runner grants it no rights.
    if token is not None:
        monkeypatch.setenv('TINGLE_EVAL_RUN_TOKEN', token)
    sys.argv = ['x', '--run', '--i-approve-model-calls', '--out', str(out)]
    return mod.main()



def test_interrupted_ambiguous_call_is_not_retried(monkeypatch, tmp_path):
    """Reviewer regression: a request dispatched then interrupted has an unknown provider
    outcome; a second attempt must refuse without a single new call."""
    mod = load_mod()
    records, _, _ = mod.load(mod.DEFAULT_INPUT)
    out = tmp_path / 'evidence'

    class Ambiguous(FakeLlm):
        def complete_structured(self, **kw):
            self.calls.append(kw)
            raise KeyboardInterrupt()  # dispatched, then interrupted before any response

    with pytest.raises(KeyboardInterrupt):
        run_main(mod, monkeypatch, Ambiguous(lambda n: {}), out)
    token = json.loads((out / 'run.token').read_text())['token']
    second = FakeLlm(lambda n: parsed_for(mod, records[(n - 1) * 12:n * 12]))
    with pytest.raises(SystemExit):
        run_main(mod, monkeypatch, second, out, token=token)
    assert len(second.calls) == 0, 'ambiguous dispatched request was repeated without outcome reconciliation'


def test_write_ahead_marks_interrupted_request_in_flight(monkeypatch, tmp_path):
    """Death mid-call leaves the in-flight request marked ambiguous; earlier evidence intact."""
    mod = load_mod()
    records, _, _ = mod.load(mod.DEFAULT_INPUT)
    out = tmp_path / 'evidence'

    class DiesOnSecond(FakeLlm):
        def complete_structured(self, **kw):
            if len(self.calls) == 1:
                self.calls.append(kw)
                raise KeyboardInterrupt()
            return super().complete_structured(**kw)

    with pytest.raises(KeyboardInterrupt):
        run_main(mod, monkeypatch, DiesOnSecond(lambda n: parsed_for(mod, records[(n - 1) * 12:n * 12])), out)
    manifest = json.loads((out / 'manifest.json').read_text())
    assert manifest['status'] == 'interrupted'
    assert manifest['requests'][0]['validation'] == 'ok'
    assert manifest['requests'][1]['status'] == 'in_flight' and 'validation' not in manifest['requests'][1]
    assert (out / 'request-1.raw.txt').exists() and not (out / 'request-2.raw.txt').exists()


def test_existing_dir_refuses_any_second_process(monkeypatch, tmp_path):
    mod = load_mod()
    out = tmp_path / 'evidence'
    out.mkdir()
    llm = FakeLlm(lambda n: {})
    with pytest.raises(SystemExit):
        run_main(mod, monkeypatch, llm, out)
    assert llm.calls == []


def test_completed_run_dir_also_refuses_reuse(monkeypatch, tmp_path):
    """Even a finished run's directory is terminal: first outputs can never be overwritten."""
    mod = load_mod()
    records, _, _ = mod.load(mod.DEFAULT_INPUT)
    out = tmp_path / 'evidence'
    assert run_main(mod, monkeypatch, FakeLlm(lambda n: parsed_for(mod, records[(n - 1) * 12:n * 12])), out) == 0
    raw1 = (out / 'request-1.raw.txt').read_text()
    second = FakeLlm(lambda n: parsed_for(mod, records[(n - 1) * 12:n * 12]))
    with pytest.raises(SystemExit):
        run_main(mod, monkeypatch, second, out)
    assert second.calls == [] and (out / 'request-1.raw.txt').read_text() == raw1
