"""Tool wrapper that lets the Chairman LlmAgent trigger a verdict synthesis.

The Chairman LlmAgent in root_agent.py exposes the full 15-tool surface
already. When the user (via voice or chat) asks for the verdict, the
Chairman calls this tool, which delegates to ChairmanSynthesizer.
"""
from __future__ import annotations

from typing import Any


async def synthesize_verdict(
    audio_id: str,
    ticker: str,
    source_label: str = "live audio source",
    user_id: str = "demo",
) -> dict[str, Any]:
    """Run the full Chairman synthesis pipeline + persist the verdict.

    Reads every score_block and argument for the given audio_id, runs
    the weighted boardroom committee math (with hysteresis + named
    dissent identification), detects cross-call pattern matches that
    fired during the audio, generates the prose verdict with the
    Chairman persona, computes per-position portfolio impact, drafts
    paper trades for any actionable impact, and persists everything
    to MongoDB.

    Args:
      audio_id: ObjectId hex / session id of the audio source.
      ticker: The primary ticker the verdict applies to.
      source_label: Human label of the source (used in the prose).
      user_id: Demo-mode user id (default 'demo').

    Returns:
      The full verdict dict:
        final_action, final_score, confidence, thesis,
        key_dissent_narrative, pattern_callout_narrative,
        named_dissent, pattern_callouts, disagreement_spread,
        votes, composites, portfolio_impacts, drafted_trade_ids,
        citation_line_indices.
    """
    # Lazy import so the tools/__init__.py loads quickly.
    from ..chairman_synthesis import ChairmanSynthesizer
    synth = ChairmanSynthesizer()
    return await synth.synthesize(
        audio_id=audio_id,
        ticker=ticker,
        source_label=source_label,
        user_id=user_id,
    )
