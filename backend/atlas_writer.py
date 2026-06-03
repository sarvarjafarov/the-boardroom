"""AtlasWriter — durable write queue for MongoDB MCP operations.

Atlas M0 free-tier primary nodes occasionally fail SSL handshakes for
extended windows. When that happens, our synthesis-path writes
(record_score_block, record_argument, draft_paper_trade, record_verdict,
portfolio_impacts inserts) start failing and the demo loses its
persistence layer.

This module wraps mcp_call with a durable in-memory queue + background
retry loop. Every write that fails goes onto the queue. A background
asyncio task drains the queue with exponential backoff, retrying
indefinitely until Atlas recovers. The queue is bounded so a multi-hour
outage doesn't blow up RAM.

Crucially the caller's hot path NEVER waits on Atlas. The first attempt
runs inline (so when Atlas is healthy, there's no latency penalty);
failures are silently deferred to the queue.

To use:
    from atlas_writer import durable_write
    await durable_write("insert-many", {
        "database": "boardroom", "collection": "verdicts",
        "documents": [...],
    })

The writer can be inspected via /api/debug/queue (defined in main.py).
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any

_log = logging.getLogger("boardroom.atlas_writer")

MAX_QUEUE_LEN = 5000               # drop oldest if queue grows beyond this
BACKOFF_MIN_S = 1.0
BACKOFF_MAX_S = 30.0
RETRY_BATCH_SIZE = 50              # per loop iteration


class AtlasWriter:
    def __init__(self) -> None:
        # Each entry: (tool, args, attempts, first_seen_ts).
        self._queue: deque[tuple[str, dict[str, Any], int, float]] = deque()
        self._stats = {
            "queued": 0,
            "retried": 0,
            "succeeded": 0,
            "dropped": 0,
            "current_backoff_s": BACKOFF_MIN_S,
        }
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._retry_loop(), name="atlas-writer-retry")

    async def stop(self) -> None:
        self._stopped.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    def stats(self) -> dict[str, Any]:
        return {**self._stats, "queue_depth": len(self._queue)}

    async def submit(self, tool: str, args: dict[str, Any]) -> None:
        """Try the write inline. On failure, enqueue for background retry."""
        # Lazy import so this module loads even when MCP isn't available
        # (e.g., unit tests of the queue logic itself).
        from agents.tools._mcp_client import mcp_call
        try:
            await mcp_call(tool, args)
        except Exception as exc:
            # Defer to the queue. The first attempt's cost is the only
            # latency the caller pays; subsequent retries are background.
            _log.debug("inline write failed (%s), queueing: %s", tool, exc)
            await self._enqueue(tool, args)

    async def _enqueue(self, tool: str, args: dict[str, Any]) -> None:
        async with self._lock:
            if len(self._queue) >= MAX_QUEUE_LEN:
                self._queue.popleft()
                self._stats["dropped"] += 1
            self._queue.append((tool, args, 0, time.monotonic()))
            self._stats["queued"] += 1

    async def _retry_loop(self) -> None:
        """Background drain loop with exponential backoff on failures."""
        backoff = BACKOFF_MIN_S
        from agents.tools._mcp_client import mcp_call

        while not self._stopped.is_set():
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=backoff)
                return
            except asyncio.TimeoutError:
                pass

            # Snapshot a batch.
            async with self._lock:
                batch: list[tuple[str, dict[str, Any], int, float]] = []
                for _ in range(min(RETRY_BATCH_SIZE, len(self._queue))):
                    batch.append(self._queue.popleft())

            if not batch:
                # Idle — reset backoff and tick slower.
                backoff = BACKOFF_MIN_S
                continue

            any_success = False
            still_failing: list[tuple[str, dict[str, Any], int, float]] = []
            for tool, args, attempts, first_seen in batch:
                self._stats["retried"] += 1
                try:
                    await mcp_call(tool, args)
                    any_success = True
                    self._stats["succeeded"] += 1
                except Exception as exc:
                    still_failing.append((tool, args, attempts + 1, first_seen))

            # Requeue failures at the FRONT so they retry first.
            if still_failing:
                async with self._lock:
                    for item in reversed(still_failing):
                        self._queue.appendleft(item)
                # Grow backoff if no success in this batch.
                if not any_success:
                    backoff = min(BACKOFF_MAX_S, backoff * 2)
            else:
                backoff = BACKOFF_MIN_S

            self._stats["current_backoff_s"] = backoff
            _log.debug(
                "retry batch done: success=%d still_failing=%d backoff=%.1fs queue=%d",
                len(batch) - len(still_failing), len(still_failing), backoff, len(self._queue),
            )


# Process-wide singleton.
writer = AtlasWriter()


async def durable_write(tool: str, args: dict[str, Any]) -> None:
    """Public entry point. Equivalent to `await mcp_call(tool, args)` for the
    happy path; falls back to queue retry on failure. NEVER raises."""
    await writer.submit(tool, args)
