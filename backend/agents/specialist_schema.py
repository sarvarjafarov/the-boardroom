"""Shared output envelope for specialist agents.

Every specialist produces a standardized score block alongside its legacy
panel-specific fields. The synthesizer uses these standardized fields to
compute a weighted committee signal. Legacy fields remain unchanged for
frontend compatibility.

All specialists emit a score_block with this shape:
{
    "score": int,              # 0-100, where 50 is neutral
    "label": str,              # "bullish" | "bearish" | "neutral"
    "confidence": str,         # "LOW" | "MEDIUM" | "HIGH"
    "reason": str,             # one-sentence plain-English justification
    "drivers": [               # evidence the score is built on
        {
            "evidence": str,   # the specific data point or phrase
            "direction": str,  # "bullish" | "bearish" | "neutral"
            "weight": float,   # 0.0-1.0, relative contribution in this agent
        },
        ...
    ],
    "sample_size": int,        # how many data points (sentences, articles)
    "freshness": str,          # "fresh" | "stale" | "unknown"
}

Score-to-label thresholds (uniform across all agents):
score >= 65   -> bullish
score <= 35   -> bearish
else          -> neutral

Confidence conventions:
HIGH   = multiple consistent signals, adequate sample size, fresh data
MEDIUM = signals mostly agree OR adequate sample OR fresh (two of three)
LOW    = thin sample, stale data, or conflicting internal signals
"""

from __future__ import annotations

from typing import Any


def make_score_block(
    score: int | float,
    confidence: str = "MEDIUM",
    reason: str = "",
    drivers: list[dict[str, Any]] | None = None,
    sample_size: int = 0,
    freshness: str = "unknown",
) -> dict[str, Any]:
    """Build the standardized score block. Clamps and normalizes defensively."""
    try:
        score_i = int(round(float(score)))
    except (TypeError, ValueError):
        score_i = 50

    score_i = max(0, min(100, score_i))

    if score_i >= 65:
        label = "bullish"
    elif score_i <= 35:
        label = "bearish"
    else:
        label = "neutral"

    conf = (confidence or "MEDIUM").upper().strip()
    if conf not in ("LOW", "MEDIUM", "HIGH"):
        conf = "MEDIUM"

    fresh = (freshness or "unknown").lower().strip()
    if fresh not in ("fresh", "stale", "unknown"):
        fresh = "unknown"

    return {
        "score": score_i,
        "label": label,
        "confidence": conf,
        "reason": (reason or "").strip()[:280],
        "drivers": list(drivers or [])[:8],
        "sample_size": max(0, int(sample_size)),
        "freshness": fresh,
    }


def make_driver(evidence: str, direction: str, weight: float = 0.5) -> dict[str, Any]:
    """Helper for building a single driver entry."""
    d = (direction or "neutral").lower().strip()
    if d not in ("bullish", "bearish", "neutral"):
        d = "neutral"

    try:
        w = float(weight or 0.0)
    except (TypeError, ValueError):
        w = 0.0

    w = max(0.0, min(1.0, w))

    return {
        "evidence": (evidence or "").strip()[:200],
        "direction": d,
        "weight": round(w, 2),
    }
