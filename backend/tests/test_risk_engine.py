from __future__ import annotations

from app.config.settings import Settings
from app.risk.risk_engine import RiskEngine, TradePermissionRequest


def test_daily_loss_shutdown():
    state = RiskEngine().evaluate_daily_state(daily_pnl_pct=-4.1, consecutive_losses=0)

    assert state.hard_shutdown is True
    assert state.allow_new_trades is False
    assert state.daily_hard_shutdown is True
    assert state.loss_streak_shutdown is False


def test_loss_streak_shutdown_is_not_reported_as_daily_hard_loss():
    engine = RiskEngine()
    state = engine.evaluate_daily_state(daily_pnl_pct=0, consecutive_losses=4, recent_consecutive_losses=0)

    assert state.hard_shutdown is True
    assert state.daily_hard_shutdown is False
    assert state.loss_streak_shutdown is True
    assert "daily hard loss shutdown" not in state.reasons


def test_loss_cooldown_uses_recent_losses_and_can_expire():
    engine = RiskEngine()
    active = engine.evaluate_daily_state(daily_pnl_pct=0, consecutive_losses=3, recent_consecutive_losses=3)
    expired = engine.evaluate_daily_state(daily_pnl_pct=0, consecutive_losses=3, recent_consecutive_losses=0)

    assert active.cooldown_required is True
    assert active.allow_new_trades is False
    assert expired.cooldown_required is False
    assert expired.allow_new_trades is True


def test_position_sizing_uses_stop_distance():
    engine = RiskEngine()
    result = engine.calculate_position_size(
        account_equity=10_000,
        risk_pct=0.2,
        entry_price=100,
        stop_loss=99,
        leverage=2,
    )

    assert result.valid is True
    assert result.quantity > 0
    assert result.notional <= 10_000 * 0.10


def test_risk_is_capped_at_a_tier_minimum_above_score_75():
    # Phase 2A: scores >= cap_risk_at_score (75) are all sized at the A-tier
    # minimum (0.30) regardless of grade — no upward interpolation.
    engine = RiskEngine()
    state = engine.evaluate_daily_state(daily_pnl_pct=0, consecutive_losses=0)

    a_plus = engine.assess_setup_score(100, "london", state)
    a = engine.assess_setup_score(80, "london", state)
    a_floor = engine.assess_setup_score(75, "london", state)

    assert a_plus.grade == "A+"  # grade label preserved
    assert a_plus.risk_pct == 0.30
    assert a.risk_pct == 0.30
    assert a_floor.risk_pct == 0.30


def test_setup_risk_assessment_uses_stricter_off_session_tiers():
    engine = RiskEngine()
    state = engine.evaluate_daily_state(daily_pnl_pct=0, consecutive_losses=0)

    off_c = engine.assess_setup_score(60, "off_session", state)
    off_b = engine.assess_setup_score(70, "off_session", state)
    no_trade = engine.assess_setup_score(59, "off_session", state)

    assert off_c.grade == "C"
    assert off_c.risk_pct == 0.05
    assert off_b.grade == "B"
    assert off_b.risk_pct == 0.15
    assert no_trade.grade == "NO_TRADE"
    assert no_trade.risk_pct == 0


def test_setup_risk_assessment_respects_drawdown_multiplier():
    engine = RiskEngine()
    state = engine.evaluate_daily_state(daily_pnl_pct=-2.1, consecutive_losses=0)

    assessment = engine.assess_setup_score(100, "london", state)

    # base risk is capped to the A-tier minimum (0.30), then the drawdown
    # multiplier (0.5 at -2.1%) is applied -> 0.15.
    assert assessment.base_risk_pct == 0.30
    assert assessment.risk_pct == 0.15


def test_max_concurrent_trades_blocks_new_trade():
    engine = RiskEngine()
    settings = engine.settings
    decision = engine.evaluate_trade_permission(
        TradePermissionRequest(
            bot_enabled=True,
            exchange_connected=True,
            live_requested=False,
            daily_pnl_pct=0,
            consecutive_losses=0,
            open_trades=settings.max_concurrent_trades,
            market_regime="good",
            session_tradable=True,
            coin_in_watchlist=True,
            liquid_enough=True,
            spread_bps=1,
            expected_net_profit_pct=0.2,
            setup_score=85,
            setup_grade="A",
            btc_eth_confirmed=True,
            risk_reward=1.5,
            position_size_valid=True,
            order_size_valid=True,
        )
    )

    assert decision.allowed is False
    assert "max concurrent trades reached" in decision.reasons


def test_trade_permission_names_loss_streak_shutdown_separately():
    engine = RiskEngine()
    decision = engine.evaluate_trade_permission(
        TradePermissionRequest(
            bot_enabled=True,
            exchange_connected=True,
            live_requested=False,
            daily_pnl_pct=0,
            consecutive_losses=4,
            recent_consecutive_losses=0,
            open_trades=0,
            market_regime="good",
            session_tradable=True,
            coin_in_watchlist=True,
            liquid_enough=True,
            spread_bps=1,
            expected_net_profit_pct=0.2,
            setup_score=90,
            setup_grade="A+",
            btc_eth_confirmed=True,
            risk_reward=1.5,
            position_size_valid=True,
            order_size_valid=True,
        )
    )

    assert decision.allowed is False
    assert "loss-streak stop reached for this session" in decision.reasons
    assert "daily hard loss limit reached" not in decision.reasons


def test_trade_permission_respects_effective_score_threshold():
    engine = RiskEngine()
    decision = engine.evaluate_trade_permission(
        TradePermissionRequest(
            bot_enabled=True,
            exchange_connected=True,
            live_requested=False,
            daily_pnl_pct=0,
            consecutive_losses=0,
            open_trades=0,
            market_regime="good",
            session_tradable=True,
            coin_in_watchlist=True,
            liquid_enough=True,
            spread_bps=1,
            expected_net_profit_pct=0.2,
            setup_score=65,
            setup_grade="B",
            btc_eth_confirmed=True,
            risk_reward=1.5,
            position_size_valid=True,
            order_size_valid=True,
            setup_score_threshold=70,
        )
    )

    assert decision.allowed is False
    assert "setup score is below required threshold 70" in decision.reasons


def test_unclear_regime_is_blocked_by_default():
    engine = RiskEngine(Settings())
    decision = engine.evaluate_trade_permission(
        TradePermissionRequest(
            bot_enabled=True,
            exchange_connected=True,
            live_requested=False,
            daily_pnl_pct=0,
            consecutive_losses=0,
            open_trades=0,
            market_regime="unclear",
            session_tradable=True,
            coin_in_watchlist=True,
            liquid_enough=True,
            spread_bps=1,
            expected_net_profit_pct=0.2,
            setup_score=95,
            setup_grade="A+",
            btc_eth_confirmed=True,
            risk_reward=1.5,
            position_size_valid=True,
            order_size_valid=True,
        )
    )

    assert decision.allowed is False
    assert "market regime is not tradable" in decision.reasons


def test_unclear_regime_can_be_explicitly_allowed():
    engine = RiskEngine(Settings(allow_unclear_regime_trading=True))
    decision = engine.evaluate_trade_permission(
        TradePermissionRequest(
            bot_enabled=True,
            exchange_connected=True,
            live_requested=False,
            daily_pnl_pct=0,
            consecutive_losses=0,
            open_trades=0,
            market_regime="unclear",
            session_tradable=True,
            coin_in_watchlist=True,
            liquid_enough=True,
            spread_bps=1,
            expected_net_profit_pct=0.2,
            setup_score=95,
            setup_grade="A+",
            btc_eth_confirmed=True,
            risk_reward=1.5,
            position_size_valid=True,
            order_size_valid=True,
        )
    )

    assert decision.allowed is True


def _allowed_request(**overrides) -> TradePermissionRequest:
    base = dict(
        bot_enabled=True,
        exchange_connected=True,
        live_requested=False,
        daily_pnl_pct=0,
        consecutive_losses=0,
        open_trades=0,
        market_regime="good",
        session_tradable=True,
        coin_in_watchlist=True,
        liquid_enough=True,
        spread_bps=1,
        expected_net_profit_pct=0.2,
        setup_score=85,
        setup_grade="A",
        btc_eth_confirmed=True,
        risk_reward=1.5,
        position_size_valid=True,
        order_size_valid=True,
    )
    base.update(overrides)
    return TradePermissionRequest(**base)


def test_leader_confirmation_required_by_default_blocks_unconfirmed():
    # Classic behavior unchanged: default leader_confirmation_required=True.
    decision = RiskEngine().evaluate_trade_permission(_allowed_request(btc_eth_confirmed=False))
    assert "BTC/ETH confirmation is invalid" in decision.reasons


def test_leader_confirmation_exemption_allows_unconfirmed_entry():
    # Mean-reversion engines set leader_confirmation_required=False -> the gate is
    # relaxed (the real btc_eth_confirmed value still flows to exposure separately).
    decision = RiskEngine().evaluate_trade_permission(
        _allowed_request(btc_eth_confirmed=False, leader_confirmation_required=False)
    )
    assert "BTC/ETH confirmation is invalid" not in decision.reasons
    assert decision.allowed is True
