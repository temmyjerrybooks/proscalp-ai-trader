from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.exchanges.base import Candle


@pytest.fixture
def trending_candles() -> list[Candle]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candles: list[Candle] = []
    price = 100.0
    for index in range(220):
        price += 0.08
        candles.append(
            Candle(
                timestamp=start + timedelta(minutes=5 * index),
                open=price - 0.05,
                high=price + 0.2,
                low=price - 0.2,
                close=price,
                volume=1_000 + index * 5,
            )
        )
    return candles
