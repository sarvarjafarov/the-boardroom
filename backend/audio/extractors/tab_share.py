"""Browser tab share extractor.

The frontend captures `getDisplayMedia({audio: true})` and forwards
16 kHz mono Int16 PCM frames over a WebSocket. The FastAPI WS handler
calls `session.push_pcm(chunk)` for each frame, which lands in this
extractor's internal queue. Our `pcm_frames()` async iterator drains
the queue and yields frames to the Gemini Live pump.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

from .base import Extractor


class TabShareExtractor(Extractor):
    def __init__(self, session) -> None:
        super().__init__(session)
        # 64 frames * 40ms = 2.56s of buffering at worst. Plenty.
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=64)

    async def start(self) -> None:
        # Nothing to bring up — frames are pushed in by the WS handler.
        return

    async def push_pcm(self, chunk: bytes) -> None:
        try:
            self._queue.put_nowait(chunk)
        except asyncio.QueueFull:
            # Drop oldest frame, push new. We always prefer freshness
            # over completeness for live audio.
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self._queue.put_nowait(chunk)
            except asyncio.QueueFull:
                pass

    async def pcm_frames(self) -> AsyncIterator[bytes]:
        while not self._stopped.is_set():
            try:
                chunk = await asyncio.wait_for(self._queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            yield chunk

    async def stop(self) -> None:
        self._stopped.set()
        # Drain remaining frames so the pcm_frames consumer can exit.
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
