"""Phase 2B Branch 1 refinement 2: tests for the protective-orders circuit-breaker."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config.settings import Settings, TradingMode
from app.services.bot_runner import BotRunner


def _runner() -> BotRunner:
    s = Settings(
        trading_mode=TradingMode.TESTNET,
        market_type="futures",
        exchange_resting_exits_enabled=True,
        protective_orders_failure_threshold=3,
        protective_orders_failure_window_hours=1,
    )
    runner = BotRunner(settings=s)
    runner._risk_event = AsyncMock()
    runner.alerts = MagicMock()
    runner.alerts.send = AsyncMock()
    return runner


@pytest.mark.asyncio
async def test_success_outcomes_never_trip_the_breaker():
    runner = _runner()
    for _ in range(10):
        tripped = await runner._record_protective_attach_outcome(MagicMock(), success=True)
        assert tripped is False
    assert runner._use_exchange_resting_exits() is True
    runner.alerts.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_failures_below_threshold_do_not_trip():
    runner = _runner()
    for _ in range(2):  # threshold is 3
        tripped = await runner._record_protective_attach_outcome(MagicMock(), success=False)
        assert tripped is False
    assert runner._use_exchange_resting_exits() is True


@pytest.mark.asyncio
async def test_failures_at_threshold_trip_breaker_disabling_resting_for_the_day():
    runner = _runner()
    for i in range(2):
        await runner._record_protective_attach_outcome(MagicMock(), success=False)
    # third failure trips
    tripped = await runner._record_protective_attach_outcome(MagicMock(), success=False)
    assert tripped is True
    assert runner._use_exchange_resting_exits() is False
    runner.alerts.send.assert_awaited_once()
    args, _ = runner.alerts.send.await_args
    assert args[0] == "protective_orders_circuit_breaker"
    # Risk event was emitted with the same alert_type / payload keys
    risk_call = runner._risk_event.await_args
    assert risk_call.args[2] == "protective_orders_circuit_breaker"
    payload = risk_call.args[4]
    assert payload["threshold"] == 3
    assert payload["window_hours"] == 1


@pytest.mark.asyncio
async def test_out_of_window_failures_drop_off_and_do_not_trip():
    runner = _runner()
    # Two failures stamped > 1h ago (outside the 1h window)
    stale = datetime.now(timezone.utc) - timedelta(hours=2)
    runner._protective_failures.extend([stale, stale])
    # One fresh failure should NOT trip the breaker because the stale ones expire
    tripped = await runner._record_protective_attach_outcome(MagicMock(), success=False)
    assert tripped is False
    assert runner._use_exchange_resting_exits() is True


@pytest.mark.asyncio
async def test_breaker_auto_resets_at_next_utc_day_start():
    runner = _runner()
    # Manually set the trip to yesterday
    runner._resting_disabled_until_utc_day = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    # Reading the gate should auto-reset because today != yesterday
    assert runner._use_exchange_resting_exits() is True
    assert runner._resting_disabled_until_utc_day is None
