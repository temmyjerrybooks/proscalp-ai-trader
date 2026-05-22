from __future__ import annotations

from datetime import datetime, timezone

from app.api.routes_backtesting import _candle_limit, _filter_candles, _parse_backtest_bound
from app.exchanges.base import Candle


def test_parse_backtest_bound_expands_end_date() -> None:
    assert _parse_backtest_bound("2026-05-18", end=False) == datetime(2026, 5, 18, tzinfo=timezone.utc)
    assert _parse_backtest_bound("2026-05-18", end=True) == datetime(2026, 5, 19, tzinfo=timezone.utc)


def test_candle_limit_respects_date_window_and_cap() -> None:
    start = datetime(2026, 5, 18, tzinfo=timezone.utc)
    end = datetime(2026, 5, 19, tzinfo=timezone.utc)

    assert _candle_limit("5m", 50, start, end) == 308
    assert _candle_limit("1m", 50, start, end) == 1460


def test_filter_candles_uses_selected_window() -> None:
    candles = [
        Candle(datetime(2026, 5, 18, 0, 0, tzinfo=timezone.utc), 1, 1, 1, 1, 1),
        Candle(datetime(2026, 5, 18, 1, 0, tzinfo=timezone.utc), 1, 1, 1, 1, 1),
        Candle(datetime(2026, 5, 19, 0, 0, tzinfo=timezone.utc), 1, 1, 1, 1, 1),
    ]

    filtered = _filter_candles(
        candles,
        datetime(2026, 5, 18, 0, 30, tzinfo=timezone.utc),
        datetime(2026, 5, 19, 0, 0, tzinfo=timezone.utc),
    )

    assert [candle.timestamp.hour for candle in filtered] == [1]
