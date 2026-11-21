"""Test safety: never reach a real mailbox, network, or plugin database."""
import json
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import runtime_stubs
runtime_stubs.install()
import hermes_tingle_store as store
import hermes_tingle_sources as sources


@pytest.fixture(autouse=True)
def synthetic_settings(tmp_path, monkeypatch):
    import hermes_constants

    settings = {
        "calendar_exclude_substrings": ["Excluded Calendar"],
        "workstream_exclude_values": ["Work Alpha"],
        "source_exclude_substrings": ["tracker.example.invalid", "private-work:"],
        "ticket_exclude_patterns": [r"\bTASK-\d+\b"],
        "delivery": "telegram:-1001234567890:42",
        "timezone": "Etc/UTC",
        "daily_schedule": "5 8,16 * * *",
        "date_only_time": "09:10",
        "availability_start": "09:00",
        "availability_end": "20:00",
        "deadline_poll_minutes": 30,
    }
    (tmp_path / "hermes-tingle.json").write_text(json.dumps(settings))
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    return settings


@pytest.fixture(autouse=True)
def isolate_external_state(tmp_path, monkeypatch, synthetic_settings):
    # Establish hermes_constants.get_hermes_home before plugin_db/store operations;
    # otherwise parallel pytest workers can contend on the repository-root lock.
    del synthetic_settings  # explicit fixture-order dependency
    def connect(_name):
        connection = sqlite3.connect(tmp_path / "default-test.db")
        connection.row_factory = sqlite3.Row
        return connection

    def forbidden(*args, **kwargs):
        raise AssertionError("unmocked external access is forbidden in tests")

    monkeypatch.setattr(store, "plugin_db", connect)
    store.enable_source("email", enabled_by="synthetic fixture consent")
    store.enable_source("calendar", enabled_by="synthetic fixture consent")
    store.set_plugin_meta('calendar_account_binding', json.dumps({'generation':store.source_generation('calendar'),'account':'c'*64}))
    from delivery_fakes import bind_synthetic_email
    bind_synthetic_email(store)
    monkeypatch.setattr(sources, "calendar_account_identity", lambda: "c" * 64)
    monkeypatch.setattr(sources, "body_account_identity", lambda: ("owner@example.invalid", "a1d5d4103d619e71d41f4d8e96798978615d95d0ed9f31a860b4524840fd8ccf"))
    monkeypatch.setattr(sources, "_gws_bridge", forbidden)
    monkeypatch.setattr(sources, "_google_api", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
