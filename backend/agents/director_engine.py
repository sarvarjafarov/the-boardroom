"""DirectorEngine — orchestrates 4 directors per AudioSession.

Started by the FastAPI source-lifecycle endpoints after the AudioSession
is up. Ended when the source ends. Tracks all running engines in a
process-wide registry so the FastAPI layer can hand them out by audio_id.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from .director_agent import DirectorAgent
from .personas import all_director_ids

if TYPE_CHECKING:
    from audio.pipeline import AudioSession

_log = logging.getLogger("boardroom.director_engine")


class DirectorEngine:
    """One per active AudioSession. Owns 4 DirectorAgents."""

    def __init__(self, session: "AudioSession") -> None:
        self.session = session
        self.directors: dict[str, DirectorAgent] = {}

    async def start(self) -> None:
        for did in all_director_ids():
            agent = DirectorAgent(did, self.session)
            self.directors[did] = agent
            await agent.start()
        _log.info(
            "director_engine started on audio %s (%d directors)",
            self.session.audio_id, len(self.directors),
        )

    async def stop(self) -> None:
        await asyncio.gather(
            *(agent.stop() for agent in self.directors.values()),
            return_exceptions=True,
        )
        _log.info("director_engine stopped on audio %s", self.session.audio_id)


# Process-wide registry of active engines, keyed by audio_id.
_engines: dict[str, DirectorEngine] = {}
_lock = asyncio.Lock()


async def start_engine_for_session(session: "AudioSession") -> DirectorEngine:
    async with _lock:
        if session.audio_id in _engines:
            return _engines[session.audio_id]
        engine = DirectorEngine(session)
        _engines[session.audio_id] = engine
    await engine.start()
    return engine


async def stop_engine_for_session(audio_id: str) -> None:
    async with _lock:
        engine = _engines.pop(audio_id, None)
    if engine:
        await engine.stop()


def get_engine(audio_id: str) -> DirectorEngine | None:
    return _engines.get(audio_id)
