"""Phase 2B Branch 1: tests for OrderManager.attach_protective_orders + cancel."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from app.config.settings import Settings, TradingMode
from app.execution.order_manager import OrderManager
from app.exchanges.base import (
    Balance, Candle, ExchangeAdapter, OrderBook, OrderRequest, OrderResult, Position, Ticker,
)
from app.strategies.base_strategy import StrategySignal


def _signal(direction="long", entry=100.0, stop=99.0, tps=(101.2, 101.8, 102.6)) -> StrategySignal:
    return StrategySignal(
        setup_name="test setup",
        symbol="BTCUSDT",
        direction=direction,  # type: ignore[arg-type]
        entry_price=entry,
        stop_loss=stop,
        take_profit_levels=list(tps),
        trailing_stop=0.0,
        expected_move=2.0,
        risk_reward_ratio=2.6,
        confidence_score=80.0,
        accepted=True,
    )


class FakeExchange(ExchangeAdapter):
    """Records every place_order / cancel_order call; can be configured to slow or fail."""
    name = "fake"

    def __init__(self, *, place_delay_s: float = 0.0, fail_on: str | None = None) -> None:
        self.placed: list[OrderRequest] = []
        self.cancelled: list[tuple[str, str]] = []
        self.place_delay_s = place_delay_s
        self.fail_on = fail_on  # "stop_market" | "take_profit_market" | "cancel" | None
        self._order_counter = 0

    async def fetch_balances(self): return []
    async def fetch_tickers(self): return []
    async def fetch_order_book(self, symbol, limit=50):
        return OrderBook(symbol, bids=[(99.99, 1)], asks=[(100.01, 1)])
    async def fetch_ohlcv(self, symbol, timeframe, limit=200): return []
    async def fetch_open_orders(self, symbol=None): return []
    async def fetch_positions(self): return []
    async def close_position(self, symbol):
        return OrderResult("0", symbol, "filled", "sell", "market", 0)
    async def set_leverage(self, symbol, leverage): return True

    async def place_order(self, request):
        if self.place_delay_s > 0:
            await asyncio.sleep(self.place_delay_s)
        if self.fail_on and request.order_type == self.fail_on:
            raise RuntimeError(f"injected failure on {request.order_type}")
        self.placed.append(request)
        self._order_counter += 1
        return OrderResult(
            order_id=f"o{self._order_counter}", symbol=request.symbol,
            status="new", side=request.side, order_type=request.order_type,
            quantity=request.quantity,
        )

    async def cancel_order(self, symbol, order_id):
        if self.fail_on == "cancel":
            raise RuntimeError("cancel failed")
        self.cancelled.append((symbol, order_id))
        return OrderResult(order_id, symbol, "canceled", "sell", "stop_market", 0)

    # Protective orders are conditional -> algo endpoints. Delegate to the same
    # recording logic so existing assertions on the OrderRequest still hold.
    async def place_algo_order(self, request):
        return await self.place_order(request)

    async def cancel_algo_order(self, symbol, order_id):
        return await self.cancel_order(symbol, order_id)


def _mk_manager(*, slow_ms: int | None = None, **fake_kw) -> tuple[OrderManager, FakeExchange]:
    settings_kw = {"trading_mode": TradingMode.TESTNET}
    if slow_ms is not None:
        settings_kw["protective_order_max_elapsed_ms"] = slow_ms
    exchange = FakeExchange(**fake_kw)
    manager = OrderManager(exchange, settings=Settings(**settings_kw))
    return manager, exchange


@pytest.mark.asyncio
async def test_attach_protective_orders_places_stop_then_tp_sequentially():
    manager, exch = _mk_manager()
    result = await manager.attach_protective_orders(_signal(direction="long"), trade_id="t1")
    assert result.success is True
    assert result.stop_order_id == "o1"
    assert result.take_profit_order_id == "o2"
    assert len(exch.placed) == 2
    # Stop placed first, take-profit second, both as closePosition sell orders
    assert exch.placed[0].order_type == "stop_market"
    assert exch.placed[0].side == "sell"
    assert exch.placed[0].close_position is True
    assert exch.placed[0].stop_price == 99.0
    assert exch.placed[0].working_type == "MARK_PRICE"
    assert exch.placed[1].order_type == "take_profit_market"
    assert exch.placed[1].stop_price == 102.6  # final TP rung


@pytest.mark.asyncio
async def test_attach_protective_orders_short_side_flips_to_buy():
    manager, exch = _mk_manager()
    await manager.attach_protective_orders(_signal(direction="short"), trade_id="t2")
    assert all(r.side == "buy" for r in exch.placed)


@pytest.mark.asyncio
async def test_attach_protective_orders_stop_failure_aborts_tp():
    manager, exch = _mk_manager(fail_on="stop_market")
    result = await manager.attach_protective_orders(_signal(), trade_id="t3")
    assert result.success is False
    assert result.stop_order_id is None
    assert result.take_profit_order_id is None  # TP not attempted when stop fails
    assert len(exch.placed) == 0
    assert any("stop_market" in r for r in result.reasons)


@pytest.mark.asyncio
async def test_attach_protective_orders_tp_failure_leaves_stop_placed():
    manager, exch = _mk_manager(fail_on="take_profit_market")
    result = await manager.attach_protective_orders(_signal(), trade_id="t4")
    assert result.success is False
    assert result.stop_order_id == "o1"  # stop did succeed
    assert result.take_profit_order_id is None
    assert any("take_profit_market" in r for r in result.reasons)


@pytest.mark.asyncio
async def test_attach_protective_orders_slow_warning(monkeypatch):
    # threshold 10ms, sleep 50ms per order -> ~100ms total, should warn
    manager, exch = _mk_manager(slow_ms=10, place_delay_s=0.05)
    from app.execution import order_manager as om
    captured: list[tuple[str, dict]] = []
    def _capture_warning(event, **kw):
        captured.append((event, kw))
    monkeypatch.setattr(om.logger, "warning", _capture_warning)
    result = await manager.attach_protective_orders(_signal(), trade_id="t5")
    assert result.elapsed_ms >= 80  # 2 x ~50ms sleeps
    assert any(event == "protective_order_slow" for event, _ in captured)


@pytest.mark.asyncio
async def test_cancel_protective_orders_calls_adapter_for_each():
    manager, exch = _mk_manager()
    issues = await manager.cancel_protective_orders("BTCUSDT", "sl-1", "tp-1")
    assert issues == []
    assert ("BTCUSDT", "sl-1") in exch.cancelled
    assert ("BTCUSDT", "tp-1") in exch.cancelled


@pytest.mark.asyncio
async def test_cancel_protective_orders_skips_none_ids():
    manager, exch = _mk_manager()
    issues = await manager.cancel_protective_orders("BTCUSDT", None, "tp-1")
    assert issues == []
    assert exch.cancelled == [("BTCUSDT", "tp-1")]


@pytest.mark.asyncio
async def test_cancel_protective_orders_captures_failures_without_raising():
    manager, exch = _mk_manager(fail_on="cancel")
    issues = await manager.cancel_protective_orders("BTCUSDT", "sl-1", "tp-1")
    assert len(issues) == 2  # both fail, neither raises
    assert all("cancel failed" in issue for issue in issues)
