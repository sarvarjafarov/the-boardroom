"""Extractor base class + factory."""
from __future__ import annotations

import abc
import asyncio
from typing import AsyncIterator, TYPE_CHECKING

if TYPE_CHECKING:
    from ..pipeline import AudioSession


class Extractor(abc.ABC):
    """Produces 16 kHz mono Int16 PCM frames for one audio source."""

    def __init__(self, session: "AudioSession") -> None:
        self.session = session
        self._stopped = asyncio.Event()

    @abc.abstractmethod
    async def start(self) -> None: ...

    @abc.abstractmethod
    async def pcm_frames(self) -> AsyncIterator[bytes]:
        """Async generator yielding PCM byte frames. Each frame should be
        a clean multiple of 2 bytes (Int16 samples). 40-100ms per frame
        is the sweet spot for Gemini Live."""
        ...

    async def push_pcm(self, chunk: bytes) -> None:
        """Override for push-based extractors (tab share). No-op by default
        for pull-based extractors (YouTube Live, file upload)."""
        return

    @abc.abstractmethod
    async def stop(self) -> None: ...


def make_extractor(session: "AudioSession") -> Extractor:
    """Return the right Extractor implementation for the source type."""
    from ..pipeline import SourceType
    from .tab_share import TabShareExtractor
    from .youtube_live import YouTubeLiveExtractor

    if session.source_type == SourceType.TAB_SHARE:
        return TabShareExtractor(session)
    if session.source_type == SourceType.YOUTUBE_LIVE:
        return YouTubeLiveExtractor(session)
    # Day 7: X_SPACES + FILE_UPLOAD implementations land later. Until then,
    # fall back to TabShare semantics so the audio path doesn't crash —
    # the API layer should refuse those source types before reaching here.
    return TabShareExtractor(session)
