"""TradeExecutor — submits orders to Alpaca paper trading.

NEVER auto-executes. Always requires explicit human confirmation.
"""

from __future__ import annotations

import os
from typing import Any

from alpaca.common.enums import Sort
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, LimitOrderRequest, MarketOrderRequest

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")


def _norm_order_side(raw: Any) -> str:
    """Normalize Alpaca OrderSide / string-ish values to BUY|SELL."""
    s = str(raw or "").strip()
    if not s:
        return ""
    u = s.upper()
    if u in {"BUY", "SELL"}:
        return u
    if "SELL" in u:
        return "SELL"
    if "BUY" in u:
        return "BUY"
    lo = s.lower()
    if lo.startswith("s"):
        return "SELL"
    if lo.startswith("b"):
        return "BUY"
    return u


class TradeExecutor:
    def __init__(self) -> None:
        if ALPACA_API_KEY and ALPACA_SECRET_KEY:
            # `url_override` lets us point the client at the paper API URL.
            self.client: TradingClient | None = TradingClient(
                ALPACA_API_KEY,
                ALPACA_SECRET_KEY,
                paper=True,
                url_override=ALPACA_BASE_URL,
            )
        else:
            self.client = None

    def is_configured(self) -> bool:
        return self.client is not None

    def get_account(self) -> dict[str, Any]:
        if not self.client:
            return {"error": "Alpaca not configured"}
        try:
            acct = self.client.get_account()
            return {
                "cash": str(getattr(acct, "cash", "")),
                "portfolio_value": str(getattr(acct, "portfolio_value", "")),
                "buying_power": str(getattr(acct, "buying_power", "")),
                "equity": str(getattr(acct, "equity", "")),
                "last_equity": str(getattr(acct, "last_equity", "")),
                "long_market_value": str(getattr(acct, "long_market_value", "")),
                "short_market_value": str(getattr(acct, "short_market_value", "")),
                "status": str(getattr(acct, "status", "")),
            }
        except Exception as exc:
            return {"error": str(exc)}

    def get_pl_analytics(self) -> dict[str, Any]:
        """Compute account-level P&L from Alpaca account + positions.

        Day P&L = equity - last_equity (Alpaca's day reset snapshot)
        Unrealized P&L = sum(position.unrealized_pl) across all positions
        Unrealized % = sum($unrealized_pl) / sum(cost_basis) across positions
        """
        if not self.client:
            return {"error": "Alpaca not configured"}
        try:
            acct = self.client.get_account()
            positions = self.client.get_all_positions()

            equity = float(getattr(acct, "equity", 0) or 0)
            last_equity = float(getattr(acct, "last_equity", 0) or 0)
            day_pl_abs = equity - last_equity
            day_pl_pct = (day_pl_abs / last_equity * 100.0) if last_equity > 0 else 0.0

            total_unrealized_pl = 0.0
            total_cost_basis = 0.0
            for p in positions:
                try:
                    upl = float(getattr(p, "unrealized_pl", 0) or 0)
                    cost = float(getattr(p, "cost_basis", 0) or 0)
                    total_unrealized_pl += upl
                    total_cost_basis += cost
                except (TypeError, ValueError):
                    continue

            total_unrealized_pct = (
                (total_unrealized_pl / total_cost_basis * 100.0) if total_cost_basis > 0 else 0.0
            )

            return {
                "equity": round(equity, 2),
                "last_equity": round(last_equity, 2),
                "day_pl_abs": round(day_pl_abs, 2),
                "day_pl_pct": round(day_pl_pct, 2),
                "total_unrealized_pl": round(total_unrealized_pl, 2),
                "total_unrealized_pct": round(total_unrealized_pct, 2),
                "total_cost_basis": round(total_cost_basis, 2),
                "position_count": len(positions),
            }
        except Exception as exc:
            return {"error": str(exc)}

    def submit_order(
        self,
        ticker: str,
        side: str,
        qty: int,
        limit_price: float | None = None,
    ) -> dict[str, Any]:
        """Submit a market or limit order. `side` must be 'buy' or 'sell'."""
        if not self.client:
            return {"error": "Alpaca not configured"}
        try:
            order_side = OrderSide.BUY if str(side).lower() == "buy" else OrderSide.SELL
            symbol = str(ticker).upper()
            qty_i = int(qty)

            if qty_i <= 0:
                return {"error": "qty must be > 0"}

            if limit_price is not None:
                req = LimitOrderRequest(
                    symbol=symbol,
                    qty=qty_i,
                    side=order_side,
                    time_in_force=TimeInForce.DAY,
                    limit_price=round(float(limit_price), 2),
                )
            else:
                req = MarketOrderRequest(
                    symbol=symbol,
                    qty=qty_i,
                    side=order_side,
                    time_in_force=TimeInForce.DAY,
                )

            order = self.client.submit_order(req)
            return {
                "order_id": str(getattr(order, "id", "")),
                "ticker": symbol,
                "side": _norm_order_side(getattr(order, "side", side)),
                "qty": qty_i,
                "status": str(getattr(order, "status", "")),
                "limit_price": limit_price,
            }
        except Exception as exc:
            return {"error": str(exc)}

    def get_positions(self) -> list[dict[str, Any]]:
        if not self.client:
            return []
        try:
            positions = self.client.get_all_positions()
            out: list[dict[str, Any]] = []
            for p in positions:
                out.append(
                    {
                        "ticker": getattr(p, "symbol", ""),
                        "qty": str(getattr(p, "qty", "")),
                        "avg_entry": str(getattr(p, "avg_entry_price", "")),
                        "market_value": str(getattr(p, "market_value", "")),
                        "unrealized_pl": str(getattr(p, "unrealized_pl", "")),
                        "unrealized_plpc": str(getattr(p, "unrealized_plpc", "")),
                    }
                )
            return out
        except Exception:
            return []

    def get_orders(self, limit: int = 50) -> dict[str, Any]:
        """Recent orders (open + closed), newest first."""
        if not self.client:
            return {"error": "Alpaca not configured", "orders": []}
        try:
            req = GetOrdersRequest(
                status=QueryOrderStatus.ALL,
                limit=min(max(limit, 1), 500),
                direction=Sort.DESC,
            )
            orders = self.client.get_orders(filter=req)
            out: list[dict[str, Any]] = []
            for o in orders:
                out.append(
                    {
                        "id": str(getattr(o, "id", "")),
                        "symbol": str(getattr(o, "symbol", "") or ""),
                        "side": _norm_order_side(getattr(o, "side", "")),
                        "qty": str(getattr(o, "qty", "") or ""),
                        "filled_qty": str(getattr(o, "filled_qty", "") or ""),
                        "filled_avg_price": str(getattr(o, "filled_avg_price", "") or ""),
                        "status": str(getattr(o, "status", "") or ""),
                        "type": str(getattr(o, "type", "") or getattr(o, "order_type", "") or ""),
                        "limit_price": str(getattr(o, "limit_price", "") or ""),
                        "submitted_at": getattr(o, "submitted_at", None).isoformat()
                        if getattr(o, "submitted_at", None)
                        else None,
                        "filled_at": getattr(o, "filled_at", None).isoformat()
                        if getattr(o, "filled_at", None)
                        else None,
                    }
                )
            return {"orders": out}
        except Exception as exc:
            return {"error": str(exc), "orders": []}

