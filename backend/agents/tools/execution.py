"""Execution tools — Alpaca paper trading.

Two-stage flow ensures the user is always the one who pulls the trigger:
  1. draft_paper_trade — persists a pending order in MongoDB. The UI
     shows the draft next to the verdict; user can approve or reject.
  2. execute_paper_trade — called only by the explicit user-approval
     endpoint /api/orders/confirm. Sends the order to Alpaca paper.

The directors NEVER execute. The Chairman drafts. The user confirms.
This is both a fiduciary-design choice and a hackathon safety story.
"""
from __future__ import annotations

import os
from typing import Any

from ._mcp_client import mcp_call

DB = os.getenv("MONGODB_DB", "boardroom")


async def draft_paper_trade(
    user_id: str,
    audio_id: str,
    verdict_id: str,
    ticker: str,
    side: str,
    qty: float,
    rationale: str,
    limit_price: float | None = None,
) -> dict[str, Any]:
    """Persist a DRAFT order for the user to confirm. Does NOT execute.

    Called by the Chairman (or the Portfolio Impact Engine) after a verdict.
    The drafted order lands in MongoDB → portfolio_impacts collection with
    status='pending'. The UI shows it next to the verdict card.

    Args:
      user_id: Stable user identifier.
      audio_id: ObjectId hex of the audio source that triggered the draft.
      verdict_id: ObjectId hex of the verdict.
      ticker: Stock symbol.
      side: 'buy' | 'sell'.
      qty: Number of shares (positive float).
      rationale: 1-2 sentence reason shown next to the Approve button.
      limit_price: Optional cap (buys) / floor (sells).

    Returns:
      {"ok": True, "draft_id": str (ObjectId hex), "ticker": str, "side": str, "qty": float}
    """
    s = (side or "").lower().strip()
    if s not in ("buy", "sell"):
        return {"error": "side must be 'buy' or 'sell'"}
    if not ticker or qty <= 0:
        return {"error": "ticker and positive qty required"}

    doc = {
        "audio_id": audio_id,
        "verdict_id": verdict_id,
        "user_id": user_id,
        "ticker": ticker.upper(),
        "side": s,
        "qty": float(qty),
        "limit_price": float(limit_price) if limit_price else None,
        "rationale": rationale,
        "status": "pending",
    }
    await mcp_call("insert-many", {
        "database": DB, "collection": "portfolio_impacts", "documents": [doc],
    })
    return {
        "ok": True,
        "ticker": doc["ticker"],
        "side": doc["side"],
        "qty": doc["qty"],
        "limit_price": doc["limit_price"],
    }


async def execute_paper_trade(draft_id: str) -> dict[str, Any]:
    """Send an approved draft to Alpaca paper. Called ONLY by the explicit
    user-approval endpoint /api/orders/confirm — never directly by a director.

    Reuses EarningsEdge's `trade_executor.py` (TradeExecutor.submit_order).

    Args:
      draft_id: ObjectId hex of the portfolio_impacts row to execute.

    Returns:
      {"ok": True, "alpaca_order_id": str, "status": str} on success,
      {"ok": False, "error": str} on failure.
    """
    # TODO Day 6 — read the draft from MongoDB, call TradeExecutor.submit_order,
    # update the draft status to 'executed' with the alpaca_order_id.
    # For Day 1, return placeholder shape.
    return {"ok": True, "alpaca_order_id": "STUB", "status": "stub", "_stub": True}
