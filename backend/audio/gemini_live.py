"""GeminiLiveSession — owns the bidi Gemini Live transcription socket.

Lifted from EarningsEdge's transcript_agent and simplified:
  - No TTS path here (TTS is a separate gemini-2.5-flash-preview-tts call
    triggered by the Chairman during voice Q&A — completely independent).
  - No reconnection backoff complexity: if the socket dies, we surface
    the EOF up the pipeline. The outer AudioSession decides what to do.
  - We DO keep the VAD config tuning that made EarningsEdge production-
    stable: silence_duration_ms=30000 so natural pauses don't end the
    user's turn and trigger a server-side 1000 close.

What surfaces upstream:
  `transcript_lines()` is an async generator yielding `(text, ts_from_start,
  speaker_raw)`. The text is the LATEST finalized transcription delta from
  Gemini Live, ts_from_start is monotonic seconds since session start, and
  speaker_raw is whatever speaker tag Gemini Live attached (often '0' or '1').
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from typing import Any, AsyncIterator

from google import genai
from google.genai import types

_log = logging.getLogger("boardroom.audio.gemini_live")

LIVE_MODEL = os.getenv("GEMINI_LIVE_MODEL", "gemini-3.1-flash-live-preview")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# 100 ms of 16 kHz mono Int16 silence — used as a keepalive ping when no
# real audio frames are arriving.
SILENT_FRAME = bytes(3200)


class GeminiLiveSession:
    """One Gemini Live bidirectional session, transcription-only.

    Usage:
        gls = GeminiLiveSession(audio_id="...")
        await gls.start()
        async def pump():
            await gls.send_pcm(frame)
        async for text, ts, speaker in gls.transcript_lines():
            ...
        await gls.stop()
    """

    def __init__(self, audio_id: str) -> None:
        self.audio_id = audio_id
        self._client: genai.Client | None = None
        self._stack: contextlib.AsyncExitStack | None = None
        self._live: Any = None
        self._send_lock = asyncio.Lock()
        self._closed = asyncio.Event()
        self._started_at: float = 0.0

    async def start(self) -> None:
        if not GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY not set")
        self._client = genai.Client(api_key=GEMINI_API_KEY)
        await self._open_socket()
        self._started_at = time.monotonic()
        _log.info("gemini_live started for audio_id=%s", self.audio_id)

    async def stop(self) -> None:
        self._closed.set()
        if self._stack:
            try:
                await self._stack.aclose()
            except Exception:
                pass
            self._stack = None
        self._live = None
        _log.info("gemini_live stopped for audio_id=%s", self.audio_id)

    async def send_pcm(self, chunk: bytes) -> None:
        """Send a PCM frame to Gemini Live. Drops silently if not started."""
        if self._live is None or self._closed.is_set():
            return
        async with self._send_lock:
            try:
                await self._live.send_realtime_input(
                    audio=types.Blob(
                        data=chunk,
                        mime_type="audio/pcm;rate=16000",
                    )
                )
            except Exception as exc:
                # Defer reconnect to the recv loop, which will detect the
                # closed iterator on its next loop iteration.
                _log.debug("send_pcm error: %s", exc)

    async def transcript_lines(self) -> AsyncIterator[tuple[str, float, str | None]]:
        """Yield finalized transcript lines as (text, ts_from_start, speaker_raw).

        Implements the same "consume input_transcription deltas, batch into
        sentence-ended lines" pattern EarningsEdge used. A delta is emitted
        as one line whenever the buffer ends with '.', '!', '?', a CALL/QA
        turn boundary, or has accumulated > 280 chars.
        """
        buf = ""
        last_emit = time.monotonic()
        try:
            while not self._closed.is_set():
                if self._live is None:
                    await self._open_socket()
                async for response in self._live.receive():
                    if self._closed.is_set():
                        return
                    text_delta, speaker = self._extract_delta_and_speaker(response)
                    if text_delta:
                        buf += text_delta
                        # Flush on sentence end OR buffer overflow.
                        if any(buf.rstrip().endswith(c) for c in (".", "!", "?")) or len(buf) >= 280:
                            line = buf.strip()
                            if line:
                                yield line, time.monotonic() - self._started_at, speaker
                            buf = ""
                            last_emit = time.monotonic()
                        # Force flush if the buffer is just sitting there.
                        elif (time.monotonic() - last_emit) > 4.0 and buf.strip():
                            yield buf.strip(), time.monotonic() - self._started_at, speaker
                            buf = ""
                            last_emit = time.monotonic()
                    # Server-initiated turn complete → flush any partial.
                    sc = getattr(response, "server_content", None)
                    if sc and getattr(sc, "turn_complete", None) and buf.strip():
                        yield buf.strip(), time.monotonic() - self._started_at, speaker
                        buf = ""
                        last_emit = time.monotonic()
                # `async for` exited normally — server closed cleanly.
                # Reopen and continue.
                if self._closed.is_set():
                    return
                await asyncio.sleep(0.2)
                await self._open_socket()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            _log.warning("transcript_lines loop ended on %s: %s", self.audio_id, exc)

    # --- Internals --------------------------------------------------------

    def _build_config(self) -> types.LiveConnectConfig:
        # CRITICAL: keep automatic VAD enabled (otherwise input_transcription
        # is buffered until activity_end and never streams), but make it
        # tolerant of long silences so natural pauses don't end the turn.
        # On the production-tested EarningsEdge config, this prevents the
        # rapid 1000-close storm.
        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            input_audio_transcription=types.AudioTranscriptionConfig(),
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    disabled=False,
                    end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_LOW,
                    start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_LOW,
                    silence_duration_ms=30000,
                    prefix_padding_ms=200,
                ),
            ),
        )

    async def _open_socket(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except Exception:
                pass
        self._stack = contextlib.AsyncExitStack()
        config = self._build_config()
        self._live = await self._stack.enter_async_context(
            self._client.aio.live.connect(model=LIVE_MODEL, config=config)
        )

    def _extract_delta_and_speaker(self, response: Any) -> tuple[str, str | None]:
        """Pick out input_audio_transcription deltas and speaker tags."""
        sc = getattr(response, "server_content", None)
        if sc is None:
            return "", None
        it = getattr(sc, "input_transcription", None)
        text = getattr(it, "text", None) if it else None
        if not text:
            return "", None
        # Speaker tag: some Gemini Live revs surface it as `speaker_id`
        # in the input_transcription. We pass through whatever's there.
        speaker = getattr(it, "speaker_id", None) or getattr(it, "speaker", None)
        return text, str(speaker) if speaker is not None else None
