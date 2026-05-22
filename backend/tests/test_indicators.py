from __future__ import annotations

from app.indicators.technical import build_indicator_snapshot, ema, rsi


def test_indicator_calculations(trending_candles):
    values = [candle.close for candle in trending_candles]
    ema_values = ema(values, 9)
    rsi_values = rsi(values)
    snapshot = build_indicator_snapshot(trending_candles)

    assert len(ema_values) == len(values)
    assert ema_values[-1] < values[-1]
    assert rsi_values[-1] > 50
    assert snapshot.ema_9 > snapshot.ema_21 > snapshot.ema_50
    assert snapshot.vwap > 0
    assert snapshot.atr > 0
