from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.config.settings import Settings, TradingMode
from app.database.models import Trade
from app.exchanges.base import Position
from app.regime.detector import RegimeResult
from app.scoring.setup_score import SetupScoreResult
from app.services.bot_runner import BotRunner, _safe_error_message
from app.sessions.session_manager import SessionManager
from app.strategies.base_strategy import StrategyContext


@pytest.mark.asyncio
async def test_bot_runner_blocks_when_autonomous_loop_disabled() -> None:
    runner = BotRunner(Settings(autonomous_trading_enabled=False, trading_mode=TradingMode.TESTNET))

    status = await runner.start()

    assert status.status == "blocked"
    assert status.enabled is False
    assert "AUTONOMOUS_TRADING_ENABLED=false" in status.messages[-1]


@pytest.mark.asyncio
async def test_bot_runner_blocks_live_without_live_flag() -> None:
    runner = BotRunner(Settings(trading_mode=TradingMode.LIVE_FUTURES, live_trading_enabled=False))

    status = await runner.start()

    assert status.status == "blocked"
    assert status.enabled is False
    assert "LIVE_TRADING_ENABLED=false" in status.messages[-1]


def test_leader_confirmation_allows_neutral_a_setup() -> None:
    runner = BotRunner(Settings())
    context = StrategyContext(
        symbol="ETHUSDT",
        candles_by_timeframe={},
        session_name="new_york",
        regime="good",
        coin_strength_score=85,
    )
    score = SetupScoreResult(82, "A", "normal entry", {}, [])

    assert runner._leader_confirmation_valid(context, "long", score) is True


def test_safe_error_message_redacts_signed_exchange_query() -> None:
    message = (
        "Client error '400 Bad Request' for url "
        "'https://demo-fapi.binance.com/fapi/v1/leverage?symbol=LTCUSDT&leverage=2"
        "&timestamp=1779121780283&recvWindow=10000&signature=abc123'"
    )

    safe = _safe_error_message(message)

    assert "1779121780283" not in safe
    assert "abc123" not in safe
    assert "timestamp=[redacted]" in safe
    assert "signature=[redacted]" in safe


def test_leader_confirmation_rejects_conflict() -> None:
    runner = BotRunner(Settings())
    context = StrategyContext(
        symbol="ETHUSDT",
        candles_by_timeframe={},
        session_name="new_york",
        regime="good",
        coin_strength_score=85,
        btc_direction="short",
        eth_direction=None,
    )
    score = SetupScoreResult(92, "A+", "aggressive entry allowed", {}, [])

    assert runner._leader_confirmation_valid(context, "long", score) is False


def test_trading_session_allows_off_session_with_stricter_threshold() -> None:
    settings = Settings(
        off_session_trading_enabled=True,
        off_session_score_threshold_c=60,
    )
    runner = BotRunner(settings)
    session = SessionManager(settings).trading_session(
        now=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        regime="good",
    )

    assert session.name == "off_session"
    assert session.tradable is True
    assert runner._minimum_score_for_session(session) == 60


def test_normal_session_uses_normal_threshold() -> None:
    settings = Settings(normal_score_threshold_c=55, off_session_score_threshold_c=60)
    runner = BotRunner(settings)
    session = SessionManager(settings).trading_session(
        now=datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc),
        regime="good",
    )

    assert session.name == "london"
    assert session.tradable is True
    assert runner._minimum_score_for_session(session) == 55


def test_runtime_status_shows_tradable_off_session() -> None:
    settings = Settings(off_session_trading_enabled=True)
    runner = BotRunner(settings)
    session = SessionManager(settings).trading_session(
        now=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        regime="good",
    )

    runner._update_cycle_status(session, RegimeResult("good", 70, True, 30))

    assert runner.status.current_session == "off_session"


def test_runtime_status_exposes_scan_workload_settings() -> None:
    settings = Settings(bot_cycle_symbol_limit=25)
    runner = BotRunner(settings)

    assert runner.status.cycle_symbol_limit == 25
    assert runner.status.strategy_count == len(runner.strategies)
    assert runner.status.last_signal_batch_at is None


def test_current_signal_ids_publish_last_completed_batch_as_copy() -> None:
    runner = BotRunner(Settings())

    runner._pending_signal_ids = ["signal-1", "signal-2"]
    assert runner.current_signal_ids() == []

    runner._publish_pending_signal_batch()
    ids = runner.current_signal_ids()
    ids.append("signal-3")

    assert runner.current_signal_ids() == ["signal-1", "signal-2"]
    assert runner._pending_signal_ids == []


def test_current_signal_ids_clear_after_empty_completed_batch() -> None:
    runner = BotRunner(Settings())

    runner._pending_signal_ids = ["signal-1"]
    runner._publish_pending_signal_batch()
    runner._pending_signal_ids = []
    runner._publish_pending_signal_batch()

    assert runner.current_signal_ids() == []


def test_exposure_positions_deduplicate_database_and_exchange_symbols() -> None:
    runner = BotRunner(Settings())
    trade = Trade(
        symbol="BTCUSDT",
        side="long",
        exchange="binance",
        mode="testnet",
        setup_name="EMA pullback scalp",
        entry_price=100.0,
        stop_loss=99.0,
        take_profit={"levels": [102.0]},
        quantity=1.0,
        status="open",
    )
    position = Position(
        symbol="BTCUSDT",
        side="long",
        quantity=1.0,
        entry_price=100.0,
        mark_price=101.0,
    )

    exposures = runner._exposure_positions([trade], [position], "london")

    assert len(exposures) == 1
    assert exposures[0].symbol == "BTCUSDT"
    assert exposures[0].notional == 101.0
    assert exposures[0].session == "unknown"
    assert exposures[0].source == "database+exchange"
    assert runner._active_symbol_count([trade], [position]) == 1


def test_database_exposure_uses_trade_entry_session_and_stop_risk() -> None:
    runner = BotRunner(Settings())
    trade = Trade(
        symbol="ETHUSDT",
        side="short",
        exchange="binance",
        mode="testnet",
        setup_name="EMA pullback scalp",
        entry_price=100.0,
        stop_loss=102.0,
        take_profit={"levels": [98.0]},
        quantity=3.0,
        status="open",
        extra={"entry_session": "london"},
    )

    exposures = runner._exposure_positions([trade], [], "off_session")

    assert exposures[0].session == "london"
    assert exposures[0].open_risk == 6.0


def test_exchange_only_exposure_uses_conservative_estimated_risk() -> None:
    runner = BotRunner(Settings())
    position = Position(
        symbol="SOLUSDT",
        side="long",
        quantity=10.0,
        entry_price=100.0,
        mark_price=101.0,
    )

    exposures = runner._exposure_positions([], [position], "off_session")

    assert exposures[0].source == "exchange_estimated_risk"
    assert exposures[0].open_risk == 10.1


def test_scan_only_reasons_include_daily_and_concurrency_guards() -> None:
    settings = Settings(max_concurrent_trades=5, max_trades_per_day=50)
    runner = BotRunner(settings)

    reasons = runner._scan_only_reasons(
        active_symbol_count=5,
        open_order_count=5,
        today_trade_count=50,
        daily_pnl_pct=10.1,
    )

    assert "max concurrent trade limit reached" in reasons
    assert "open order limit reached" in reasons
    assert "daily trade limit reached" in reasons
    assert "daily profit target reached; protecting gains" in reasons


def test_closed_trade_outcome_updates_loss_streak() -> None:
    runner = BotRunner(Settings())
    losing_trade = Trade(
        symbol="BTCUSDT",
        side="long",
        exchange="binance",
        mode="testnet",
        setup_name="EMA pullback scalp",
        entry_price=100.0,
        stop_loss=99.0,
        take_profit={"levels": [101.0]},
        quantity=1.0,
        status="closed",
        realized_pnl=-1.0,
    )
    winning_trade = Trade(
        symbol="ETHUSDT",
        side="long",
        exchange="binance",
        mode="testnet",
        setup_name="EMA pullback scalp",
        entry_price=100.0,
        stop_loss=99.0,
        take_profit={"levels": [101.0]},
        quantity=1.0,
        status="closed",
        realized_pnl=2.0,
    )

    runner._record_closed_trade_outcome(losing_trade)
    runner._record_closed_trade_outcome(losing_trade)
    assert runner._consecutive_losses == 2
    assert runner.status.consecutive_losses == 2
    assert runner.status.recent_consecutive_losses == 2

    runner._record_closed_trade_outcome(winning_trade)
    assert runner._consecutive_losses == 0
    assert runner.status.consecutive_losses == 0
    assert runner.status.recent_consecutive_losses == 0


def test_orphan_reconciled_trade_does_not_reset_loss_streak() -> None:
    runner = BotRunner(Settings(loss_streak_min_abs_pnl=0.01))
    runner._consecutive_losses = 3
    runner._recent_consecutive_losses = 3
    runner._update_loss_status()
    orphan_trade = Trade(
        symbol="BTCUSDT",
        side="long",
        exchange="binance",
        mode="testnet",
        setup_name="Exchange reconciled position",
        entry_price=100.0,
        stop_loss=99.0,
        take_profit={"levels": [101.0]},
        quantity=1.0,
        status="closed",
        realized_pnl=5.0,
        extra={"reconciled_orphan_position": True, "entry_session": "unknown", "entry_regime": "unknown"},
    )

    runner._record_closed_trade_outcome(orphan_trade)

    assert runner._consecutive_losses == 3
    assert runner.status.consecutive_losses == 3
    assert runner.status.recent_consecutive_losses == 3


def test_dust_positive_trade_does_not_reset_loss_streak() -> None:
    runner = BotRunner(Settings(loss_streak_min_abs_pnl=0.01))
    runner._consecutive_losses = 2
    runner._recent_consecutive_losses = 2
    runner._update_loss_status()
    dust_win = Trade(
        symbol="BTCUSDT",
        side="long",
        exchange="binance",
        mode="testnet",
        setup_name="EMA pullback scalp",
        entry_price=100.0,
        stop_loss=99.0,
        take_profit={"levels": [101.0]},
        quantity=1.0,
        status="closed",
        realized_pnl=0.003,
        extra={"signal_id": "signal-1", "setup_score": 90},
    )

    runner._record_closed_trade_outcome(dust_win)

    assert runner._consecutive_losses == 2
    assert runner.status.consecutive_losses == 2
    assert runner.status.recent_consecutive_losses == 2
