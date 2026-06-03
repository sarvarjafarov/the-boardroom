"""Speaker Diarizer — map Gemini Live raw speaker IDs to human roles.

Gemini Live tags speakers with `speaker_id` ('0', '1', '2', ...) but doesn't
know who anyone IS. For earnings calls, news, conferences, and Fed pressers,
the directors care about the ROLE:

  CEO / CFO / EXECUTIVE — speaks from the company
  ANCHOR / HOST — runs the show (operator on calls, anchor on TV)
  GUEST — invited expert, NOT the host
  ANALYST — asks questions during Q&A
  UNKNOWN — can't tell yet

Strategy:
  - We track a rolling buffer of recent lines per raw speaker_id.
  - Every N seconds (or when a new speaker_id appears), we ask
    gemini-3.5-flash to classify each speaker based on what they've
    said so far.
  - We cache results per (audio_id, speaker_id) so the same speaker
    only gets classified once per session unless their pattern shifts.

The classifier is cheap and tolerant — if it can't decide, it returns
UNKNOWN and we keep listening.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections import defaultdict
from typing import Any

from google import genai
from google.genai import types

_log = logging.getLogger("boardroom.audio.diarizer")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
DIARIZER_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")

VALID_ROLES = ("CEO", "CFO", "EXECUTIVE", "ANCHOR", "GUEST", "ANALYST", "UNKNOWN")

DIARIZE_PROMPT = """You are a speaker classifier for live financial audio.

Below are short samples of what each speaker said during a transcript so far.
For each speaker, identify their most likely ROLE and, when possible, their
NAME based on what they say (e.g., "I'm Elon Musk, CEO of Tesla").

Valid roles (use exactly one of these):
  CEO          — chief executive of the company being discussed
  CFO          — chief financial officer
  EXECUTIVE    — other named C-suite or division head from the company
  ANCHOR       — host, operator, news anchor, conference moderator
  GUEST        — invited expert, NOT the host
  ANALYST      — buy-side or sell-side analyst asking questions
  UNKNOWN      — cannot determine yet

For NAME: extract a real name only when explicitly stated ("My name is..." /
"Joining us is...") or strongly implied. Otherwise return null.

Return a JSON OBJECT keyed by speaker_id. Each value: {{"role": "...", "name": null | "..."}}.

SAMPLES:
{samples}

Return ONLY the JSON object, no prose."""


class Diarizer:
    """Maintains per-audio-session diarization state and classifies speakers."""

    def __init__(self) -> None:
        self._client: genai.Client | None = None
        # Per-audio-id state: rolling line buffer per speaker + cached labels.
        self._buffers: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
        self._labels: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        self._lock = asyncio.Lock()

    def _ensure_client(self) -> genai.Client:
        if self._client is None:
            if not GEMINI_API_KEY:
                raise RuntimeError("GEMINI_API_KEY not set")
            self._client = genai.Client(api_key=GEMINI_API_KEY)
        return self._client

    def attach_line(self, audio_id: str, speaker_raw: str | None, text: str) -> None:
        """Append a line to the per-speaker rolling buffer."""
        if not speaker_raw:
            return
        buf = self._buffers[audio_id][speaker_raw]
        buf.append(text)
        # Keep last 8 lines per speaker — enough for classification, bounded.
        if len(buf) > 8:
            buf.pop(0)

    def cached_label(self, audio_id: str, speaker_raw: str | None) -> dict[str, Any] | None:
        if not speaker_raw:
            return None
        return self._labels[audio_id].get(speaker_raw)

    async def classify_all_speakers(self, audio_id: str) -> dict[str, dict[str, Any]]:
        """Re-classify every speaker we've seen on this audio session.

        Cheap to call repeatedly because we batch every known speaker into
        one LLM round trip. Returns the updated label map.
        """
        speakers = self._buffers.get(audio_id, {})
        if not speakers:
            return {}

        # Format the samples block: each speaker's recent lines.
        sample_lines: list[str] = []
        for sid, lines in speakers.items():
            joined = " ".join(lines[-8:])[:800]
            sample_lines.append(f'speaker_id "{sid}": "{joined}"')
        samples = "\n".join(sample_lines)

        prompt = DIARIZE_PROMPT.format(samples=samples)
        client = self._ensure_client()
        try:
            resp = await client.aio.models.generate_content(
                model=DIARIZER_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.0,
                    response_mime_type="application/json",
                ),
            )
            raw = (getattr(resp, "text", None) or "").strip()
        except Exception as exc:
            _log.warning("diarizer LLM call failed: %s", exc)
            return self._labels[audio_id]

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            if not m:
                return self._labels[audio_id]
            try:
                data = json.loads(m.group(0))
            except json.JSONDecodeError:
                return self._labels[audio_id]

        if not isinstance(data, dict):
            return self._labels[audio_id]

        async with self._lock:
            for sid, entry in data.items():
                if not isinstance(entry, dict):
                    continue
                role = entry.get("role")
                name = entry.get("name")
                if role not in VALID_ROLES:
                    role = "UNKNOWN"
                self._labels[audio_id][sid] = {
                    "role": role,
                    "name": name if isinstance(name, str) and name.strip() else None,
                }
        return self._labels[audio_id]

    def cleanup(self, audio_id: str) -> None:
        self._buffers.pop(audio_id, None)
        self._labels.pop(audio_id, None)
