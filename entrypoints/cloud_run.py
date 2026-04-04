"""Cloud Run request helpers for InteractiveBrokersPlatform."""

from __future__ import annotations

from datetime import datetime
from importlib import import_module

import pytz


def _load_market_calendar(calendar_name: str, *, logger) -> object | None:
    try:
        module = import_module("pandas_market_calendars")
        return module.get_calendar(calendar_name)
    except Exception as exc:
        logger(
            f"pandas_market_calendars unavailable for {calendar_name}: {exc}; "
            "falling back to weekday-only market-open check"
        )
        return None


def is_market_open_today(
    *,
    calendar_name="NYSE",
    timezone_name="America/New_York",
    logger=lambda _message: None,
) -> bool:
    tz_ny = pytz.timezone(timezone_name)
    now_ny = datetime.now(tz_ny)
    calendar = _load_market_calendar(calendar_name, logger=logger)
    if calendar is None:
        return now_ny.weekday() < 5
    schedule = calendar.schedule(start_date=now_ny.date(), end_date=now_ny.date())
    return len(getattr(schedule, "index", ())) > 0
