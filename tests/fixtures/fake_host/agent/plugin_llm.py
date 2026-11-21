"""Fake host facade for OFFLINE tests: schema-valid canned result, no provider.
FAKE_FACADE_IMPORTS_BOOTSTRAP=1 makes the first dispatch lazily import the bootstrap,
mirroring the real auxiliary_client -> process_bootstrap -> hermes_bootstrap chain."""
import json, os
from types import SimpleNamespace

class PluginLlm:
    def __init__(self, plugin_id=None):
        self.plugin_id = plugin_id
    def complete_structured(self, **kw):
        if os.environ.get("FAKE_FACADE_IMPORTS_BOOTSTRAP") == "1":
            import hermes_bootstrap  # noqa: lazy, mirrors the real client path
        records = json.loads(kw["input"][0]["text"])["conversations"]
        parsed = {"results": [dict(id=r["id"], disposition="not_actionable", action="",
                                   evidence_message=0, evidence_quote="", why="",
                                   resolved_later=False, uncertainty="", expiry_span="", expiry_date="", expiry_is_item_own=False) for r in records]}
        return SimpleNamespace(text=json.dumps(parsed), parsed=parsed,
                               provider="offline-fake", model="offline-fake", usage=None)
