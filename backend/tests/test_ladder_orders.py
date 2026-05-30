"""Phase 2B Branch 2: OrderManager ladder-order mechanics
(attach / advance stop / replace tiers / cancel)."""
from __future__ import annotations

import pytest

from app.config.settings import Settings, TradingMode
from app.exchanges.base import ExchangeAdapter, OrderBook, OrderResult
from app.execution.exit_ladder import SymbolRules, build_ladder_plan
from app.execution.order_manager import OrderManager

_RULES = SymbolRules(tick_size=0.01, step_size=0.0001, min_qty=0.0001, min_notional=5.0)


def _plan(direction="long", quantity=1.0):
    return build_ladder_plan(
        settings=Settings(), direction=direction, entry_price=1000.0, stop_loss=995.0,
        atr=10.0, quantity=quantity, rules=_RULES,
    )


class FakeExchange(ExchangeAdapter):
    """Records an ordered event log of placements/cancels; supports fail injection."""
    name = "fake"

    def __init__(self, *, fail_on_types=None):
        self.events: list[tuple[str, str]] = []  # ("place"|"cancel", detail)
        self.placed = []
        self.cancelled = []
        self.fail_on_types = set(fail_on_types or [])
        self._n = 0

    async def fetch_balances(self): return []
    async def fetch_tickers(self): return []
    async def fetch_order_book(self, symbol, limit=50):
        return OrderBook(symbol, bids=[(999.9, 1)], asks=[(1000.1, 1)])
    async def fetch_ohlcv(self, symbol, timeframe, limit=200): return []
    async def fetch_open_orders(self, symbol=None): return []
    async def fetch_positions(self): return []
    async def close_position(self, symbol): return OrderResult("0", symbol, "filled", "sell", "market", 0)
    async def set_leverage(self, symbol, leverage): return True

    async def fetch_symbol_rules(self, symbol):
        return {"tick_size": 0.01, "step_size": 0.0001, "min_qty": 0.0001, "min_notional": 5.0}

    async def place_order(self, request):
        if request.order_type in self.fail_on_types:
            raise RuntimeError(f"injected failure on {request.order_type}")
        self._n += 1
        oid = f"o{self._n}"
        self.events.append(("place", request.order_type))
        self.placed.append(request)
        return OrderResult(oid, request.symbol, "new", request.side, request.order_type, request.quantity)

    async def cancel_order(self, symbol, order_id):
        if "cancel" in self.fail_on_types:
            raise RuntimeError("cancel failed")
        self.events.append(("cancel", order_id))
        self.cancelled.append((symbol, order_id))
        return OrderResult(order_id, symbol, "canceled", "sell", "stop_market", 0)

    # Ladder orders are conditional -> algo endpoints; delegate to the same recorder.
    async def place_algo_order(self, request):
        return await self.place_order(request)

    async def cancel_algo_order(self, symbol, order_id):
        return await self.cancel_order(symbol, order_id)


def _manager(exch):
    return OrderManager(exch, settings=Settings(trading_mode=TradingMode.TESTNET, market_type="futures"))


@pytest.mark.asyncio
async def test_attach_full_ladder_places_stop_tiers_runner():
    exch = FakeExchange()
    mgr = _manager(exch)
    result = await mgr.attach_ladder_orders(_plan(), "BTCUSDT", "long", "t1")
    assert result.success is True
    assert result.ladder_active is True
    types = [t for (_, t) in exch.events]
    # stop first, then 4 reduceOnly TP tiers, then trailing runner
    assert types == ["stop_market", "take_profit_market", "take_profit_market",
                     "take_profit_market", "take_profit_market", "trailing_stop_market"]
    assert exch.placed[0].close_position is True
    assert all(p.reduce_only for p in exch.placed[1:5])
    assert all(not p.close_position for p in exch.placed[1:5])
    assert exch.placed[5].order_type == "trailing_stop_market"
    assert exch.placed[5].callback_rate is not None
    assert len(result.tier_orders) == 4
    assert result.runner_order_id is not None


@pytest.mark.asyncio
async def test_attach_long_uses_sell_exit_side():
    exch = FakeExchange()
    result = await _manager(exch).attach_ladder_orders(_plan("long"), "BTCUSDT", "long", "t1")
    assert all(p.side == "sell" for p in exch.placed)
    assert result.success


@pytest.mark.asyncio
async def test_attach_short_uses_buy_exit_side():
    exch = FakeExchange()
    await _manager(exch).attach_ladder_orders(_plan("short"), "BTCUSDT", "short", "t1")
    assert all(p.side == "buy" for p in exch.placed)


@pytest.mark.asyncio
async def test_attach_stop_failure_aborts_everything():
    exch = FakeExchange(fail_on_types=["stop_market"])
    result = await _manager(exch).attach_ladder_orders(_plan(), "BTCUSDT", "long", "t1")
    assert result.success is False
    assert result.ladder_active is False  # no stop -> nothing else attempted
    assert result.stop_order_id is None
    assert exch.placed == []  # stop failed first, no tiers/runner attempted


@pytest.mark.asyncio
async def test_attach_one_tier_failure_is_non_fatal_position_still_protected():
    exch = FakeExchange(fail_on_types=["take_profit_market"])
    result = await _manager(exch).attach_ladder_orders(_plan(), "BTCUSDT", "long", "t1")
    # stop + runner placed; all tiers failed
    assert result.success is False
    assert result.stop_order_id is not None      # position protected by the stop
    assert result.ladder_active is False          # no tiers landed
    assert len(result.tier_orders) == 0
    assert any("tier" in r for r in result.reasons)


@pytest.mark.asyncio
async def test_attach_runner_failure_keeps_ladder_active():
    exch = FakeExchange(fail_on_types=["trailing_stop_market"])
    result = await _manager(exch).attach_ladder_orders(_plan(), "BTCUSDT", "long", "t1")
    assert result.success is False           # runner failed
    assert result.ladder_active is True      # stop + tiers present -> sync still manages it
    assert result.runner_order_id is None
    assert any("runner" in r for r in result.reasons)


@pytest.mark.asyncio
async def test_advance_stop_places_new_before_cancelling_old():
    exch = FakeExchange()
    mgr = _manager(exch)
    new_id, issues = await mgr.advance_ladder_stop("BTCUSDT", "long", 1005.0, "old-stop-1", "t1")
    assert new_id == "o1"
    assert issues == []
    # The NEW stop is placed BEFORE the old one is cancelled (never unprotected).
    assert exch.events[0] == ("place", "stop_market")
    assert exch.events[1] == ("cancel", "old-stop-1")


@pytest.mark.asyncio
async def test_advance_stop_placement_failure_leaves_old_in_place():
    exch = FakeExchange(fail_on_types=["stop_market"])
    new_id, issues = await _manager(exch).advance_ladder_stop("BTCUSDT", "long", 1005.0, "old-stop-1", "t1")
    assert new_id is None
    assert exch.cancelled == []  # old stop NOT cancelled when the new one fails
    assert issues and "placement failed" in issues[0]


@pytest.mark.asyncio
async def test_cancel_orders_skips_none_and_captures_failures():
    exch = FakeExchange()
    issues = await _manager(exch).cancel_orders("BTCUSDT", ["a", None, "b"])
    assert issues == []
    assert exch.cancelled == [("BTCUSDT", "a"), ("BTCUSDT", "b")]

    exch2 = FakeExchange(fail_on_types=["cancel"])
    issues2 = await _manager(exch2).cancel_orders("BTCUSDT", ["a", "b"])
    assert len(issues2) == 2


@pytest.mark.asyncio
async def test_replace_ladder_tiers_places_tiers_and_runner_only():
    exch = FakeExchange()
    tiers, runner_id, reasons = await _manager(exch).replace_ladder_tiers(_plan(), "BTCUSDT", "long", "t1")
    types = [t for (_, t) in exch.events]
    assert "stop_market" not in types  # the marching stop is NOT touched
    assert types.count("take_profit_market") == 4
    assert types.count("trailing_stop_market") == 1
    assert len(tiers) == 4
    assert runner_id is not None


@pytest.mark.asyncio
async def test_attach_ladder_rejects_single_plan():
    exch = FakeExchange()
    single = _plan(quantity=0.008)  # too small -> single mode
    assert single.mode == "single"
    with pytest.raises(ValueError, match="non-ladder plan"):
        await _manager(exch).attach_ladder_orders(single, "BTCUSDT", "long", "t1")


@pytest.mark.asyncio
async def test_build_ladder_plan_uses_adapter_rules():
    exch = FakeExchange()
    plan = await _manager(exch).build_ladder_plan(
        direction="long", entry_price=1000.0, stop_loss=995.0, atr=10.0, quantity=1.0, symbol="BTCUSDT",
    )
    assert plan.mode == "full"
    assert len(plan.tiers) == 4
