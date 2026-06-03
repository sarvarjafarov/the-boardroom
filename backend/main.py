"""FastAPI gateway for The Boardroom.

Day-2 surface:
  GET  /health                          — liveness
  POST /api/sources/youtube_live        — start YouTube Live ingest, returns audio_id
  POST /api/sources/tab_share           — register tab share, returns audio_id
  POST /api/sources/end                 — stop a source
  WS   /ws/audio/{audio_id}             — push PCM frames (tab share)
  WS   /ws/transcripts/{audio_id}       — receive live transcript line stream

Days 3-7 add: /api/agent/invoke, /api/orders/*, etc.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from typing import Any

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from audio import SourceType, registry
from audio.line_processor import LineProcessor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
_log = logging.getLogger("boardroom.main")

app = FastAPI(title="The Boardroom")

_origins = [
    o.strip() for o in os.getenv(
        "ALLOWED_ORIGINS",
        "http://localhost:3000,http://localhost:8080,http://127.0.0.1:3000,http://127.0.0.1:8080",
    ).split(",") if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Map from audio_id to its LineProcessor so /api/sources/end can flush it.
_processors: dict[str, LineProcessor] = {}


@app.get("/health")
async def health() -> dict[str, Any]:
    sessions = registry.all()
    return {
        "status": "ok",
        "active_sessions": len(sessions),
        "sessions": [
            {"audio_id": s.audio_id, "type": s.source_type.value, "label": s.label, "ticker": s.ticker}
            for s in sessions
        ],
    }


# --- Source lifecycle -----------------------------------------------------

class StartYouTubeBody(BaseModel):
    url: str
    label: str
    ticker: str | None = None
    user_id: str = "demo"


@app.post("/api/sources/youtube_live")
async def start_youtube_live(body: StartYouTubeBody) -> dict[str, Any]:
    """Start ingesting a YouTube Live URL. Returns the audio_id.

    The actual audio + transcription pipeline runs as a background task
    on the FastAPI event loop. Clients subscribe to the transcript stream
    via WS /ws/transcripts/{audio_id}.
    """
    audio_id = uuid.uuid4().hex
    session = await registry.register(
        audio_id=audio_id,
        user_id=body.user_id,
        source_type=SourceType.YOUTUBE_LIVE,
        label=body.label,
        source_url=body.url,
        ticker=body.ticker,
    )
    processor = LineProcessor(audio_id)
    _processors[audio_id] = processor
    session.on_line(processor.handle)
    try:
        await session.start()
    except Exception as exc:
        await registry.stop(audio_id)
        _processors.pop(audio_id, None)
        raise HTTPException(status_code=400, detail=f"youtube_live start failed: {exc}")

    # Persist source record in MongoDB.
    from agents.tools._mcp_client import mcp_call
    await mcp_call("insert-many", {
        "database": "boardroom",
        "collection": "audio_sources",
        "documents": [{
            "_id": audio_id,
            "user_id": body.user_id,
            "type": SourceType.YOUTUBE_LIVE.value,
            "label": body.label,
            "source_url": body.url,
            "ticker": body.ticker.upper() if body.ticker else None,
            "pinned": False,
            "status": "listening",
        }],
    })
    return {"audio_id": audio_id, "status": "listening"}


class StartTabShareBody(BaseModel):
    label: str
    ticker: str | None = None
    user_id: str = "demo"


@app.post("/api/sources/tab_share")
async def start_tab_share(body: StartTabShareBody) -> dict[str, Any]:
    """Register a tab share source. Returns the audio_id and the WS URL the
    frontend should open to push PCM frames."""
    audio_id = uuid.uuid4().hex
    session = await registry.register(
        audio_id=audio_id,
        user_id=body.user_id,
        source_type=SourceType.TAB_SHARE,
        label=body.label,
        ticker=body.ticker,
    )
    processor = LineProcessor(audio_id)
    _processors[audio_id] = processor
    session.on_line(processor.handle)
    await session.start()

    from agents.tools._mcp_client import mcp_call
    await mcp_call("insert-many", {
        "database": "boardroom",
        "collection": "audio_sources",
        "documents": [{
            "_id": audio_id,
            "user_id": body.user_id,
            "type": SourceType.TAB_SHARE.value,
            "label": body.label,
            "source_url": None,
            "ticker": body.ticker.upper() if body.ticker else None,
            "pinned": False,
            "status": "listening",
        }],
    })
    return {
        "audio_id": audio_id,
        "status": "listening",
        "ws_url": f"/ws/audio/{audio_id}",
    }


class EndBody(BaseModel):
    audio_id: str


@app.post("/api/sources/end")
async def end_source(body: EndBody) -> dict[str, Any]:
    proc = _processors.pop(body.audio_id, None)
    if proc:
        try:
            await proc.flush()
        finally:
            proc.cleanup()
    await registry.stop(body.audio_id)
    from agents.tools._mcp_client import mcp_call
    await mcp_call("update-many", {
        "database": "boardroom",
        "collection": "audio_sources",
        "filter": {"_id": body.audio_id},
        "update": {"$set": {"status": "ended"}},
    })
    return {"ok": True}


# --- Tab share audio inbound WebSocket -----------------------------------

@app.websocket("/ws/audio/{audio_id}")
async def ws_audio_in(ws: WebSocket, audio_id: str) -> None:
    session = registry.get(audio_id)
    if not session:
        await ws.close(code=4004)
        return
    if session.source_type != SourceType.TAB_SHARE:
        await ws.close(code=4005)
        return
    await ws.accept()
    try:
        while True:
            msg = await ws.receive()
            if "bytes" in msg and msg["bytes"]:
                await session.push_pcm(msg["bytes"])
            elif "text" in msg and msg["text"] == "stop":
                break
    except WebSocketDisconnect:
        pass
    finally:
        pass


# --- Transcript stream outbound WebSocket -------------------------------

@app.websocket("/ws/transcripts/{audio_id}")
async def ws_transcripts_out(ws: WebSocket, audio_id: str) -> None:
    session = registry.get(audio_id)
    if not session:
        await ws.close(code=4004)
        return
    await ws.accept()
    q = session.subscribe()
    try:
        while True:
            try:
                line = await asyncio.wait_for(q.get(), timeout=30.0)
            except asyncio.TimeoutError:
                # Send a keepalive ping every 30s.
                await ws.send_text(json.dumps({"type": "ping"}))
                continue
            await ws.send_text(json.dumps({
                "type": "line",
                "idx": line.idx,
                "ts": line.ts_from_start,
                "text": line.text,
                "speaker_raw": line.speaker_raw,
                "speaker_role": line.speaker_role,
                "speaker_name": line.speaker_name,
                "topics": line.topics,
            }))
    except WebSocketDisconnect:
        pass
    finally:
        session.unsubscribe(q)
