from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.exchanges.base import Balance, Candle, ExchangeAdapter, OrderBook, OrderRequest, OrderResult, Position, Ticker


class MockExchange(ExchangeAdapter):
    name = "mock"

    async def fetch_balances(self):
        return [Balance("USDT", 1000, total=1000)]

    async def fetch_tickers(self):
        return [Ticker("BTCUSDT", 100, 100_000_000, 1, 102, 98, "mock")]

    async def fetch_order_book(self, symbol: str, limit: int = 50):
        return OrderBook(symbol, bids=[(99.9, 10), (99.8, 10)], asks=[(100.1, 10), (100.2, 10)])

    async def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 200):
        return [Candle(datetime.now(timezone.utc), 99, 101, 98, 100, 1000)]

    async def place_order(self, request: OrderRequest):
        return OrderResult("1", request.symbol, "filled", request.side, request.order_type, request.quantity)

    async def cancel_order(self, symbol: str, order_id: str):
        return OrderResult(order_id, symbol, "canceled", "buy", "limit", 0)

    async def fetch_open_orders(self, symbol: str | None = None):
        return []

    async def fetch_positions(self):
        return [Position("BTCUSDT", "long", 1, 100, 101)]

    async def close_position(self, symbol: str):
        return OrderResult("2", symbol, "filled", "sell", "market", 1)

    async def set_leverage(self, symbol: str, leverage: int):
        return True


@pytest.mark.asyncio
async def test_exchange_adapter_mock_slippage_and_min_size():
    exchange = MockExchange()
    slippage = await exchange.estimate_slippage("BTCUSDT", "buy", 5)
    valid = await exchange.validate_min_order_size("BTCUSDT", 0.1, 100)

    assert slippage > 0
    assert valid is True
