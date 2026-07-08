"""Portfolio risk diagnostics with QPK import fallback until pin bumps."""

from __future__ import annotations

from typing import Any

try:
    from quant_platform_kit.risk.portfolio_diagnostics import extract_portfolio_risk_diagnostics
except ImportError:  # pragma: no cover - exercised only before QPK pin bump

    def _position_unrealized_pnl(position: Any) -> float | None:
        quantity = float(getattr(position, "quantity", 0.0) or 0.0)
        if quantity == 0.0:
            return 0.0
        market_value = float(getattr(position, "market_value", 0.0) or 0.0)
        average_cost = getattr(position, "average_cost", None)
        if average_cost is None:
            return None
        cost_basis = abs(quantity) * float(average_cost)
        return market_value - cost_basis

    def extract_portfolio_risk_diagnostics(snapshot: Any) -> dict[str, float | int]:
        diagnostics: dict[str, float | int] = {}
        total_equity = float(getattr(snapshot, "total_equity", 0.0) or 0.0)
        metadata = dict(getattr(snapshot, "metadata", None) or {})
        if metadata.get("unrealized_pnl_pct") is not None:
            diagnostics["unrealized_pnl_pct"] = float(metadata["unrealized_pnl_pct"])
        elif total_equity > 0.0:
            positions = getattr(snapshot, "positions", ()) or ()
            unrealized = 0.0
            has_cost_basis = False
            for position in positions:
                position_pnl = _position_unrealized_pnl(position)
                if position_pnl is None:
                    continue
                has_cost_basis = True
                unrealized += position_pnl
            if has_cost_basis or not positions:
                diagnostics["unrealized_pnl_pct"] = float(unrealized / total_equity)
        if metadata.get("consecutive_losses") is not None:
            diagnostics["consecutive_losses"] = int(metadata["consecutive_losses"])
        return diagnostics

__all__ = ["extract_portfolio_risk_diagnostics"]
