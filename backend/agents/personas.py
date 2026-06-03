"""Director persona loader.

The personas live in MongoDB (loaded by `scripts/seed_mongo.py` from
`seed_data/director_personas.json`). At runtime we fetch them via the
MongoDB MCP tool layer so the agent system prompt + biases + voice
profile reflect any edits without a rebuild.

This module is the single source of truth for persona access. Every
component that needs a director's identity (the per-director sub-agents,
the Chairman's Q&A routing, the debate engine, the TTS voice picker)
imports `get_director` from here.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any


@lru_cache(maxsize=1)
def _personas() -> dict[str, dict[str, Any]]:
    """Load all directors + the Chairman from MongoDB once per process."""
    # Lazy import so unit tests that mock the tool layer can override it.
    from .tools._mcp_client import mcp_call_sync

    rows = mcp_call_sync("find", {
        "database": "boardroom", "collection": "director_personas",
    })
    if not isinstance(rows, list):
        return {}
    return {row["_id"]: row for row in rows if isinstance(row, dict) and "_id" in row}


def get_director(director_id: str) -> dict[str, Any]:
    """Return one director's full persona dict.

    Args:
      director_id: One of 'bull', 'bear', 'quant', 'strategist', 'chairman'.

    Returns:
      The persona document. KeyError if not found.
    """
    return _personas()[director_id]


def all_director_ids() -> list[str]:
    """The four debating directors (excludes the Chairman)."""
    return ["bull", "bear", "quant", "strategist"]


def reload_personas() -> None:
    """Force re-fetch on next access. Useful in dev when seed data changes."""
    _personas.cache_clear()
