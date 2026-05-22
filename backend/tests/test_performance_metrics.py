from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.api.routes_performance import _performance_from_trades
from app.database.models import Trade


def _trade(pnl: float, setup: str, index: int) -> Trade:
    opened = datetime(2026, 5, 18, 8, 0, tzinfo=timezone.utc) + timedelta(minutes=index)
    return Trade(
        symbol="BTCUSDT",
        side="long",
        exchange="binance",
        mode="testnet",
        setup_name=setup,
        entry_price=100.0,
        stop_loss=99.0,
        take_profit={"levels": [101.0]},
        quantity=1.0,
        status="closed",
        realized_pnl=pnl,
        opened_at=opened,
        closed_at=opened + timedelta(minutes=5),
    )


def test_performance_from_trades_computes_live_trade_metrics() -> None:
    report = _performance_from_trades(
        [
            _trade(5.0, "EMA pullback scalp", 1),
            _trade(-2.0, "EMA pullback scalp", 2),
            _trade(1.0, "VWAP reclaim scalp", 3),
        ],
        starting_equity=10_000,
    )

    assert report["total_trades"] == 3
    assert report["total_pnl"] == 4.0
    assert report["win_rate"] == 66.67
    assert report["profit_factor"] == 3.0
    assert report["best_strategy"] == "EMA pullback scalp"
    assert report["worst_strategy"] == "VWAP reclaim scalp"
    assert report["pnl_chart"][-1] == 10004.0
