"""DashboardBus — pub/sub fan-out for the live UI.

The frontend connects ONE WebSocket per active boardroom audio_id and
expects a unified stream of typed events:

  { "type": "transcript_line", "data": {...} }
  { "type": "score_block",     "data": {...} }
  { "type": "argument",        "data": {...} }
  { "type": "verdict",         "data": {...} }
  { "type": "portfolio_impact", "data": {...} }
  { "type": "ping" }

The Director / Debate / Chairman code publishes events into the bus
via `bus.publish(audio_id, ...)`. The FastAPI WebSocket handler creates
one subscriber per connection and drains the per-subscriber queue to
the client.

We keep this decoupled from MongoDB so the UI can render in real time
even while persistence is briefly degraded.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

_log = logging.getLogger("boardroom.dashboard_bus")


class DashboardBus:
    def __init__(self) -> None:
        # audio_id → list of subscriber queues
        self._subs: dict[str, list[asyncio.Queue[dict[str, Any]]]] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, audio_id: str) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=512)
        async with self._lock:
            self._subs.setdefault(audio_id, []).append(q)
        return q

    async def unsubscribe(self, audio_id: str, q: asyncio.Queue[dict[str, Any]]) -> None:
        async with self._lock:
            lst = self._subs.get(audio_id, [])
            try:
                lst.remove(q)
            except ValueError:
                pass
            if not lst:
                self._subs.pop(audio_id, None)

    def publish(self, audio_id: str, event_type: str, data: dict[str, Any]) -> None:
        """Non-blocking fan-out. Drops on slow consumer."""
        subs = self._subs.get(audio_id)
        if not subs:
            return
        msg = {"type": event_type, "data": data}
        for q in subs:
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                # Slow consumer — drop. Live UI prefers freshness over completeness.
                pass


# Process-wide singleton.
bus = DashboardBus()
