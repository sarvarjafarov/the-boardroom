"""Thin wrapper around the official `mcp` Python SDK so tool functions can
call MongoDB MCP operations as plain async functions.

Refined from PortfolioPilot's mcp_client:
  - Envelope stripping picks the FIRST <untrusted-user-data> match whose
    body JSON-parses (the guardrail prose mentions the tags inline)
  - EJSON $oid normalization → plain hex strings
  - 10s timeout on the MCP path; falls back to pymongo direct after
    3 consecutive MCP failures (Cloud Run + Atlas free-tier blocking)
  - `mcp_call_sync` wrapper for the persona loader (which runs at agent
    construction time before any event loop is alive)
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


# --- Configuration --------------------------------------------------------

_MONGODB_CONN = os.getenv("MDB_MCP_CONNECTION_STRING") or os.getenv("MONGODB_URI", "")
_MCP_TIMEOUT_S = float(os.getenv("MCP_TIMEOUT_S", "10"))
_MCP_FAILURE_THRESHOLD = 3

# Module-level failure counter — process-wide circuit breaker.
_mcp_consecutive_failures = 0


# --- Envelope handling ----------------------------------------------------

# The MongoDB MCP server wraps query results in a prompt-injection guardrail:
#   <untrusted-user-data-{uuid}>
#   {actual JSON payload}
#   </untrusted-user-data-{uuid}>
# But the SAME tags are mentioned inline in the prose warning before that.
# We find every envelope and return the first one whose body JSON-parses.
_ENVELOPE_RE = re.compile(
    r"<untrusted-user-data-[0-9a-f-]+>(.*?)</untrusted-user-data-[0-9a-f-]+>",
    re.DOTALL,
)


def _strip_envelope(text: str) -> tuple[str, bool]:
    matches = _ENVELOPE_RE.findall(text)
    if not matches:
        return text, False
    for inner in matches:
        stripped = inner.strip()
        if not stripped:
            continue
        try:
            json.loads(stripped)
            return stripped, True
        except (TypeError, ValueError):
            continue
    return matches[0].strip(), True


def _normalize_oids(value: Any) -> Any:
    """Convert Mongo EJSON `{"$oid": "..."}` shape into a plain hex string."""
    if isinstance(value, dict):
        if set(value.keys()) == {"$oid"}:
            return value["$oid"]
        return {k: _normalize_oids(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize_oids(v) for v in value]
    return value


# --- MCP transport --------------------------------------------------------

def _mcp_url() -> str:
    base = os.getenv("MONGODB_MCP_URL", "http://127.0.0.1:8088").rstrip("/")
    return base if base.endswith("/mcp") else f"{base}/mcp"


async def mcp_call(tool: str, args: dict[str, Any]) -> Any:
    """Primary entry point. MCP-first; falls back to pymongo on failure."""
    global _mcp_consecutive_failures
    if _mcp_consecutive_failures < _MCP_FAILURE_THRESHOLD:
        try:
            result = await asyncio.wait_for(
                _mcp_call_strict(tool, args), timeout=_MCP_TIMEOUT_S,
            )
            _mcp_consecutive_failures = 0
            return result
        except Exception as exc:
            _mcp_consecutive_failures += 1
            print(
                f"[mcp] failed (count={_mcp_consecutive_failures}) tool={tool} "
                f"err={type(exc).__name__}: {exc!r:.200}",
                flush=True,
            )
    return await _pymongo_fallback(tool, args)


async def _mcp_call_strict(tool: str, args: dict[str, Any]) -> Any:
    """The architecture-canonical path: every call through the MCP server."""
    async with streamablehttp_client(_mcp_url()) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            if tool != "connect" and _MONGODB_CONN:
                try:
                    await session.call_tool(
                        "connect", {"connectionString": _MONGODB_CONN},
                    )
                except Exception:
                    pass
            result = await session.call_tool(tool, args)

    payloads: list[Any] = []
    for part in result.content or []:
        text = getattr(part, "text", None)
        if not isinstance(text, str):
            continue
        inner, had_envelope = _strip_envelope(text)
        candidate = inner if had_envelope else text
        try:
            payloads.append(_normalize_oids(json.loads(candidate)))
        except (TypeError, ValueError):
            payloads.append(text)
    if not payloads:
        return None
    structured = [p for p in payloads if not isinstance(p, str)]
    return structured[-1] if structured else payloads[-1]


# --- pymongo fallback -----------------------------------------------------

_pymongo_client = None


def _get_pymongo_client():
    global _pymongo_client
    if _pymongo_client is None:
        from pymongo import MongoClient
        from pymongo.read_preferences import ReadPreference
        import certifi
        uri = _MONGODB_CONN
        if not uri:
            raise RuntimeError("MONGODB_URI / MDB_MCP_CONNECTION_STRING is not set")
        # Atlas M0 free tier occasionally fails SSL handshakes on the primary
        # shard. readPreference=secondaryPreferred lets us continue reading
        # from healthy secondaries when the primary is in TLS distress.
        # Writes still go to the primary; on primary failure we'll surface
        # the error and the retry logic in mcp_call / mcp_call_sync will
        # back off.
        _pymongo_client = MongoClient(
            uri,
            tlsCAFile=certifi.where(),
            read_preference=ReadPreference.SECONDARY_PREFERRED,
            retryReads=True,
            retryWrites=True,
            serverSelectionTimeoutMS=15000,
        )
    return _pymongo_client


def _doc_clean(doc: dict[str, Any]) -> dict[str, Any]:
    """ObjectId → hex string, recursively, to match MCP normalization."""
    out: dict[str, Any] = {}
    for k, v in doc.items():
        if hasattr(v, "binary"):  # bson.ObjectId duck-type
            out[k] = str(v)
        elif isinstance(v, dict):
            out[k] = _doc_clean(v)
        elif isinstance(v, list):
            out[k] = [_doc_clean(x) if isinstance(x, dict) else x for x in v]
        else:
            out[k] = v
    return out


async def _pymongo_fallback(tool: str, args: dict[str, Any]) -> Any:
    """Implements just the MCP tools we actually call. Wraps the sync pymongo
    operation in asyncio.to_thread to keep the FastAPI event loop free.

    Retries up to 3 times with backoff on Atlas SSL flakiness."""
    db_name = args.get("database") or os.getenv("MONGODB_DB", "boardroom")
    coll_name = args.get("collection")

    from pymongo.errors import AutoReconnect, ServerSelectionTimeoutError

    def _run() -> Any:
        client = _get_pymongo_client()
        db = client[db_name]
        if tool == "find":
            filt = args.get("filter") or {}
            sort = args.get("sort")
            limit = int(args.get("limit") or 0)
            cur = db[coll_name].find(filt)
            if sort:
                cur = cur.sort([(k, v) for k, v in sort.items()])
            if limit > 0:
                cur = cur.limit(limit)
            return [_doc_clean(d) for d in cur]
        if tool == "insert-many":
            docs = args.get("documents") or []
            if not docs:
                return "No documents to insert."
            res = db[coll_name].insert_many(docs)
            return f"Inserted {len(res.inserted_ids)} documents."
        if tool == "update-many":
            res = db[coll_name].update_many(
                args.get("filter") or {},
                args.get("update") or {},
                upsert=bool(args.get("upsert")),
            )
            return f"Matched {res.matched_count}, modified {res.modified_count}."
        if tool == "list-databases":
            return list(client.list_database_names())
        if tool == "connect":
            client.admin.command("ping")
            return "connected"
        raise NotImplementedError(f"pymongo fallback does not implement tool {tool!r}")

    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            return await asyncio.to_thread(_run)
        except (AutoReconnect, ServerSelectionTimeoutError) as exc:
            last_exc = exc
            global _pymongo_client
            _pymongo_client = None
            await asyncio.sleep(0.6 * (attempt + 1))
            continue
    if last_exc:
        raise last_exc
    return None


# --- Sync entry point (for persona loader at agent construction time) -----

def mcp_call_sync(tool: str, args: dict[str, Any]) -> Any:
    """Blocking variant. Used by the persona loader which runs at module
    import time (before any event loop exists).

    Atlas M0 free-tier shards intermittently fail SSL handshakes under
    contention. We retry up to 4 times with short backoffs so the
    persona loader survives a flaky shard rather than nuking the agent
    start-up path.
    """
    import time as _time
    from pymongo.errors import AutoReconnect, ServerSelectionTimeoutError

    db_name = args.get("database") or os.getenv("MONGODB_DB", "boardroom")
    coll_name = args.get("collection")

    last_exc: Exception | None = None
    for attempt in range(4):
        try:
            client = _get_pymongo_client()
            db = client[db_name]
            if tool == "find":
                filt = args.get("filter") or {}
                cur = db[coll_name].find(filt)
                if args.get("sort"):
                    cur = cur.sort([(k, v) for k, v in args["sort"].items()])
                if args.get("limit"):
                    cur = cur.limit(int(args["limit"]))
                return [_doc_clean(d) for d in cur]
            raise NotImplementedError(f"mcp_call_sync does not implement tool {tool!r}")
        except (AutoReconnect, ServerSelectionTimeoutError) as exc:
            last_exc = exc
            # Reset the cached client so the next attempt opens a fresh
            # topology connection (might land on a healthier shard).
            global _pymongo_client
            _pymongo_client = None
            _time.sleep(0.6 * (attempt + 1))
            continue
    if last_exc:
        raise last_exc
    return []
