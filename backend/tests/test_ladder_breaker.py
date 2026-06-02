"""Phase 2B ladder-fix item 4 — partial-attach-rate circuit breaker (C2).

AC4-1..AC4-7. The breaker watches ATTACH health (full/partial/failed) on its own
state, independent of C1's total-failure counter and of the ladder_sync_anomaly
accounting tripwire — neither may mask the other.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.config.settings import Settings, TradingMode
from app.database.models import Trade
from app.execution.exit_ladder import classify_attach
from app.services.bot_runner import BotRunner
from unittest.mock import AsyncMock, MagicMock


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


# AC4-1 — classifier buckets full / partial / failed; fix-A counts as accounted.
def test_AC4_1_classifier_buckets():
    # all 4 tiers rested + runner -> full
    assert classify_attach(stop_placed=True, planned_tier_count=4, rested_tier_count=4,
                           immediate_fill_count=0, planned_runner=True, runner_placed=True) == "full"
    # 3 rested + 1 market-closed by fix A -> still FULL (accounted, not degraded)
    assert classify_attach(stop_placed=True, planned_tier_count=4, rested_tier_count=3,
                           immediate_fill_count=1, planned_runner=True, runner_placed=True) == "full"
    # 1 of 4 tiers rested, 3 genuinely dropped -> partial
    assert classify_attach(stop_placed=True, planned_tier_count=4, rested_tier_count=1,
                           immediate_fill_count=0, planned_runner=True, runner_placed=True) == "partial"
    # runner dropped -> partial
    assert classify_attach(stop_placed=True, planned_tier_count=4, rested_tier_count=4,
                           immediate_fill_count=0, planned_runner=True, runner_placed=False) == "partial"
    # stop didn't place -> failed
    assert classify_attach(stop_placed=False, planned_tier_count=4, rested_tier_count=0,
                           immediate_fill_count=0, planned_runner=True, runner_placed=False) == "failed"


# AC4-2 — C2 trips when partial rate >= P over >= min_sample attaches.
@pytest.mark.asyncio
async def test_AC4_2_c2_trips_on_partial_rate():
    runner = _runner()
    db = _db()
    # 6 full then 2 partial: at sample 8, 2/8 = 25% >= 20% -> trip
    tripped = False
    for c in (["full"] * 6 + ["partial"] * 2):
        tripped = await runner._record_ladder_attach_classification(db, c)
    assert tripped is True
    assert runner._ladder_disabled_by_breaker is True
    assert runner._use_ladder_exits() is False
    assert "ladder_circuit_breaker" in _events(runner)


# AC4-3 — does NOT trip below the min-sample floor (1-of-2 = 50% but sample<8).
@pytest.mark.asyncio
async def test_AC4_3_c2_no_trip_below_floor():
    runner = _runner()
    db = _db()
    assert await runner._record_ladder_attach_classification(db, "partial") is False
    assert await runner._record_ladder_attach_classification(db, "full") is False
    assert runner._ladder_disabled_by_breaker is False
    assert runner._use_ladder_exits() is True
    assert "ladder_circuit_breaker" not in _events(runner)


# AC4-4 — C1 (total attach failures >= N_fail in W) still trips (no regression).
@pytest.mark.asyncio
async def test_AC4_4_c1_still_trips():
    runner = _runner()
    db = _db()
    tripped = False
    for _ in range(runner.settings.protective_orders_failure_threshold):
        tripped = await runner._record_protective_attach_outcome(db, False)
    assert tripped is True
    assert runner._resting_disabled_until_utc_day is not None  # existing resting disable
    # C1 disables resting -> the ladder is off transitively (no separate sticky flag;
    # that is C2's mechanism). C1 keeps its own state/auto-reset, unchanged.
    assert runner._use_exchange_resting_exits() is False
    assert runner._use_ladder_exits() is False
    assert runner._ladder_disabled_by_breaker is False
    assert "protective_orders_circuit_breaker" in _events(runner)


# AC4-5a — all-partial run trips the breaker with ZERO sync anomalies.
@pytest.mark.asyncio
async def test_AC4_5a_breaker_fires_without_sync_anomaly():
    runner = _runner()
    db = _db()
    for _ in range(runner.settings.ladder_partial_attach_min_sample):
        await runner._record_ladder_attach_classification(db, "partial")
    assert runner._ladder_disabled_by_breaker is True
    assert "ladder_circuit_breaker" in _events(runner)
    assert "ladder_sync_anomaly" not in _events(runner)  # breaker fired on its own


# AC4-5b — a sync anomaly with healthy attach rate fires the tripwire, NOT the breaker.
@pytest.mark.asyncio
async def test_AC4_5b_tripwire_fires_without_breaker():
    runner = _runner()
    db = _db()
    trade = Trade(id="t1", symbol="BTCUSDT", side="long", exchange="binance", mode="testnet",
                  setup_name="test", entry_price=1000.0, stop_loss=995.0,
                  take_profit={"levels": [1003.0]}, quantity=0.8, status="open",
                  opened_at=datetime.now(timezone.utc), realized_pnl=0.0,
                  extra={"tier_orders": [], "original_position_qty": 1.0})
    extra = trade.extra
    # unexplained residual: 0.2 left the position with nothing accounted -> tripwire
    await runner._ladder_residual_crosscheck(db, trade, extra, exchange_qty=0.8)
    assert "ladder_sync_anomaly" in _events(runner)
    assert runner._ladder_disabled_by_breaker is False        # breaker untouched
    assert len(runner._attach_classifications) == 0           # separate state, never fed


# AC4-6 — trip action: ladder flag False, single-TP resumes, alert sent, reason logged.
@pytest.mark.asyncio
async def test_AC4_6_trip_action():
    runner = _runner()
    db = _db()
    for c in (["full"] * 6 + ["partial"] * 2):
        await runner._record_ladder_attach_classification(db, c)
    assert runner.settings.five_tier_ladder_enabled is False   # flag flipped off
    assert runner._use_ladder_exits() is False                 # ladder path off
    assert runner._use_exchange_resting_exits() is True        # single-TP resting still on
    # Telegram alert sent on the breaker channel
    assert any(c.args[0] == "ladder_circuit_breaker" for c in runner.alerts.send.await_args_list)
    # trip reason + counters logged on the risk event
    payload = next(c.args[4] for c in runner._risk_event.await_args_list
                   if c.args[2] == "ladder_circuit_breaker")
    assert payload["reason"] == "partial_attach_rate"
    assert payload["partial_rate"] >= runner.settings.ladder_partial_attach_rate_threshold
    assert payload["sample"] >= runner.settings.ladder_partial_attach_min_sample


# AC4-7 — recovery is manual: more healthy attaches do NOT auto-re-arm.
@pytest.mark.asyncio
async def test_AC4_7_no_auto_rearm():
    runner = _runner()
    db = _db()
    for c in (["full"] * 6 + ["partial"] * 2):
        await runner._record_ladder_attach_classification(db, c)
    assert runner._ladder_disabled_by_breaker is True
    # feed a long clean streak — must NOT re-enable the ladder
    for _ in range(30):
        await runner._record_ladder_attach_classification(db, "full")
    assert runner._ladder_disabled_by_breaker is True
    assert runner._use_ladder_exits() is False
