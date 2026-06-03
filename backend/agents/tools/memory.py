"""Long-term boardroom memory tools — pattern library + track records.

These are the tools that make the boardroom feel like it REMEMBERS:
  - search_pattern_library: 'Same compute-promise language as MSFT Q3 2024'
  - search_track_records: 'Bear was right 3 of 4 times on this name'

Both go through MongoDB MCP (no LLM calls, just structured queries).
"""
from __future__ import annotations

import os
from typing import Any

from ._mcp_client import mcp_call

DB = os.getenv("MONGODB_DB", "boardroom")


async def search_pattern_library(
    director: str | None = None,
    trigger_keyword: str | None = None,
    min_hit_rate: float = 0.5,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Search the cross-call pattern library.

    The Bear most often calls this — bearish patterns dominate the library.
    Returns historical patterns that match either a director's domain or
    a trigger keyword from the live audio.

    Args:
      director: Filter to patterns 'owned' by this director ('bull', 'bear',
        'quant', 'strategist'). Leave None to search all.
      trigger_keyword: Match against the pattern's trigger_keywords list.
        Pass a substring like 'compute' or 'guidance'.
      min_hit_rate: Filter to patterns with at least this hit_rate (0-1).
      limit: Max patterns to return.

    Returns:
      List of pattern documents from the `patterns` collection. Each row
      includes: name, description, instances (historical matches),
      hit_rate, median_drawdown_7d, callout_template.
    """
    filt: dict[str, Any] = {"hit_rate": {"$gte": float(min_hit_rate)}}
    if director:
        filt["owned_by_director"] = director
    if trigger_keyword:
        filt["trigger_keywords"] = {"$regex": trigger_keyword, "$options": "i"}
    rows = await mcp_call("find", {
        "database": DB, "collection": "patterns",
        "filter": filt, "limit": int(limit),
    })
    return rows if isinstance(rows, list) else []


async def search_track_records(
    director: str,
    ticker: str | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    """Look up a director's historical accuracy.

    The Chairman uses this when weighting committee votes — directors with
    consistently better track records on a sector or ticker get effective-
    weight boosts.

    The directors themselves call this when reasoning about a ticker they
    have history with: 'I was wrong on TSLA Q4 2023 — let me re-examine'.

    Args:
      director: One of 'bull', 'bear', 'quant', 'strategist'.
      ticker: Filter to this ticker's history. Leave None for all-history.
      limit: Max records to return.

    Returns:
      {
        "director": str,
        "ticker": str | None,
        "records": [{call_id, prediction, score, thesis, hit, ...}, ...],
        "hit_rate": float,
        "sample_size": int,
      }
    """
    filt: dict[str, Any] = {"director": director}
    if ticker:
        filt["ticker"] = ticker.upper()
    rows = await mcp_call("find", {
        "database": DB, "collection": "track_records",
        "filter": filt, "limit": int(limit),
    })
    records = rows if isinstance(rows, list) else []
    hits = sum(1 for r in records if r.get("hit"))
    return {
        "director": director,
        "ticker": ticker.upper() if ticker else None,
        "records": records,
        "hit_rate": (hits / len(records)) if records else 0.0,
        "sample_size": len(records),
    }
