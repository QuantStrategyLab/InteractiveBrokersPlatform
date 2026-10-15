"""Explicit historical OHLC contract; no corporate-action basis substitution."""

from __future__ import annotations

import json
import logging
from enum import StrEnum
from math import isclose, isfinite


class PriceBasis(StrEnum):
    RAW_UNADJUSTED = "raw_unadjusted"
    SPLIT_ADJUSTED = "split_adjusted"
    SPLIT_DIVIDEND_ADJUSTED = "split_dividend_adjusted"
    TOTAL_RETURN_INDEX = "total_return_index"


DEFAULT_PRICE_BASIS = PriceBasis.SPLIT_ADJUSTED.value
VOLUME_BASIS = "provider_native_unscaled"
OHLC_FIELDS = ("open", "high", "low", "close")


def validate_price_basis(value) -> str:
    if not isinstance(value, str) or value not in tuple(PriceBasis):
        # Do not echo an arbitrary configuration value into diagnostics.
        raise ValueError("price_basis must be one of: " + ", ".join(PriceBasis))
    return str(value)


def basis_diagnostic(requested, *, provider, source_columns=(), resolved=None, reason=None):
    return {
        "requested_basis": requested,
        "resolved_basis": resolved,
        "provider": provider,
        "source_columns": list(source_columns),
        "volume_basis": VOLUME_BASIS,
        "reason": reason,
    }


class PriceBasisError(ValueError):
    """A requested history basis could not be proven or produced.

    Only bounded contract metadata is logged; never provider exception text,
    prices, account identifiers, credentials or request arguments.
    """

    def __init__(self, requested, *, provider, reason, source_columns=()):
        self.diagnostic = basis_diagnostic(
            requested, provider=provider, source_columns=source_columns, reason=reason,
        )
        message = "price_basis_unavailable " + json.dumps(self.diagnostic, sort_keys=True)
        logging.getLogger(__name__).warning(message)
        super().__init__(message)


def require_supported_ohlc_basis(requested, *, provider):
    requested = validate_price_basis(requested)
    if requested == PriceBasis.TOTAL_RETURN_INDEX:
        raise PriceBasisError(requested, provider=provider, reason="total_return_index_is_not_ohlc")
    if requested == PriceBasis.RAW_UNADJUSTED:
        raise PriceBasisError(requested, provider=provider, reason="raw_prices_not_provided")
    return requested


class HistoricalCandles(list):
    """Numeric candles stay unchanged; one batch carries non-sensitive metadata.

    A custom history loader must declare every OHLC field's basis. Unknown or
    conflicting metadata is rejected at the adapter boundary.
    """

    def __init__(self, rows, *, price_basis, provider, source_columns, field_basis=None):
        super().__init__(rows)
        self.diagnostic = basis_diagnostic(
            validate_price_basis(price_basis), provider=provider,
            source_columns=source_columns, resolved=price_basis,
        )
        self.field_basis = dict(field_basis) if field_basis is not None else {
            field: price_basis for field in OHLC_FIELDS
        }


def validate_candles(candles, requested):
    if not isinstance(candles, HistoricalCandles):
        raise PriceBasisError(requested, provider="history_loader", reason="missing_basis_metadata")
    diagnostic = candles.diagnostic
    provider = diagnostic["provider"]
    columns = diagnostic["source_columns"]

    def fail(reason):
        raise PriceBasisError(requested, provider=provider, source_columns=columns, reason=reason)

    if (diagnostic["resolved_basis"] != requested
            or diagnostic["requested_basis"] != requested
            or any(candles.field_basis.get(field) != requested for field in OHLC_FIELDS)):
        fail("mixed_or_mismatched_basis")
    if not candles:
        fail("empty_history")
    for row in candles:
        try:
            prices = [float(row[field]) for field in OHLC_FIELDS]
            volume = float(row["volume"])
            if not all(isfinite(value) and value > 0 for value in prices):
                fail("invalid_price")
            if not isfinite(volume) or volume < 0:
                fail("invalid_volume")
            opening, high, low, close = prices
            # A scaled O/H/L boundary can differ from Adj Close by one rounding
            # unit. Tolerate floating-point noise without changing any value.
            lower, upper = min(opening, close), max(opening, close)
            if ((low > lower and not isclose(low, lower, rel_tol=1e-12))
                    or (high < upper and not isclose(high, upper, rel_tol=1e-12))
                    or (low > high and not isclose(low, high, rel_tol=1e-12))):
                fail("inconsistent_ohlc")
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            if isinstance(exc, PriceBasisError):
                raise
            fail("missing_or_invalid_ohlcv")
    return candles
