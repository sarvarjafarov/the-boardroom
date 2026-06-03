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
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from audio import SourceType, registry
from audio.line_processor import LineProcessor
from agents.director_engine import start_engine_for_session, stop_engine_for_session
from agents.debate_engine import start_debate_for_session, stop_debate_for_session
from dashboard_bus import bus
from atlas_writer import writer as atlas_writer

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


@app.on_event("startup")
async def _on_startup() -> None:
    await atlas_writer.start()


@app.on_event("shutdown")
async def _on_shutdown() -> None:
    await atlas_writer.stop()


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
        "atlas_writer": atlas_writer.stats(),
    }


@app.get("/api/debug/queue")
async def debug_queue() -> dict[str, Any]:
    return atlas_writer.stats()


@app.get("/api/diary/recent")
async def diary_recent(user_id: str = "demo", limit: int = 12) -> list[dict[str, Any]]:
    """Most-recent decisions the user logged, with their +7d / +30d outcomes.

    Powers the Decision Diary panel in the UI. Reads from MongoDB; falls
    back to an empty array if the read times out so the UI never breaks."""
    from agents.tools._mcp_client import mcp_call
    try:
        rows = await mcp_call("find", {
            "database": "boardroom",
            "collection": "decision_diary",
            "filter": {"user_id": user_id},
            "sort": {"executed_at": -1},
            "limit": int(limit),
        })
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


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

    # Spin up the 4 directors + the debate engine against this session.
    await start_engine_for_session(session)
    await start_debate_for_session(session)

    # Persist source record in MongoDB — fire-and-forget so the request
    # returns even if Atlas writes are temporarily slow.
    from agents.tools._mcp_client import mcp_call
    asyncio.create_task(mcp_call("insert-many", {
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
    }))
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

    # Spin up the 4 directors + the debate engine against this session.
    await start_engine_for_session(session)
    await start_debate_for_session(session)

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
    # Every Atlas-side operation here is non-fatal — we never want the UI
    # to hang on /api/sources/end. The verdict still surfaces via the
    # dashboard WS verdict event from the background synthesis task.
    try:
        await stop_debate_for_session(body.audio_id)
    except Exception:
        pass
    try:
        await stop_engine_for_session(body.audio_id)
    except Exception:
        pass
    proc = _processors.pop(body.audio_id, None)
    if proc:
        try:
            await proc.flush()
        except Exception:
            pass
        finally:
            proc.cleanup()
    session = registry.get(body.audio_id)
    ticker = session.ticker if session else None
    label = session.label if session else "audio source"
    try:
        await registry.stop(body.audio_id)
    except Exception:
        pass
    from agents.tools._mcp_client import mcp_call
    asyncio.create_task(mcp_call("update-many", {
        "database": "boardroom",
        "collection": "audio_sources",
        "filter": {"_id": body.audio_id},
        "update": {"$set": {"status": "ended"}},
    }))

    # Trigger the Chairman synthesis in the background; the frontend will
    # receive the verdict via the dashboard WS `verdict` event the moment
    # synthesis completes. The HTTP response returns immediately so the
    # UI doesn't hang on Atlas write latency.
    if ticker:
        from agents.chairman_synthesis import ChairmanSynthesizer
        synth = ChairmanSynthesizer()
        asyncio.create_task(synth.synthesize(
            audio_id=body.audio_id,
            ticker=ticker,
            source_label=label,
            user_id="demo",
        ))
    return {"ok": True, "synthesizing": bool(ticker)}


@app.get("/api/verdict/{audio_id}")
async def get_verdict(audio_id: str) -> dict[str, Any]:
    """Read the persisted verdict for a finished audio source."""
    from agents.tools._mcp_client import mcp_call
    rows = await mcp_call("find", {
        "database": "boardroom",
        "collection": "verdicts",
        "filter": {"audio_id": audio_id},
        "sort": {"_id": -1},
        "limit": 1,
    })
    rows = rows if isinstance(rows, list) else []
    if not rows:
        raise HTTPException(status_code=404, detail="no verdict for that audio_id")
    return rows[0]


class SynthesizeNowBody(BaseModel):
    audio_id: str
    ticker: str
    source_label: str = "live audio source"
    user_id: str = "demo"


@app.post("/api/verdict/synthesize_now")
async def synthesize_now(body: SynthesizeNowBody) -> dict[str, Any]:
    """Trigger an immediate verdict synthesis for a still-running session.
    Useful when the user taps 'get current verdict' before the source ends."""
    from agents.chairman_synthesis import ChairmanSynthesizer
    synth = ChairmanSynthesizer()
    try:
        verdict = await synth.synthesize(
            audio_id=body.audio_id,
            ticker=body.ticker,
            source_label=body.source_label,
            user_id=body.user_id,
        )
        return {"ok": True, "verdict": verdict}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


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


# --- Unified dashboard WebSocket (the live UI's single subscription) -----

@app.websocket("/ws/dashboard/{audio_id}")
async def ws_dashboard(ws: WebSocket, audio_id: str) -> None:
    """The frontend's one WebSocket per active boardroom.

    Streams every typed event for the session:
      transcript_line | score_block | argument | verdict | portfolio_impact | ping
    """
    await ws.accept()
    q = await bus.subscribe(audio_id)
    try:
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=20.0)
            except asyncio.TimeoutError:
                await ws.send_text(json.dumps({"type": "ping"}))
                continue
            await ws.send_text(json.dumps(msg, default=str))
    except WebSocketDisconnect:
        pass
    finally:
        await bus.unsubscribe(audio_id, q)


# --- Alpaca paper trade execution ----------------------------------------

class ConfirmTradeBody(BaseModel):
    audio_id: str
    ticker: str
    side: str               # buy | sell
    qty: float
    user_id: str = "demo"


@app.post("/api/orders/confirm")
async def confirm_trade(body: ConfirmTradeBody) -> dict[str, Any]:
    """User has approved a drafted trade. Send it to Alpaca paper.

    The frontend Approve button posts the impact's audio_id + ticker +
    side + qty. We submit to Alpaca paper via the imported TradeExecutor
    and mark the corresponding portfolio_impact draft as 'executed'.
    """
    from trade_executor import TradeExecutor
    from agents.tools._mcp_client import mcp_call

    side = body.side.lower().strip()
    if side not in ("buy", "sell"):
        raise HTTPException(status_code=400, detail="side must be 'buy' or 'sell'")
    if body.qty <= 0:
        raise HTTPException(status_code=400, detail="qty must be positive")

    executor = TradeExecutor()
    try:
        result = executor.submit_order(
            ticker=body.ticker.upper(),
            side=side,
            qty=int(body.qty),
            limit_price=None,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"alpaca submit failed: {exc}")

    # Mark the matching draft impact as executed.
    try:
        await mcp_call("update-many", {
            "database": "boardroom",
            "collection": "portfolio_impacts",
            "filter": {
                "audio_id": body.audio_id,
                "ticker": body.ticker.upper(),
                "side": side,
                "status": "pending",
            },
            "update": {"$set": {
                "status": "executed",
                "alpaca_order_id": result.get("id") if isinstance(result, dict) else None,
            }},
        })
    except Exception:
        pass

    # Publish so the UI can update the impact card to "executed".
    try:
        bus.publish(body.audio_id, "trade_executed", {
            "ticker": body.ticker.upper(),
            "side": side,
            "qty": body.qty,
            "alpaca_order_id": (result.get("id") if isinstance(result, dict) else None),
            "status": (result.get("status") if isinstance(result, dict) else None),
        })
    except Exception:
        pass

    return {"ok": True, "alpaca": result}


# --- Voice Q&A WebSocket -------------------------------------------------

@app.websocket("/ws/voice_qa/{audio_id}")
async def ws_voice_qa(ws: WebSocket, audio_id: str) -> None:
    """Real-time voice question loop.

    Frontend captures mic, sends raw 16 kHz mono Int16 PCM frames.
    We pipe them into a lightweight Gemini Live session for transcription,
    accumulate until the user pauses, then run the question through the
    Chairman LlmAgent and send back the text answer (and optionally TTS
    chunks).
    """
    from audio.gemini_live import GeminiLiveSession
    from agents.root_agent import root_agent
    from google.adk.runners import InMemoryRunner
    from google.genai import types as genai_types

    await ws.accept()
    live = GeminiLiveSession(f"voice-{audio_id}")
    try:
        await live.start()
    except Exception as exc:
        await ws.send_text(json.dumps({"type": "error", "message": f"voice live start: {exc}"}))
        await ws.close()
        return

    question_buffer: list[str] = []
    silence_seconds = 0.0

    async def consume_transcripts() -> None:
        async for text, ts, _ in live.transcript_lines():
            question_buffer.append(text)
            await ws.send_text(json.dumps({"type": "transcript_partial", "text": text}))

    transcript_task = asyncio.create_task(consume_transcripts())
    last_frame_at = asyncio.get_event_loop().time()

    try:
        while True:
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=2.0)
            except asyncio.TimeoutError:
                # No frames for 2 s — treat the buffered transcript as a complete question.
                if question_buffer:
                    full_q = " ".join(question_buffer).strip()
                    question_buffer.clear()
                    await _run_chairman_qa(ws, audio_id, full_q)
                continue
            if "bytes" in msg and msg["bytes"]:
                await live.send_pcm(msg["bytes"])
                last_frame_at = asyncio.get_event_loop().time()
            elif "text" in msg and msg["text"] == "stop":
                break
    except WebSocketDisconnect:
        pass
    finally:
        transcript_task.cancel()
        try:
            await transcript_task
        except (asyncio.CancelledError, Exception):
            pass
        try:
            await live.stop()
        except Exception:
            pass


async def _run_chairman_qa(ws: WebSocket, audio_id: str, question: str) -> None:
    """Hand a transcribed question to the Chairman LlmAgent and stream back text."""
    if not question or len(question) < 3:
        return
    from agents.root_agent import root_agent
    from google.adk.runners import InMemoryRunner
    from google.genai import types as genai_types

    runner = InMemoryRunner(agent=root_agent, app_name="boardroom_chairman")
    try:
        session = await runner.session_service.create_session(
            app_name="boardroom_chairman", user_id="demo",
        )
        # Prepend a context line so the Chairman knows which audio we're in.
        prompt = (
            f"You are in the live boardroom for audio_id={audio_id}. "
            f"User asked via voice: '{question}'. "
            "Identify which 1-3 directors are most relevant, fetch their score_blocks "
            "from MongoDB if helpful, and answer in 1-3 sentences. "
            "Stay in the Chairman voice — composed, structured, decisional."
        )
        async for event in runner.run_async(
            user_id="demo",
            session_id=session.id,
            new_message=genai_types.Content(
                role="user",
                parts=[genai_types.Part(text=prompt)],
            ),
        ):
            content = getattr(event, "content", None)
            if not content or not getattr(content, "parts", None):
                continue
            for part in content.parts:
                if getattr(part, "text", None):
                    await ws.send_text(json.dumps({
                        "type": "chairman_text",
                        "text": part.text,
                    }))
    except Exception as exc:
        await ws.send_text(json.dumps({"type": "error", "message": f"chairman: {exc}"}))


# --- Static frontend mount (served at /) ---------------------------------

_FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.isdir(_FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")
