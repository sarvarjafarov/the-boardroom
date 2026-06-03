"""ChairmanSynthesizer — the user-facing verdict generator.

End-to-end flow when called (typically at session end, or on user
trigger via /api/verdict/{audio_id}):

  1. Fetch all latest score_blocks per (director, topic) for the audio.
  2. Fetch all arguments emitted during this audio's debates.
  3. Run the boardroom committee: per-director composite + weighted
     aggregate + named dissent (via chairman_committee.run_boardroom_committee).
  4. Detect cross-call pattern callouts — patterns the directors cited
     during their score_block reasoning are pulled and ranked.
  5. Generate a prose thesis with Gemini 3.5 Flash, using the Chairman
     persona prompt + structured committee output. Strictly JSON shape.
  6. Run the Portfolio Impact Engine: read the user's actual holdings,
     score impact on the audio's primary ticker, generate specific
     action recommendations grounded in the verdict.
  7. Draft paper trades (via draft_paper_trade tool) for each meaningful
     action. The user later approves them via /api/orders/confirm.
  8. Persist the final verdict to MongoDB via record_verdict tool.

The Chairman LlmAgent (root_agent.py) calls this whole pipeline as a
single tool (synthesize_verdict) when responding to user requests for
the verdict. That keeps the slow LLM-routing path off the synthesis
hot path while preserving the LlmAgent shape for Vertex AI Agent Engine
compliance.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any

from google import genai
from google.genai import types

from .chairman_committee import run_boardroom_committee
from .personas import get_director
from .tools._mcp_client import mcp_call
from .tools.execution import draft_paper_trade
from .tools.portfolio import get_portfolio, record_verdict

_log = logging.getLogger("boardroom.chairman_synthesis")

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")

THESIS_PROMPT = """{chairman_system_prompt}

You are writing the final boardroom VERDICT after listening to:
  Source:   {source_label}
  Ticker:   {ticker}

The four directors' composite scores from the boardroom committee:
{composites_block}

Weighted committee score: {committee_score}/100 ({signal}, {confidence} confidence)
Disagreement spread:      {disagreement_spread} points (max - min across directors)

Named dissent: {dissent_block}

Pattern callouts that fired in this audio (each from the boardroom's
historical pattern library — hit_rate is over real prior instances):
{patterns_block}

Top arguments exchanged during the debate (most-disagreed first):
{arguments_block}

YOUR TASK: Write the boardroom verdict as a JSON object. Speak as the
Chairman — composed, structured, decisional. Do not invent figures.
Quote evidence verbatim where present.

REQUIRED JSON SHAPE:
{{
  "thesis": "<2-3 sentences. State the action ({signal}) + confidence
    ({confidence}) + the central reason that drove the committee score.
    Reference the named dissent respectfully.>",
  "key_dissent_narrative": "<1-2 sentences. Quote the dissenting
    director's specific argument, then state how the committee weighed
    it.>",
  "pattern_callout_narrative": "<0-1 sentence per fired pattern. Quote
    the pattern's prior-instance outcome.>"
}}

Return ONLY the JSON object, no prose."""


# Severity thresholds for portfolio actions.
SEVERITY_DRIFT_MEANINGFUL = 0.05    # > 5 % drift triggers meaningful
SEVERITY_DRIFT_URGENT = 0.10        # > 10 % drift triggers urgent
SEVERITY_SCORE_URGENT = 25          # committee_score ≤ 25 or ≥ 75 → urgent regardless


class ChairmanSynthesizer:
    """Stateless. One instance can synthesize verdicts for many audio_ids."""

    def __init__(self) -> None:
        self._client: genai.Client | None = None

    def _ensure_client(self) -> genai.Client:
        if self._client is None:
            api_key = os.getenv("GEMINI_API_KEY", "")
            if not api_key:
                raise RuntimeError("GEMINI_API_KEY not set")
            self._client = genai.Client(api_key=api_key)
        return self._client

    # --- Public entry point ----------------------------------------------

    async def synthesize(
        self,
        audio_id: str,
        ticker: str,
        source_label: str,
        user_id: str = "demo",
    ) -> dict[str, Any]:
        """Produce + persist the final verdict for an audio source.

        Returns the verdict dict (everything that was persisted).
        """
        _log.info("chairman synthesis start audio_id=%s ticker=%s", audio_id, ticker)

        # 1. Load everything.
        latest_blocks = await self._fetch_latest_blocks(audio_id)
        arguments = await self._fetch_arguments(audio_id)
        pattern_callouts = await self._detect_pattern_callouts(latest_blocks)
        previous_verdict = await self._fetch_previous_verdict(audio_id)
        previous_signal = previous_verdict.get("signal") if previous_verdict else None

        if not latest_blocks:
            verdict = self._empty_verdict(audio_id, ticker)
            await self._persist_verdict(verdict)
            return verdict

        # 2. Run committee math.
        committee = run_boardroom_committee(
            latest_blocks, previous_signal=previous_signal,
        )

        # 3. Generate the prose with the Chairman persona.
        prose = await self._generate_thesis(
            ticker=ticker,
            source_label=source_label,
            committee=committee,
            pattern_callouts=pattern_callouts,
            arguments=arguments,
        )

        # 4. Portfolio impact + draft trades.
        portfolio = await get_portfolio(user_id)
        impacts = await self._compute_portfolio_impacts(
            audio_id=audio_id,
            user_id=user_id,
            ticker=ticker,
            committee=committee,
            portfolio=portfolio,
            patterns=pattern_callouts,
        )

        # 5. Draft paper trades for non-trivial impacts. (Day 6 wires the
        # confirm endpoint to Alpaca paper.)
        drafted_ids = await self._draft_trades(
            audio_id=audio_id,
            user_id=user_id,
            committee=committee,
            impacts=impacts,
        )

        # 6. Assemble + persist verdict.
        citation_idxs = self._collect_citation_indices(latest_blocks, arguments)
        verdict = {
            "audio_id": audio_id,
            "ticker": ticker,
            "final_action": committee["signal"],
            "final_score": committee["score"],
            "confidence": committee["confidence"],
            "thesis": prose.get("thesis") or committee.get("thesis", ""),
            "key_dissent_narrative": prose.get("key_dissent_narrative", ""),
            "pattern_callout_narrative": prose.get("pattern_callout_narrative", ""),
            "named_dissent": committee.get("named_dissent"),
            "pattern_callouts": [p["_id"] for p in pattern_callouts],
            "disagreement_spread": committee.get("disagreement_spread", 0),
            "votes": committee.get("votes", []),
            "composites": committee.get("composites", {}),
            "portfolio_impacts": impacts,
            "drafted_trade_ids": drafted_ids,
            "citation_line_indices": citation_idxs,
            "ts": time.time(),
        }
        await self._persist_verdict(verdict)
        _log.info(
            "chairman synthesis done audio_id=%s action=%s score=%d confidence=%s drafts=%d",
            audio_id, verdict["final_action"], verdict["final_score"],
            verdict["confidence"], len(drafted_ids),
        )
        return verdict

    # --- Data fetch -------------------------------------------------------

    async def _fetch_latest_blocks(
        self, audio_id: str
    ) -> dict[tuple[str, str], dict[str, Any]]:
        rows = await mcp_call("find", {
            "database": "boardroom",
            "collection": "director_score_blocks",
            "filter": {"audio_id": audio_id},
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
                continue
            latest[key] = row
        return latest

    async def _fetch_arguments(self, audio_id: str) -> list[dict[str, Any]]:
        rows = await mcp_call("find", {
            "database": "boardroom",
            "collection": "arguments",
            "filter": {"audio_id": audio_id},
            "sort": {"ts_from_start": 1},
            "limit": 50,
        })
        return rows if isinstance(rows, list) else []

    async def _fetch_previous_verdict(self, audio_id: str) -> dict[str, Any] | None:
        rows = await mcp_call("find", {
            "database": "boardroom",
            "collection": "verdicts",
            "filter": {"audio_id": audio_id},
            "sort": {"ts": -1},
            "limit": 1,
        })
        rows = rows if isinstance(rows, list) else []
        return rows[0] if rows else None

    # --- Pattern callout detection ---------------------------------------

    async def _detect_pattern_callouts(
        self,
        latest_blocks: dict[tuple[str, str], dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Surface patterns that fired during the audio.

        Strategy:
          1. Scan score_blocks' reason text for phrases that match seeded
             pattern names or trigger keywords.
          2. Score-rank by which director cited them with highest weight.
        """
        # Load every pattern from MongoDB. Cheap — ~8 documents.
        patterns = await mcp_call("find", {
            "database": "boardroom",
            "collection": "patterns",
            "filter": {},
            "limit": 50,
        })
        patterns = patterns if isinstance(patterns, list) else []
        if not patterns:
            return []

        # Concatenate all director reason text + driver evidence into one
        # haystack we can keyword-match against pattern triggers.
        haystack_parts: list[str] = []
        for block in latest_blocks.values():
            haystack_parts.append(str(block.get("reason", "")))
            for d in block.get("drivers") or []:
                haystack_parts.append(str(d.get("evidence", "")))
        haystack = " ".join(haystack_parts).lower()
        if not haystack.strip():
            return []

        # Match. A pattern fires if ANY of its trigger_keywords appears in
        # the haystack (case-insensitive substring match).
        fired: list[dict[str, Any]] = []
        for p in patterns:
            triggers = [str(k).lower() for k in p.get("trigger_keywords") or []]
            if not triggers:
                continue
            if any(t in haystack for t in triggers):
                fired.append(p)
        # Rank by hit_rate then sample_size — high-confidence patterns first.
        fired.sort(
            key=lambda p: (float(p.get("hit_rate", 0)), int(p.get("sample_size", 0))),
            reverse=True,
        )
        return fired[:5]

    # --- Thesis generation -----------------------------------------------

    async def _generate_thesis(
        self,
        *,
        ticker: str,
        source_label: str,
        committee: dict[str, Any],
        pattern_callouts: list[dict[str, Any]],
        arguments: list[dict[str, Any]],
    ) -> dict[str, str]:
        chairman = get_director("chairman")
        composites = committee.get("composites", {})
        composites_lines = [
            f"  {d:11s}  score={c['score']:3d}/100  label={c['label']:8s}  "
            f"confidence={c['confidence']}  reason: \"{c['reason'][:140]}\""
            for d, c in composites.items()
        ]
        composites_block = "\n".join(composites_lines) or "  (no scores yet)"

        dissent = committee.get("named_dissent")
        if dissent:
            dissent_block = (
                f"{dissent.get('director', '?').title()} "
                f"({dissent.get('score', 0)}/100): {dissent.get('thesis', '')}"
            )
        else:
            dissent_block = "(no significant dissent — directors aligned)"

        patterns_lines = []
        for p in pattern_callouts[:3]:
            patterns_lines.append(
                f"  - {p.get('_id', '?')}: \"{p.get('name', '')}\". "
                f"hit_rate={p.get('hit_rate')} sample_size={p.get('sample_size')}. "
                f"median_drawdown_7d={p.get('median_drawdown_7d')} "
                f"median_run_7d={p.get('median_run_7d')}."
            )
        patterns_block = "\n".join(patterns_lines) or "  (no patterns fired)"

        argument_lines = []
        for arg in arguments[:6]:
            argument_lines.append(
                f"  [{arg.get('topic', '?')}] {arg.get('from_director', '?').upper()} → "
                f"{arg.get('to_director', '?').upper()}: \"{arg.get('claim', '')}\""
            )
        arguments_block = "\n".join(argument_lines) or "  (no arguments emitted yet)"

        prompt = THESIS_PROMPT.format(
            chairman_system_prompt=chairman["system_prompt"],
            source_label=source_label,
            ticker=ticker,
            composites_block=composites_block,
            committee_score=committee.get("score"),
            signal=committee.get("signal"),
            confidence=committee.get("confidence"),
            disagreement_spread=committee.get("disagreement_spread"),
            dissent_block=dissent_block,
            patterns_block=patterns_block,
            arguments_block=arguments_block,
        )

        client = self._ensure_client()
        try:
            resp = await client.aio.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.3,
                    response_mime_type="application/json",
                ),
            )
            raw = (getattr(resp, "text", None) or "").strip()
        except Exception as exc:
            _log.warning("thesis generation failed: %s", exc)
            return {"thesis": committee.get("thesis", ""),
                    "key_dissent_narrative": "",
                    "pattern_callout_narrative": ""}

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            if not m:
                return {"thesis": committee.get("thesis", ""),
                        "key_dissent_narrative": "",
                        "pattern_callout_narrative": ""}
            try:
                data = json.loads(m.group(0))
            except json.JSONDecodeError:
                return {"thesis": committee.get("thesis", ""),
                        "key_dissent_narrative": "",
                        "pattern_callout_narrative": ""}
        if not isinstance(data, dict):
            return {"thesis": "", "key_dissent_narrative": "", "pattern_callout_narrative": ""}
        return {
            "thesis": str(data.get("thesis") or "").strip(),
            "key_dissent_narrative": str(data.get("key_dissent_narrative") or "").strip(),
            "pattern_callout_narrative": str(data.get("pattern_callout_narrative") or "").strip(),
        }

    # --- Portfolio Impact Engine -----------------------------------------

    async def _compute_portfolio_impacts(
        self,
        *,
        audio_id: str,
        user_id: str,
        ticker: str,
        committee: dict[str, Any],
        portfolio: dict[str, Any],
        patterns: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """For each held position related to the audio's ticker, compute
        severity + recommended action grounded in the committee verdict.

        For MVP we handle direct exposure to the audio's ticker only.
        Day 7 polish can extend to sector/factor correlation.
        """
        positions = portfolio.get("positions") or []
        if not positions or not ticker:
            return []

        committee_score = int(committee.get("score", 50))
        signal = committee.get("signal", "Hold")
        thesis = (committee.get("thesis", "") or "")[:200]
        named_dissent = committee.get("named_dissent")

        impacts: list[dict[str, Any]] = []
        for pos in positions:
            pos_ticker = str(pos.get("ticker", "")).upper()
            if pos_ticker != ticker.upper():
                continue
            qty = float(pos.get("qty") or 0)
            current_weight = float(pos.get("weight_pct") or 0.0)

            # Severity = a function of how far the committee is from 50
            # neutral AND how big the position is.
            score_drift = abs(committee_score - 50) / 50.0
            position_size_factor = min(1.0, current_weight / 0.15)  # >= 15 % position = full weight
            severity_score = score_drift * position_size_factor

            if severity_score >= 0.6 or committee_score <= SEVERITY_SCORE_URGENT or committee_score >= (100 - SEVERITY_SCORE_URGENT):
                severity = "urgent"
            elif severity_score >= 0.3:
                severity = "meaningful"
            else:
                severity = "minor"

            # Recommended action.
            if signal in ("Trim Aggressively",):
                target_weight = max(0.0, current_weight * 0.5)
                side = "sell"
            elif signal in ("Trim",):
                target_weight = max(0.0, current_weight * 0.7)
                side = "sell"
            elif signal in ("Add Aggressively",):
                target_weight = min(0.40, current_weight * 1.5)
                side = "buy"
            elif signal in ("Add",):
                target_weight = min(0.30, current_weight * 1.2)
                side = "buy"
            else:
                target_weight = current_weight
                side = "hold"

            weight_delta = target_weight - current_weight
            qty_delta = round(qty * (weight_delta / current_weight)) if current_weight > 0 else 0

            rationale_parts = [thesis]
            if named_dissent:
                rationale_parts.append(
                    f"Dissent ({named_dissent.get('director', '?').title()}): "
                    f"{named_dissent.get('thesis', '')[:120]}"
                )
            if patterns:
                top = patterns[0]
                rationale_parts.append(
                    f"Pattern: {top.get('name', '')} "
                    f"(hit_rate {top.get('hit_rate')}, n={top.get('sample_size')})"
                )

            impacts.append({
                "audio_id": audio_id,
                "user_id": user_id,
                "ticker": pos_ticker,
                "current_position_pct": round(current_weight, 4),
                "current_qty": qty,
                "target_position_pct": round(target_weight, 4),
                "qty_delta": qty_delta,
                "side": side,
                "severity": severity,
                "rationale": " | ".join(rationale_parts)[:600],
                "contributing_directors": [
                    v.get("director") for v in committee.get("votes") or []
                    if v.get("director")
                ],
            })

        # Persist to MongoDB for later inspection / UI rendering. Non-fatal:
        # if Atlas is transiently unreachable we still want the user-facing
        # verdict to be returned with the in-memory impacts.
        if impacts:
            try:
                await mcp_call("insert-many", {
                    "database": "boardroom",
                    "collection": "portfolio_impacts",
                    "documents": impacts,
                })
            except Exception as exc:
                _log.warning(
                    "portfolio_impacts persist failed (continuing): %s", exc,
                )
        return impacts

    # --- Trade drafting --------------------------------------------------

    async def _draft_trades(
        self,
        *,
        audio_id: str,
        user_id: str,
        committee: dict[str, Any],
        impacts: list[dict[str, Any]],
    ) -> list[str]:
        """Persist a draft paper trade per impact that requires action.

        Holds (qty_delta == 0) skip the draft. Day-6 UI surfaces these
        drafts next to the verdict for one-tap user approval.
        """
        drafted_ids: list[str] = []
        verdict_id = ""  # filled in by record_verdict; for now leave blank
        for impact in impacts:
            qty_delta = int(impact.get("qty_delta", 0) or 0)
            if qty_delta == 0:
                continue
            side = "buy" if qty_delta > 0 else "sell"
            try:
                result = await draft_paper_trade(
                    user_id=user_id,
                    audio_id=audio_id,
                    verdict_id=verdict_id,
                    ticker=impact["ticker"],
                    side=side,
                    qty=abs(qty_delta),
                    rationale=impact.get("rationale", ""),
                )
            except Exception as exc:
                _log.warning("draft_paper_trade failed (continuing): %s", exc)
                result = {"ok": False}
            if result.get("ok"):
                # We don't have the inserted id back from MCP cleanly; use a
                # synthetic marker so downstream code can count drafts.
                drafted_ids.append(f"{audio_id}:{impact['ticker']}:{side}")
        return drafted_ids

    # --- Persistence ------------------------------------------------------

    async def _persist_verdict(self, verdict: dict[str, Any]) -> None:
        """Persist the verdict, but never fail the synthesis if MongoDB is
        transiently unreachable. The user-facing verdict is the in-memory
        dict returned by synthesize() — persistence is for history.
        """
        try:
            await record_verdict(
                audio_id=verdict["audio_id"],
                ticker=verdict["ticker"],
                final_action=verdict["final_action"],
                final_score=int(verdict["final_score"]),
                confidence=verdict["confidence"],
                thesis=verdict["thesis"],
                named_dissent=verdict.get("named_dissent") or {},
                pattern_callouts=verdict.get("pattern_callouts", []),
                recommended_action_ids=verdict.get("drafted_trade_ids", []),
                citation_line_indices=verdict.get("citation_line_indices", []),
            )
        except Exception as exc:
            _log.warning(
                "verdict persistence failed (continuing) audio_id=%s err=%s",
                verdict.get("audio_id"), exc,
            )
            verdict["_persistence_warning"] = str(exc)

    def _collect_citation_indices(
        self,
        latest_blocks: dict[tuple[str, str], dict[str, Any]],
        arguments: list[dict[str, Any]],
    ) -> list[int]:
        idxs: set[int] = set()
        for block in latest_blocks.values():
            for i in block.get("supporting_line_indices") or []:
                try:
                    idxs.add(int(i))
                except (TypeError, ValueError):
                    pass
        for arg in arguments:
            i = arg.get("supports_line_idx")
            if i is not None:
                try:
                    idxs.add(int(i))
                except (TypeError, ValueError):
                    pass
        return sorted(idxs)

    def _empty_verdict(self, audio_id: str, ticker: str) -> dict[str, Any]:
        return {
            "audio_id": audio_id,
            "ticker": ticker,
            "final_action": "Hold",
            "final_score": 50,
            "confidence": "LOW",
            "thesis": "Insufficient signal — waiting for directors to score.",
            "key_dissent_narrative": "",
            "pattern_callout_narrative": "",
            "named_dissent": None,
            "pattern_callouts": [],
            "disagreement_spread": 0,
            "votes": [],
            "composites": {},
            "portfolio_impacts": [],
            "drafted_trade_ids": [],
            "citation_line_indices": [],
            "ts": time.time(),
        }
