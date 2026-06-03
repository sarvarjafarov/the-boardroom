"""Market intelligence tools.

Days 2-5 wire these to real APIs (Alpha Vantage, Finnhub, FRED, yfinance).
Day 1 returns shape-correct placeholders so the ADK can register them.

Director's perspective:
  - The Bull + The Bear call get_analyst_consensus and get_peer_comp to
    ground their thesis in numbers.
  - The Strategist calls get_macro_snapshot to place a signal in macro
    context (rates, CPI, yield curve).
  - The Quant calls get_quote to compute deltas vs reported figures.
"""
from __future__ import annotations

import os
from typing import Any


async def get_quote(ticker: str) -> dict[str, Any]:
    """Last trading price for a ticker — Alpha Vantage GLOBAL_QUOTE.

    The Quant calls this to compute deltas vs reported figures. Not used
    for forward signal (use get_analyst_consensus for that).

    Args:
      ticker: Stock symbol (e.g. 'NVDA').

    Returns:
      {"ticker": str, "price": float, "change_pct": float} on success,
      {"error": str} on failure.
    """
    # TODO Day 3 — wire to Alpha Vantage. For Day 1, return placeholder shape.
    return {"ticker": ticker.upper(), "price": 0.0, "change_pct": 0.0, "_stub": True}


async def get_analyst_consensus(ticker: str) -> dict[str, Any]:
    """Hybrid Finnhub + yfinance analyst consensus snapshot.

    The Bull / Bear call this to ground their thesis in real sell-side data.

    Args:
      ticker: Stock symbol.

    Returns:
      {
        "baseline_score": int (0-100),
        "label": "bullish" | "neutral" | "bearish",
        "target_mean": float | None,
        "target_upside_pct": float | None,
        "total_analysts": int | None,
      }
    """
    # TODO Day 3 — copy + simplify EarningsEdge tools.get_analyst_recommendation.
    return {
        "ticker": ticker.upper(),
        "baseline_score": 50,
        "label": "neutral",
        "target_mean": None,
        "target_upside_pct": None,
        "total_analysts": None,
        "_stub": True,
    }


async def get_peer_comp(ticker: str) -> dict[str, Any]:
    """Peer median PEG and P/E vs target ticker, growth-adjusted.

    The Bull / Bear / Strategist call this to place valuation in peer
    context. PEG is preferred because it normalizes for growth.

    Args:
      ticker: Stock symbol.

    Returns:
      {
        "target_peg": float | None,
        "median_peer_peg": float | None,
        "target_pe": float | None,
        "median_peer_pe": float | None,
        "premium_pct": float | None,
        "peer_count": int,
      }
    """
    # TODO Day 3 — copy + simplify EarningsEdge tools.get_competitors logic.
    return {
        "ticker": ticker.upper(),
        "target_peg": None,
        "median_peer_peg": None,
        "target_pe": None,
        "median_peer_pe": None,
        "premium_pct": None,
        "peer_count": 0,
        "_stub": True,
    }


async def get_macro_snapshot() -> dict[str, Any]:
    """FRED macro snapshot — yield curve, CPI, Fed funds, unemployment.

    The Strategist calls this to place an audio signal in macro context.

    Returns:
      {
        "yield_curve": {"2y": float, "10y": float, "30y": float, "spread": float},
        "cpi_yoy_pct": float,
        "fed_funds_pct": float,
        "unemployment_pct": float,
        "policy_stance": "hawkish" | "neutral" | "dovish",
      }
    """
    # TODO Day 5 — copy + simplify EarningsEdge tools.get_macro_snapshot.
    return {
        "yield_curve": {"2y": None, "10y": None, "30y": None, "spread": None},
        "cpi_yoy_pct": None,
        "fed_funds_pct": None,
        "unemployment_pct": None,
        "policy_stance": "neutral",
        "_stub": True,
    }
