"""Post-only (maker) entry tests.

Covers the two things that matter:
- execute_signal(maker=True) rests on the MAKER side of the book with TIF=GTX
  (post-only), instead of crossing the spread as taker; and bails if the live book
  has drifted too far from the signal price.
- _protect_filled_pending reconstructs the signal from a filled-while-resting trade
  and attaches Branch-1 protection — closing the unprotected-pending gap.
"""

from __future__ import annotations

from datetime import datetime, timezone

from unittest.mock import AsyncMock

from app.config.settings import Settings, TradingMode
from app.database.models import Trade
from app.exchanges.base import Balance, Candle, ExchangeAdapter, OrderBook, OrderRequest, OrderResult, Ticker
from app.execution.order_manager import OrderManager
from app.risk.risk_engine import TradePermissionRequest
from app.services.bot_runner import BotRunner
from app.strategies.base_strategy import StrategySignal


class RecordingExchange(ExchangeAdapter):
    name = "recording"

    def __init__(self, book: OrderBook | None = None) -> None:
        self.book = book or OrderBook("BTCUSDT", bids=[(99.99, 10)], asks=[(100.01, 10)])
        self.last_order: OrderRequest | None = None

    async def fetch_balances(self):
        return [Balance("USDT", 1000, total=1000)]

    async def fetch_tickers(self):
        return [Ticker("BTCUSDT", 100, 100_000_000, 1, 102, 98, "recording")]

    async def fetch_order_book(self, symbol: str, limit: int = 50):
        return self.book

    async def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 200):
        return [Candle(datetime.now(timezone.utc), 99, 101, 98, 100, 1000)]

    async def place_order(self, request: OrderRequest):
        self.last_order = request
        return OrderResult("1", request.symbol, "filled", request.side, request.order_type,
                           request.quantity, request.quantity)

    async def cancel_order(self, symbol: str, order_id: str):
        return OrderResult(order_id, symbol, "canceled", "buy", "limit", 0)

    async def fetch_open_orders(self, symbol: str | None = None):
        return []

    async def fetch_positions(self):
        return []

    async def close_position(self, symbol: str):
        return OrderResult("2", symbol, "filled", "sell", "market", 1)

    async def set_leverage(self, symbol: str, leverage: int):
        return True


def _signal(direction="long", entry=100.0, stop=99.0) -> StrategySignal:
    tps = [101.2, 101.8, 102.6] if direction == "long" else [98.8, 98.2, 97.4]
    return StrategySignal(
        setup_name="mr", symbol="BTCUSDT", direction=direction, entry_price=entry,
        stop_loss=stop, take_profit_levels=tps, trailing_stop=stop, expected_move=1.8,
        risk_reward_ratio=1.8, confidence_score=72, reasons_for_entry=["t"], accepted=True,
    )


def _permission() -> TradePermissionRequest:
    return TradePermissionRequest(
        bot_enabled=True, exchange_connected=True, live_requested=False, daily_pnl_pct=0,
        consecutive_losses=0, open_trades=0, market_regime="good", session_tradable=True,
        coin_in_watchlist=True, liquid_enough=True, spread_bps=1, expected_net_profit_pct=0.5,
        setup_score=80, setup_grade="A", btc_eth_confirmed=True, risk_reward=1.8,
        position_size_valid=True, order_size_valid=True, leader_confirmation_required=False,
    )


def _manager(exchange) -> OrderManager:
    return OrderManager(exchange, settings=Settings(trading_mode=TradingMode.TESTNET))


# ------------------------------------------------------------------ maker pricing

async def test_maker_long_rests_at_bid_with_gtx():
    ex = RecordingExchange()
    report = await _manager(ex).execute_signal(_signal("long"), _permission(), quantity=0.1, maker=True)
    assert report.accepted is True
    assert ex.last_order.order_type == "limit"
    assert ex.last_order.time_in_force == "GTX"            # post-only
    assert ex.last_order.side == "buy"
    assert ex.last_order.price == 99.99                    # rests at best_bid (maker), not crossing


async def test_maker_short_rests_at_ask_with_gtx():
    ex = RecordingExchange()
    report = await _manager(ex).execute_signal(_signal("short"), _permission(), quantity=0.1, maker=True)
    assert report.accepted is True
    assert ex.last_order.side == "sell"
    assert ex.last_order.time_in_force == "GTX"
    assert ex.last_order.price == 100.01                  # rests at best_ask (maker)


async def test_taker_default_crosses_the_spread():
    ex = RecordingExchange()
    await _manager(ex).execute_signal(_signal("long"), _permission(), quantity=0.1)  # maker=False
    assert ex.last_order.time_in_force != "GTX"
    assert ex.last_order.price > 100.01                   # taker crosses the ask


async def test_maker_price_drift_guard_rejects():
    ex = RecordingExchange()
    # signal entry far from the live book -> drift guard trips -> no order placed
    report = await _manager(ex).execute_signal(_signal("long", entry=110.0, stop=108.0),
                                               _permission(), quantity=0.1, maker=True)
    assert report.accepted is False
    assert ex.last_order is None


# ------------------------------------------------------------------ unprotected-pending gap closure

async def test_protect_filled_pending_reconstructs_signal_and_attaches():
    runner = BotRunner(Settings(trading_mode=TradingMode.TESTNET))
    runner._attach_branch1_protective = AsyncMock()
    trade = Trade(
        symbol="ETHUSDT", side="long", entry_price=100.0, stop_loss=99.0,
        take_profit={"levels": [101.0, 102.0, 103.0]}, setup_name="mean_reversion_scalp",
        status="open", quantity=1.0,
    )

    await runner._protect_filled_pending(db=object(), trade=trade)

    runner._attach_branch1_protective.assert_awaited_once()
    signal = runner._attach_branch1_protective.call_args.args[2]
    assert signal.direction == "long"
    assert signal.symbol == "ETHUSDT"
    assert signal.stop_loss == 99.0
    assert signal.take_profit_levels == [101.0, 102.0, 103.0]
