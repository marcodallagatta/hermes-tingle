"""Import real Hermes modules in pinned compatibility tests; allow stubs elsewhere."""
import importlib
import os
import pytest


_REQUIRED = os.environ.get("HERMES_REQUIRE_RUNTIME") == "1"


def hermes_import(name, *, reason="requires the Hermes runtime"):
    """Never let a missing Hermes dependency silently skip the pinned CI suite."""
    if _REQUIRED:
        return importlib.import_module(name)
    return pytest.importorskip(name, reason=reason)
