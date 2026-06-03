"""AudioSession — one live audio source bound to one Gemini Live transcription
session.

Responsibilities:
  - Own the lifecycle of an Extractor (the per-source-type audio producer).
  - Own the lifecycle of a GeminiLiveSession (the transcription consumer).
  - Forward PCM frames Extractor → Gemini Live.
  - Surface transcript lines from Gemini Live → transcript_queue.
  - Subscribers (the director loops, the FastAPI WS, the persister) read
    from transcript_queue.

This is the seam between the audio world (PCM frames) and the agent world
(typed transcript lines). It is deliberately small — orchestration only,
no business logic.
"""
from __future__ import annotations

import asyncio
import enum
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

_log = logging.getLogger("boardroom.audio")


class SourceType(str, enum.Enum):
    TAB_SHARE = "tab_share"
    YOUTUBE_LIVE = "youtube_live"
    X_SPACES = "x_spaces"
    FILE_UPLOAD = "file_upload"


@dataclass
class TranscriptLine:
    """One finalized line of speech.

    `speaker_raw` is the speaker tag Gemini Live attached (often '0', '1', ...).
    The Diarizer maps this to a human role ('CEO', 'CFO', 'ANCHOR', etc.).
    """
    idx: int
    ts_from_start: float
    text: str
    speaker_raw: str | None = None
    speaker_role: str | None = None       # set by Diarizer
    speaker_name: str | None = None        # set by Diarizer when identifiable
    topics: list[str] = field(default_factory=list)  # set by TopicRouter


@dataclass
class AudioSession:
    """One live audio source. Created via SessionRegistry.register().

    The session is started via `await session.start()` and stopped via
    `await session.stop()`. Between, subscribers attach via
    `session.subscribe()` to receive transcript lines.
    """
    audio_id: str
    user_id: str
    source_type: SourceType
    label: str
    source_url: str | None = None
    ticker: str | None = None
    started_at: float = field(default_factory=time.monotonic)

    # Internal state — populated on start().
    _extractor: Any = None
    _live_session: Any = None
    _transcript_queue: asyncio.Queue[TranscriptLine] = field(
        default_factory=lambda: asyncio.Queue(maxsize=2048)
    )
    _subscribers: list[asyncio.Queue[TranscriptLine]] = field(default_factory=list)
    _tasks: list[asyncio.Task[Any]] = field(default_factory=list)
    _running: bool = False
    _line_idx: int = 0
    _on_line_callbacks: list[Callable[[TranscriptLine], Awaitable[None]]] = field(default_factory=list)

    async def start(self) -> None:
        """Spin up the Extractor + Gemini Live session + fan-out task."""
        if self._running:
            return
        # Lazy imports so module loads without google-genai installed in
        # environments that only need types.
        from .extractors.base import make_extractor
        from .gemini_live import GeminiLiveSession

        self._extractor = make_extractor(self)
        self._live_session = GeminiLiveSession(self.audio_id)

        await self._extractor.start()
        await self._live_session.start()

        self._tasks = [
            asyncio.create_task(self._forward_audio_to_live(), name=f"audio-fwd-{self.audio_id}"),
            asyncio.create_task(self._consume_transcripts(), name=f"audio-recv-{self.audio_id}"),
        ]
        self._running = True
        _log.info("AudioSession %s started (type=%s)", self.audio_id, self.source_type.value)

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        if self._extractor:
            try:
                await self._extractor.stop()
            except Exception:
                pass
        if self._live_session:
            try:
                await self._live_session.stop()
            except Exception:
                pass
        _log.info("AudioSession %s stopped", self.audio_id)

    def subscribe(self) -> asyncio.Queue[TranscriptLine]:
        """Attach a new subscriber. Returns a fresh queue that will receive
        every subsequent transcript line until the session stops or the
        subscriber disconnects."""
        q: asyncio.Queue[TranscriptLine] = asyncio.Queue(maxsize=2048)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[TranscriptLine]) -> None:
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    def on_line(self, callback: Callable[[TranscriptLine], Awaitable[None]]) -> None:
        """Register a fan-out callback. Useful when a subscriber wants
        push-style delivery without managing a queue (e.g., persistence
        worker, director loops in Day 3)."""
        self._on_line_callbacks.append(callback)

    async def push_pcm(self, chunk: bytes) -> None:
        """Called by the FastAPI tab_share WebSocket handler. The Extractor
        for SourceType.TAB_SHARE is just a sink that forwards what's pushed
        here into the Gemini Live session."""
        if not self._running or not self._extractor:
            return
        await self._extractor.push_pcm(chunk)

    # --- Internal pumps ---------------------------------------------------

    async def _forward_audio_to_live(self) -> None:
        """Pump PCM frames from the Extractor into the Gemini Live session."""
        try:
            async for frame in self._extractor.pcm_frames():
                if not self._running:
                    return
                await self._live_session.send_pcm(frame)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            _log.warning("audio forward loop ended on %s: %s", self.audio_id, exc)

    async def _consume_transcripts(self) -> None:
        """Read transcript lines from Gemini Live, route to subscribers."""
        try:
            async for line_text, ts, speaker_raw in self._live_session.transcript_lines():
                if not self._running:
                    return
                line = TranscriptLine(
                    idx=self._line_idx,
                    ts_from_start=ts,
                    text=line_text,
                    speaker_raw=speaker_raw,
                )
                self._line_idx += 1
                # Push to subscribers + fire callbacks.
                for q in self._subscribers:
                    try:
                        q.put_nowait(line)
                    except asyncio.QueueFull:
                        pass  # slow subscriber — drop, never block
                for cb in self._on_line_callbacks:
                    try:
                        await cb(line)
                    except Exception as exc:
                        _log.warning("on_line callback failed: %s", exc)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            _log.warning("transcript consume loop ended on %s: %s", self.audio_id, exc)


class SessionRegistry:
    """Process-wide registry of active AudioSessions, keyed by audio_id."""

    def __init__(self) -> None:
        self._sessions: dict[str, AudioSession] = {}
        self._lock = asyncio.Lock()

    async def register(
        self,
        *,
        audio_id: str,
        user_id: str,
        source_type: SourceType,
        label: str,
        source_url: str | None = None,
        ticker: str | None = None,
    ) -> AudioSession:
        """Create + register a new AudioSession. Does NOT start it — caller does."""
        async with self._lock:
            session = AudioSession(
                audio_id=audio_id,
                user_id=user_id,
                source_type=source_type,
                label=label,
                source_url=source_url,
                ticker=ticker.upper() if ticker else None,
            )
            self._sessions[audio_id] = session
            return session

    def get(self, audio_id: str) -> AudioSession | None:
        return self._sessions.get(audio_id)

    async def stop(self, audio_id: str) -> None:
        async with self._lock:
            session = self._sessions.pop(audio_id, None)
        if session:
            await session.stop()

    def all(self) -> list[AudioSession]:
        return list(self._sessions.values())


# Single process-wide registry used by FastAPI handlers + the agent loops.
registry = SessionRegistry()
