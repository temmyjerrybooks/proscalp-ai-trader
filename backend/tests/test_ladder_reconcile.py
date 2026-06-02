"""Phase 2B ladder-fix item 3 — reconciliation on tiers-ACTUALLY-PLACED.

Acceptance criteria AC-1..AC-8 from the item-3 spec. Every test drives the
real BotRunner reconciler through a status-driven mock adapter (no live exchange).

INV-1  ledger balances at close (Σ dispositions == original position qty)
INV-2  fills come ONLY from terminal status, never from order absence
INV-3  expected-total derives from placed_set, never the ladder plan
INV-4  anomaly == unexplained residual / original_position_qty
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.config.settings import Settings, TradingMode
from app.database.models import Trade
from app.exchanges.base import ExchangeAdapter, OrderBook, OrderResult, Position
from app.services.bot_runner import BotRunner
from unittest.mock import AsyncMock, MagicMock


# --------------------------------------------------------------------------- fake

class ReconcileFake(ExchangeAdapter):
    """Status-driven mock: each algo order id maps to (status, avg_price, filled_qty).
    fetch_algo_order reports the configured terminal status — the reconciler must
    branch on that, never on the order merely being absent from the open set."""
    name = "fake"

    def __init__(self, *, positions=None, open_algo_ids=None, order_status=None):
        self._positions = positions or []
        self._open_algo_ids = set(open_algo_ids) if open_algo_ids is not None else set()
        # order_id -> dict(status=..., avg=..., filled_qty=...)
        self._order_status = order_status or {}
        self.placed = []
        self.cancelled = []
        self._n = 0

    async def fetch_balances(self): return []
    async def fetch_tickers(self): return []
    async def fetch_order_book(self, symbol, limit=50):
        return OrderBook(symbol, bids=[(1003.9, 1)], asks=[(1004.1, 1)])
    async def fetch_ohlcv(self, symbol, timeframe, limit=200): return []
    async def set_leverage(self, symbol, leverage): return True
    async def close_position(self, symbol): return OrderResult("0", symbol, "filled", "sell", "market", 0)
    async def fetch_symbol_rules(self, symbol):
        return {"tick_size": 0.01, "step_size": 0.0001, "min_qty": 0.0001, "min_notional": 5.0}

    async def fetch_positions(self):
        return list(self._positions)

    async def fetch_open_orders(self, symbol=None):
        return [OrderResult(oid, symbol or "BTCUSDT", "new", "sell", "take_profit_market", 0)
                for oid in self._open_algo_ids]

    async def fetch_open_algo_orders(self, symbol=None):
        return await self.fetch_open_orders(symbol)

    async def fetch_algo_order(self, symbol, order_id):
        spec = self._order_status.get(order_id, {"status": "new", "avg": 0.0, "filled_qty": 0.0})
        return OrderResult(order_id, symbol, spec["status"], "sell", "take_profit_market",
                           spec.get("qty", 0.2), filled_quantity=spec.get("filled_qty", 0.0),
                           average_price=spec.get("avg") or None)

    async def fetch_order(self, symbol, order_id):
        return await self.fetch_algo_order(symbol, order_id)

    async def place_algo_order(self, request):
        return await self.place_order(request)

    async def cancel_algo_order(self, symbol, order_id):
        return await self.cancel_order(symbol, order_id)

    async def place_order(self, request):
        self._n += 1
        self.placed.append(request)
        return OrderResult(f"new{self._n}", request.symbol, "new", request.side,
                           request.order_type, request.quantity)

    async def cancel_order(self, symbol, order_id):
        self.cancelled.append((symbol, order_id))
        return OrderResult(order_id, symbol, "canceled", "sell", "stop_market", 0)


def _runner() -> BotRunner:
    s = Settings(trading_mode=TradingMode.TESTNET, market_type="futures",
                 exchange_resting_exits_enabled=True, five_tier_ladder_enabled=True)
    runner = BotRunner(settings=s)
    runner._risk_event = AsyncMock()
    runner.alerts.send = AsyncMock()
    return runner


def _db():
    db = MagicMock()
    db.commit = AsyncMock()
    return db


def _events(runner) -> list[str]:
    return [c.args[2] for c in runner._risk_event.await_args_list]


def _payload(runner, name) -> dict:
    return next(c.args[4] for c in runner._risk_event.await_args_list if c.args[2] == name)


def _leg(index, oid, price, qty, *, status="resting", filled_qty=0.0):
    return {"index": index, "order_id": oid, "price": price, "quantity": qty,
            "filled": status in ("filled", "partially_filled", "filled_at_attach"),
            "status": status, "filled_qty": filled_qty, "realized_pnl": None, "closed_by": None}


def _trade(*, qty, tiers, original_qty=None, runner_id="run1", runner_qty=0.2,
           runner_status="resting", stop_id="sl1"):
    original = original_qty if original_qty is not None else qty
    placed_slice = sum(t["quantity"] for t in tiers) + (runner_qty if runner_id else 0.0)
    return Trade(
        id="t1", symbol="BTCUSDT", side="long", exchange="binance", mode="testnet",
        setup_name="test", entry_price=1000.0, stop_loss=995.0,
        take_profit={"levels": [1003.0]}, quantity=qty, status="open", realized_pnl=0.0,
        opened_at=datetime.now(timezone.utc),
        extra={
            "ladder_active": True, "ladder_mode": "full", "entry_atr": 10.0,
            "stop_order_id": stop_id, "runner_order_id": runner_id,
            "runner_quantity": runner_qty, "runner_status": runner_status,
            "tier_orders": tiers, "tiers_filled": 0, "be_plus_armed": False,
            "runner_active": False, "time_partial_done": False,
            "original_quantity": original, "original_position_qty": original,
            "remaining_quantity": qty, "placed_slice_total": round(placed_slice, 10),
            "ladder_booked_events": [],
        },
    )


# AC-1 — partial attach: only 2 of 4 tiers were ever placed; accounting derives
# from the placed legs, NOT the 4-tier plan. Both fill; INV-1/INV-4 hold.
@pytest.mark.asyncio
async def test_AC1_partial_attach_accounting_from_placed_not_plan():
    runner = _runner()
    trade = _trade(qty=0.6, original_qty=1.0,
                   tiers=[_leg(1, "tp1", 1003.0, 0.2), _leg(2, "tp2", 1006.0, 0.2)])
    # only tp1/tp2 ever existed; both now filled, position dropped 1.0 -> 0.6
    adapter = ReconcileFake(
        positions=[Position("BTCUSDT", "long", 0.6, 1000.0, mark_price=1006.5)],
        open_algo_ids={"run1", "sl1"},
        order_status={"tp1": {"status": "filled", "avg": 1003.0},
                      "tp2": {"status": "filled", "avg": 1006.0}},
    )
    await runner._sync_ladder_trades(_db(), adapter, [trade])
    assert trade.extra["tiers_filled"] == 2
    assert "ladder_sync_anomaly" not in _events(runner)  # accounted 0.4 == observed 0.4


# AC-2 — fix-A FILLED_AT_ATTACH slice is in placed_set, counted in accounting,
# and does NOT trip the residual tripwire (regression guard for "B masked by A").
@pytest.mark.asyncio
async def test_AC2_filled_at_attach_counted_no_false_positive():
    runner = _runner()
    trade = _trade(qty=0.8, original_qty=1.0, tiers=[
        _leg(1, None, 1003.0, 0.2, status="filled_at_attach", filled_qty=0.2),
        _leg(2, "tp2", 1006.0, 0.2), _leg(3, "tp3", 1010.0, 0.2),
    ])
    adapter = ReconcileFake(
        positions=[Position("BTCUSDT", "long", 0.8, 1000.0, mark_price=1004.0)],
        open_algo_ids={"tp2", "tp3", "run1", "sl1"},
    )
    await runner._sync_ladder_trades(_db(), adapter, [trade])
    # observed_decrease 0.2 == accounted (the filled_at_attach slice) -> no anomaly
    assert "ladder_sync_anomaly" not in _events(runner)


# AC-3 — stop sweep at close: resting tiers come back CANCELED (not fills),
# zero phantom PnL, slice attributed to the stop fill; no anomaly.
@pytest.mark.asyncio
async def test_AC3_stop_sweep_cancels_tiers_no_phantom_fill():
    runner = _runner()
    trade = _trade(qty=1.0, tiers=[_leg(1, "tp1", 1003.0, 0.2), _leg(2, "tp2", 1006.0, 0.2)])
    adapter = ReconcileFake(
        positions=[],  # flat
        order_status={"tp1": {"status": "canceled"}, "tp2": {"status": "canceled"},
                      "run1": {"status": "canceled"},
                      "sl1": {"status": "filled", "avg": 994.0}},
    )
    await runner._sync_ladder_trades(_db(), adapter, [trade])
    assert trade.status == "closed"
    assert "ladder_tier_filled" not in _events(runner)   # no fills booked from cancels
    assert "ladder_sync_anomaly" not in _events(runner)  # ledger balances via the stop
    closed = _payload(runner, "ladder_trade_closed")
    assert closed["ledger_balanced"] is True
    assert closed["reason"] == "stop_loss_exchange"
    assert closed["closer_qty"] == pytest.approx(1.0)


# AC-4 — full out-of-band manual close: all legs canceled, no stop/runner fill;
# the remainder is an explained external close; no anomaly.
@pytest.mark.asyncio
async def test_AC4_external_close_no_anomaly():
    runner = _runner()
    trade = _trade(qty=1.0, tiers=[_leg(1, "tp1", 1003.0, 0.2), _leg(2, "tp2", 1006.0, 0.2)])
    adapter = ReconcileFake(
        positions=[],
        order_status={"tp1": {"status": "canceled"}, "tp2": {"status": "canceled"},
                      "run1": {"status": "canceled"}, "sl1": {"status": "canceled"}},
    )
    await runner._sync_ladder_trades(_db(), adapter, [trade])
    assert trade.status == "closed"
    assert "ladder_sync_anomaly" not in _events(runner)
    closed = _payload(runner, "ladder_trade_closed")
    assert closed["reason"] == "external_close"
    assert closed["ledger_balanced"] is True


# AC-5 — single leg partial-then-cancel: fills 0.4 of 0.5, stop sweeps 0.1.
@pytest.mark.asyncio
async def test_AC5_partial_then_cancel_balances():
    runner = _runner()
    trade = _trade(qty=0.5, tiers=[_leg(1, "tp1", 1003.0, 0.5)], runner_id=None, runner_qty=0.0)
    adapter = ReconcileFake(
        positions=[],
        order_status={"tp1": {"status": "partially_filled", "avg": 1003.0, "filled_qty": 0.4},
                      "sl1": {"status": "filled", "avg": 994.0}},
    )
    await runner._sync_ladder_trades(_db(), adapter, [trade])
    leg = trade.extra["tier_orders"][0]
    assert leg["filled_qty"] == pytest.approx(0.4)        # FILLED portion
    assert trade.quantity == pytest.approx(0.1)           # stop swept remainder
    closed = _payload(runner, "ladder_trade_closed")
    assert closed["slice_filled_qty"] == pytest.approx(0.4)
    assert closed["closer_qty"] == pytest.approx(0.1)
    assert closed["ledger_balanced"] is True              # 0.4 + 0.1 == 0.5
    assert "ladder_sync_anomaly" not in _events(runner)


# AC-6 — stop progression churn: old stop algo_ids are cancel+replaced and are
# never counted as fills; reconciler tracks the current stop; no anomaly.
@pytest.mark.asyncio
async def test_AC6_stop_progression_churn_ignored_in_accounting():
    runner = _runner()
    trade = _trade(qty=0.6, original_qty=1.0,
                   tiers=[_leg(1, "tp1", 1003.0, 0.2), _leg(2, "tp2", 1006.0, 0.2)])
    # two tiers filled, mark high -> stop ratchets (cancel old, place new)
    adapter = ReconcileFake(
        positions=[Position("BTCUSDT", "long", 0.6, 1000.0, mark_price=1011.0)],
        open_algo_ids={"run1", "sl1"},
        order_status={"tp1": {"status": "filled", "avg": 1003.0},
                      "tp2": {"status": "filled", "avg": 1006.0}},
    )
    await runner._sync_ladder_trades(_db(), adapter, [trade])
    assert "ladder_stop_advanced" in _events(runner)
    assert trade.extra["stop_order_id"] != "sl1"          # tracks the new stop id
    assert ("BTCUSDT", "sl1") in adapter.cancelled         # old stop cancelled
    # realized PnL is exactly the two tier fills — no phantom from the stop cancel
    assert "ladder_sync_anomaly" not in _events(runner)


# AC-7 — dedup: the same terminal fill seen across two polls books PnL once.
@pytest.mark.asyncio
async def test_AC7_dedup_books_fill_once():
    runner = _runner()
    trade = _trade(qty=0.8, original_qty=1.0,
                   tiers=[_leg(1, "tp1", 1003.0, 0.2), _leg(2, "tp2", 1006.0, 0.2)])
    adapter = ReconcileFake(
        positions=[Position("BTCUSDT", "long", 0.8, 1000.0, mark_price=1004.0)],
        open_algo_ids={"tp2", "run1", "sl1"},
        order_status={"tp1": {"status": "filled", "avg": 1003.0}},
    )
    await runner._sync_ladder_trades(_db(), adapter, [trade])
    pnl_after_first = trade.realized_pnl
    fills_first = _events(runner).count("ladder_tier_filled")
    # second identical poll (tp1 still reports filled) -> no double-book
    await runner._sync_ladder_trades(_db(), adapter, [trade])
    assert trade.realized_pnl == pytest.approx(pnl_after_first)
    assert _events(runner).count("ladder_tier_filled") == fills_first
    # direct dedup-key guard
    leg = trade.extra["tier_orders"][0]
    assert runner._ladder_book_fill(trade, trade.extra, leg,
                                    fill_price=1003.0, filled_qty=0.2, status="filled") is None


# AC-8 — NEGATIVE TEST: inject an unexplained residual (qty left the position
# with no FILLED/PARTIAL status or closer) -> the tripwire MUST fire.
@pytest.mark.asyncio
async def test_AC8_unexplained_residual_fires_anomaly():
    runner = _runner()
    trade = _trade(qty=0.8, original_qty=1.0,
                   tiers=[_leg(1, "tp1", 1003.0, 0.2), _leg(2, "tp2", 1006.0, 0.2)])
    # tp1 LEFT the resting set but came back CANCELED (no fill); yet the position
    # dropped 1.0 -> 0.8. 0.2 left with nothing to explain it.
    adapter = ReconcileFake(
        positions=[Position("BTCUSDT", "long", 0.8, 1000.0, mark_price=1004.0)],
        open_algo_ids={"tp2", "run1", "sl1"},
        order_status={"tp1": {"status": "canceled"}},
    )
    await runner._sync_ladder_trades(_db(), adapter, [trade])
    assert "ladder_tier_filled" not in _events(runner)   # cancel is not a fill (INV-2)
    assert "ladder_sync_anomaly" in _events(runner)
    payload = _payload(runner, "ladder_sync_anomaly")
    assert payload["unexplained_residual_qty"] == pytest.approx(0.2)
    assert payload["accounted_filled_qty"] == pytest.approx(0.0)
    assert payload["drift_pct"] == pytest.approx(20.0)


# ----- Item 2 <-> 1/3/4 integration: concurrent attach feeds the pipeline -----

from app.execution.order_manager import OrderManager  # noqa: E402
from app.execution.exit_ladder import classify_attach  # noqa: E402
from app.strategies.base_strategy import StrategyContext, StrategySignal  # noqa: E402


class AttachCloseFake(ExchangeAdapter):
    """Drives a concurrent attach (per-price -2021 / hard-error injection) then a
    status-driven close, so item 2's gather output flows through items 1/3/4
    end-to-end. Order ids encode their kind so the close reports terminal status
    without the test capturing ids."""
    name = "fake"

    def __init__(self, *, raise_2021_on_prices=None, hard_fail_on_prices=None):
        self.raise_2021_on_prices = set(raise_2021_on_prices or [])
        self.hard_fail_on_prices = set(hard_fail_on_prices or [])
        self._positions = [Position("BTCUSDT", "long", 1.0, 1000.0, mark_price=1004.0)]
        self.placed = []
        self.cancelled = []
        self._n = 0

    async def fetch_balances(self): return []
    async def fetch_tickers(self): return []
    async def fetch_order_book(self, symbol, limit=50):
        return OrderBook(symbol, bids=[(999.9, 1)], asks=[(1000.1, 1)])
    async def fetch_ohlcv(self, symbol, timeframe, limit=200): return []
    async def set_leverage(self, symbol, leverage): return True
    async def close_position(self, symbol): return OrderResult("0", symbol, "filled", "sell", "market", 0)
    async def fetch_symbol_rules(self, symbol):
        return {"tick_size": 0.01, "step_size": 0.0001, "min_qty": 0.0001, "min_notional": 5.0}

    async def fetch_positions(self):
        return list(self._positions)

    async def fetch_open_algo_orders(self, symbol=None):
        return []  # at close, all resting tiers have left the book

    async def fetch_algo_order(self, symbol, order_id):
        # Encoded by kind: stop -> filled (the closer); tp/run -> canceled (swept).
        if str(order_id).startswith("stop"):
            return OrderResult(order_id, symbol, "filled", "sell", "stop_market", 1.0,
                               filled_quantity=1.0, average_price=994.0)
        return OrderResult(order_id, symbol, "canceled", "sell", "take_profit_market", 0.2)

    async def place_algo_order(self, request):
        self.placed.append(request)
        if request.order_type == "stop_market":
            return OrderResult("stop1", request.symbol, "new", request.side, request.order_type, 0)
        if request.order_type == "trailing_stop_market":
            return OrderResult("run1", request.symbol, "new", request.side, request.order_type, request.quantity)
        if request.stop_price in self.raise_2021_on_prices:
            raise RuntimeError('Binance: {"code":-2021,"msg":"Order would immediately trigger."}')
        if request.stop_price in self.hard_fail_on_prices:
            raise RuntimeError('Binance: {"code":-4131,"msg":"rate limited"}')
        self._n += 1
        return OrderResult(f"tp-{self._n}", request.symbol, "new", request.side, request.order_type, request.quantity)

    async def place_order(self, request):  # market closes (fix-A slices, flatten)
        self._n += 1
        self.placed.append(request)
        return OrderResult(f"mkt-{self._n}", request.symbol, "new", request.side, request.order_type, request.quantity)

    async def cancel_order(self, symbol, order_id):
        self.cancelled.append((symbol, order_id))
        return OrderResult(order_id, symbol, "canceled", "sell", "stop_market", 0)

    async def cancel_algo_order(self, symbol, order_id):
        return await self.cancel_order(symbol, order_id)

    async def fetch_open_orders(self, symbol=None): return []


def _scored():
    signal = StrategySignal(setup_name="test", symbol="BTCUSDT", direction="long",
                            entry_price=1000.0, stop_loss=995.0,
                            take_profit_levels=[1001.5, 1003.0, 1005.0, 1008.0],
                            trailing_stop=0.0, expected_move=8.0, risk_reward_ratio=2.0,
                            confidence_score=80.0, accepted=True)
    context = StrategyContext(symbol="BTCUSDT", candles_by_timeframe={}, session_name="london",
                              regime="trend", coin_strength_score=70.0)
    return MagicMock(signal=signal, context=context, signal_id="s1")


def _open_trade():
    return Trade(id="t1", symbol="BTCUSDT", side="long", exchange="binance", mode="testnet",
                 setup_name="test", entry_price=1000.0, stop_loss=995.0,
                 take_profit={"levels": [1001.5]}, quantity=1.0, status="open", realized_pnl=0.0,
                 opened_at=datetime.now(timezone.utc), extra={})


async def _attach_then_close(runner, adapter):
    scored = _scored()
    trade = _open_trade()
    manager = OrderManager(adapter, settings=runner.settings)
    await runner._attach_ladder_exits(_db(), manager, scored.signal, trade, scored)
    tiers = trade.extra.get("tier_orders", [])
    rested = sum(1 for t in tiers if t.get("status") == "resting")
    immediate = sum(1 for t in tiers if t.get("status") == "filled_at_attach")
    bucket = classify_attach(stop_placed=bool(trade.extra.get("stop_order_id")),
                             planned_tier_count=4, rested_tier_count=rested,
                             immediate_fill_count=immediate, planned_runner=True,
                             runner_placed=bool(trade.extra.get("runner_order_id")))
    adapter._positions = []
    await runner._sync_ladder_trades(_db(), adapter, [trade])
    return trade, bucket


# IT2-1a — AC2-4 path (2 tiers -2021->market) closes balanced; classifier FULL.
@pytest.mark.asyncio
async def test_IT2_1a_2021_subset_balances_and_full():
    runner = _runner()
    adapter = AttachCloseFake(raise_2021_on_prices={1001.5, 1003.0})
    trade, bucket = await _attach_then_close(runner, adapter)
    assert bucket == "full"
    assert trade.status == "closed"
    closed = _payload(runner, "ladder_trade_closed")
    assert closed["ledger_balanced"] is True
    assert "ladder_sync_anomaly" not in _events(runner)


# IT2-1b — AC2-5 path (1 hard-error drop) closes balanced; classifier PARTIAL.
@pytest.mark.asyncio
async def test_IT2_1b_hard_drop_balances_and_partial():
    runner = _runner()
    adapter = AttachCloseFake(hard_fail_on_prices={1001.5})
    trade, bucket = await _attach_then_close(runner, adapter)
    assert bucket == "partial"
    assert trade.status == "closed"
    closed = _payload(runner, "ladder_trade_closed")
    assert closed["ledger_balanced"] is True


# IT2-2 — a tier that raised in the gather (never placed) is never read as a fill.
@pytest.mark.asyncio
async def test_IT2_2_dropped_tier_never_phantom_fill():
    runner = _runner()
    adapter = AttachCloseFake(hard_fail_on_prices={1001.5})  # tier1 (1001.5) dropped
    trade, _ = await _attach_then_close(runner, adapter)
    indices = [t["index"] for t in trade.extra.get("tier_orders", [])]
    assert 1 not in indices  # dropped tier has no leg in the placed_set
    fills = [c.args[4] for c in runner._risk_event.await_args_list if c.args[2] == "ladder_tier_filled"]
    assert all(f.get("tier") != 1 for f in fills)
