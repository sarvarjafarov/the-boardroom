"""Topic Router — classify each transcript line into one or more financial topics.

12-topic taxonomy chosen to map cleanly onto the categories the directors
care about (and that the EarningsEdge highlight-lexicon agent identified
as analyst-relevant):

  guidance | revenue | margins | capital_allocation | strategy | risk |
  qa | macro | M&A | regulatory | competitive | crisis

How it works:
  We don't classify each line one-by-one — that would be ~1 LLM call per
  line. Instead we batch 5-10 consecutive lines and ask a single Gemini
  3.5 Flash call to return a parallel array of topic-lists.

  Batching balances latency (we want labels available for downstream
  directors in <2s) against cost (one call per 5-10 lines, not per line).

The output topic tags are written back onto the TranscriptLine objects
in-place. Subsequent consumers (the director loops, the topic dashboard
in the UI) can read line.topics directly.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any

from google import genai
from google.genai import types

_log = logging.getLogger("boardroom.audio.topic_router")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
ROUTER_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")

VALID_TOPICS = (
    "guidance",
    "revenue",
    "margins",
    "capital_allocation",
    "strategy",
    "risk",
    "qa",
    "macro",
    "M&A",
    "regulatory",
    "competitive",
    "crisis",
)

BATCH_PROMPT = """You are a topic classifier for live financial audio transcripts (earnings calls, news, conferences, Fed pressers).

For each line below, return the topics it covers. A line may be tagged with 0, 1, 2, or 3 topics. Only use topics from this fixed list:

  guidance — forward outlook, guidance raised/lowered/reaffirmed
  revenue — top-line revenue figures, growth, segment breakdowns
  margins — gross margin, operating margin, cost structure
  capital_allocation — buybacks, dividends, capex, M&A spending, debt
  strategy — product roadmap, market positioning, AI/long-term narrative
  risk — risks named, hedging language, "near-term choppiness", "headwinds"
  qa — analyst questions or management answers to questions
  macro — Fed/rates/FX/inflation/cycle context
  M&A — mergers, acquisitions, divestitures, partnership announcements
  regulatory — regulatory filings, lawsuits, government policy impact
  competitive — competitor mentions, market share, pricing dynamics
  crisis — recalls, scandals, executive departures, major operational failures

Return a JSON ARRAY of arrays. The N-th inner array contains the topic strings for the N-th line. If a line is generic/operator boilerplate ("thank you for joining"), return an empty array for that line.

LINES:
{lines}

Return ONLY the JSON array, no prose."""


class TopicRouter:
    """Stateless wrapper around a Gemini Flash classification call."""

    def __init__(self) -> None:
        self._client: genai.Client | None = None

    def _ensure_client(self) -> genai.Client:
        if self._client is None:
            if not GEMINI_API_KEY:
                raise RuntimeError("GEMINI_API_KEY not set")
            self._client = genai.Client(api_key=GEMINI_API_KEY)
        return self._client

    async def classify_batch(self, lines: list[str]) -> list[list[str]]:
        """Classify a batch of lines. Returns parallel array of topic lists."""
        if not lines:
            return []
        formatted = "\n".join(f"[{i}] {line}" for i, line in enumerate(lines))
        prompt = BATCH_PROMPT.format(lines=formatted)
        client = self._ensure_client()
        try:
            resp = await client.aio.models.generate_content(
                model=ROUTER_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.0,
                    response_mime_type="application/json",
                ),
            )
            raw = (getattr(resp, "text", None) or "").strip()
        except Exception as exc:
            _log.warning("topic_router LLM call failed: %s", exc)
            return [[] for _ in lines]

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # Try to recover by extracting first [...] block.
            m = re.search(r"\[.*\]", raw, re.DOTALL)
            if not m:
                return [[] for _ in lines]
            try:
                data = json.loads(m.group(0))
            except json.JSONDecodeError:
                return [[] for _ in lines]

        if not isinstance(data, list) or len(data) != len(lines):
            # Safer to return empty than misalign topics with wrong lines.
            return [[] for _ in lines]

        cleaned: list[list[str]] = []
        for row in data:
            if not isinstance(row, list):
                cleaned.append([])
                continue
            row_topics = [
                t for t in row
                if isinstance(t, str) and t in VALID_TOPICS
            ]
            cleaned.append(row_topics)
        return cleaned
