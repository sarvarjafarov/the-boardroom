"""DebateEngine — turns four independent scorers into a boardroom.

Every DEBATE_ROUND_S seconds, the engine:
  1. Pulls the LATEST score_block per (director, topic) from MongoDB for
     this audio_id.
  2. For each topic with ≥3 directors scoring it, computes pairwise
     disagreement (max-min spread + label divergence).
  3. Selects the top MAX_TOPICS_PER_ROUND most-contested topics.
  4. For each contested topic, picks the highest-scoring and lowest-
     scoring directors and prompts them to write directed rebuttals
     targeting each other's evidence.
  5. Persists both rebuttals to MongoDB → `arguments` collection.

A directed rebuttal is the seam between "N agents scored differently"
and "they are arguing". Each rebuttal cites:
  - the specific driver/evidence quote from the opponent it challenges
  - a specific transcript line idx that grounds the rebuttal
  - optionally, a pattern_id from the patterns library

Frequency control:
  - We track (topic → (high_director, low_director, high_score, low_score))
    of the last debate round per topic.
  - We only re-debate if scores have shifted >= DEBATE_RESHIFT_THRESHOLD,
    OR the high/low directors changed, OR enough time has passed.
  - This prevents Gemini-call spam when the same topic stays contested
    with stable scores.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any, TYPE_CHECKING

from google import genai
from google.genai import types

from .personas import get_director
from .tools._mcp_client import mcp_call
from .tools.portfolio import record_argument

if TYPE_CHECKING:
    from audio.pipeline import AudioSession

_log = logging.getLogger("boardroom.debate_engine")

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")

# Cadence + thresholds.
DEBATE_ROUND_S = 15                  # run a round every 15 s
MIN_DIRECTORS_SCORING_TOPIC = 3      # need at least 3 directors before debate
MIN_DISAGREEMENT_SPREAD = 25         # high - low score ≥ 25 to qualify
DEBATE_RESHIFT_THRESHOLD = 10        # re-debate if scores shifted ≥ 10
DEBATE_RESHIFT_TIMEOUT_S = 60        # OR ≥ 60 s since last debate of this topic
MAX_TOPICS_PER_ROUND = 2             # max contested topics to debate per round
RECENT_LINES_FOR_CONTEXT = 10        # how many transcript lines to show debaters


REBUTTAL_PROMPT = """{persona_system_prompt}

You are scoring topic "{topic}" for {ticker} on the live audio source: {source_label}.

Your score on this topic: {self_score}/100 ({self_label}, {self_confidence})
Your reasoning: "{self_reason}"
Your drivers:
{self_drivers_block}

Your colleague {opponent_name} ({opponent_subtitle}) scored the same topic
{opponent_score}/100 ({opponent_label}, {opponent_confidence}) with this reasoning:
  "{opponent_reason}"

{opponent_name}'s drivers (cite one of these in your rebuttal):
{opponent_drivers_block}

RECENT LINES on this topic (most recent last; quote one of these in your rebuttal):
{recent_lines_block}

YOUR TASK: Write a 1-sentence rebuttal of {opponent_name}'s position. The rebuttal must:
  - Quote ONE specific phrase from one of {opponent_name}'s drivers above (rebuttal_target_evidence)
  - Cite ONE specific transcript line by idx that supports your counter-claim
  - Stay in your persona's voice (use your characteristic style)

Return ONLY the following JSON:
{{
  "claim": "<your one-sentence rebuttal, max 240 chars>",
  "rebuttal_target_evidence": "<exact verbatim phrase from one of opponent's drivers>",
  "supports_line_idx": <transcript line idx (integer) you are citing>
}}"""


class DebateEngine:
    """One per AudioSession."""

    def __init__(self, session: "AudioSession") -> None:
        self.session = session
        self.audio_id = session.audio_id

        # Per-topic last-debate state, used to avoid spammy re-debates.
        # topic → {"high_dir", "low_dir", "high_score", "low_score", "at"}
        self._last_round_state: dict[str, dict[str, Any]] = {}

        self._client: genai.Client | None = None
        self._running = False
        self._task: asyncio.Task[None] | None = None

    # --- Lifecycle --------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(
            self._debate_loop(), name=f"debate-{self.audio_id}",
        )
        _log.info("debate_engine started on audio %s", self.audio_id)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        _log.info("debate_engine stopped on audio %s", self.audio_id)

    async def run_one_round_now(self) -> int:
        """Trigger an immediate round (smoke tests / final synthesis).
        Returns the number of debates emitted."""
        return await self._run_round()

    # --- Main loop --------------------------------------------------------

    async def _debate_loop(self) -> None:
        try:
            while self._running:
                await asyncio.sleep(DEBATE_ROUND_S)
                if not self._running:
                    return
                try:
                    n = await self._run_round()
                    if n:
                        _log.info(
                            "debate round on %s: emitted %d argument(s)",
                            self.audio_id, n,
                        )
                except Exception as exc:
                    _log.warning("debate round failed on %s: %s", self.audio_id, exc)
        except asyncio.CancelledError:
            return

    async def _run_round(self) -> int:
        """Find contested topics and emit rebuttals. Returns argument count."""
        blocks = await self._fetch_latest_score_blocks_per_director_topic()
        if not blocks:
            return 0

        contested = self._select_contested_topics(blocks)
        if not contested:
            return 0

        # Limit debates per round; the tail of contested is just noise.
        contested = contested[:MAX_TOPICS_PER_ROUND]
        emitted = 0
        for entry in contested:
            try:
                argument_count = await self._debate_topic(entry, blocks)
                emitted += argument_count
            except Exception as exc:
                _log.warning(
                    "debate_topic failed (topic=%s) on %s: %s",
                    entry.get("topic"), self.audio_id, exc,
                )
        return emitted

    # --- Topic selection --------------------------------------------------

    async def _fetch_latest_score_blocks_per_director_topic(self) -> dict[tuple[str, str], dict[str, Any]]:
        """Return (director, topic) → most-recent score_block for this audio."""
        rows = await mcp_call("find", {
            "database": "boardroom",
            "collection": "director_score_blocks",
            "filter": {"audio_id": self.audio_id},
            "sort": {"ts_from_start": -1},
            "limit": 200,
        })
        rows = rows if isinstance(rows, list) else []
        latest: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            key = (row.get("director"), row.get("topic"))
            if not key[0] or not key[1]:
                continue
            if key in latest:
                continue  # rows are sorted DESC; first hit is the latest
            latest[key] = row
        return latest

    def _select_contested_topics(
        self,
        latest: dict[tuple[str, str], dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Group blocks by topic, compute disagreement, return ranked list.

        Each returned entry has:
          {topic, high_dir, low_dir, high_score, low_score, spread,
           high_block, low_block, all_blocks_by_director}
        """
        by_topic: dict[str, dict[str, dict[str, Any]]] = {}
        for (director, topic), block in latest.items():
            by_topic.setdefault(topic, {})[director] = block

        candidates: list[dict[str, Any]] = []
        now = time.monotonic()
        for topic, by_director in by_topic.items():
            if len(by_director) < MIN_DIRECTORS_SCORING_TOPIC:
                continue
            sorted_blocks = sorted(by_director.items(), key=lambda kv: int(kv[1].get("score", 50)))
            low_director, low_block = sorted_blocks[0]
            high_director, high_block = sorted_blocks[-1]
            high_score = int(high_block.get("score", 50))
            low_score = int(low_block.get("score", 50))
            spread = high_score - low_score
            if spread < MIN_DISAGREEMENT_SPREAD:
                continue
            # Frequency control — skip if we recently debated the same
            # pair at substantially the same scores.
            last = self._last_round_state.get(topic)
            if last and (now - last.get("at", 0)) < DEBATE_RESHIFT_TIMEOUT_S:
                same_pair = (
                    last.get("high_dir") == high_director
                    and last.get("low_dir") == low_director
                )
                score_shift = (
                    abs(high_score - last.get("high_score", 50))
                    + abs(low_score - last.get("low_score", 50))
                )
                if same_pair and score_shift < DEBATE_RESHIFT_THRESHOLD:
                    continue
            candidates.append({
                "topic": topic,
                "high_dir": high_director,
                "low_dir": low_director,
                "high_score": high_score,
                "low_score": low_score,
                "spread": spread,
                "high_block": high_block,
                "low_block": low_block,
                "all_blocks_by_director": by_director,
            })

        # Rank by disagreement spread — biggest fights first.
        candidates.sort(key=lambda c: c["spread"], reverse=True)
        return candidates

    # --- Per-topic debate -------------------------------------------------

    async def _debate_topic(
        self,
        entry: dict[str, Any],
        all_latest_blocks: dict[tuple[str, str], dict[str, Any]],
    ) -> int:
        """Run a 2-rebuttal debate on one topic. Returns argument count emitted."""
        topic = entry["topic"]
        high_dir = entry["high_dir"]
        low_dir = entry["low_dir"]
        high_block = entry["high_block"]
        low_block = entry["low_block"]

        recent_lines = await self._fetch_recent_topic_lines(topic)
        ticker = self.session.ticker or "(unknown)"
        source_label = self.session.label

        # Both directors rebut at once for round-trip efficiency.
        high_rebuttal_coro = self._generate_rebuttal(
            self_director_id=high_dir,
            self_block=high_block,
            opponent_director_id=low_dir,
            opponent_block=low_block,
            topic=topic,
            ticker=ticker,
            source_label=source_label,
            recent_lines=recent_lines,
        )
        low_rebuttal_coro = self._generate_rebuttal(
            self_director_id=low_dir,
            self_block=low_block,
            opponent_director_id=high_dir,
            opponent_block=high_block,
            topic=topic,
            ticker=ticker,
            source_label=source_label,
            recent_lines=recent_lines,
        )
        high_arg, low_arg = await asyncio.gather(
            high_rebuttal_coro, low_rebuttal_coro, return_exceptions=True
        )

        emitted = 0
        ts = float(high_block.get("ts_from_start", 0.0))
        if isinstance(high_arg, dict) and high_arg.get("claim"):
            try:
                await record_argument(
                    audio_id=self.audio_id,
                    topic=topic,
                    ts_from_start=ts,
                    from_director=high_dir,
                    to_director=low_dir,
                    claim=high_arg["claim"],
                    rebuttal_target_evidence=high_arg.get("rebuttal_target_evidence", ""),
                    supports_line_idx=int(high_arg.get("supports_line_idx", -1) or -1),
                )
                emitted += 1
            except Exception as exc:
                _log.warning("record_argument failed: %s", exc)

        if isinstance(low_arg, dict) and low_arg.get("claim"):
            try:
                await record_argument(
                    audio_id=self.audio_id,
                    topic=topic,
                    ts_from_start=ts,
                    from_director=low_dir,
                    to_director=high_dir,
                    claim=low_arg["claim"],
                    rebuttal_target_evidence=low_arg.get("rebuttal_target_evidence", ""),
                    supports_line_idx=int(low_arg.get("supports_line_idx", -1) or -1),
                )
                emitted += 1
            except Exception as exc:
                _log.warning("record_argument failed: %s", exc)

        # Update last-round state.
        self._last_round_state[topic] = {
            "high_dir": high_dir,
            "low_dir": low_dir,
            "high_score": entry["high_score"],
            "low_score": entry["low_score"],
            "at": time.monotonic(),
        }
        return emitted

    async def _fetch_recent_topic_lines(self, topic: str) -> list[dict[str, Any]]:
        rows = await mcp_call("find", {
            "database": "boardroom",
            "collection": "transcript_lines",
            "filter": {"audio_id": self.audio_id, "topics": topic},
            "sort": {"idx": -1},
            "limit": RECENT_LINES_FOR_CONTEXT,
        })
        rows = rows if isinstance(rows, list) else []
        # Return chronological order (oldest → newest) for the prompt.
        return list(reversed(rows))

    # --- Rebuttal generation ---------------------------------------------

    async def _generate_rebuttal(
        self,
        *,
        self_director_id: str,
        self_block: dict[str, Any],
        opponent_director_id: str,
        opponent_block: dict[str, Any],
        topic: str,
        ticker: str,
        source_label: str,
        recent_lines: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        self_persona = get_director(self_director_id)
        opponent_persona = get_director(opponent_director_id)

        def _fmt_drivers(block: dict[str, Any]) -> str:
            drivers = block.get("drivers") or []
            if not drivers:
                return "(no drivers cited)"
            return "\n".join(
                f"  - [{d.get('direction')}] \"{d.get('evidence')}\""
                for d in drivers
            )

        recent_lines_block = "\n".join(
            f"[{l.get('idx')}] ({l.get('speaker_role') or '?'}) {l.get('text')}"
            for l in recent_lines
        ) or "(no recent lines on this topic)"

        prompt = REBUTTAL_PROMPT.format(
            persona_system_prompt=self_persona["system_prompt"],
            topic=topic,
            ticker=ticker,
            source_label=source_label,
            self_score=self_block.get("score"),
            self_label=self_block.get("label"),
            self_confidence=self_block.get("confidence"),
            self_reason=self_block.get("reason", ""),
            self_drivers_block=_fmt_drivers(self_block),
            opponent_name=opponent_persona["display_name"],
            opponent_subtitle=opponent_persona["subtitle"],
            opponent_score=opponent_block.get("score"),
            opponent_label=opponent_block.get("label"),
            opponent_confidence=opponent_block.get("confidence"),
            opponent_reason=opponent_block.get("reason", ""),
            opponent_drivers_block=_fmt_drivers(opponent_block),
            recent_lines_block=recent_lines_block,
        )

        client = self._ensure_client()
        try:
            resp = await client.aio.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.4,
                    response_mime_type="application/json",
                ),
            )
            raw = (getattr(resp, "text", None) or "").strip()
        except Exception as exc:
            _log.warning("rebuttal gemini call failed (%s vs %s): %s",
                         self_director_id, opponent_director_id, exc)
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
        if not isinstance(data, dict) or not data.get("claim"):
            return None
        return data

    def _ensure_client(self) -> genai.Client:
        if self._client is None:
            api_key = os.getenv("GEMINI_API_KEY", "")
            if not api_key:
                raise RuntimeError("GEMINI_API_KEY not set")
            self._client = genai.Client(api_key=api_key)
        return self._client


# --- Process-wide registry, mirrors DirectorEngine pattern ---------------

_engines: dict[str, DebateEngine] = {}
_lock = asyncio.Lock()


async def start_debate_for_session(session: "AudioSession") -> DebateEngine:
    async with _lock:
        if session.audio_id in _engines:
            return _engines[session.audio_id]
        engine = DebateEngine(session)
        _engines[session.audio_id] = engine
    await engine.start()
    return engine


async def stop_debate_for_session(audio_id: str) -> None:
    async with _lock:
        engine = _engines.pop(audio_id, None)
    if engine:
        await engine.stop()


def get_debate_engine(audio_id: str) -> DebateEngine | None:
    return _engines.get(audio_id)
