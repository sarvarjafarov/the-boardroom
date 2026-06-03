"""Portfolio + state tools — all reads/writes through MongoDB MCP."""
from __future__ import annotations

import os
from typing import Any

from ._mcp_client import mcp_call

DB = os.getenv("MONGODB_DB", "boardroom")


async def get_portfolio(user_id: str = "demo") -> dict[str, Any]:
    """Return the user's current positions + cash + target allocation.

    The boardroom must call this BEFORE recommending any portfolio action.
    Each director reasons about their own thesis; the Portfolio Impact
    Engine cross-references these positions to translate director views
    into specific trade actions.

    Args:
      user_id: Stable user identifier. Default 'demo' for hackathon single-user mode.

    Returns:
      {
        "user_id": str,
        "positions": [{ticker, qty, avg_cost, weight_pct}, ...],
        "cash_usd": float,
        "total_value_usd": float,
      }
    """
    rows = await mcp_call("find", {
        "database": DB, "collection": "portfolios",
        "filter": {"user_id": user_id}, "limit": 1,
    })
    if isinstance(rows, list) and rows:
        p = rows[0]
        return {
            "user_id": user_id,
            "positions": p.get("positions") or [],
            "cash_usd": p.get("cash_usd", 0.0),
            "total_value_usd": p.get("total_value_usd", 0.0),
        }
    return {"user_id": user_id, "positions": [], "cash_usd": 0.0, "total_value_usd": 0.0}


async def record_score_block(
    audio_id: str,
    director: str,
    topic: str,
    ts_from_start: float,
    score: int,
    label: str,
    confidence: str,
    reason: str,
    drivers: list[dict[str, Any]],
    supporting_line_indices: list[int],
    sample_size: int = 0,
    freshness: str = "fresh",
) -> dict[str, Any]:
    """Persist a director's per-topic reasoning as a score_block.

    Each director emits one of these every ~8 seconds per active topic.
    The score block is the atomic unit of director reasoning — the
    Chairman synthesis reads the latest of these per director × topic
    to compute the verdict.

    Args:
      audio_id: ObjectId hex string of the audio_sources row.
      director: One of 'bull', 'bear', 'quant', 'strategist'.
      topic: One of the 12 topic categories (guidance, revenue, ...).
      ts_from_start: Seconds elapsed from audio start.
      score: 0-100 directional score.
      label: 'bullish' | 'neutral' | 'bearish' (derived from score).
      confidence: 'LOW' | 'MEDIUM' | 'HIGH'.
      reason: One-sentence plain-English justification.
      drivers: [{evidence, direction, weight}, ...] supporting drivers.
      supporting_line_indices: Transcript line idxs that ground the score.
      sample_size: Number of transcript lines the score is based on.
      freshness: 'fresh' | 'stale' | 'unknown'.

    Returns:
      {"ok": True, "score": int, "label": str}
    """
    doc = {
        "audio_id": audio_id,
        "director": director,
        "topic": topic,
        "ts_from_start": float(ts_from_start),
        "score": int(score),
        "label": label,
        "confidence": confidence,
        "reason": reason,
        "drivers": list(drivers or []),
        "supporting_line_indices": list(supporting_line_indices or []),
        "sample_size": int(sample_size),
        "freshness": freshness,
    }
    await mcp_call("insert-many", {
        "database": DB, "collection": "director_score_blocks", "documents": [doc],
    })
    return {"ok": True, "score": int(score), "label": label}


async def record_argument(
    audio_id: str,
    topic: str,
    ts_from_start: float,
    from_director: str,
    to_director: str,
    claim: str,
    rebuttal_target_evidence: str,
    supports_line_idx: int,
    pattern_id: str | None = None,
) -> dict[str, Any]:
    """Persist a directed rebuttal between two directors on a specific topic.

    The Debate Engine calls this after the pairwise-disagreement compute
    identifies the highest- and lowest-scoring directors on a contested
    topic and prompts them to argue.

    Args:
      audio_id: ObjectId hex of the audio source.
      topic: The topic being argued.
      ts_from_start: Seconds elapsed from audio start when the rebuttal was made.
      from_director: Director who wrote the rebuttal.
      to_director: Director being rebutted.
      claim: One-sentence rebuttal.
      rebuttal_target_evidence: The specific quote from the other director's
        score block that is being challenged.
      supports_line_idx: Transcript line index that grounds the rebuttal.
      pattern_id: Optional pattern_id if the rebuttal cites a known pattern.

    Returns:
      {"ok": True}
    """
    doc = {
        "audio_id": audio_id,
        "topic": topic,
        "ts_from_start": float(ts_from_start),
        "from_director": from_director,
        "to_director": to_director,
        "claim": claim,
        "rebuttal_target_evidence": rebuttal_target_evidence,
        "supports_line_idx": int(supports_line_idx),
    }
    if pattern_id:
        doc["pattern_id"] = pattern_id
    await mcp_call("insert-many", {
        "database": DB, "collection": "arguments", "documents": [doc],
    })
    return {"ok": True}


async def record_verdict(
    audio_id: str,
    ticker: str,
    final_action: str,
    final_score: int,
    confidence: str,
    thesis: str,
    named_dissent: dict[str, Any],
    pattern_callouts: list[str],
    recommended_action_ids: list[str],
    citation_line_indices: list[int],
) -> dict[str, Any]:
    """Persist the Chairman's final synthesis verdict for an audio source.

    Called once per audio at the end of listening (or whenever the user
    asks for the current verdict). The verdict is the user-facing artifact
    — the structured 'what should I do' card.

    Args:
      audio_id: ObjectId hex of the audio source.
      ticker: The primary ticker the verdict applies to.
      final_action: 'Add' | 'Hold' | 'Trim' | 'Trim Aggressively'.
      final_score: 0-100 committee weighted score.
      confidence: 'LOW' | 'MEDIUM' | 'HIGH'.
      thesis: 2-3 sentence verdict reasoning.
      named_dissent: {director, score, thesis} — the strongest dissenter.
      pattern_callouts: List of pattern_ids that matched in this audio.
      recommended_action_ids: List of portfolio_impacts ObjectId hex strings.
      citation_line_indices: Transcript line idxs that ground the verdict.

    Returns:
      {"ok": True, "verdict_action": str}
    """
    doc = {
        "audio_id": audio_id,
        "ticker": ticker,
        "final_action": final_action,
        "final_score": int(final_score),
        "confidence": confidence,
        "thesis": thesis,
        "named_dissent": named_dissent,
        "pattern_callouts": list(pattern_callouts or []),
        "recommended_action_ids": list(recommended_action_ids or []),
        "citation_line_indices": list(citation_line_indices or []),
    }
    await mcp_call("insert-many", {
        "database": DB, "collection": "verdicts", "documents": [doc],
    })
    return {"ok": True, "verdict_action": final_action}


async def log_decision(
    user_id: str,
    verdict_id: str,
    audio_id: str,
    ticker: str,
    action_taken: str,
    qty_delta: float,
    execution_method: str = "manual",
    followed_recommendation: bool = True,
) -> dict[str, Any]:
    """Log a user action into the decision_diary collection.

    Called when the user taps 'I did this' on a verdict card. The diary
    drives the personal alpha tracker: at +7d and +30d the system scores
    the actual outcome against what the boardroom recommended.

    Args:
      user_id: Stable user identifier.
      verdict_id: ObjectId hex of the verdict that triggered this action.
      audio_id: ObjectId hex of the audio that generated the verdict.
      ticker: Affected ticker.
      action_taken: 'trimmed' | 'added' | 'held' | 'ignored'.
      qty_delta: Signed change in shares (negative = sold).
      execution_method: 'alpaca_paper' | 'manual'.
      followed_recommendation: Whether the user did what the boardroom advised.

    Returns:
      {"ok": True}
    """
    doc = {
        "user_id": user_id,
        "verdict_id": verdict_id,
        "audio_id": audio_id,
        "ticker": ticker,
        "action_taken": action_taken,
        "qty_delta": float(qty_delta),
        "execution_method": execution_method,
        "followed_recommendation": bool(followed_recommendation),
        "outcome_7d_pct": None,
        "outcome_30d_pct": None,
    }
    await mcp_call("insert-many", {
        "database": DB, "collection": "decision_diary", "documents": [doc],
    })
    return {"ok": True}
