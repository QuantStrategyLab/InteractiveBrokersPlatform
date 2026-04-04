from __future__ import annotations

from datetime import datetime

import pytz

from entrypoints.cloud_run import is_market_open_today


def test_is_market_open_today_falls_back_to_weekday_when_calendar_import_fails(monkeypatch):
    monkeypatch.setattr(
        "entrypoints.cloud_run.import_module",
        lambda _name: (_ for _ in ()).throw(TypeError("broken calendar")),
    )
    monkeypatch.setattr(
        "entrypoints.cloud_run.datetime",
        type(
            "FakeDatetime",
            (),
            {
                "now": staticmethod(
                    lambda _tz: datetime(2026, 4, 6, 12, 0, 0, tzinfo=pytz.timezone("America/New_York"))
                )
            },
        ),
    )

    assert is_market_open_today() is True
