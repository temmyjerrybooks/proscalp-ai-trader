from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.config.settings import Settings, TradingMode
from app.execution.order_manager import OrderManager
from app.exchanges.base import Balance, Candle, ExchangeAdapter, OrderBook, OrderRequest, OrderResult, Position, Ticker
from app.risk.risk_engine import TradePermissionRequest
from app.strategies.base_strategy import StrategySignal


class RecordingExchange(ExchangeAdapter):
    name = "recording"

    def __init__(
        self,
        book: OrderBook | None = None,
        order_status: str = "filled",
        filled_quantity: float | None = None,
    ) -> None:
        self.book = book or OrderBook("BTCUSDT", bids=[(99.99, 10)], asks=[(100.01, 10)])
        self.last_order: OrderRequest | None = None
        self.order_status = order_status
        self.filled_quantity = filled_quantity

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
        filled_quantity = request.quantity if self.filled_quantity is None else self.filled_quantity
        return OrderResult(
            "1",
            request.symbol,
            self.order_status,
            request.side,
            request.order_type,
            request.quantity,
            filled_quantity,
        )

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


def _signal(confidence: float = 72) -> StrategySignal:
    return StrategySignal(
        setup_name="test scalp",
        symbol="BTCUSDT",
        direction="long",
        entry_price=100,
        stop_loss=99,
        take_profit_levels=[101.2, 101.8, 102.6],
        trailing_stop=99.45,
        expected_move=1.8,
        risk_reward_ratio=1.8,
        confidence_score=confidence,
        reasons_for_entry=["test"],
        accepted=True,
    )


def _permission(setup_score: int = 80) -> TradePermissionRequest:
    return TradePermissionRequest(
        bot_enabled=True,
        exchange_connected=True,
        live_requested=False,
        daily_pnl_pct=0,
        consecutive_losses=0,
        open_trades=0,
        market_regime="good",
        session_tradable=True,
        coin_in_watchlist=True,
        liquid_enough=True,
        spread_bps=1,
        expected_net_profit_pct=0.5,
        setup_score=setup_score,
        setup_grade="A",
        btc_eth_confirmed=True,
        risk_reward=1.8,
        position_size_valid=True,
        order_size_valid=True,
    )


@pytest.mark.asyncio
async def test_high_setup_score_uses_market_order_even_when_signal_confidence_is_lower() -> None:
    exchange = RecordingExchange()
    manager = OrderManager(
        exchange,
        settings=Settings(trading_mode=TradingMode.TESTNET, market_order_min_score=88),
    )

    report = await manager.execute_signal(_signal(confidence=72), _permission(setup_score=90), quantity=0.1)

    assert report.accepted is True
    assert exchange.last_order is not None
    assert exchange.last_order.order_type == "market"
    assert exchange.last_order.time_in_force is None


@pytest.mark.asyncio
async def test_scalp_limit_uses_ioc_and_crosses_book_with_small_buffer() -> None:
    exchange = RecordingExchange()
    manager = OrderManager(
        exchange,
        settings=Settings(
            trading_mode=TradingMode.TESTNET,
            market_order_min_score=88,
            scalp_limit_time_in_force="IOC",
            aggressive_limit_slippage_bps=2.5,
        ),
    )

    report = await manager.execute_signal(_signal(confidence=72), _permission(setup_score=80), quantity=0.1)

    assert report.accepted is True
    assert exchange.last_order is not None
    assert exchange.last_order.order_type == "limit"
    assert exchange.last_order.time_in_force == "IOC"
    assert exchange.last_order.price is not None
    assert exchange.last_order.price > exchange.book.best_ask


@pytest.mark.asyncio
async def test_stale_signal_is_rejected_before_order_is_sent() -> None:
    exchange = RecordingExchange(OrderBook("BTCUSDT", bids=[(101.99, 10)], asks=[(102.01, 10)]))
    manager = OrderManager(
        exchange,
        settings=Settings(
            trading_mode=TradingMode.TESTNET,
            market_order_min_score=88,
            max_signal_price_drift_bps=12,
        ),
    )

    report = await manager.execute_signal(_signal(confidence=72), _permission(setup_score=80), quantity=0.1)

    assert report.accepted is False
    assert exchange.last_order is None
    assert any("signal price drift" in reason for reason in report.reasons)


@pytest.mark.asyncio
async def test_ioc_limit_without_immediate_fill_is_not_treated_as_open_trade() -> None:
    exchange = RecordingExchange(order_status="new", filled_quantity=0)
    manager = OrderManager(
        exchange,
        settings=Settings(
            trading_mode=TradingMode.TESTNET,
            market_order_min_score=88,
            scalp_limit_time_in_force="IOC",
        ),
    )

    report = await manager.execute_signal(_signal(confidence=72), _permission(setup_score=80), quantity=0.1)

    assert report.accepted is False
    assert exchange.last_order is not None
    assert any("not confirmed as filled immediately" in reason for reason in report.reasons)
