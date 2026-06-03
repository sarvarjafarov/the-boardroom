"""YouTube Live extractor — yt-dlp + ffmpeg pipeline.

For YouTube Live URLs (and re-streamed CNBC/Bloomberg/finance channels),
we launch:

  yt-dlp -f bestaudio --no-part -o - $URL
     | ffmpeg -i pipe:0 -f s16le -ar 16000 -ac 1 -

Both processes are managed as asyncio subprocesses. We read PCM frames
from ffmpeg's stdout in 40 ms chunks (640 samples × 2 bytes = 1280 B)
and yield them through `pcm_frames()`.

If either process exits unexpectedly, `pcm_frames()` cleanly ends. The
AudioSession's outer pump catches the EOF and stops the session, which
in turn triggers the Chairman's end-of-session synthesis.
"""
from __future__ import annotations

import asyncio
import logging
import shlex
import shutil
import sys
from typing import AsyncIterator

from .base import Extractor

_log = logging.getLogger("boardroom.audio.youtube_live")

FRAME_BYTES = 1280  # 640 samples × 2 bytes = 40 ms at 16 kHz
READ_TIMEOUT_S = 60  # if no PCM arrives for this long, give up


class YouTubeLiveExtractor(Extractor):
    def __init__(self, session) -> None:
        super().__init__(session)
        self._ytdlp_proc: asyncio.subprocess.Process | None = None
        self._ffmpeg_proc: asyncio.subprocess.Process | None = None

    async def start(self) -> None:
        url = self.session.source_url
        if not url:
            raise RuntimeError("YouTubeLiveExtractor requires source_url")

        ytdlp_path = shutil.which("yt-dlp")
        ffmpeg_path = shutil.which("ffmpeg")
        if not ytdlp_path:
            raise RuntimeError("yt-dlp not on PATH (pip install yt-dlp)")
        if not ffmpeg_path:
            raise RuntimeError("ffmpeg not on PATH (brew install ffmpeg / apt install ffmpeg)")

        _log.info("youtube_live: launching pipeline for %s", url)

        # asyncio subprocess streams don't expose `fileno()`, so we can't
        # chain `stdin=ytdlp.stdout` directly. Use a shell pipeline with
        # quoted argument so the URL is parsed safely. -o - writes audio
        # to stdout; ffmpeg decodes to 16 kHz mono Int16 PCM on stdout.
        cmd = (
            f"{shlex.quote(ytdlp_path)} -f bestaudio --no-part --quiet --no-warnings "
            f"-o - {shlex.quote(url)} "
            f"| {shlex.quote(ffmpeg_path)} -loglevel error -i pipe:0 "
            f"-f s16le -ar 16000 -ac 1 -"
        )
        self._ffmpeg_proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

    async def pcm_frames(self) -> AsyncIterator[bytes]:
        if not self._ffmpeg_proc or not self._ffmpeg_proc.stdout:
            return
        try:
            while not self._stopped.is_set():
                try:
                    chunk = await asyncio.wait_for(
                        self._ffmpeg_proc.stdout.readexactly(FRAME_BYTES),
                        timeout=READ_TIMEOUT_S,
                    )
                except asyncio.IncompleteReadError as exc:
                    # Stream ended (yt-dlp exited or YouTube live ended).
                    if exc.partial:
                        yield exc.partial
                    return
                except asyncio.TimeoutError:
                    _log.warning("youtube_live: no PCM for %ds, stopping", READ_TIMEOUT_S)
                    return
                yield chunk
        except asyncio.CancelledError:
            return

    async def stop(self) -> None:
        self._stopped.set()
        # Shell pipeline runs both yt-dlp and ffmpeg under a parent shell;
        # we kill the parent group so both children terminate cleanly.
        proc = self._ffmpeg_proc
        if not proc:
            return
        try:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        except ProcessLookupError:
            pass
        except Exception as exc:
            _log.warning("youtube_live: error stopping subprocess: %s", exc)
