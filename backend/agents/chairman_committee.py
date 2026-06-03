"""Boardroom-specific committee math.

EarningsEdge's `committee.py` (imported Day 1) was designed for the
EarningsEdge specialist set (analyst, news, macro, peer, technical,
metrics, sentiment) and weights those by their relevance during
coverage vs live modes.

The Boardroom has a different shape: 4 directors, each emitting
multiple score_blocks across multiple topics. We need to:
  1. Aggregate each director's per-topic score_blocks into ONE
     per-director composite score.
  2. Run a weighted committee across the 4 directors, applying
     confidence multipliers and hysteresis.
  3. Identify the named dissent — the director whose composite is
     furthest from the committee score.

We use EarningsEdge's compute_committee internally for steps 2-3 by
mapping our 4 directors into EarningsEdge's specialist slot names so
the well-tested hysteresis + disagreement-spread logic transfers
unchanged.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from .committee import compute_committee, LIVE_WEIGHTS


# Per-topic importance weights when computing a director's composite
# score. Mirrors the analyst-relevance hierarchy from EarningsEdge's
# highlight-lexicon agent — guidance and margins matter most because
# they directly drive price moves on the day; macro and competitive
# matter as context.
TOPIC_WEIGHTS = {
    "guidance":           0.18,
    "revenue":            0.13,
    "margins":            0.13,
    "capital_allocation": 0.10,
    "strategy":           0.10,
    "risk":               0.08,
    "qa":                 0.07,
    "macro":              0.07,
    "M&A":                0.05,
    "regulatory":         0.04,
    "competitive":        0.03,
    "crisis":             0.02,
}

# Confidence-to-multiplier mapping, applied when aggregating per-director.
_CONF_MULT = {"HIGH": 1.0, "MEDIUM": 0.75, "LOW": 0.5}

# How to map our 4 directors onto EarningsEdge specialist-name slots so
# we can reuse compute_committee unchanged. Picking 4 slots whose LIVE
# weights are close to equal — when we re-normalize inside the engine,
# this gives each director roughly 25 % effective base weight.
DIRECTOR_TO_SPECIALIST_SLOT = {
    "bull":       "metrics",     # 0.21
    "bear":       "sentiment",   # 0.21
    "quant":      "news",        # 0.18
    "strategist": "macro",       # 0.10  ← intentionally lighter (Strategist is contextual)
}


def _aggregate_per_director(
    blocks_by_director_topic: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Compute a per-director composite score_block from per-topic blocks.

    For each director, the composite score is:
      Σ (topic_weight * confidence_multiplier * topic_score) /
      Σ (topic_weight * confidence_multiplier)

    Other fields are derived for the committee engine's consumption.
    """
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for (director, _topic), block in blocks_by_director_topic.items():
        if director and block:
            grouped[director].append(block)

    composites: dict[str, dict[str, Any]] = {}
    for director, blocks in grouped.items():
        num = 0.0
        den = 0.0
        sample_size = 0
        any_high_conf = False
        any_low_conf = False
        latest_drivers: list[dict[str, Any]] = []
        latest_reason = ""
        latest_freshness = "fresh"
        for block in blocks:
            topic = block.get("topic", "")
            score = float(block.get("score", 50))
            conf = str(block.get("confidence", "MEDIUM")).upper()
            tw = TOPIC_WEIGHTS.get(topic, 0.04)
            cm = _CONF_MULT.get(conf, 0.75)
            num += tw * cm * score
            den += tw * cm
            sample_size += int(block.get("sample_size", 0))
            if conf == "HIGH":
                any_high_conf = True
            if conf == "LOW":
                any_low_conf = True
            # Keep the most recent reason / drivers for thesis use.
            if not latest_reason:
                latest_reason = str(block.get("reason", ""))
                latest_drivers = list(block.get("drivers") or [])[:6]
                latest_freshness = str(block.get("freshness") or "fresh")

        if den <= 0:
            continue
        composite_score = num / den
        if composite_score >= 65:
            label = "bullish"
        elif composite_score <= 35:
            label = "bearish"
        else:
            label = "neutral"
        # Composite confidence: HIGH only when at least one HIGH and no LOW;
        # MEDIUM when no HIGH; LOW when ANY LOW dominates.
        if any_low_conf and not any_high_conf:
            composite_conf = "LOW"
        elif any_high_conf and not any_low_conf:
            composite_conf = "HIGH"
        else:
            composite_conf = "MEDIUM"

        composites[director] = {
            "score": int(round(composite_score)),
            "label": label,
            "confidence": composite_conf,
            "reason": latest_reason,
            "drivers": latest_drivers,
            "sample_size": sample_size,
            "freshness": latest_freshness,
        }
    return composites


def run_boardroom_committee(
    blocks_by_director_topic: dict[tuple[str, str], dict[str, Any]],
    *,
    previous_signal: str | None = None,
) -> dict[str, Any]:
    """Top-level entry point.

    Args:
      blocks_by_director_topic: {(director, topic): latest_score_block}
      previous_signal: the prior verdict's signal (Add/Hold/Trim/...) for
        hysteresis, or None for cold start.

    Returns:
      {
        "signal":              "Add" | "Hold" | "Trim" | "Trim Aggressively" | ...,
        "score":               int (0-100, weighted committee),
        "confidence":          "LOW" | "MEDIUM" | "HIGH",
        "thesis":              str (prose),
        "key_risk":            str (prose),
        "votes":               [{director, score, label, confidence, reason,
                                effective_weight, effective_weight_pct}, ...],
        "missing_directors":   [str, ...],
        "disagreement_spread": int (max-min across composite scores),
        "named_dissent":       {director, score, thesis} | None,
        "composites":          {director: composite_block},   # debug
      }
    """
    composites = _aggregate_per_director(blocks_by_director_topic)
    if not composites:
        return {
            "signal": "Hold",
            "score": 50,
            "confidence": "LOW",
            "thesis": "No director scores yet — waiting for sufficient signal.",
            "key_risk": "Awaiting data from all directors.",
            "votes": [],
            "missing_directors": ["bull", "bear", "quant", "strategist"],
            "disagreement_spread": 0,
            "named_dissent": None,
            "composites": {},
        }

    # Map directors → EarningsEdge specialist slots so we can reuse the
    # battle-tested compute_committee math (hysteresis, disagreement
    # confidence calibration, weight normalization).
    score_blocks_for_ee: dict[str, dict[str, Any] | None] = {}
    slot_to_director: dict[str, str] = {}
    for director, block in composites.items():
        slot = DIRECTOR_TO_SPECIALIST_SLOT.get(director)
        if not slot:
            continue
        score_blocks_for_ee[slot] = block
        slot_to_director[slot] = director
    # Fill missing slots so compute_committee doesn't get confused —
    # we pass None for any slot we don't have, which compute_committee
    # treats as "missing specialist" and ignores in weighting.
    for slot in LIVE_WEIGHTS.keys():
        score_blocks_for_ee.setdefault(slot, None)

    ee_result = compute_committee(
        score_blocks_for_ee,
        mode="live",
        previous_signal=previous_signal,
    )

    # Re-map votes back to director names.
    votes = []
    for vote in ee_result.get("votes") or []:
        slot = vote.get("name", "")
        director = slot_to_director.get(slot)
        if not director:
            continue
        votes.append({**vote, "name": director, "director": director})

    # Identify named dissent — director whose composite is furthest from
    # the committee score.
    cs = ee_result.get("score", 50)
    farthest_director = None
    farthest_dist = -1
    farthest_block = None
    for director, block in composites.items():
        dist = abs(int(block["score"]) - int(cs))
        if dist > farthest_dist:
            farthest_dist = dist
            farthest_director = director
            farthest_block = block
    named_dissent = None
    if farthest_director and farthest_block and farthest_dist >= 15:
        named_dissent = {
            "director": farthest_director,
            "score": int(farthest_block["score"]),
            "thesis": farthest_block.get("reason", ""),
        }

    # Re-map signal name to boardroom action vocabulary.
    signal = ee_result.get("signal", "HOLD")
    action_map = {
        "BUY": "Add",
        "HOLD": "Hold",
        "SELL": "Trim",
    }
    final_action = action_map.get(signal.upper(), "Hold")
    # If committee is very confident bearish (≤ 20) treat as Trim Aggressively.
    if int(cs) <= 20:
        final_action = "Trim Aggressively"
    elif int(cs) >= 80:
        final_action = "Add Aggressively"

    return {
        "signal": final_action,
        "score": int(cs),
        "confidence": ee_result.get("confidence", "MEDIUM"),
        "thesis": ee_result.get("thesis", ""),
        "key_risk": ee_result.get("key_risk", ""),
        "votes": votes,
        "missing_directors": [
            d for d in ("bull", "bear", "quant", "strategist")
            if d not in composites
        ],
        "disagreement_spread": ee_result.get("disagreement_spread", 0),
        "named_dissent": named_dissent,
        "composites": composites,
    }
