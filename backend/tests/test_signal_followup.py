from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.database.models import Signal, Trade
from app.exchanges.base import Candle
from app.signals.followup import evaluate_signal_follow_up


BASE_TIME = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _signal(
    *,
    accepted: bool = False,
    direction: str = "long",
    entry: float = 100.0,
    stop: float = 99.0,
    target: float | None = 102.0,
) -> Signal:
    row = Signal(
        id="signal-1",
        symbol="BTCUSDT",
        setup_name="EMA pullback scalp",
        direction=direction,
        entry_price=entry,
        stop_loss=stop,
        take_profit={"levels": [target]} if target else {"levels": []},
        confidence_score=80,
        accepted=accepted,
        reasons_for_entry=["trend aligned"] if accepted else [],
        rejection_reasons=[] if accepted else ["setup not confirmed"],
    )
    row.created_at = BASE_TIME
    return row


def _candle(minutes: int, high: float, low: float, close: float) -> Candle:
    return Candle(
        timestamp=BASE_TIME + timedelta(minutes=minutes),
        open=100.0,
        high=high,
        low=low,
        close=close,
        volume=1000.0,
    )


def test_rejected_signal_that_hits_target_is_bad_rejection() -> None:
    result = evaluate_signal_follow_up(
        _signal(accepted=False),
        None,
        [_candle(1, high=102.5, low=100.1, close=102.1)],
        decision_accepted=False,
        now=BASE_TIME + timedelta(minutes=5),
    )

    assert result["status"] == "would_win"
    assert result["verdict"] == "bad_rejection"
    assert result["basis"] == "planned_shadow"
    assert result["settled"] is True


def test_rejected_signal_that_hits_stop_is_good_rejection() -> None:
    result = evaluate_signal_follow_up(
        _signal(accepted=False),
        None,
        [_candle(1, high=100.2, low=98.8, close=99.1)],
        decision_accepted=False,
        now=BASE_TIME + timedelta(minutes=5),
    )

    assert result["status"] == "would_lose"
    assert result["verdict"] == "good_rejection"
    assert result["exit_reason"] == "stop_loss"
    assert result["settled"] is True


def test_signal_without_trade_plan_is_not_used_for_decision_quality() -> None:
    result = evaluate_signal_follow_up(
        _signal(accepted=False, entry=100.0, stop=100.0, target=None),
        None,
        [_candle(1, high=100.4, low=99.9, close=100.3)],
        decision_accepted=False,
        now=BASE_TIME + timedelta(minutes=5),
        min_move_pct=0.15,
    )

    assert result["status"] == "not_trackable"
    assert result["verdict"] == "unknown"
    assert result["settled"] is False


def test_unclosed_shadow_trade_stays_pending() -> None:
    result = evaluate_signal_follow_up(
        _signal(accepted=False),
        None,
        [_candle(1, high=100.7, low=99.4, close=100.2)],
        decision_accepted=False,
        now=BASE_TIME + timedelta(minutes=185),
    )

    assert result["status"] == "still_running"
    assert result["verdict"] == "pending"
    assert result["settled"] is False


def test_actual_closed_losing_trade_is_bad_acceptance() -> None:
    trade = Trade(
        id="trade-1",
        symbol="BTCUSDT",
        side="long",
        exchange="binance",
        mode="testnet",
        setup_name="EMA pullback scalp",
        entry_price=100.0,
        stop_loss=99.0,
        take_profit={"levels": [102.0]},
        quantity=10.0,
        status="closed",
        realized_pnl=-10.0,
        extra={"close_reason": "stop_loss"},
    )
    result = evaluate_signal_follow_up(
        _signal(accepted=True),
        trade,
        [],
        decision_accepted=True,
        now=BASE_TIME + timedelta(minutes=5),
    )

    assert result["status"] == "actual_negative"
    assert result["verdict"] == "bad_acceptance"
    assert result["basis"] == "actual_trade"
    assert result["settled"] is True


def test_missing_followup_market_data_stops_waiting_after_grace_period() -> None:
    result = evaluate_signal_follow_up(
        _signal(accepted=False),
        None,
        [],
        decision_accepted=False,
        now=BASE_TIME + timedelta(minutes=5),
    )

    assert result["status"] == "market_data_unavailable"
    assert result["verdict"] == "unknown"
    assert result["settled"] is False


# ---- Phase 2B Branch 1: fee-aware shadow replay ----

def test_shadow_take_profit_pnl_is_fee_adjusted_with_gross_preserved() -> None:
    """Default settings: fee_rate_bps=6, slippage_bps=4 -> round-trip 0.20 pct-pts.
    A long entry @100 with target @102 = gross +2.0%; net should be ~+1.80%."""
    result = evaluate_signal_follow_up(
        _signal(accepted=False, target=102.0),
        None,
        [_candle(1, high=102.5, low=100.1, close=102.1)],
        decision_accepted=False,
        now=BASE_TIME + timedelta(minutes=5),
    )
    assert result["pnl_pct_gross"] == pytest.approx(2.0, abs=1e-3)
    assert result["pnl_pct"] == pytest.approx(1.80, abs=1e-3)


def test_shadow_stop_loss_pnl_is_fee_adjusted_negative() -> None:
    """Long stop @99 = gross -1.0%; net ~-1.20% after costs."""
    result = evaluate_signal_follow_up(
        _signal(accepted=False),
        None,
        [_candle(1, high=100.2, low=98.8, close=99.1)],
        decision_accepted=False,
        now=BASE_TIME + timedelta(minutes=5),
    )
    assert result["pnl_pct_gross"] == pytest.approx(-1.0, abs=1e-3)
    assert result["pnl_pct"] == pytest.approx(-1.20, abs=1e-3)


def test_shadow_fee_adjustment_can_be_disabled() -> None:
    """When shadow_replay_fee_aware=False, pnl_pct == pnl_pct_gross."""
    # get_settings is lru_cached; clear so override takes effect.
    from app.config.settings import get_settings, Settings
    get_settings.cache_clear()
    try:
        get_settings.__wrapped__ = lambda: Settings(shadow_replay_fee_aware=False)  # type: ignore[attr-defined]
        import app.signals.followup as fu
        # easier: patch the module-level get_settings
        original = fu.get_settings
        fu.get_settings = lambda: Settings(shadow_replay_fee_aware=False)
        try:
            result = evaluate_signal_follow_up(
                _signal(accepted=False, target=102.0),
                None,
                [_candle(1, high=102.5, low=100.1, close=102.1)],
                decision_accepted=False,
                now=BASE_TIME + timedelta(minutes=5),
            )
            assert result["pnl_pct"] == result["pnl_pct_gross"]
            assert result["pnl_pct"] == pytest.approx(2.0, abs=1e-3)
        finally:
            fu.get_settings = original
    finally:
        get_settings.cache_clear()


# Import pytest at the bottom so the helper-only file at the top stays clean.
import pytest  # noqa: E402
