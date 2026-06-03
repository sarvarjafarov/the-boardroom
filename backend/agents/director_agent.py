"""DirectorAgent — single director reasoning over the transcript stream.

This is NOT an ADK LlmAgent. It's a direct Gemini 3.5 Flash call per
score emission — much faster than wrapping each director in the
LlmAgent runner (which is built for multi-turn tool-call dialogs).

The Chairman IS an LlmAgent (because it routes voice Q&A through tools);
the directors are direct scorers because each emission is a single,
structured judgment call.

Per emission, the director:
  1. Pre-fetches grounding data based on its persona (patterns, analyst
     consensus, macro snapshot, etc.) — these are cheap MongoDB MCP
     queries or cached API calls.
  2. Builds a prompt: persona system instruction + recent topic lines +
     grounding context.
  3. Calls Gemini 3.5 Flash with JSON response mode to get a structured
     score_block.
  4. Persists the score_block via the MongoDB MCP tool layer.

Frequency control: a director only emits for a topic when it has at
least MIN_NEW_LINES_FOR_EMIT new lines on that topic since the last
emission, AND at least MIN_SECONDS_BETWEEN_EMITS seconds have elapsed.
This balances cost (Gemini calls) against responsiveness.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections import defaultdict
from typing import Any, TYPE_CHECKING

from google import genai
from google.genai import types

from .personas import get_director
from .tools.memory import search_pattern_library, search_track_records
from .tools.market import get_analyst_consensus, get_macro_snapshot, get_peer_comp
from .tools.portfolio import record_score_block

if TYPE_CHECKING:
    from audio.pipeline import AudioSession, TranscriptLine

_log = logging.getLogger("boardroom.director")

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")

# Per-topic emission rate-limiting.
MIN_NEW_LINES_FOR_EMIT = 3      # need 3 new topic-tagged lines since last emit
MIN_SECONDS_BETWEEN_EMITS = 8.0  # at least 8 s between emissions per topic
RECENT_LINES_WINDOW = 8         # send last 8 lines of the topic as context

# Prompt template the directors fill in per emission. Returns JSON.
SCORE_PROMPT = """{persona_system_prompt}

You are scoring the following SPECIFIC TOPIC from a live audio source:
  topic: {topic}
  ticker: {ticker}
  source: {source_label}

RECENT LINES on this topic (most recent last; line_idx in brackets):
{recent_lines_block}

GROUNDING CONTEXT (already fetched on your behalf):
{grounding_block}

YOUR TASK: Emit a score_block as a JSON object. Use only the data above —
do not invent figures. Score 0-100 where 50 is neutral.

REQUIRED JSON SHAPE:
{{
  "score": <0-100>,
  "confidence": "LOW" | "MEDIUM" | "HIGH",
  "reason": "<one sentence justification, max 280 chars>",
  "drivers": [
    {{"evidence": "<verbatim phrase from a recent line>", "direction": "bullish"|"bearish"|"neutral", "weight": <0.0-1.0>}}
  ],
  "supporting_line_indices": [<line_idx>, ...],
  "pattern_matched": "<pattern_id if a grounding pattern matched and you are citing it; else null>"
}}

Return ONLY the JSON object, no prose."""


class DirectorAgent:
    """One director reasoning over one AudioSession."""

    def __init__(self, director_id: str, session: "AudioSession") -> None:
        self.director_id = director_id
        self.session = session
        self.persona = get_director(director_id)

        # Topic-keyed state.
        self._topic_lines: dict[str, list["TranscriptLine"]] = defaultdict(list)
        self._unscored_lines_per_topic: dict[str, int] = defaultdict(int)
        self._last_emit_at_per_topic: dict[str, float] = defaultdict(lambda: 0.0)

        # Per-session cached grounding (fetched once at start).
        self._macro_cache: dict[str, Any] | None = None
        self._peer_cache: dict[str, Any] | None = None
        self._consensus_cache: dict[str, Any] | None = None
        self._track_record_cache: dict[str, Any] | None = None

        self._client: genai.Client | None = None
        self._running = False
        self._main_task: asyncio.Task[None] | None = None

    # --- Lifecycle --------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        # Subscribe via push callback so we don't compete with other subscribers.
        # The AudioSession awaits each callback, so we register the async
        # wrapper (the sync version is kept for direct dispatch in tests).
        self.session.on_line(self._on_line_async_compatible)
        self._main_task = asyncio.create_task(
            self._emission_loop(), name=f"director-{self.director_id}-{self.session.audio_id}"
        )
        # Warm grounding caches in the background — directors can start
        # scoring immediately with the empty cache; subsequent emissions
        # benefit from the fetched data.
        asyncio.create_task(self._warm_grounding_cache())
        _log.info("director %s started on audio %s", self.director_id, self.session.audio_id)

    async def stop(self) -> None:
        self._running = False
        if self._main_task:
            self._main_task.cancel()
            try:
                await self._main_task
            except (asyncio.CancelledError, Exception):
                pass
        _log.info("director %s stopped on audio %s", self.director_id, self.session.audio_id)

    # --- Internal: line intake and emission loop --------------------------

    def _on_line(self, line: "TranscriptLine") -> None:
        """Synchronous push from the AudioSession. Just buffers — no LLM."""
        # Topics may be empty initially (TopicRouter is async). We re-check
        # the same line in the emission loop in case topics have been filled
        # in since by the LineProcessor.
        for topic in line.topics or []:
            self._topic_lines[topic].append(line)
            # Trim the per-topic buffer to a working set; we never need more
            # than the last few dozen lines per topic.
            if len(self._topic_lines[topic]) > 40:
                self._topic_lines[topic].pop(0)
            self._unscored_lines_per_topic[topic] += 1

    async def _on_line_async_compatible(self, line: "TranscriptLine") -> None:
        """Awaitable wrapper for AudioSession's on_line callback contract."""
        self._on_line(line)

    async def _emission_loop(self) -> None:
        """Poll every 2 s for topics ready to emit; score them sequentially.

        Sequential rather than parallel keeps Gemini quota consumption
        predictable and prevents one stalled call from blocking others on
        the same director."""
        try:
            while self._running:
                await asyncio.sleep(2.0)
                ready = self._topics_ready_to_emit()
                for topic in ready:
                    try:
                        await self._emit_score_for_topic(topic)
                    except Exception as exc:
                        _log.warning(
                            "director %s emit failed on topic=%s: %s",
                            self.director_id, topic, exc,
                        )
        except asyncio.CancelledError:
            return

    def _topics_ready_to_emit(self) -> list[str]:
        now = time.monotonic()
        ready: list[str] = []
        for topic, count in list(self._unscored_lines_per_topic.items()):
            if count < MIN_NEW_LINES_FOR_EMIT:
                continue
            if (now - self._last_emit_at_per_topic[topic]) < MIN_SECONDS_BETWEEN_EMITS:
                continue
            ready.append(topic)
        return ready

    # --- Grounding --------------------------------------------------------

    async def _warm_grounding_cache(self) -> None:
        """Fetch persona-specific grounding once at session start."""
        ticker = self.session.ticker
        coros: list[Any] = []

        if self.director_id == "bull":
            coros.append(self._fetch_consensus(ticker))
        elif self.director_id == "bear":
            coros.append(self._fetch_track_record(ticker))
        elif self.director_id == "strategist":
            coros.append(self._fetch_macro())
            coros.append(self._fetch_peers(ticker))
        # Quant: no warm cache — reads numbers from the transcript itself.

        if coros:
            try:
                await asyncio.gather(*coros, return_exceptions=True)
            except Exception:
                pass

    async def _fetch_consensus(self, ticker: str | None) -> None:
        if not ticker:
            return
        try:
            self._consensus_cache = await get_analyst_consensus(ticker)
        except Exception:
            self._consensus_cache = None

    async def _fetch_macro(self) -> None:
        try:
            self._macro_cache = await get_macro_snapshot()
        except Exception:
            self._macro_cache = None

    async def _fetch_peers(self, ticker: str | None) -> None:
        if not ticker:
            return
        try:
            self._peer_cache = await get_peer_comp(ticker)
        except Exception:
            self._peer_cache = None

    async def _fetch_track_record(self, ticker: str | None) -> None:
        if not ticker:
            return
        try:
            self._track_record_cache = await search_track_records(
                director=self.director_id, ticker=ticker, limit=5,
            )
        except Exception:
            self._track_record_cache = None

    # --- Per-emission flow ------------------------------------------------

    async def _emit_score_for_topic(self, topic: str) -> None:
        recent = self._topic_lines[topic][-RECENT_LINES_WINDOW:]
        if not recent:
            return

        # Pattern lookup is per-topic and very cheap — always run it for
        # directors who own patterns (bear, bull, quant, strategist).
        matching_patterns = await self._fetch_patterns_for_topic(topic)

        # Build grounding block.
        grounding_lines: list[str] = []
        if self._consensus_cache and self.director_id == "bull":
            grounding_lines.append(
                f"Analyst consensus baseline_score={self._consensus_cache.get('baseline_score')} "
                f"label={self._consensus_cache.get('label')} "
                f"target_upside_pct={self._consensus_cache.get('target_upside_pct')}"
            )
        if self._macro_cache and self.director_id == "strategist":
            yc = self._macro_cache.get("yield_curve") or {}
            grounding_lines.append(
                f"Macro: policy_stance={self._macro_cache.get('policy_stance')} "
                f"2y={yc.get('2y')} 10y={yc.get('10y')} spread={yc.get('spread')} "
                f"cpi_yoy_pct={self._macro_cache.get('cpi_yoy_pct')}"
            )
        if self._peer_cache and self.director_id == "strategist":
            grounding_lines.append(
                f"Peer comp: target_peg={self._peer_cache.get('target_peg')} "
                f"median_peer_peg={self._peer_cache.get('median_peer_peg')} "
                f"premium_pct={self._peer_cache.get('premium_pct')} "
                f"peer_count={self._peer_cache.get('peer_count')}"
            )
        if self._track_record_cache and self.director_id == "bear":
            grounding_lines.append(
                f"Your prior track on {self._track_record_cache.get('ticker')}: "
                f"hit_rate={self._track_record_cache.get('hit_rate'):.0%} "
                f"sample_size={self._track_record_cache.get('sample_size')}"
            )
        if matching_patterns:
            for p in matching_patterns[:3]:
                grounding_lines.append(
                    f"Pattern {p['_id']!r}: '{p.get('name')}' "
                    f"hit_rate={p.get('hit_rate')} sample_size={p.get('sample_size')} "
                    f"trigger_keywords={p.get('trigger_keywords')}"
                )
        grounding_block = "\n".join(grounding_lines) if grounding_lines else "(none — score from the transcript alone)"

        # Build recent-lines block with line_idx for citation.
        recent_lines_block = "\n".join(
            f"[{l.idx}] ({l.speaker_role or l.speaker_raw or '?'}) {l.text}"
            for l in recent
        )

        prompt = SCORE_PROMPT.format(
            persona_system_prompt=self.persona["system_prompt"],
            topic=topic,
            ticker=self.session.ticker or "(unknown)",
            source_label=self.session.label,
            recent_lines_block=recent_lines_block,
            grounding_block=grounding_block,
        )

        score_block = await self._call_gemini_for_score(prompt)
        if not score_block:
            _log.debug("director %s topic=%s — no score (LLM call returned empty)",
                       self.director_id, topic)
            return

        # Derive label from score per the EarningsEdge schema.
        score = int(max(0, min(100, score_block.get("score", 50))))
        if score >= 65:
            label = "bullish"
        elif score <= 35:
            label = "bearish"
        else:
            label = "neutral"

        sb = {
            "audio_id": self.session.audio_id,
            "director": self.director_id,
            "topic": topic,
            "ts_from_start": recent[-1].ts_from_start,
            "score": score,
            "label": label,
            "confidence": score_block.get("confidence", "MEDIUM"),
            "reason": score_block.get("reason", ""),
            "drivers": score_block.get("drivers", [])[:8],
            "supporting_line_indices": score_block.get("supporting_line_indices", [])[:8],
            "sample_size": len(recent),
            "freshness": "fresh",
        }
        # Publish to the live UI bus first — this is non-blocking and lets
        # the frontend render even if MongoDB persistence is slow.
        try:
            from dashboard_bus import bus
            bus.publish(self.session.audio_id, "score_block", sb)
        except Exception:
            pass
        # Persist (non-fatal if Atlas is flaky).
        try:
            await record_score_block(
                audio_id=sb["audio_id"], director=sb["director"], topic=sb["topic"],
                ts_from_start=sb["ts_from_start"], score=sb["score"], label=sb["label"],
                confidence=sb["confidence"], reason=sb["reason"], drivers=sb["drivers"],
                supporting_line_indices=sb["supporting_line_indices"],
                sample_size=sb["sample_size"], freshness=sb["freshness"],
            )
        except Exception as exc:
            _log.debug("record_score_block deferred: %s", exc)

        # Reset counters for this topic.
        self._unscored_lines_per_topic[topic] = 0
        self._last_emit_at_per_topic[topic] = time.monotonic()
        _log.info(
            "director %s emitted topic=%s score=%d label=%s",
            self.director_id, topic, score, label,
        )

    async def _fetch_patterns_for_topic(self, topic: str) -> list[dict[str, Any]]:
        """Search the boardroom's pattern library for patterns relevant to
        this director + this topic. Patterns are keyword-triggered, so we
        derive a trigger keyword from the topic name."""
        # Map our 12-topic taxonomy onto pattern trigger keywords.
        topic_to_keyword = {
            "guidance": "guidance",
            "strategy": "GPU",         # The Bear's compute-promise pattern triggers on 'GPU' etc.
            "margins": "margin",
            "capital_allocation": "capital",
            "macro": "rate",
            "qa": "guidance",
            "risk": "headwind",
            "competitive": "competitive",
            "M&A": "acquisition",
            "regulatory": "regulatory",
            "crisis": "recall",
            "revenue": "guidance",
        }
        kw = topic_to_keyword.get(topic)
        try:
            return await search_pattern_library(
                director=self.director_id,
                trigger_keyword=kw,
                limit=3,
            )
        except Exception:
            return []

    # --- LLM call ---------------------------------------------------------

    def _ensure_client(self) -> genai.Client:
        if self._client is None:
            api_key = os.getenv("GEMINI_API_KEY", "")
            if not api_key:
                raise RuntimeError("GEMINI_API_KEY not set")
            self._client = genai.Client(api_key=api_key)
        return self._client

    async def _call_gemini_for_score(self, prompt: str) -> dict[str, Any] | None:
        client = self._ensure_client()
        try:
            resp = await client.aio.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.2,
                    response_mime_type="application/json",
                ),
            )
            raw = (getattr(resp, "text", None) or "").strip()
        except Exception as exc:
            _log.warning("director %s gemini call failed: %s", self.director_id, exc)
            return None

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            if not m:
                return None
            try:
                data = json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
        if not isinstance(data, dict):
            return None
        return data
