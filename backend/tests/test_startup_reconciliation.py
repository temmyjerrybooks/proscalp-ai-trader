"""Phase 2B Branch 1: tests for BotRunner._startup_reconciliation 3-state handler."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config.settings import Settings, TradingMode
from app.database.models import Trade
from app.exchanges.base import (
    ExchangeAdapter, OrderBook, OrderRequest, OrderResult, Position,
)
from app.execution.order_manager import OrderManager, ProtectiveOrdersResult
from app.services.bot_runner import BotRunner


def _trade(*, id: str, symbol: str, stop_id: str | None = None, tp_id: str | None = None) -> Trade:
    extra: dict = {}
    if stop_id:
        extra["stop_order_id"] = stop_id
    if tp_id:
        extra["take_profit_order_id"] = tp_id
    return Trade(
        id=id, symbol=symbol, side="long", exchange="binance", mode="testnet",
        setup_name="test", entry_price=100.0, stop_loss=99.0,
        take_profit={"levels": [101.2, 101.8, 102.6]}, quantity=0.1, status="open", extra=extra,
    )


def _order(order_id: str, symbol: str, client_order_id: str) -> OrderResult:
    return OrderResult(
        order_id=order_id, symbol=symbol, status="new",
        side="sell", order_type="stop_market", quantity=0.0,
        raw={"clientOrderId": client_order_id},
    )


class FakeExchange(ExchangeAdapter):
    name = "fake"
    def __init__(self, positions=None, open_orders=None):
        self._positions = positions or []
        self._open_orders = open_orders or []
        self.cancelled: list[tuple[str, str]] = []
    async def fetch_balances(self): return []
    async def fetch_tickers(self): return []
    async def fetch_order_book(self, symbol, limit=50): return OrderBook(symbol, bids=[(99.99,1)], asks=[(100.01,1)])
    async def fetch_ohlcv(self, symbol, timeframe, limit=200): return []
    async def place_order(self, request): return OrderResult("x", request.symbol, "new", request.side, request.order_type, 0)
    async def cancel_order(self, symbol, order_id):
        self.cancelled.append((symbol, order_id))
        return OrderResult(order_id, symbol, "canceled", "sell", "stop_market", 0)
    async def fetch_open_orders(self, symbol=None): return list(self._open_orders)
    async def fetch_positions(self): return list(self._positions)
    async def close_position(self, symbol): return OrderResult("0", symbol, "filled", "sell", "market", 0)
    async def set_leverage(self, symbol, leverage): return True


def _runner() -> BotRunner:
    s = Settings(
        trading_mode=TradingMode.TESTNET,
        exchange_resting_exits_enabled=True,
        startup_reconciliation_enabled=True,
    )
    return BotRunner(settings=s)


@pytest.mark.asyncio
async def test_state1_db_trade_with_matching_protective_orders_is_reconciled(monkeypatch):
    runner = _runner()
    trade = _trade(id="t1", symbol="BTCUSDT", stop_id="sl-1", tp_id="tp-1")
    positions = [Position("BTCUSDT", "long", 0.1, 100.0, 101.0)]
    open_orders = [_order("sl-1", "BTCUSDT", "proscalp-sl-xxx"), _order("tp-1", "BTCUSDT", "proscalp-tp-yyy")]
    adapter = FakeExchange(positions=positions, open_orders=open_orders)
    runner._open_database_trades = AsyncMock(return_value=[trade])
    runner._risk_event = AsyncMock()
    attach_calls: list = []
    async def _fake_attach(self, signal, trade_id, **kw):
        attach_calls.append(trade_id)
        return ProtectiveOrdersResult(stop_order_id="x", take_profit_order_id="y", elapsed_ms=1.0, success=True)
    monkeypatch.setattr(OrderManager, "attach_protective_orders", _fake_attach)
    db = MagicMock()
    db.commit = AsyncMock()
    await runner._startup_reconciliation(db, adapter)
    assert attach_calls == []  # state 1: no repair needed
    assert adapter.cancelled == []
    db.commit.assert_awaited_once()
    risk_payload = runner._risk_event.await_args.args[4]
    assert risk_payload == {"reconciled": 1, "repaired": 0, "orphan_cancelled": 0}


@pytest.mark.asyncio
async def test_state2_db_trade_without_protective_orders_triggers_repair(monkeypatch):
    runner = _runner()
    trade = _trade(id="t2", symbol="ETHUSDT")  # no stored stop/tp ids
    positions = [Position("ETHUSDT", "long", 1.0, 3000.0, 3010.0)]
    adapter = FakeExchange(positions=positions, open_orders=[])  # no protective orders on exchange
    runner._open_database_trades = AsyncMock(return_value=[trade])
    runner._risk_event = AsyncMock()
    attach_calls: list = []
    async def _fake_attach(self, signal, trade_id, **kw):
        attach_calls.append((trade_id, signal.symbol))
        return ProtectiveOrdersResult(stop_order_id="new-sl", take_profit_order_id="new-tp", elapsed_ms=42.0, success=True)
    monkeypatch.setattr(OrderManager, "attach_protective_orders", _fake_attach)
    db = MagicMock(); db.commit = AsyncMock()
    await runner._startup_reconciliation(db, adapter)
    assert attach_calls == [("t2", "ETHUSDT")]
    risk_payload = runner._risk_event.await_args.args[4]
    assert risk_payload["repaired"] == 1
    # The trade row now carries the new protective IDs
    assert trade.extra["stop_order_id"] == "new-sl"
    assert trade.extra["take_profit_order_id"] == "new-tp"
    assert trade.extra["exchange_resting_active"] is True


@pytest.mark.asyncio
async def test_state3_orphan_protective_orders_get_cancelled(monkeypatch):
    runner = _runner()
    # No DB trades, but exchange has orphan protective orders (no matching position either)
    open_orders = [_order("orph-1", "SOLUSDT", "proscalp-sl-abc"), _order("orph-2", "SOLUSDT", "proscalp-tp-def")]
    adapter = FakeExchange(positions=[], open_orders=open_orders)
    runner._open_database_trades = AsyncMock(return_value=[])
    runner._risk_event = AsyncMock()
    monkeypatch.setattr(OrderManager, "attach_protective_orders", AsyncMock())
    db = MagicMock(); db.commit = AsyncMock()
    await runner._startup_reconciliation(db, adapter)
    assert sorted(adapter.cancelled) == [("SOLUSDT", "orph-1"), ("SOLUSDT", "orph-2")]
    risk_payload = runner._risk_event.await_args.args[4]
    assert risk_payload["orphan_cancelled"] == 2


@pytest.mark.asyncio
async def test_non_proscalp_orders_are_not_touched(monkeypatch):
    """Protective orders with foreign client_order_id prefixes (manual user orders)
    must never be cancelled by the reconciliation pass."""
    runner = _runner()
    foreign = _order("user-1", "BTCUSDT", "manual-order-xyz")
    adapter = FakeExchange(positions=[], open_orders=[foreign])
    runner._open_database_trades = AsyncMock(return_value=[])
    runner._risk_event = AsyncMock()
    monkeypatch.setattr(OrderManager, "attach_protective_orders", AsyncMock())
    db = MagicMock(); db.commit = AsyncMock()
    await runner._startup_reconciliation(db, adapter)
    assert adapter.cancelled == []  # foreign order untouched
