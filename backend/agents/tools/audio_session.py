"""Audio session lifecycle tools.

The Chairman calls these to bound the listening session. Real audio
ingestion happens outside the agent's tool surface — in the FastAPI
WebSocket handlers + audio extractors. These tools just persist the
session metadata so directors and the Chairman can reason about
'this audio' vs 'all audio in this ticker's history'.
"""
from __future__ import annotations

import os
from typing import Any

from ._mcp_client import mcp_call

DB = os.getenv("MONGODB_DB", "boardroom")


async def start_listening(
    user_id: str,
    source_type: str,
    label: str,
    source_url: str | None = None,
    ticker: str | None = None,
    pinned: bool = False,
) -> dict[str, Any]:
    """Register a new audio source. Returns its audio_id for downstream calls.

    The FastAPI audio handler calls this when an audio source first
    starts producing transcript lines. Directors then attach their
    score_blocks to this audio_id.

    Args:
      user_id: Stable user identifier.
      source_type: 'tab_share' | 'youtube_live' | 'x_spaces' | 'file_upload'.
      label: Human-readable label (e.g., 'Tesla Q3 2024 Earnings Call').
      source_url: URL of the source (YouTube live, X Spaces, etc.). Optional
        for tab_share and file_upload.
      ticker: Resolved primary ticker if known. Optional.
      pinned: True to keep the source 'always listening'.

    Returns:
      {"audio_id": str (ObjectId hex), "status": "listening"}
    """
    doc = {
        "user_id": user_id,
        "type": source_type,
        "label": label,
        "source_url": source_url,
        "ticker": ticker.upper() if ticker else None,
        "pinned": bool(pinned),
        "status": "listening",
    }
    res = await mcp_call("insert-many", {
        "database": DB, "collection": "audio_sources", "documents": [doc],
    })
    # The MCP insert-many returns a summary string; for Day 1, return a
    # placeholder ObjectId-shaped string. Day 2 implementation will fetch
    # the inserted_id from a follow-up find.
    return {"audio_id": "stub-audio-id", "status": "listening", "_stub": True}


async def end_listening(audio_id: str) -> dict[str, Any]:
    """Mark an audio source as ended. Triggers final Chairman synthesis.

    The FastAPI audio handler calls this when the source closes
    (tab share stopped, YouTube live ended, X Spaces over, file fully
    processed). The Chairman then runs the final committee synthesis
    over the accumulated score_blocks.

    Args:
      audio_id: ObjectId hex of the audio_sources row.

    Returns:
      {"ok": True, "status": "ended", "trigger_synthesis": True}
    """
    await mcp_call("update-many", {
        "database": DB, "collection": "audio_sources",
        "filter": {"_id": audio_id},
        "update": {"$set": {"status": "ended"}},
    })
    return {"ok": True, "status": "ended", "trigger_synthesis": True}
