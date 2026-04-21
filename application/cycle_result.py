"""Structured strategy cycle results for InteractiveBrokersPlatform."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class StrategyCycleResult:
    """Structured output from one strategy cycle."""

    result: str
    signal_metadata: dict[str, Any] = field(default_factory=dict)
    target_weights: dict[str, float] | None = None
    execution_summary: dict[str, Any] = field(default_factory=dict)
    reconciliation_record: dict[str, Any] = field(default_factory=dict)
    reconciliation_record_path: str | None = None


def coerce_strategy_cycle_result(value: StrategyCycleResult | str) -> StrategyCycleResult:
    """Keep request-handling tolerant of older string-only test doubles."""
    if isinstance(value, StrategyCycleResult):
        return value
    return StrategyCycleResult(result=str(value))
