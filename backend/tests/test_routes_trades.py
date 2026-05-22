from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from app.api.routes_trades import _csv_row, _parse_history_bound


def test_parse_history_bound_includes_full_end_date() -> None:
    assert _parse_history_bound("2026-05-17", end=False) == datetime(2026, 5, 17, tzinfo=timezone.utc)
    assert _parse_history_bound("2026-05-17", end=True) == datetime(2026, 5, 18, tzinfo=timezone.utc)


def test_parse_history_bound_accepts_iso_datetime() -> None:
    parsed = _parse_history_bound("2026-05-17T10:30:00Z", end=False)

    assert parsed == datetime(2026, 5, 17, 10, 30, tzinfo=timezone.utc)


def test_parse_history_bound_rejects_invalid_date() -> None:
    with pytest.raises(HTTPException):
        _parse_history_bound("not-a-date", end=False)


def test_csv_row_formats_dates_and_take_profit() -> None:
    row = _csv_row(
        {
            "opened_at": datetime(2026, 5, 17, 10, 30, tzinfo=timezone.utc),
            "closed_at": None,
            "symbol": "BTCUSDT",
            "entry_session": "london",
            "entry_regime": "strong",
            "take_profit": {"levels": [101, 102]},
        }
    )

    assert row["opened_at"] == "2026-05-17T10:30:00+00:00"
    assert row["closed_at"] == ""
    assert row["entry_session"] == "london"
    assert row["entry_regime"] == "strong"
    assert row["take_profit"] == '{"levels":[101,102]}'
