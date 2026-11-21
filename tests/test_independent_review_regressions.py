"""Additional synthetic review regressions. Copy into tests/ to run with its isolation fixtures."""
import json
from datetime import datetime, timedelta, timezone

import hermes_tingle_store as store
import hermes_tingle_sources as sources


def test_aged_pending_id_must_not_fetch_or_expose_rich_metadata(monkeypatch):
    now = datetime.now(timezone.utc)
    store.enable_source("email", lookback_days=1, enabled_by="synthetic owner")
    store.stage_gmail_sync(history_id="100", message_ids=["synthetic-aged-id"],
                           now=now-timedelta(days=2))
    rich_reads = []
    def bridge(*args, **kwargs):
        params = json.loads(args[-1])
        if "getProfile" in args:
            return {"historyId": "101"}
        if "list" in args:
            return {"messages": []}
        if params.get("format") == "minimal":
            return {"id": "synthetic-aged-id", "internalDate": str(int((now-timedelta(days=10)).timestamp()*1000))}
        rich_reads.append(params)
        return {"id": "synthetic-aged-id", "threadId": "synthetic-thread", "labelIds": ["INBOX"],
                "internalDate": str(int((now-timedelta(days=10)).timestamp()*1000)),
                "snippet": "synthetic old source content", "payload": {"headers": [
                    {"name": "Date", "value": now.strftime("%a, %d %b %Y %H:%M:%S +0000")},
                    {"name": "Subject", "value": "synthetic stale message"}]}}
    monkeypatch.setattr(sources, "_gws_bridge", bridge)
    result = sources._collect_email_candidates(now=now, lookback_days=1)
    assert result["emails"] == []
    assert rich_reads == []
