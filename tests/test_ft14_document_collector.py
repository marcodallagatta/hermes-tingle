"""The Drive `document` source kind is removed.

Drive content ingestion never shipped in a release build and the README no
longer lists it, so the kind is refused everywhere except legacy cleanup:
recorded optin rows stay on file as history and remain disable/purgeable.
"""

from __future__ import annotations

import sqlite3

import pytest

import hermes_tingle_sources as sources
import hermes_tingle_store as store


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    db_path = tmp_path / "data.db"

    def connect(_name: str):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(store, "plugin_db", connect)
    return store


class TestRemovedDocumentKind:
    def test_enable_refused(self, isolated):
        with pytest.raises(ValueError):
            isolated.enable_source("document", enabled_by="synthetic principal")

    def test_source_enabled_refused(self, isolated):
        with pytest.raises(ValueError):
            isolated.source_enabled("document")

    def test_no_document_collector(self):
        assert not hasattr(sources, "_collect_document_matches")

    def test_snapshot_has_no_document_review(self, isolated):
        review = sources.collect_snapshot()
        assert "document_review" not in review

    def test_capabilities_and_offers_exclude_document(self, isolated):
        assert "document" not in sources.source_capabilities()
        assert "document" not in sources.source_offers()

    def test_probe_refused(self, isolated):
        with pytest.raises(sources.SourceReadError):
            sources.probe_source("document")

    def test_legacy_row_stays_as_evidence(self, isolated):
        # simulate a recorded grant from an older build, inserted directly
        conn = isolated._connect()
        conn.execute(
            "UPDATE hermes_tingle_source_optins SET enabled=1, enabled_by='legacy owner', "
            "enabled_at='2026-01-01T00:00:00+00:00' WHERE kind='document'")
        conn.commit()
        conn.close()
        rows = {r["kind"]: r for r in isolated.list_source_optins()}
        assert rows["document"]["enabled"] == 1
        # legacy cleanup path stays open
        row = isolated.disable_source("document", note="legacy cleanup")
        assert row["enabled"] == 0 and row["disabled_at"]
