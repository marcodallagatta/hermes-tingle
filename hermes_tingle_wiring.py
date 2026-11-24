# Copyright (C) 2025-2026 Hermes Tingle Authors
# SPDX-License-Identifier: MIT
"""Production wiring: connect the plugin's inert interfaces to Hermes.

At plugin load ``integrate`` registers the ``post_gateway_admission`` hook.
The chat collector reads only after ``wire_chat_history`` connects the
retained-session store and source consent exists. Nothing runs at import.

REAL COVERAGE, labeled honestly: the public gateway-session listing returns
live sessions by routing key (platform/chat/thread). The resolver returns
active session routes for a chat, including separate topics/threads; the
collector caps that list at five sessions and reads at most 200 messages across
them. It covers each selected session's CURRENT segment - ancestor lineage
expansion materializes every lineage row before limiting in the pinned
implementation, so it is never used - and compaction-archived rows are
excluded. The collector's review carries a ``coverage`` field stating this;
it never implies full history. Route discovery calls Hermes for active session
records platform-wide; these full rows can include stored prompt/config fields.
Tingle uses only ``id``, ``source`` and ``chat_id`` to select routes, and does
not persist or transmit other returned fields. The five-route and 200-message
caps bound transcript reads, not the platform-wide listing; no messages from
other chats are fetched.

Source-side bounds: each session query requests only the remaining per-chat
budget, with ``latest=True`` and ``include_ancestors=False``. The collector
adds the returned rows to one aggregate cap across selected sessions and marks
the review incomplete when that cap is reached; it fetches no sentinel row.
"""

from __future__ import annotations

import itertools
from typing import Any

try:
    from . import hermes_tingle_sources as sources
except ImportError:  # pragma: no cover - top-level pytest import
    import hermes_tingle_sources as sources

CHAT_COVERAGE_NOTE = sources.CHAT_COVERAGE_NOTE

def wire_chat_history(state_db: Any | None) -> None:
    """Connect Hermes' retained session store as the chat history source.

    The pinned public ``SessionDB`` surface provides
    ``list_gateway_sessions(platform=, active_only=)`` and ``get_messages``.
    Reads Hermes' OWN retained history only, never a live messaging account.
    """
    if state_db is None:
        sources.set_chat_history_interfaces(None, None)
        return
    list_sessions = getattr(state_db, "list_gateway_sessions", None)
    find = getattr(state_db, "find_session_by_origin", None)
    get_messages = getattr(state_db, "get_messages", None)
    if not callable(get_messages) or (not callable(list_sessions) and not callable(find)):
        raise ValueError(
            "state_db must expose list_gateway_sessions and get_messages")

    def session_resolver(platform: str, chat_id: str):
        if callable(list_sessions):
            rows = list_sessions(platform=platform, active_only=True)
            if not isinstance(rows, (list, tuple)):
                return []
            wanted_platform = str(platform or "").casefold()
            wanted_chat = str(chat_id or "")
            result = []
            seen = set()
            for row in rows:
                if not isinstance(row, dict):
                    continue
                session_id = row.get("id")
                if (not isinstance(session_id, str) or not session_id or session_id in seen
                        or str(row.get("source") or "").casefold() != wanted_platform
                        or str(row.get("chat_id") or "") != wanted_chat):
                    continue
                seen.add(session_id)
                result.append(session_id)
            return result
        if not callable(find):
            return []
        session_id = find(platform=platform, chat_id=chat_id)
        return [session_id] if isinstance(session_id, str) and session_id else []

    def message_reader(session_id: str, limit: int | None = None):
        # Apply the collector's remaining per-chat budget before the DB read;
        # no per-session sentinel may overrun the aggregate cap.
        if limit is None:
            limit = sources.MAX_CHAT_READS_PER_CHAT
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError("chat message read limit must be a non-negative integer")
        limit = min(limit, sources.MAX_CHAT_READS_PER_CHAT)
        if limit == 0:
            return
        # include_ancestors=False is deliberate: with it True the pinned
        # store materializes EVERY lineage row via _read_all before applying
        # the limit in Python (verified against the pinned implementation) -
        # an unbounded read. Single-session + latest + requested limit is
        # bounded in SQL; earlier lineage segments are disclosed as not covered.
        rows: Any = get_messages(session_id, limit=limit,
                                latest=True, include_ancestors=False)
        iterable = rows if rows is not None else ()
        for row in itertools.islice(iter(iterable), limit):
            if not isinstance(row, dict):
                continue
            ts = row.get("timestamp", row.get("created_at"))
            yield {"role": row.get("role", ""), "content": row.get("content", ""),
                   "timestamp": ts}

    sources.set_chat_history_interfaces(session_resolver, message_reader)


def wire_from_runtime(ctx: Any = None) -> dict[str, bool]:
    """Wire what the pinned Hermes runtime makes available to a plugin.

    Chat history: opened through the PUBLIC ``hermes_state`` module
    (SessionDB over the default state path). Unavailable outside a real
    Hermes runtime (stub tests, missing state) - stays unwired, collector
    reports no_history_interface.

    Source discovery: the admitted-inbound hook is registered by integrate().
    Never raises into plugin load."""
    wired = {"chat_history": False}
    try:
        import hermes_state  # public plugin API module in the real runtime
        db_path = getattr(hermes_state, "DEFAULT_DB_PATH", None)
        session_db_cls = getattr(hermes_state, "SessionDB", None)
        if db_path is not None and session_db_cls is not None:
            wire_chat_history(session_db_cls(db_path, read_only=True))
            wired["chat_history"] = True
    except Exception:
        pass  # not inside a Hermes runtime (or state unavailable): stay inert
    return wired


def observe_admitted_inbound(*, session_key: Any = "", platform: Any = "",
                             source: Any = None, message_id: Any = "",
                             text: Any = "", **kwargs: Any) -> None:
    """``post_gateway_admission`` hook - the ADMITTED inbound lifecycle point
    (verified payload at the pinned runtime: session_key, platform string,
    source dict, message_id, text; fired once per message that survived
    authorization/admission, fail-open). The earlier pre_gateway_dispatch
    point was BEFORE auth and was removed: nothing may persist pre-admission
    traffic. ALWAYS returns None - the message proceeds to the agent;
    dispatch is never altered.

    Chat-scope discovery: an ADMITTED message is
    Hermes' own authorization that this profile sees the chat, so the chat
    enters the default-ingestion registry - DMs and groups alike. This is
    how groups and other available chats enter scope; a stranger's /tingle
    command still never registers a chat, registration reads and retains no
    content, and the owner's exclusion list removes any chat at any time."""
    del session_key, message_id, kwargs
    if isinstance(source, dict):
        chat_ref = f"{str(platform or '').strip()}:{str(source.get('chat_id', '') or '').strip()}"
        try:
            from . import hermes_tingle_store as _store
        except ImportError:  # pragma: no cover - test harness import
            import hermes_tingle_store as _store
        _store.note_known_chat(chat_ref, origin="inbound")
        try:
            from .hermes_tingle_discovery import observe_source
        except ImportError:
            from hermes_tingle_discovery import observe_source
        observe_source(dict(source, platform=str(platform or '')))
    return None


def wire_from_hermes(*, state_db: Any = None) -> dict[str, bool]:
    """Connect an explicitly offered chat history interface."""
    if state_db is not None:
        wire_chat_history(state_db)
    return {"chat_history": state_db is not None}
