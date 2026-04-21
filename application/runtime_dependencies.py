"""Runtime dependency bundles for IBKR rebalance orchestration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from quant_platform_kit.common.ports import NotificationPort, PortfolioPort


@dataclass(frozen=True)
class IBKRRebalanceConfig:
    translator: Callable[..., str]
    separator: str
    strategy_display_name: str | None = None
    reconciliation_output_path: str | Path | None = None


@dataclass(frozen=True)
class IBKRRebalanceRuntime:
    connect_ib: Callable[[], Any]
    portfolio_port_factory: Callable[[Any], PortfolioPort]
    compute_signals: Callable[[Any, set[str]], tuple[Any, ...]]
    execute_rebalance: Callable[..., Any]
    notifications: NotificationPort
