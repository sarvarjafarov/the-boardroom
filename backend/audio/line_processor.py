"""LineProcessor — orchestrates Topic Router + Diarizer + MongoDB persistence
on every transcript line an AudioSession produces.

Strategy: latency-minimized batched processing.

For each line arriving from the Gemini Live transcription:
  1. Push it into the Diarizer's per-speaker buffer (cheap, in-memory).
  2. Add it to the pending-classification batch.
  3. Whenever the batch hits BATCH_SIZE lines OR BATCH_FLUSH_S seconds
     since the last flush, kick off:
       - Topic Router on the batch
       - Diarizer re-classification of speakers we haven't labeled yet
       - MongoDB write through the MCP layer

The transcript_queue subscribers see lines IMMEDIATELY (raw text + speaker_raw).
Topics + speaker_role are filled in asynchronously as the batch completes.
For the live UI this is fine — topics show up ~1-2s after the line appears.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from .topic_router import TopicRouter
from .diarizer import Diarizer

if TYPE_CHECKING:
    from .pipeline import TranscriptLine

_log = logging.getLogger("boardroom.audio.line_processor")

BATCH_SIZE = 6              # lines per Topic Router call
BATCH_FLUSH_S = 4.0         # seconds — flush even if batch isn't full
DIARIZE_EVERY_N_LINES = 8   # speaker re-classification frequency
PERSIST_THROTTLE_S = 2.0    # bulk persist no more than every 2s


class LineProcessor:
    """Per-AudioSession orchestrator. Constructed once and bound via
    `session.on_line(processor.handle)` so every new line gets pushed in."""

    def __init__(self, audio_id: str) -> None:
        self.audio_id = audio_id
        self.topic_router = TopicRouter()
        self.diarizer = Diarizer()

        # Pending batch + state.
        self._pending: list["TranscriptLine"] = []
        self._lock = asyncio.Lock()
        self._last_flush_at = time.monotonic()
        self._lines_since_diarize = 0
        self._unflushed_lines: list["TranscriptLine"] = []
        self._last_persist_at = time.monotonic()

    async def handle(self, line: "TranscriptLine") -> None:
        """Called once per finalized transcript line."""
        # Push into diarizer buffer immediately so cached labels apply.
        self.diarizer.attach_line(self.audio_id, line.speaker_raw, line.text)

        # Apply any cached label to THIS line right away.
        cached = self.diarizer.cached_label(self.audio_id, line.speaker_raw)
        if cached:
            line.speaker_role = cached.get("role")
            line.speaker_name = cached.get("name")

        # Publish to the live UI bus right now — topics fill in
        # asynchronously when the batch flushes.
        try:
            from dashboard_bus import bus
            bus.publish(self.audio_id, "transcript_line", {
                "idx": line.idx,
                "ts_from_start": line.ts_from_start,
                "text": line.text,
                "speaker_raw": line.speaker_raw,
                "speaker_role": line.speaker_role,
                "speaker_name": line.speaker_name,
                "topics": list(line.topics or []),
            })
        except Exception:
            pass

        # Batch for topic classification + persistence.
        async with self._lock:
            self._pending.append(line)
            self._unflushed_lines.append(line)
            self._lines_since_diarize += 1

            should_flush_topics = (
                len(self._pending) >= BATCH_SIZE
                or (time.monotonic() - self._last_flush_at) >= BATCH_FLUSH_S
            )
            if should_flush_topics:
                batch = self._pending
                self._pending = []
                self._last_flush_at = time.monotonic()
                # Fire-and-forget — we don't want to block the inbound stream.
                asyncio.create_task(self._flush_topics(batch))

            should_diarize = self._lines_since_diarize >= DIARIZE_EVERY_N_LINES
            if should_diarize:
                self._lines_since_diarize = 0
                asyncio.create_task(self._diarize_all())

            should_persist = (
                (time.monotonic() - self._last_persist_at) >= PERSIST_THROTTLE_S
                and self._unflushed_lines
            )
            if should_persist:
                batch = self._unflushed_lines
                self._unflushed_lines = []
                self._last_persist_at = time.monotonic()
                asyncio.create_task(self._persist(batch))

    async def flush(self) -> None:
        """Called by AudioSession.stop() — flush every pending op so the
        transcripts collection ends in a consistent state."""
        async with self._lock:
            if self._pending:
                batch = self._pending
                self._pending = []
                await self._flush_topics(batch)
            if self._unflushed_lines:
                batch = self._unflushed_lines
                self._unflushed_lines = []
                await self._persist(batch)
        # Final diarization pass so the persisted transcript has best-effort
        # speaker labels even for the closing speakers.
        await self._diarize_all()

    # --- Internals --------------------------------------------------------

    async def _flush_topics(self, batch: list["TranscriptLine"]) -> None:
        if not batch:
            return
        texts = [line.text for line in batch]
        try:
            topics_per_line = await self.topic_router.classify_batch(texts)
        except Exception as exc:
            _log.warning("topic flush failed on %s: %s", self.audio_id, exc)
            return
        # Write topics back onto the line objects in-place. Subscribers
        # holding references see the updates the next time they read.
        for line, topics in zip(batch, topics_per_line):
            line.topics = topics

    async def _diarize_all(self) -> None:
        try:
            await self.diarizer.classify_all_speakers(self.audio_id)
        except Exception as exc:
            _log.warning("diarize_all failed on %s: %s", self.audio_id, exc)

    async def _persist(self, lines: list["TranscriptLine"]) -> None:
        """Append a batch of finalized lines to the `transcripts` collection."""
        if not lines:
            return
        # Lazy import so this module is testable without the MCP layer.
        from agents.tools._mcp_client import mcp_call

        docs = [{
            "audio_id": self.audio_id,
            "idx": line.idx,
            "ts_from_start": line.ts_from_start,
            "text": line.text,
            "speaker_raw": line.speaker_raw,
            "speaker_role": line.speaker_role,
            "speaker_name": line.speaker_name,
            "topics": list(line.topics or []),
        } for line in lines]
        try:
            await mcp_call("insert-many", {
                "database": "boardroom",
                "collection": "transcript_lines",
                "documents": docs,
            })
        except Exception as exc:
            _log.warning("persist transcript batch failed: %s", exc)

    def cleanup(self) -> None:
        """Drop per-audio in-memory state (called after session stop + persist flush)."""
        self.diarizer.cleanup(self.audio_id)
