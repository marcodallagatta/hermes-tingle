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
    if _missing("hermes_yaml"):
        # Hermes vendors hermes_yaml (ruamel safe, duplicate keys rejected).
        # Mirror that contract with PyYAML so tests exercise the same document
        # rules without depending on a real Hermes checkout.
        import yaml

        class _NoDuplicateKeys(yaml.SafeLoader):
            pass

        def _mapping(loader, node):
            result = {}
            for key_node, value_node in node.value:
                key = loader.construct_object(key_node, deep=True)
                if key in result:
                    raise yaml.constructor.ConstructorError('duplicate key', key)
                result[key] = loader.construct_object(value_node, deep=True)
            return result

        _NoDuplicateKeys.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)
        module = types.ModuleType('hermes_yaml')

        # SafeLoader subclass: no arbitrary Python construction, only duplicate-key rejection.
        def safe_load(stream):
            text = stream if isinstance(stream, (str, bytes)) else stream.read()
            return yaml.load(text, Loader=_NoDuplicateKeys)

        module.safe_load = safe_load
        module.YAMLError = yaml.YAMLError
        sys.modules['hermes_yaml'] = module
    if _missing("plugins.plugin_storage"):
        package = sys.modules.setdefault("plugins", types.ModuleType("plugins"))
        storage = types.ModuleType("plugins.plugin_storage")

        def plugin_db(_name: str):
            raise RuntimeError("tests must monkeypatch hermes_tingle_store.plugin_db")

        storage.plugin_db = plugin_db
        package.plugin_storage = storage
        sys.modules["plugins.plugin_storage"] = storage
