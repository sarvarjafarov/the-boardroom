"""Committee engine — weighted aggregation of specialist score blocks.

Every specialist emits a score_block with shape:
  {score: 0-100, label, confidence: LOW/MEDIUM/HIGH, reason, drivers,
   sample_size, freshness}

The committee:
  1. Picks weights based on mode (coverage or live).
  2. Halves weight for LOW-confidence specialists, then re-normalizes.
  3. Computes weighted final score.
  4. Applies hysteresis based on the previous signal state.
  5. Computes disagreement metric (score spread) to calibrate confidence.
  6. Produces a thesis string built from specialist reasons, not boilerplate.
"""

from __future__ import annotations

from typing import Any


# Base weights (must sum to 1.0 within each mode).
COVERAGE_WEIGHTS: dict[str, float] = {
    "analyst": 0.30,
    "news": 0.26,
    "macro": 0.16,
    "peer": 0.14,
    "technical": 0.14,
}

LIVE_WEIGHTS: dict[str, float] = {
    "metrics": 0.21,
    "sentiment": 0.21,
    "news": 0.18,
    "analyst": 0.15,
    "macro": 0.10,
    "peer": 0.10,
    "technical": 0.05,
}

# Confidence-based weight multiplier.
CONFIDENCE_MULTIPLIER = {
    "HIGH": 1.0,
    "MEDIUM": 1.0,
    "LOW": 0.5,
}

# Hysteresis bands — see _apply_hysteresis for semantics.
HYST_BUY_ENTRY = 70  # HOLD -> BUY requires score >= 70
HYST_SELL_ENTRY = 30  # HOLD -> SELL requires score <= 30
HYST_BUY_EXIT = 55  # BUY stays BUY if score >= 55
HYST_SELL_EXIT = 45  # SELL stays SELL if score <= 45


def _effective_weight(base: float, confidence: str) -> float:
    mult = CONFIDENCE_MULTIPLIER.get((confidence or "MEDIUM").upper(), 1.0)
    return base * mult


def _label_from_score(score: float) -> str:
    if score >= HYST_BUY_ENTRY:
        return "bullish"
    if score <= HYST_SELL_ENTRY:
        return "bearish"
    return "neutral"


def _apply_hysteresis(score: float, previous_signal: str | None) -> str:
    """Convert a numeric committee score to BUY/HOLD/SELL with hysteresis.

    State transitions:
      HOLD  -> BUY if score >= 70, SELL if score <= 30, else HOLD
      BUY   -> BUY if score >= 55, else HOLD (never jumps straight to SELL)
      SELL  -> SELL if score <= 45, else HOLD (never jumps straight to BUY)

    If previous_signal is None (cold start), use the hard thresholds
    (70 / 30) with no middle-zone HOLD stickiness.
    """
    prev = (previous_signal or "").upper().strip()

    if prev == "BUY":
        if score >= HYST_BUY_EXIT:
            return "BUY"
        if score <= HYST_SELL_ENTRY:
            return "SELL"  # collapse directly on extreme swing
        return "HOLD"

    if prev == "SELL":
        if score <= HYST_SELL_EXIT:
            return "SELL"
        if score >= HYST_BUY_ENTRY:
            return "BUY"
        return "HOLD"

    if score >= HYST_BUY_ENTRY:
        return "BUY"
    if score <= HYST_SELL_ENTRY:
        return "SELL"
    return "HOLD"


def _committee_confidence(
    votes: list[dict[str, Any]],
    weighted_score: float,
) -> str:
    """Committee confidence is driven by agreement, not averaging.

    Agreement = do specialists' scores cluster around the weighted score?
    - HIGH if max spread <= 15 points AND at least one HIGH specialist
    - LOW if max spread >= 35 points
    - otherwise MEDIUM

    If fewer than 3 specialists voted, cap at MEDIUM regardless.
    """
    _ = weighted_score
    if len(votes) < 3:
        return "LOW" if len(votes) < 2 else "MEDIUM"

    scores = [v["score"] for v in votes]
    spread = max(scores) - min(scores)
    has_high_specialist = any(v["confidence"] == "HIGH" for v in votes)

    if spread >= 35:
        return "LOW"
    if spread <= 15 and has_high_specialist:
        return "HIGH"
    return "MEDIUM"


def _build_thesis(votes: list[dict[str, Any]], final_signal: str, weighted_score: float) -> str:
    """One plain-English sentence summarizing the committee verdict.

    Avoids echoing specialist reasons (those show in the digest and
    committee card). Instead: what's the committee saying and how
    confidently?
    """
    if not votes:
        return "Not enough specialist data yet to form a view."

    n = len(votes)
    scores = [v["score"] for v in votes]
    spread = max(scores) - min(scores)

    if final_signal == "BUY":
        if spread <= 15:
            return (
                f"Specialists broadly agree on the bull case — "
                f"{n} of {n} reports align around the {int(weighted_score)} score."
            )
        if spread <= 30:
            return (
                f"Mixed but leaning bullish — weighted score of "
                f"{int(weighted_score)} despite a {spread}-point spread between specialists."
            )
        return (
            f"Bullish on balance, but specialists are split "
            f"({spread}-pt spread). Treat with caution."
        )

    if final_signal == "SELL":
        if spread <= 15:
            return (
                f"Specialists broadly agree on the bear case — "
                f"weighted score of {int(weighted_score)}."
            )
        if spread <= 30:
            return (
                f"Leaning bearish — weighted score of {int(weighted_score)} "
                f"with a {spread}-point spread between specialists."
            )
        return (
            f"Bearish on balance, but specialists are split "
            f"({spread}-pt spread). Treat with caution."
        )

    # HOLD
    return (
        f"No clear edge either way — score of {int(weighted_score)} "
        f"sits in the neutral zone. Wait for better evidence."
    )


def _build_key_risk(votes: list[dict[str, Any]], final_signal: str) -> str:
    """The actual counterweight: what's the best argument against
    the committee's direction? Skip generic boilerplate."""
    if not votes:
        return "Awaiting data."

    target_opposite = (
        "bearish" if final_signal == "BUY"
        else "bullish" if final_signal == "SELL"
        else None
    )

    if target_opposite:
        opposers = [v for v in votes if v["label"] == target_opposite]
        if opposers:
            opposers.sort(key=lambda v: v["effective_weight"], reverse=True)
            top = opposers[0]
            return f"{top['name'].title()} disagrees: {top['reason']}"

    # No explicit dissenter — pick the lowest-scoring specialist
    # or the most uncertain one
    if final_signal == "HOLD":
        return "Signal is mixed; wait for a catalyst or better data before sizing."

    low_conf = [v for v in votes if v["confidence"] == "LOW"]
    if low_conf:
        low_conf.sort(key=lambda v: v["effective_weight"], reverse=True)
        top = low_conf[0]
        return f"{top['name'].title()} has low conviction: {top['reason']}"

    # All high-confidence and aligned — flag the meta-risk
    if final_signal == "BUY":
        weakest = min(votes, key=lambda v: v["score"])
    else:
        weakest = max(votes, key=lambda v: v["score"])
    return f"Weakest supporter is {weakest['name'].title()} at {weakest['score']}/100."


def compute_committee(
    score_blocks: dict[str, dict[str, Any] | None],
    *,
    mode: str,
    previous_signal: str | None = None,
) -> dict[str, Any]:
    """Run the committee and return the final verdict dict."""
    weights = LIVE_WEIGHTS if mode == "live" else COVERAGE_WEIGHTS

    votes: list[dict[str, Any]] = []
    missing: list[str] = []

    for name, base_weight in weights.items():
        sb = score_blocks.get(name)
        if not isinstance(sb, dict) or sb.get("score") is None:
            missing.append(name)
            continue
        score = float(sb.get("score", 50))
        confidence = str(sb.get("confidence", "MEDIUM")).upper()
        eff = _effective_weight(base_weight, confidence)
        votes.append({
            "name": name,
            "score": int(round(score)),
            "label": sb.get("label", _label_from_score(score)),
            "confidence": confidence,
            "reason": str(sb.get("reason", "")).strip(),
            "base_weight": base_weight,
            "effective_weight": eff,
        })

    if not votes:
        final = _apply_hysteresis(50.0, previous_signal)
        return {
            "signal": final,
            "score": 50,
            "confidence": "LOW",
            "thesis": "No specialist data available yet.",
            "key_risk": "Awaiting data from all specialists.",
            "votes": [],
            "mode": mode,
            "missing_specialists": missing,
            "disagreement_spread": 0,
        }

    total_eff = sum(v["effective_weight"] for v in votes)
    if total_eff > 0:
        for v in votes:
            v["effective_weight_pct"] = round(v["effective_weight"] / total_eff * 100.0, 1)
    else:
        for v in votes:
            v["effective_weight_pct"] = round(100.0 / len(votes), 1)

    weighted_score = (
        sum(v["score"] * v["effective_weight"] for v in votes) / (total_eff if total_eff > 0 else 1.0)
    )

    final_signal = _apply_hysteresis(weighted_score, previous_signal)
    confidence = _committee_confidence(votes, weighted_score)
    thesis = _build_thesis(votes, final_signal, weighted_score)
    key_risk = _build_key_risk(votes, final_signal)

    scores = [v["score"] for v in votes]
    spread = max(scores) - min(scores)

    return {
        "signal": final_signal,
        "score": int(round(weighted_score)),
        "confidence": confidence,
        "thesis": thesis,
        "key_risk": key_risk,
        "votes": votes,
        "mode": mode,
        "missing_specialists": missing,
        "disagreement_spread": spread,
    }

