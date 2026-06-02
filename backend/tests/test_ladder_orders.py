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

    def __init__(self, *, fail_on_types=None, raise_2021_on=None,
                 raise_2021_on_prices=None, hard_fail_on_prices=None,
                 slow_on_prices=None, slow_seconds=5.0):
        self.events: list[tuple[str, str]] = []  # ("place"|"cancel", detail)
        self.placed = []
        self.cancelled = []
        self.fail_on_types = set(fail_on_types or [])
        # Algo order types that should raise a -2021 ("would immediately trigger")
        # on placement, to exercise the item-1 race-fallback path.
        self.raise_2021_on = set(raise_2021_on or [])
        # Per-trigger-price fault injection for the item-2 concurrent gather tests.
        self.raise_2021_on_prices = set(raise_2021_on_prices or [])   # -2021 on these triggers
        self.hard_fail_on_prices = set(hard_fail_on_prices or [])     # non-2021 error
        self.slow_on_prices = set(slow_on_prices or [])               # hang past the timeout
        self.slow_seconds = slow_seconds
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
        if request.order_type in self.raise_2021_on:
            raise RuntimeError(
                'Binance response: {"code":-2021,"msg":"Order would immediately trigger."}'
            )
        if request.stop_price in self.slow_on_prices:
            import asyncio
            await asyncio.sleep(self.slow_seconds)  # hang -> exceeds the per-coro timeout
        if request.stop_price in self.raise_2021_on_prices:
            raise RuntimeError(
                'Binance response: {"code":-2021,"msg":"Order would immediately trigger."}'
            )
        if request.stop_price in self.hard_fail_on_prices:
            raise RuntimeError('Binance response: {"code":-4131,"msg":"rate limited"}')
        return await self.place_order(request)

    async def cancel_algo_order(self, symbol, order_id):
        return await self.cancel_order(symbol, order_id)


def _manager(exch):
    return OrderManager(exch, settings=Settings(trading_mode=TradingMode.TESTNET, market_type="futures"))


def _manager_timeout(exch, timeout):
    return OrderManager(exch, settings=Settings(
        trading_mode=TradingMode.TESTNET, market_type="futures",
        ladder_attach_order_timeout_s=timeout))


def _classify(result):
    from app.execution.exit_ladder import classify_attach
    return classify_attach(
        stop_placed=result.stop_order_id is not None, planned_tier_count=4,
        rested_tier_count=len(result.tier_orders), immediate_fill_count=len(result.immediate_fills),
        planned_runner=True, runner_placed=result.runner_order_id is not None,
    )


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
    tiers, runner_id, immediate_fills, reasons = await _manager(exch).replace_ladder_tiers(
        _plan(), "BTCUSDT", "long", "t1"
    )
    types = [t for (_, t) in exch.events]
    assert "stop_market" not in types  # the marching stop is NOT touched
    assert types.count("take_profit_market") == 4
    assert types.count("trailing_stop_market") == 1
    assert len(tiers) == 4
    assert immediate_fills == []
    assert runner_id is not None


# ----- Item 1: in-profit tier handling at placement -----

def test_tier_trigger_reached_pure():
    from app.execution.exit_ladder import tier_trigger_reached
    # long exits SELL on a rise -> reached when tier at/below mark
    assert tier_trigger_reached("long", 1003.0, 1007.0) is True
    assert tier_trigger_reached("long", 1010.0, 1007.0) is False
    # short exits BUY on a fall -> reached when tier at/above mark
    assert tier_trigger_reached("short", 997.0, 993.0) is True
    assert tier_trigger_reached("short", 990.0, 993.0) is False
    # non-positive mark -> never "reached" (fall back to a resting order)
    assert tier_trigger_reached("long", 1003.0, 0.0) is False


@pytest.mark.asyncio
async def test_attach_in_profit_tiers_market_closed_not_skipped():
    # mark=1007 -> tiers at 1003 & 1006 already reached (taken at market),
    # tiers at 1010 & 1016 rest as normal TP orders. ATR=10 mults [.3,.6,1,1.6].
    exch = FakeExchange()
    result = await _manager(exch).attach_ladder_orders(
        _plan("long"), "BTCUSDT", "long", "t1", mark_price=1007.0
    )
    assert result.success is True  # every tier accounted: 2 resting + 2 immediate
    assert len(result.tier_orders) == 2
    assert len(result.immediate_fills) == 2
    assert [f.index for f in result.immediate_fills] == [1, 2]
    assert all(f.trigger_reached for f in result.immediate_fills)
    types = [t for (_, t) in exch.events]
    # Stop is placed+confirmed FIRST (sequential, never in the gather). The tiers
    # then fire concurrently, so only the stop's leading position is guaranteed.
    assert types[0] == "stop_market"
    rest = types[1:]
    assert rest.count("market") == 2                 # tier1 + tier2 taken at market
    assert rest.count("take_profit_market") == 2     # tier3 + tier4 rest
    assert rest.count("trailing_stop_market") == 1   # runner
    market_orders = [p for p in exch.placed if p.order_type == "market"]
    assert len(market_orders) == 2
    assert all(p.reduce_only and p.side == "sell" for p in market_orders)


@pytest.mark.asyncio
async def test_attach_2021_race_falls_back_to_market_close():
    # Pre-check passes (mark below all tiers) but the exchange still -2021s every
    # TP placement -> each slice falls back to an immediate market close.
    exch = FakeExchange(raise_2021_on=["take_profit_market"])
    result = await _manager(exch).attach_ladder_orders(
        _plan("long"), "BTCUSDT", "long", "t1", mark_price=1002.9
    )
    assert result.success is True
    assert len(result.tier_orders) == 0
    assert len(result.immediate_fills) == 4
    assert all(not f.trigger_reached for f in result.immediate_fills)
    assert [f.index for f in result.immediate_fills] == [1, 2, 3, 4]  # mapping preserved
    types = [t for (_, t) in exch.events]
    assert types[0] == "stop_market"                       # stop first, then concurrent
    assert types.count("market") == 4                      # all 4 -2021 -> market closed
    assert types.count("trailing_stop_market") == 1        # runner still rested


@pytest.mark.asyncio
async def test_attach_2021_fallback_market_close_failure_is_recorded():
    # -2021 on the TP AND the fallback market close also fails -> recorded as a
    # reason, slice un-realized (still protected by the closePosition stop).
    exch = FakeExchange(raise_2021_on=["take_profit_market"], fail_on_types=["market"])
    result = await _manager(exch).attach_ladder_orders(
        _plan("long"), "BTCUSDT", "long", "t1", mark_price=1002.9
    )
    assert result.success is False
    assert len(result.immediate_fills) == 0
    assert result.stop_order_id is not None  # position still protected
    assert any("market close failed" in r for r in result.reasons)


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


# ----- Item 2: concurrent attach (asyncio.gather) — AC2-1..AC2-7 -----
# _plan("long") tier triggers: 1003 (idx1), 1006 (idx2), 1010 (idx3), 1016 (idx4).


@pytest.mark.asyncio
async def test_AC2_1_happy_path_all_rest_full():
    # Stop confirmed first, 5 slice legs fire concurrently, all rest.
    exch = FakeExchange()
    result = await _manager(exch).attach_ladder_orders(_plan("long"), "BTCUSDT", "long", "t1")
    assert result.success is True
    assert result.stop_order_id is not None
    assert len(result.tier_orders) == 4 and result.runner_order_id is not None
    assert result.immediate_fills == []
    assert _classify(result) == "full"


@pytest.mark.asyncio
async def test_AC2_2_stop_confirmed_before_gather():
    exch = FakeExchange()
    result = await _manager(exch).attach_ladder_orders(_plan("long"), "BTCUSDT", "long", "t1")
    # The stop is the FIRST placement recorded and is resting before any tier fires.
    assert exch.events[0] == ("place", "stop_market")
    assert result.stop_order_id is not None
    # exactly one stop_market placement (never re-placed, never in the gather)
    assert [t for (_, t) in exch.events].count("stop_market") == 1


@pytest.mark.asyncio
async def test_AC2_3_stop_failure_aborts_no_tiers_fired():
    exch = FakeExchange(fail_on_types=["stop_market"])
    result = await _manager(exch).attach_ladder_orders(_plan("long"), "BTCUSDT", "long", "t1")
    assert result.success is False
    assert result.stop_order_id is None
    assert result.ladder_active is False
    assert exch.placed == []           # tiers never fired -> no orphans
    assert result.tier_orders == [] and result.immediate_fills == []


@pytest.mark.asyncio
async def test_AC2_4_partial_gather_2021_subset_full():
    # 2 of 4 tiers -2021 in the gather -> market-closed (FILLED_AT_ATTACH); rest rest.
    exch = FakeExchange(raise_2021_on_prices={1003.0, 1006.0})
    result = await _manager(exch).attach_ladder_orders(_plan("long"), "BTCUSDT", "long", "t1")
    assert len(result.immediate_fills) == 2
    assert [f.index for f in result.immediate_fills] == [1, 2]
    assert all(not f.trigger_reached for f in result.immediate_fills)  # race, not pre-check
    assert len(result.tier_orders) == 2 and result.runner_order_id is not None
    assert result.success is True
    assert _classify(result) == "full"   # market-close is accounted, not degradation


@pytest.mark.asyncio
async def test_AC2_5_partial_gather_hard_error_partial():
    # 1 of 5 raises a non-2021 error -> dropped-but-protected gap; classifier PARTIAL.
    exch = FakeExchange(hard_fail_on_prices={1003.0})
    result = await _manager(exch).attach_ladder_orders(_plan("long"), "BTCUSDT", "long", "t1")
    assert len(result.tier_orders) == 3
    assert result.immediate_fills == []
    assert any("tier 1 placement failed" in r for r in result.reasons)
    assert result.stop_order_id is not None     # position still protected by the stop
    assert _classify(result) == "partial"


@pytest.mark.asyncio
async def test_AC2_6_mapping_integrity_mixed_outcomes():
    # idx1 (1003) -2021 -> market; idx3 (1010) hard error -> gap; idx2/idx4 rest.
    exch = FakeExchange(raise_2021_on_prices={1003.0}, hard_fail_on_prices={1010.0})
    result = await _manager(exch).attach_ladder_orders(_plan("long"), "BTCUSDT", "long", "t1")
    assert [f.index for f in result.immediate_fills] == [1]            # 1003 -> market
    assert sorted(t.index for t in result.tier_orders) == [2, 4]       # 1006, 1016 rest
    assert any("tier 3 placement failed" in r for r in result.reasons)  # 1010 dropped
    # disposition landed on the RIGHT tier (price matches index)
    by_index = {t.index: t.price for t in result.tier_orders}
    assert by_index[2] == 1006.0 and by_index[4] == 1016.0


@pytest.mark.asyncio
async def test_AC2_7_timeout_isolation():
    # idx2 (1006) hangs past the per-coro timeout -> hard-error gap; others record.
    exch = FakeExchange(slow_on_prices={1006.0}, slow_seconds=5.0)
    result = await _manager_timeout(exch, 0.05).attach_ladder_orders(_plan("long"), "BTCUSDT", "long", "t1")
    assert sorted(t.index for t in result.tier_orders) == [1, 3, 4]    # 3 still rested
    assert result.runner_order_id is not None
    assert any("tier 2 placement failed" in r for r in result.reasons)  # timeout -> gap
    assert _classify(result) == "partial"
