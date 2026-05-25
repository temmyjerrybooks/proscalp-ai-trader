"""Phase 2B Branch 1: tests for the MFE/MAE tracker on Trade rows."""
from __future__ import annotations

from app.config.settings import Settings, TradingMode
from app.database.models import Trade
from app.services.bot_runner import BotRunner


def _trade() -> Trade:
    return Trade(
        id="t1", symbol="BTCUSDT", side="long", exchange="binance", mode="testnet",
        setup_name="test", entry_price=100.0, stop_loss=99.0,
        take_profit={"levels": [102.0]}, quantity=0.1, status="open", extra={},
    )


def test_mfe_mae_updates_with_each_observation():
    runner = BotRunner(settings=Settings(trading_mode=TradingMode.TESTNET, mfe_mae_logging_enabled=True))
    trade = _trade()
    runner._update_mfe_mae(trade, 0.50)  # favorable
    assert trade.extra["mfe_pnl"] == 0.50
    assert trade.extra["mae_pnl"] == 0.0
    runner._update_mfe_mae(trade, -0.30)  # adverse
    assert trade.extra["mfe_pnl"] == 0.50  # MFE doesn't regress
    assert trade.extra["mae_pnl"] == -0.30  # MAE moves down
    runner._update_mfe_mae(trade, 1.20)  # bigger favorable
    assert trade.extra["mfe_pnl"] == 1.20
    assert trade.extra["mae_pnl"] == -0.30
    assert "mfe_mae_updated_at" in trade.extra


def test_mfe_mae_disabled_writes_nothing():
    runner = BotRunner(settings=Settings(trading_mode=TradingMode.TESTNET, mfe_mae_logging_enabled=False))
    trade = _trade()
    runner._update_mfe_mae(trade, 5.0)
    runner._update_mfe_mae(trade, -2.0)
    assert "mfe_pnl" not in (trade.extra or {})
    assert "mae_pnl" not in (trade.extra or {})


def test_mfe_mae_preserves_other_extra_fields():
    runner = BotRunner(settings=Settings(trading_mode=TradingMode.TESTNET, mfe_mae_logging_enabled=True))
    trade = _trade()
    trade.extra = {"tp1_hit": True, "remaining_quantity": 0.06}
    runner._update_mfe_mae(trade, 0.2)
    assert trade.extra["tp1_hit"] is True
    assert trade.extra["remaining_quantity"] == 0.06
    assert trade.extra["mfe_pnl"] == 0.2
