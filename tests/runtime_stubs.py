"""Import-time stubs for the Hermes runtime, used only outside a real Hermes install.

Tests must never touch a real mailbox, network, or plugin database. When the real
Hermes runtime is importable these stubs step aside; otherwise they provide the
minimal module surface the plugin imports so the suite can run anywhere (CI
included). Every external effect is still blocked by the fixtures in conftest.py.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path


def _missing(name: str) -> bool:
    try:
        __import__(name)
        return False
    except ImportError:
        return True


def install() -> None:
    if _missing("hermes_constants"):
        module = types.ModuleType("hermes_constants")
        module.get_hermes_home = lambda: Path.cwd()  # tests monkeypatch this per-test
        sys.modules["hermes_constants"] = module
    if _missing("plugins.plugin_storage"):
        package = sys.modules.setdefault("plugins", types.ModuleType("plugins"))
        storage = types.ModuleType("plugins.plugin_storage")

        def plugin_db(_name: str):
            raise RuntimeError("tests must monkeypatch hermes_tingle_store.plugin_db")

        storage.plugin_db = plugin_db
        package.plugin_storage = storage
        sys.modules["plugins.plugin_storage"] = storage
