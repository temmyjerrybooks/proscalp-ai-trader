"""Phase 2B ladder-fix item 3 — reconciliation on tiers-ACTUALLY-PLACED.

Acceptance criteria AC-1..AC-8 from the item-3 spec. Every test drives the
real BotRunner reconciler through a status-driven mock adapter (no live exchange).

INV-1  ledger balances at close (Σ dispositions == original position qty)
INV-2  fills come ONLY from terminal status, never from order absence
INV-3  expected-total derives from placed_set, never the ladder plan
INV-4  anomaly == unexplained residual / original_position_qty
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config.settings import Settings, TradingMode
from app.database.models import Trade
from app.exchanges.base import ExchangeAdapter, OrderBook, OrderResult, Position
from app.exchanges.binance_adapter import BinanceAdapter
from app.execution.exit_ladder import FILL_STATUSES, classify_leg_status
from app.services.bot_runner import BotRunner
from unittest.mock import AsyncMock, MagicMock


# --------------------------------------------------------------------------- fake

class ReconcileFake(ExchangeAdapter):
    """Status-driven mock: each algo order id maps to (status, avg_price, filled_qty).
    fetch_algo_order reports the configured terminal status — the reconciler must
    branch on that, never on the order merely being absent from the open set."""
    name = "fake"

    def __init__(self, *, positions=None, open_algo_ids=None, order_status=None, user_trades=None):
        self._positions = positions or []
        self._open_algo_ids = set(open_algo_ids) if open_algo_ids is not None else set()
        # order_id -> dict(status=..., avg=..., filled_qty=...)
        self._order_status = order_status or {}
        # Phase-2C authoritative ledger. None -> source unavailable (degraded path).
        self._user_trades = user_trades
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

    async def fetch_user_trades(self, symbol, start_ms=None, limit=200):
        if self._user_trades is None:
            raise NotImplementedError("user_trades not configured")
        return [dict(r) for r in self._user_trades]

    async def fetch_algo_order(self, symbol, order_id):
        # Returns the REAL live algo-order shape and routes it through the actual
        # adapter normalizer (BinanceAdapter._algo_result) so these tests exercise
        # the production mapping — NOT a hand-built "filled" OrderResult.
        # Verified-live shape for a triggered tier: algoStatus=FINISHED, the algo's
        # OWN executedQty/avgPrice null, and the real fill in actualQty/actualPrice.
        spec = self._order_status.get(order_id, {"status": "NEW"})
        raw = {
            "algoId": order_id, "symbol": symbol, "side": "SELL",
            "orderType": "TAKE_PROFIT_MARKET", "algoStatus": spec["status"],
            "quantity": spec.get("qty", 0.2),
            "executedQty": spec.get("executed_qty"),   # null on a FINISHED algo order
            "avgPrice": spec.get("avg_price"),          # null on a FINISHED algo order
            "actualQty": spec.get("actual_qty"),        # real child-order fill qty
            "actualPrice": spec.get("actual_price"),    # real child-order fill price
            "actualOrderId": spec.get("actual_order_id"),
            "actualType": "MARKET" if spec.get("actual_qty") else None,
        }
        return BinanceAdapter._algo_result(raw, symbol=symbol)

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


def _ut(side, price, qty, realized=0.0, *, comm=0.0, oid="o", t=0):
    """One userTrades fill (Binance USDⓈ-M shape). LONG: entry=BUY, exits=SELL;
    SHORT: entry=SELL, exits=BUY. realized==0 marks the opening fill."""
    return {"side": side, "price": str(price), "qty": str(qty),
            "realizedPnl": str(realized), "commission": str(comm),
            "commissionAsset": "USDT", "orderId": oid, "time": t}


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


# AC-1 — two tiers exited; userTrades books the realized net and tiers_filled
# advances; conservation base == exit + position holds (no anomaly).
@pytest.mark.asyncio
async def test_AC1_two_tiers_booked_from_usertrades():
    runner = _runner()
    trade = _trade(qty=0.6, original_qty=1.0,
                   tiers=[_leg(1, "tp1", 1003.0, 0.2), _leg(2, "tp2", 1006.0, 0.2)])
    adapter = ReconcileFake(
        positions=[Position("BTCUSDT", "long", 0.6, 1000.0, mark_price=1006.5)],
        open_algo_ids={"run1", "sl1"},
        user_trades=[_ut("BUY", 1000.0, 1.0, 0.0, oid="e"),
                     _ut("SELL", 1003.0, 0.2, 0.6, oid="x1"),
                     _ut("SELL", 1006.0, 0.2, 1.2, oid="x2")],
    )
    await runner._sync_ladder_trades(_db(), adapter, [trade])
    assert trade.extra["tiers_filled"] == 2              # 0.4 exited / 0.2 per tier
    assert trade.realized_pnl == pytest.approx(1.8)      # Σ realizedPnl − Σ commission(0)
    assert "ladder_sync_anomaly" not in _events(runner)  # base1.0 == exit0.4 + pos0.6


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


# AC-3 — stop-out at close: the whole position closes via the marching stop. The
# stop fill is the single reducing fill in userTrades; net is authoritative; the
# close reason is labelled from the stop's exchange status; ledger balances.
@pytest.mark.asyncio
async def test_AC3_stop_out_authoritative_net_and_reason():
    runner = _runner()
    trade = _trade(qty=1.0, tiers=[_leg(1, "tp1", 1003.0, 0.2), _leg(2, "tp2", 1006.0, 0.2)])
    adapter = ReconcileFake(
        positions=[],  # flat
        order_status={"sl1": {"status": "FINISHED", "actual_price": 994.0, "actual_qty": 1.0}},
        user_trades=[_ut("BUY", 1000.0, 1.0, 0.0, oid="e"),
                     _ut("SELL", 994.0, 1.0, -6.0, oid="stop")],
    )
    await runner._sync_ladder_trades(_db(), adapter, [trade])
    assert trade.status == "closed"
    assert trade.realized_pnl == pytest.approx(-6.0)     # authoritative stop-out net
    assert "ladder_sync_anomaly" not in _events(runner)  # base1.0 == exit1.0
    closed = _payload(runner, "ladder_trade_closed")
    assert closed["ledger_balanced"] is True
    assert closed["reason"] == "stop_loss_exchange"      # labelled from stop status


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


# AC-5 — tier partial-fill (0.4) then stop sweeps the remainder (0.1). userTrades
# carries both reducing fills; the net + full-ledger balance are exact.
@pytest.mark.asyncio
async def test_AC5_partial_then_stop_balances():
    runner = _runner()
    trade = _trade(qty=0.5, tiers=[_leg(1, "tp1", 1003.0, 0.5)], runner_id=None, runner_qty=0.0)
    adapter = ReconcileFake(
        positions=[],
        order_status={"sl1": {"status": "FINISHED", "actual_price": 994.0, "actual_qty": 0.1}},
        user_trades=[_ut("BUY", 1000.0, 0.5, 0.0, oid="e"),
                     _ut("SELL", 1003.0, 0.4, 1.2, oid="x1"),
                     _ut("SELL", 994.0, 0.1, -0.6, oid="stop")],
    )
    await runner._sync_ladder_trades(_db(), adapter, [trade])
    assert trade.realized_pnl == pytest.approx(0.6)       # 1.2 − 0.6
    closed = _payload(runner, "ladder_trade_closed")
    assert closed["slice_filled_qty"] == pytest.approx(0.5)   # all reducing fills
    assert closed["ledger_balanced"] is True                  # base 0.5 == exit 0.5
    assert "ladder_sync_anomaly" not in _events(runner)


# AC-7 — dedup: re-polling the same fill never double-books (recompute + orderId
# dedup + fetch-skip on no position change).
@pytest.mark.asyncio
async def test_AC7_dedup_books_fill_once():
    runner = _runner()
    trade = _trade(qty=0.8, original_qty=1.0,
                   tiers=[_leg(1, "tp1", 1003.0, 0.2), _leg(2, "tp2", 1006.0, 0.2)])
    adapter = ReconcileFake(
        positions=[Position("BTCUSDT", "long", 0.8, 1000.0, mark_price=1004.0)],
        open_algo_ids={"tp2", "run1", "sl1"},
        user_trades=[_ut("BUY", 1000.0, 1.0, 0.0, oid="e"),
                     _ut("SELL", 1003.0, 0.2, 0.6, oid="x1")],
    )
    await runner._sync_ladder_trades(_db(), adapter, [trade])
    pnl_after_first = trade.realized_pnl
    fills_first = _events(runner).count("ladder_tier_filled")
    await runner._sync_ladder_trades(_db(), adapter, [trade])   # identical second poll
    assert trade.realized_pnl == pytest.approx(pnl_after_first)
    assert _events(runner).count("ladder_tier_filled") == fills_first  # not re-emitted


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
        order_status={"tp1": {"status": "FINISHED", "actual_price": 1003.0, "actual_qty": 0.2},
                      "tp2": {"status": "FINISHED", "actual_price": 1006.0, "actual_qty": 0.2}},
    )
    await runner._sync_ladder_trades(_db(), adapter, [trade])
    assert "ladder_stop_advanced" in _events(runner)
    assert trade.extra["stop_order_id"] != "sl1"          # tracks the new stop id
    assert ("BTCUSDT", "sl1") in adapter.cancelled         # old stop cancelled
    # realized PnL is exactly the two tier fills — no phantom from the stop cancel
    assert "ladder_sync_anomaly" not in _events(runner)


# AC-8 — NEGATIVE TEST: the position dropped (1.0 -> 0.8) but userTrades shows NO
# reducing fill to explain it. A persistent SHORTFALL past the re-check budget MUST
# fire the conservation tripwire.
@pytest.mark.asyncio
async def test_AC8_conservation_shortfall_fires_after_budget():
    runner = _runner()
    trade = _trade(qty=0.8, original_qty=1.0,
                   tiers=[_leg(1, "tp1", 1003.0, 0.2), _leg(2, "tp2", 1006.0, 0.2)])
    adapter = ReconcileFake(
        positions=[Position("BTCUSDT", "long", 0.8, 1000.0, mark_price=1004.0)],
        open_algo_ids={"tp2", "run1", "sl1"},
        user_trades=[_ut("BUY", 1000.0, 1.0, 0.0, oid="e")],  # entry only — no exit fill
    )
    budget = runner.settings.ladder_falsefill_recheck_budget
    for _ in range(budget):
        await runner._sync_ladder_trades(_db(), adapter, [trade])
    assert "ladder_tier_filled" not in _events(runner)   # nothing booked
    assert "ladder_sync_anomaly" in _events(runner)
    payload = _payload(runner, "ladder_sync_anomaly")
    assert payload["kind"] == "shortfall"
    assert payload["residual_qty"] == pytest.approx(0.2)         # base − pos − exit
    assert payload["assertion"] == "usertrades_conservation"


# CONSERVATION OVERCOUNT — more reducing qty than the position shed is impossible
# from one position; it fires immediately (no grace).
@pytest.mark.asyncio
async def test_conservation_overcount_fires_immediately():
    runner = _runner()
    trade = _trade(qty=0.8, original_qty=1.0,
                   tiers=[_leg(1, "tp1", 1003.0, 0.2), _leg(2, "tp2", 1006.0, 0.2)])
    # base 1.0, position 0.8 -> only 0.2 could have exited, but userTrades shows 0.4.
    adapter = ReconcileFake(
        positions=[Position("BTCUSDT", "long", 0.8, 1000.0, mark_price=1004.0)],
        open_algo_ids={"tp2", "run1", "sl1"},
        user_trades=[_ut("BUY", 1000.0, 1.0, 0.0, oid="e"),
                     _ut("SELL", 1003.0, 0.2, 0.6, oid="x1"),
                     _ut("SELL", 1006.0, 0.2, 1.2, oid="x2")],
    )
    await runner._sync_ladder_trades(_db(), adapter, [trade])
    payload = _payload(runner, "ladder_sync_anomaly")
    assert payload["kind"] == "overcount"
    assert payload["residual_qty"] == pytest.approx(-0.2)        # base − pos − exit < 0


# ===== FINISHED-status regression tests (the live bug found at arming) =====

# Adapter unit test: _algo_result must source fill price/qty from actual* when the
# algo object's own executedQty/avgPrice are null (the live FINISHED shape).
def test_algo_result_sources_actual_fields_on_finished():
    finished_raw = {
        "algoId": "1000000093736374", "symbol": "TRXUSDT", "side": "SELL",
        "orderType": "TAKE_PROFIT_MARKET", "algoStatus": "FINISHED",
        "quantity": 285, "executedQty": None, "avgPrice": None,
        "actualType": "MARKET", "actualOrderId": 740725309,
        "actualPrice": 0.34260, "actualQty": 285,
    }
    r = BinanceAdapter._algo_result(finished_raw, symbol="TRXUSDT")
    assert classify_leg_status(r.status) in FILL_STATUSES   # finished -> fill
    assert r.filled_quantity == pytest.approx(285)          # from actualQty, not 0
    assert r.average_price == pytest.approx(0.34260)        # from actualPrice, not None
    # a NEW (resting) order has no actual* -> not a fill, zero qty
    new_raw = {"algoId": "x", "symbol": "TRXUSDT", "algoStatus": "NEW", "quantity": 285}
    rn = BinanceAdapter._algo_result(new_raw, symbol="TRXUSDT")
    assert classify_leg_status(rn.status) == "resting"
    assert rn.filled_quantity == 0 and rn.average_price is None


# THE CANONICAL REGRESSION: a tier triggered+FILLED but its algo order is purged /
# unreadable (-2013 "Order does not exist" on the per-order GET). The OLD path
# swallowed that as "resting" -> permanent under-attribution -> false sync_anomaly.
# userTrades carries the real fill, so it books and conservation holds.
@pytest.mark.asyncio
async def test_purged_tier_reconciles_from_usertrades_not_status():
    runner = _runner()
    trade = _trade(qty=0.8, original_qty=1.0,
                   tiers=[_leg(1, "tp1", 1003.0, 0.2), _leg(2, "tp2", 1006.0, 0.2)])
    # The algo status surface is useless for tp1 (purged / -2013-class); the booking
    # path no longer reads it. userTrades has the real fill.
    adapter = ReconcileFake(
        positions=[Position("BTCUSDT", "long", 0.8, 1000.0, mark_price=1004.0)],
        open_algo_ids={"tp2", "run1", "sl1"},
        order_status={"tp1": {"status": "EXPIRED"}},
        user_trades=[_ut("BUY", 1000.0, 1.0, 0.0, oid="e"),
                     _ut("SELL", 1003.5, 0.2, 0.7, oid="x1")],
    )
    await runner._sync_ladder_trades(_db(), adapter, [trade])
    assert trade.realized_pnl == pytest.approx(0.7)       # booked from userTrades
    assert trade.extra["tiers_filled"] == 1              # 0.2 exited / 0.2
    assert "ladder_tier_filled" in _events(runner)
    assert "ladder_sync_anomaly" not in _events(runner)   # base1.0 == exit0.2 + pos0.8


# Part 3: a runner that FINISHED closes the position -> attribute as the runner,
# NOT external_close (the same enum gap mislabelled BTC/DOGE as external_close).
@pytest.mark.asyncio
async def test_finished_runner_attributes_as_runner_close():
    runner = _runner()
    trade = _trade(qty=0.2, original_qty=0.2,
                   tiers=[_leg(1, "tp1", 1003.0, 0.2)], runner_id="run1", runner_qty=0.2)
    adapter = ReconcileFake(
        positions=[],  # flat: runner closed it
        order_status={"tp1": {"status": "canceled"},
                      "sl1": {"status": "expired"},
                      "run1": {"status": "FINISHED", "actual_price": 1020.0, "actual_qty": 0.2}},
    )
    await runner._sync_ladder_trades(_db(), adapter, [trade])
    closed = _payload(runner, "ladder_trade_closed")
    assert closed["reason"] == "trailing_runner_exchange"  # NOT external_close
    assert closed["ledger_balanced"] is True
    assert "ladder_sync_anomaly" not in _events(runner)


# ===== TASK 1 negative-direction cases =====

# SHORTFALL HEALS — the structural difference from the -2013 bug. The position
# dropped before userTrades surfaced the fill (lag): the conservation shortfall is
# GRACED (no anomaly), and once the fill posts it reconciles cleanly. The old
# -2013-as-resting NEVER healed; this self-heals.
@pytest.mark.asyncio
async def test_conservation_shortfall_heals_when_usertrades_catches_up():
    runner = _runner()
    trade = _trade(qty=0.8, original_qty=1.0,
                   tiers=[_leg(1, "tp1", 1003.0, 0.2), _leg(2, "tp2", 1006.0, 0.2)])
    # cycle 1: position already 0.8 but the fill hasn't posted to userTrades yet.
    adapter = ReconcileFake(
        positions=[Position("BTCUSDT", "long", 0.8, 1000.0, mark_price=1004.0)],
        open_algo_ids={"tp2", "run1", "sl1"},
        user_trades=[_ut("BUY", 1000.0, 1.0, 0.0, oid="e")],
    )
    await runner._sync_ladder_trades(_db(), adapter, [trade])
    assert "ladder_sync_anomaly" not in _events(runner)   # lag graced, not alarmed
    # cycle 2: the fill posts -> conservation resolves; net booked; still no anomaly.
    adapter._user_trades = [_ut("BUY", 1000.0, 1.0, 0.0, oid="e"),
                            _ut("SELL", 1003.0, 0.2, 0.6, oid="x1")]
    await runner._sync_ladder_trades(_db(), adapter, [trade])
    assert trade.realized_pnl == pytest.approx(0.6)
    assert "ladder_sync_anomaly" not in _events(runner)


# PARTIAL FILL — a tier that fills only 0.1 is booked at exactly 0.1 from
# userTrades; the rest is later swept by the closer. Full ledger balances.
@pytest.mark.asyncio
async def test_partial_fill_booked_exactly_then_closer_sweeps():
    runner = _runner()
    trade = _trade(qty=0.9, original_qty=1.0,
                   tiers=[_leg(1, "tp1", 1003.0, 0.2), _leg(2, "tp2", 1006.0, 0.2)])
    adapter = ReconcileFake(
        positions=[Position("BTCUSDT", "long", 0.9, 1000.0, mark_price=1004.0)],
        open_algo_ids={"tp1", "tp2", "run1", "sl1"},
        user_trades=[_ut("BUY", 1000.0, 1.0, 0.0, oid="e"),
                     _ut("SELL", 1003.0, 0.1, 0.3, oid="x1")],
    )
    await runner._sync_ladder_trades(_db(), adapter, [trade])
    assert trade.realized_pnl == pytest.approx(0.3)
    assert "ladder_sync_anomaly" not in _events(runner)   # 0.1 booked == 0.1 shed
    # close: remaining 0.9 swept by the closer; full ledger from userTrades.
    adapter._positions = []
    adapter._user_trades = adapter._user_trades + [_ut("SELL", 994.0, 0.9, -5.4, oid="stop")]
    await runner._sync_ladder_trades(_db(), adapter, [trade])
    closed = _payload(runner, "ladder_trade_closed")
    assert closed["ledger_balanced"] is True              # base1.0 == exit1.0
    assert trade.realized_pnl == pytest.approx(0.3 - 5.4)


def _short_ladder_trade(symbol, entry, qty, tier_prices, tier_qty, runner_qty):
    tiers = [_leg(i + 1, f"tp{i+1}", p, tier_qty) for i, p in enumerate(tier_prices)]
    return Trade(
        id=symbol.lower(), symbol=symbol, side="short", exchange="binance", mode="testnet",
        setup_name="t", entry_price=entry, stop_loss=entry * 1.01,
        take_profit={"levels": [tier_prices[0]]}, quantity=qty, status="open", realized_pnl=0.0,
        opened_at=datetime.now(timezone.utc),
        extra={"ladder_active": True, "ladder_mode": "full", "entry_atr": entry * 0.002,
               "stop_order_id": "sl1", "runner_order_id": "run1", "runner_quantity": runner_qty,
               "runner_status": "resting", "tier_orders": tiers, "tiers_filled": 0,
               "be_plus_armed": False, "runner_active": False, "time_partial_done": False,
               "original_position_qty": qty, "remaining_quantity": qty, "ladder_booked_events": []},
    )


# CANONICAL REGRESSION — the real production BTC trade (65364bf1) the OLD path booked
# as +0.0077 (1 of 6 fills) and false-halted on. userTrades reconciles the TRUE net
# +0.8477 from all 4 tiers + runner; balanced; no anomaly. SHORT -> entry=SELL, exits=BUY.
@pytest.mark.asyncio
async def test_canonical_btc_multitier_runner_authoritative_net():
    runner = _runner()
    trade = _short_ladder_trade("BTCUSDT", 61785.30, 0.0076,
                                [61647.7, 61594.2, 61499.5, 61404.8], 0.0015, 0.0016)
    ut = [
        _ut("SELL", 61785.30, 0.0076, 0.0, comm=0.18782731, oid="e"),
        _ut("BUY", 61733.90, 0.0015, 0.07710, comm=0.03704034, oid="x1"),
        _ut("BUY", 61741.50, 0.0015, 0.06570, comm=0.03704490, oid="x2"),
        _ut("BUY", 61742.20, 0.0015, 0.06465, comm=0.03704532, oid="x3"),
        _ut("BUY", 61669.40, 0.0015, 0.17385, comm=0.03700164, oid="x4"),
        _ut("BUY", 61259.30, 0.0016, 0.84160, comm=0.03920595, oid="run"),
    ]
    adapter = ReconcileFake(positions=[], user_trades=ut)
    await runner._sync_ladder_trades(_db(), adapter, [trade])
    assert trade.status == "closed"
    assert trade.realized_pnl == pytest.approx(0.8477, abs=1e-3)   # TRUE net, not +0.0077
    closed = _payload(runner, "ladder_trade_closed")
    assert closed["ledger_balanced"] is True                       # base 0.0076 == exit 0.0076
    assert "ladder_sync_anomaly" not in _events(runner)


# CANONICAL — the real AVAX trade where ONE tier order (242025812) filled in THREE
# userTrades rows (11+1+1). Accounting sums every row; the per-fill audit dedups by
# orderId. Net +3.1459, balanced, no anomaly.
@pytest.mark.asyncio
async def test_canonical_avax_one_order_three_partial_fills():
    runner = _runner()
    trade = _short_ladder_trade("AVAXUSDT", 7.1120, 66.0,
                                [7.090, 7.074, 7.050, 7.020], 13.0, 14.0)
    ut = [
        _ut("SELL", 7.1120, 66, 0.0, comm=0.18775680, oid="e"),
        _ut("BUY", 7.1020, 13, 0.13000, comm=0.03693040, oid="a1"),
        _ut("BUY", 7.0820, 13, 0.39000, comm=0.03682640, oid="a2"),
        _ut("BUY", 7.0670, 13, 0.58500, comm=0.03674840, oid="a3"),
        # one tier order, three partial fills (shared orderId):
        _ut("BUY", 7.0530, 11, 0.64900, comm=0.03103320, oid="a4"),
        _ut("BUY", 7.0620, 1, 0.05000, comm=0.00282480, oid="a4"),
        _ut("BUY", 7.0620, 1, 0.05000, comm=0.00282480, oid="a4"),
        _ut("BUY", 6.9930, 14, 1.66600, comm=0.03916080, oid="run"),
    ]
    adapter = ReconcileFake(positions=[], user_trades=ut)
    await runner._sync_ladder_trades(_db(), adapter, [trade])
    assert trade.status == "closed"
    assert trade.realized_pnl == pytest.approx(3.1459, abs=1e-3)
    closed = _payload(runner, "ladder_trade_closed")
    assert closed["ledger_balanced"] is True                       # base 66 == exit 66
    assert "ladder_sync_anomaly" not in _events(runner)


# TASK 3 — ONE rich exit summary: PnL (not entry score/grade/risk), gradient built
# only from BOOKED fills, single trade_closed alert (no duplicate closed-by pair).
@pytest.mark.asyncio
async def test_exit_summary_single_alert_gradient_from_booked_only():
    runner = _runner()
    trade = _trade(qty=0.4, original_qty=1.0, tiers=[
        _leg(1, "tp1", 1003.0, 0.2, status="filled", filled_qty=0.2),
        _leg(2, "tp2", 1006.0, 0.2),  # UNfilled -> must not appear in the gradient
    ])
    trade.extra.update({"grade": "A", "setup_score": 88, "risk_pct": 0.25, "be_plus_armed": True})
    trade.extra["tier_orders"][0]["fill_price"] = 1003.0
    trade.realized_pnl = 0.6
    trade.closed_at = trade.opened_at + timedelta(minutes=12)
    summary = runner._build_exit_summary(trade, "trailing_runner_exchange")
    assert "TP1" in summary and "1003.0" in summary                # booked tier appears
    assert "TP2" not in summary                                    # unfilled tier excluded
    assert "NET" in summary and "0.6" in summary                   # PnL present
    for forbidden in ("grade", "score", "risk"):
        assert forbidden not in summary                            # entry context dropped
    assert "BE+ Y" in summary and "hold 12m" in summary
    # single trade_closed alert via the real finalize path (no duplicate pair)
    runner.alerts.send.reset_mock()
    await runner._finalize_trade_close(_db(), trade, 1003.0, "trailing_runner_exchange")
    sent = [c.args[0] for c in runner.alerts.send.await_args_list]
    assert sent.count("trade_closed") == 1
    assert "stop_loss_hit" not in sent and "take_profit_hit" not in sent


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
    # New geometry [0.6,1.0,1.5,2.0]xATR(5.0): tier1=1003.0, tier2=1005.0.
    adapter = AttachCloseFake(raise_2021_on_prices={1003.0, 1005.0})
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
    adapter = AttachCloseFake(hard_fail_on_prices={1003.0})  # new tier1 = 0.6xATR(5.0)
    trade, bucket = await _attach_then_close(runner, adapter)
    assert bucket == "partial"
    assert trade.status == "closed"
    closed = _payload(runner, "ladder_trade_closed")
    assert closed["ledger_balanced"] is True


# IT2-2 — a tier that raised in the gather (never placed) is never read as a fill.
@pytest.mark.asyncio
async def test_IT2_2_dropped_tier_never_phantom_fill():
    runner = _runner()
    adapter = AttachCloseFake(hard_fail_on_prices={1003.0})  # tier1 (1003.0) dropped
    trade, _ = await _attach_then_close(runner, adapter)
    indices = [t["index"] for t in trade.extra.get("tier_orders", [])]
    assert 1 not in indices  # dropped tier has no leg in the placed_set
    fills = [c.args[4] for c in runner._risk_event.await_args_list if c.args[2] == "ladder_tier_filled"]
    assert all(f.get("tier") != 1 for f in fills)
