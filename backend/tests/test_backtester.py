from __future__ import annotations

from app.backtesting.backtester import Backtester
from app.strategies.ema_pullback import EMAPullbackStrategy


def test_backtester_returns_required_metrics(trending_candles):
    report = Backtester().run("BTCUSDT", trending_candles, EMAPullbackStrategy(enabled=True))

    assert report.total_trades >= 0
    assert report.win_rate >= 0
    assert report.equity_curve
