"""Phase 2B Branch 2: BotRunner ladder sync-path + entry-attach integration tests.

Covers partial-fill detection, stop progression, BE+ arming, runner activation,
time-based exits, full-close finalization (150-count audit), trade routing, the
ladder feature-flag gate, and min-notional graceful degradation at entry.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config.settings import Settings, TradingMode
from app.database.models import Trade
from app.exchanges.base import ExchangeAdapter, OrderBook, OrderResult, Position
from app.services.bot_runner import BotRunner
from app.strategies.base_strategy import StrategyContext, StrategySignal


# --------------------------------------------------------------------------- fakes

class FakeExchange(ExchangeAdapter):
    name = "fake"

    def __init__(self, *, positions=None, open_order_ids=None, order_fills=None, user_trades=None):
        self._positions = positions or []
        self._open_order_ids = set(open_order_ids) if open_order_ids is not None else set()
        self._order_fills = order_fills or {}  # order_id -> average_price
        # Phase-2C: authoritative fill ledger. None -> source unavailable (the
        # reconcile degrades, mirroring an adapter that can't report userTrades).
        self._user_trades = user_trades
        self.placed = []
        self.cancelled = []
        self._n = 0

    async def fetch_balances(self): return []
    async def fetch_tickers(self): return []
    async def fetch_order_book(self, symbol, limit=50):
        return OrderBook(symbol, bids=[(1010.9, 1)], asks=[(1011.1, 1)])
    async def fetch_ohlcv(self, symbol, timeframe, limit=200): return []
    async def set_leverage(self, symbol, leverage): return True
    async def close_position(self, symbol): return OrderResult("0", symbol, "filled", "sell", "market", 0)

    async def fetch_symbol_rules(self, symbol):
        return {"tick_size": 0.01, "step_size": 0.0001, "min_qty": 0.0001, "min_notional": 5.0}

    async def fetch_positions(self):
        return list(self._positions)

    async def fetch_open_orders(self, symbol=None):
        return [OrderResult(oid, symbol or "BTCUSDT", "new", "sell", "take_profit_market", 0)
                for oid in self._open_order_ids]

    async def fetch_order(self, symbol, order_id):
        avg = self._order_fills.get(order_id, 0.0)
        # A filled tier carries real actual* data: status filled + filled_quantity
        # (actualQty) + average_price (actualPrice). TASK 1 removed the leg-qty
        # fallback (actual-only), so the fixture must report a real filled_quantity.
        return OrderResult(order_id, symbol, "filled" if avg else "new", "sell",
                           "take_profit_market", 0.2,
                           filled_quantity=(0.2 if avg else 0.0), average_price=avg or None)

    async def fetch_user_trades(self, symbol, start_ms=None, limit=200):
        # Phase-2C authoritative source. Unconfigured -> NotImplementedError so the
        # reconcile returns None and the legacy/degraded path runs (keeps tests that
        # don't model fills unaffected).
        if self._user_trades is None:
            raise NotImplementedError("user_trades not configured")
        return [dict(r) for r in self._user_trades]

    # Ladder lifecycle runs on algo endpoints; delegate to the regular fakes.
    async def fetch_open_algo_orders(self, symbol=None):
        return await self.fetch_open_orders(symbol)

    async def fetch_algo_order(self, symbol, order_id):
        return await self.fetch_order(symbol, order_id)

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
    s = Settings(
        trading_mode=TradingMode.TESTNET, market_type="futures",
        exchange_resting_exits_enabled=True, five_tier_ladder_enabled=True,
        # Pin a fixed, floor-free geometry so these LOGIC/mechanics tests stay
        # decoupled from production threshold tuning. The explicit _ladder_trade
        # tier prices [1003,1006,1010,1016] == [0.3,0.6,1.0,1.6]xATR(10); the
        # production tuple [0.6,1.0,1.5,2.0] is validated in the geometry tests.
        tp_tier_atr_multipliers=[0.3, 0.6, 1.0, 1.6], tp_tier_min_distance_pct=0.0,
    )
    runner = BotRunner(settings=s)
    runner._risk_event = AsyncMock()
    runner.alerts.send = AsyncMock()
    return runner


def _ladder_trade(*, qty=0.6, tiers_filled_ids=(), opened_minutes_ago=1.0) -> Trade:
    tiers = [
        {"index": 1, "order_id": "tp1", "price": 1003.0, "quantity": 0.2, "filled": False},
        {"index": 2, "order_id": "tp2", "price": 1006.0, "quantity": 0.2, "filled": False},
        {"index": 3, "order_id": "tp3", "price": 1010.0, "quantity": 0.2, "filled": False},
        {"index": 4, "order_id": "tp4", "price": 1016.0, "quantity": 0.2, "filled": False},
    ]
    return Trade(
        id="t1", symbol="BTCUSDT", side="long", exchange="binance", mode="testnet",
        setup_name="test", entry_price=1000.0, stop_loss=995.0,
        take_profit={"levels": [1003.0, 1006.0, 1010.0, 1016.0]}, quantity=qty,
        status="open", realized_pnl=0.0,
        opened_at=datetime.now(timezone.utc) - timedelta(minutes=opened_minutes_ago),
        extra={
            "ladder_active": True, "ladder_mode": "full", "entry_atr": 10.0,
            "stop_order_id": "sl1", "runner_order_id": "run1",
            "tier_orders": tiers, "tiers_filled": 0, "be_plus_armed": False,
            "runner_active": False, "time_partial_done": False,
            "original_quantity": 1.0, "remaining_quantity": qty,
        },
    )


def _ut(side, price, qty, realized=0.0, *, comm=0.0, oid="o", t=0):
    """One userTrades fill row (Binance USDⓈ-M shape). For a LONG: entry=BUY,
    exits=SELL. realized==0 marks the opening fill."""
    return {"side": side, "price": str(price), "qty": str(qty),
            "realizedPnl": str(realized), "commission": str(comm),
            "commissionAsset": "USDT", "orderId": oid, "time": t}


def _event_types(runner) -> list[str]:
    return [c.args[2] for c in runner._risk_event.await_args_list]


def _db():
    db = MagicMock()
    db.commit = AsyncMock()
    return db


# --------------------------------------------------------------- tier-fill + stop march

@pytest.mark.asyncio
async def test_two_tiers_filled_books_pnl_and_advances_stop():
    runner = _runner()
    trade = _ladder_trade(qty=0.6)  # 0.4 worth of tiers exited
    # AUTHORITATIVE: userTrades shows entry 1.0 + two SELL exit fills (0.2 each).
    adapter = FakeExchange(
        positions=[Position("BTCUSDT", "long", 0.6, 1000.0, mark_price=1011.0)],
        open_order_ids={"tp3", "tp4", "run1", "sl1"},
        user_trades=[
            _ut("BUY", 1000.0, 1.0, 0.0, oid="entry"),
            _ut("SELL", 1003.0, 0.2, 0.6, oid="x1"),
            _ut("SELL", 1006.0, 0.2, 1.2, oid="x2"),
        ],
    )
    await runner._sync_ladder_trades(_db(), adapter, [trade])

    assert trade.extra["tiers_filled"] == 2          # 0.4 exited / 0.2 per tier
    assert trade.realized_pnl == pytest.approx(1.8)  # Σ realizedPnl − Σ commission(0)
    # BE+ armed (mark 1011 >= entry + 0.5*ATR=1005) and stop ratcheted to +0.5% (1005).
    assert trade.extra["be_plus_armed"] is True
    assert trade.stop_loss == 1005.0
    et = _event_types(runner)
    assert "ladder_tier_filled" in et                # per-fill audit from userTrades
    assert "ladder_be_plus_armed" in et
    assert "ladder_stop_advanced" in et
    # conservation: base 1.0 == exit 0.4 + position 0.6 -> no anomaly
    assert "ladder_sync_anomaly" not in et


@pytest.mark.asyncio
async def test_status_claims_fill_but_usertrades_empty_no_phantom_book():
    """Phase-2C: the booking path NEVER books from algo status. If a tier's status
    looked 'filled' but userTrades shows NO reducing fill (and the position never
    dropped), nothing is booked, realized_pnl stays at the entry-fee baseline, and
    the conservation law holds (base == position) -> NO phantom fill, NO anomaly.
    This is the structural cure for the over-/under-book oscillation."""
    runner = _runner()
    trade = _ladder_trade(qty=1.0)
    trade.extra["original_position_qty"] = 1.0
    adapter = FakeExchange(
        positions=[Position("BTCUSDT", "long", 1.0, 1000.0, mark_price=1000.0)],
        open_order_ids={"tp1", "tp2", "tp3", "tp4", "run1", "sl1"},
        user_trades=[_ut("BUY", 1000.0, 1.0, 0.0, oid="entry")],  # entry only, no exits
    )
    await runner._sync_ladder_trades(_db(), adapter, [trade])

    et = _event_types(runner)
    assert trade.realized_pnl == pytest.approx(0.0)            # only the (zero-fee) entry
    assert all(not t.get("filled") for t in trade.extra["tier_orders"])
    assert "ladder_tier_filled" not in et                      # never booked from status
    assert "ladder_sync_anomaly" not in et                     # base 1.0 == exit 0 + pos 1.0


@pytest.mark.asyncio
async def test_runner_activation_after_all_tiers_filled():
    runner = _runner()
    trade = _ladder_trade(qty=0.2)  # only the runner quantity remains
    adapter = FakeExchange(
        positions=[Position("BTCUSDT", "long", 0.2, 1000.0, mark_price=1017.0)],
        open_order_ids={"run1", "sl1"},  # all 4 TP tiers gone
        user_trades=[
            _ut("BUY", 1000.0, 1.0, 0.0, oid="entry"),
            _ut("SELL", 1003.0, 0.2, 0.6, oid="x1"),
            _ut("SELL", 1006.0, 0.2, 1.2, oid="x2"),
            _ut("SELL", 1010.0, 0.2, 2.0, oid="x3"),
            _ut("SELL", 1016.0, 0.2, 3.2, oid="x4"),
        ],
    )
    await runner._sync_ladder_trades(_db(), adapter, [trade])
    assert trade.extra["tiers_filled"] == 4          # 0.8 exited / 0.2
    assert trade.extra["runner_active"] is True
    assert "ladder_runner_active" in _event_types(runner)


@pytest.mark.asyncio
async def test_stop_not_advanced_when_rung_above_mark():
    runner = _runner()
    trade = _ladder_trade(qty=0.4)
    # 3 tiers exited -> rung +1.0% (1010) but mark only 1008 -> highest feasible below.
    adapter = FakeExchange(
        positions=[Position("BTCUSDT", "long", 0.4, 1000.0, mark_price=1008.0)],
        open_order_ids={"tp4", "run1", "sl1"},
        user_trades=[
            _ut("BUY", 1000.0, 1.0, 0.0, oid="entry"),
            _ut("SELL", 1003.0, 0.2, 0.6, oid="x1"),
            _ut("SELL", 1006.0, 0.2, 1.2, oid="x2"),
            _ut("SELL", 1010.0, 0.2, 2.0, oid="x3"),
        ],
    )
    await runner._sync_ladder_trades(_db(), adapter, [trade])
    # stop advances to the highest rung still below mark (+0.5% = 1005),
    # not to +1.0% which would sit above the 1008 mark.
    assert trade.stop_loss == 1005.0


# ----------------------------------------------------------------- full-close finalize

@pytest.mark.asyncio
async def test_position_gone_finalizes_and_emits_150_count_event():
    runner = _runner()
    trade = _ladder_trade(qty=0.2)
    # No position on the exchange -> fully closed. AUTHORITATIVE: userTrades carries
    # the full ledger (entry 1.0; 4 tiers + runner exit 1.0); net is exact + balanced.
    adapter = FakeExchange(positions=[], user_trades=[
        _ut("BUY", 1000.0, 1.0, 0.0, oid="entry"),
        _ut("SELL", 1003.0, 0.2, 0.6, oid="x1"),
        _ut("SELL", 1006.0, 0.2, 1.2, oid="x2"),
        _ut("SELL", 1010.0, 0.2, 2.0, oid="x3"),
        _ut("SELL", 1016.0, 0.2, 3.2, oid="x4"),
        _ut("SELL", 1020.0, 0.2, 4.0, oid="run"),
    ])
    await runner._sync_ladder_trades(_db(), adapter, [trade])

    assert trade.status == "closed"
    assert trade.extra["ladder_active"] is False
    assert trade.realized_pnl == pytest.approx(11.0)  # Σ realizedPnl, zero commission
    et = _event_types(runner)
    assert "ladder_trade_closed" in et
    closed_payload = next(c.args[4] for c in runner._risk_event.await_args_list
                          if c.args[2] == "ladder_trade_closed")
    assert closed_payload["counts_toward_150"] is True
    assert closed_payload["ledger_balanced"] is True   # base 1.0 == exit 1.0
    # leftover stop/runner cancelled defensively
    assert ("BTCUSDT", "sl1") in adapter.cancelled


# -------------------------------------------------------------------- time-based exits

@pytest.mark.asyncio
async def test_time_partial_exit_reladders_remaining():
    runner = _runner()
    trade = _ladder_trade(qty=1.0, opened_minutes_ago=16.0)  # past 15-min partial
    adapter = FakeExchange(
        positions=[Position("BTCUSDT", "long", 1.0, 1000.0, mark_price=1004.0)],
        open_order_ids={"tp1", "tp2", "tp3", "tp4", "run1", "sl1"},
    )
    await runner._sync_ladder_trades(_db(), adapter, [trade])
    assert trade.extra["time_partial_done"] is True
    # 50% (0.5) market-closed by the time-partial, leaving 0.5 to re-ladder into
    # 0.1-per-tier. mark=1004 is already past the new tier1 (1003), so item 1 takes
    # that 0.1 slice at market immediately rather than posting a -2021 resting TP.
    assert trade.quantity == pytest.approx(0.4)
    # remaining 0.4 re-laddered into the 3 still-resting tiers (1006/1010/1016)
    assert trade.extra["tier_orders"]
    assert all(not t["filled"] for t in trade.extra["tier_orders"])
    assert "ladder_time_partial" in _event_types(runner)


@pytest.mark.asyncio
async def test_time_full_exit_cancels_all_and_closes():
    runner = _runner()
    trade = _ladder_trade(qty=0.6, opened_minutes_ago=46.0)  # past 45-min hard exit
    adapter = FakeExchange(
        positions=[Position("BTCUSDT", "long", 0.6, 1000.0, mark_price=1004.0)],
        open_order_ids={"tp3", "tp4", "run1", "sl1"},
        order_fills={"tp1": 1003.0, "tp2": 1006.0},
    )
    await runner._sync_ladder_trades(_db(), adapter, [trade])
    assert trade.status == "closed"
    assert trade.extra["close_reason"] == "time_exit_full"
    et = _event_types(runner)
    assert "ladder_time_full" in et
    assert "ladder_trade_closed" in et
    # stop + remaining tiers + runner all cancelled
    assert ("BTCUSDT", "sl1") in adapter.cancelled


# ----------------------------------------------------------------------------- routing

@pytest.mark.asyncio
async def test_manage_open_trades_routes_ladder_trades(monkeypatch):
    runner = _runner()
    trade = _ladder_trade()
    runner._open_database_trades = AsyncMock(return_value=[trade])
    captured = {}
    async def _fake_sync(db, adapter, trades):
        captured["trades"] = trades
    monkeypatch.setattr(runner, "_sync_ladder_trades", _fake_sync)
    monkeypatch.setattr(runner, "_sync_exchange_resting_trades", AsyncMock())
    await runner._manage_open_trades(_db(), FakeExchange(positions=[]))
    assert captured.get("trades") == [trade]


def test_use_ladder_exits_requires_both_flags():
    on = BotRunner(settings=Settings(
        trading_mode=TradingMode.TESTNET, market_type="futures",
        exchange_resting_exits_enabled=True, five_tier_ladder_enabled=True))
    assert on._use_ladder_exits() is True

    ladder_only = BotRunner(settings=Settings(
        trading_mode=TradingMode.TESTNET, market_type="futures",
        exchange_resting_exits_enabled=False, five_tier_ladder_enabled=True))
    # resting off -> ladder gate off (the start() guard also disables it in-memory)
    assert ladder_only._use_ladder_exits() is False

    paper = BotRunner(settings=Settings(
        trading_mode=TradingMode.PAPER, market_type="futures",
        exchange_resting_exits_enabled=True, five_tier_ladder_enabled=True))
    assert paper._use_ladder_exits() is False


# ------------------------------------------------------- entry attach + degradation

def _scored(qty_signal_entry=1000.0):
    signal = StrategySignal(
        setup_name="test", symbol="BTCUSDT", direction="long",
        entry_price=qty_signal_entry, stop_loss=995.0,
        take_profit_levels=[1003.0, 1006.0, 1010.0, 1016.0],
        trailing_stop=0.0, expected_move=10.0, risk_reward_ratio=2.0,
        confidence_score=80.0, accepted=True,
    )
    context = StrategyContext(
        symbol="BTCUSDT", candles_by_timeframe={}, session_name="london",
        regime="trend", coin_strength_score=70.0,
    )
    return MagicMock(signal=signal, context=context, signal_id="s1")


@pytest.mark.asyncio
async def test_entry_attach_full_ladder_persists_markers():
    runner = _runner()
    adapter = FakeExchange(positions=[])
    from app.execution.order_manager import OrderManager
    manager = OrderManager(adapter, settings=runner.settings)
    trade = Trade(
        id="t1", symbol="BTCUSDT", side="long", exchange="binance", mode="testnet",
        setup_name="test", entry_price=1000.0, stop_loss=995.0,
        take_profit={"levels": [1003.0]}, quantity=1.0, status="open", extra={},
    )
    scored = _scored()
    await runner._attach_ladder_exits(_db(), manager, scored.signal, trade, scored)
    assert trade.extra["ladder_active"] is True
    assert trade.extra["entry_atr"] == 5.0  # fallback to |entry-stop| (no candles)
    assert len(trade.extra["tier_orders"]) == 4
    assert trade.extra["stop_order_id"]
    assert "ladder_attached" in _event_types(runner)


@pytest.mark.asyncio
async def test_entry_attach_degrades_to_single_for_tiny_position():
    runner = _runner()
    adapter = FakeExchange(positions=[])
    from app.execution.order_manager import OrderManager
    manager = OrderManager(adapter, settings=runner.settings)
    trade = Trade(
        id="t1", symbol="BTCUSDT", side="long", exchange="binance", mode="testnet",
        setup_name="test", entry_price=1000.0, stop_loss=995.0,
        take_profit={"levels": [1003.0]}, quantity=0.008, status="open", extra={},
    )
    scored = _scored()
    await runner._attach_ladder_exits(_db(), manager, scored.signal, trade, scored)
    # Min-notional Option A: fell back to Branch 1 single stop+TP, NOT a ladder.
    assert trade.extra.get("ladder_active") is not True
    assert trade.extra["exchange_resting_active"] is True
    assert trade.extra["entry_atr"] == 5.0
    assert "ladder_min_notional_degraded" in _event_types(runner)
