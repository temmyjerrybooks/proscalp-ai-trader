from __future__ import annotations

from dataclasses import dataclass, field

from app.config.settings import Settings, TradingMode, get_settings


@dataclass(slots=True)
class DailyRiskState:
    allow_new_trades: bool
    warning_mode: bool
    reduce_size: bool
    aplus_only: bool
    hard_shutdown: bool
    daily_hard_shutdown: bool
    loss_streak_shutdown: bool
    cooldown_required: bool
    risk_multiplier: float
    reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PositionSizeResult:
    quantity: float
    notional: float
    risk_amount: float
    risk_pct: float
    leverage: int
    valid: bool
    reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SetupRiskAssessment:
    score: int
    grade: str
    permission: str
    session_name: str
    score_floor: int
    score_ceiling: int
    risk_range: tuple[float, float]
    base_risk_pct: float
    risk_pct: float


@dataclass(slots=True)
class TradePermissionRequest:
    bot_enabled: bool
    exchange_connected: bool
    live_requested: bool
    daily_pnl_pct: float
    consecutive_losses: int
    open_trades: int
    market_regime: str
    session_tradable: bool
    coin_in_watchlist: bool
    liquid_enough: bool
    spread_bps: float
    expected_net_profit_pct: float
    setup_score: int
    setup_grade: str
    btc_eth_confirmed: bool
    risk_reward: float
    position_size_valid: bool
    order_size_valid: bool
    setup_score_threshold: int | None = None
    recent_consecutive_losses: int | None = None
    leader_confirmation_required: bool = True


@dataclass(slots=True)
class RiskDecision:
    allowed: bool
    action: str
    reasons: list[str]


class RiskEngine:
    """Capital protection, kill switches, and position sizing."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def evaluate_daily_state(
        self,
        daily_pnl_pct: float,
        consecutive_losses: int,
        recent_consecutive_losses: int | None = None,
    ) -> DailyRiskState:
        reasons: list[str] = []
        warning_mode = daily_pnl_pct <= self.settings.daily_warning_loss_pct
        reduce_size = daily_pnl_pct <= self.settings.daily_reduce_size_loss_pct
        aplus_only = daily_pnl_pct <= self.settings.daily_aplus_only_loss_pct
        daily_hard_shutdown = daily_pnl_pct <= self.settings.daily_hard_loss_limit_pct
        loss_streak_shutdown = consecutive_losses >= self.settings.consecutive_loss_stop_after
        recent_losses = consecutive_losses if recent_consecutive_losses is None else recent_consecutive_losses
        cooldown_required = recent_losses >= self.settings.consecutive_loss_pause_after
        hard_shutdown = daily_hard_shutdown or loss_streak_shutdown

        risk_multiplier = 1.0
        if warning_mode:
            reasons.append("daily loss warning threshold reached")
        if reduce_size or consecutive_losses >= self.settings.consecutive_loss_reduce_after:
            risk_multiplier *= 0.5
            reasons.append("risk reduced after drawdown or consecutive losses")
        if aplus_only:
            risk_multiplier *= 0.35
            reasons.append("A+ setups only after deeper daily drawdown")
        if cooldown_required:
            risk_multiplier *= 0.25
            reasons.append("cooldown required after consecutive losses")
        if loss_streak_shutdown:
            reasons.append(f"loss-streak stop reached after {consecutive_losses} consecutive losses")
        if daily_hard_shutdown:
            reasons.append("daily hard loss shutdown")

        return DailyRiskState(
            allow_new_trades=not hard_shutdown and not cooldown_required,
            warning_mode=warning_mode,
            reduce_size=reduce_size,
            aplus_only=aplus_only,
            hard_shutdown=hard_shutdown,
            daily_hard_shutdown=daily_hard_shutdown,
            loss_streak_shutdown=loss_streak_shutdown,
            cooldown_required=cooldown_required,
            risk_multiplier=risk_multiplier,
            reasons=reasons,
        )

    def assess_setup_score(
        self,
        setup_score: int,
        session_name: str,
        daily_state: DailyRiskState,
    ) -> SetupRiskAssessment:
        """Map setup score to a session-aware grade and interpolated risk."""
        score = max(0, min(100, int(round(setup_score))))
        normalized_session = "off_session" if session_name == "off_session" else "normal_session"
        tiers = self._risk_tiers_for_session(normalized_session)

        for index, (grade, floor, risk_min, risk_max, permission) in enumerate(tiers):
            ceiling = 100 if index == 0 else max(floor, tiers[index - 1][1] - 1)
            if score >= floor:
                base_risk = self._interpolate_risk(score, floor, ceiling, risk_min, risk_max)
                if score >= self.settings.cap_risk_at_score:
                    # Phase 2A: do not size up on high-confidence setups (A+ shows
                    # 20% win rate / -$0.95 avg on real money). Clamp to the A-tier
                    # minimum risk so every score >= cap gets the score-75 sizing.
                    a_tier_risk_min = next((tier[2] for tier in tiers if tier[0] == "A"), risk_min)
                    base_risk = min(base_risk, a_tier_risk_min)
                return SetupRiskAssessment(
                    score=score,
                    grade=grade,
                    permission=permission,
                    session_name=normalized_session,
                    score_floor=floor,
                    score_ceiling=ceiling,
                    risk_range=(risk_min, risk_max),
                    base_risk_pct=base_risk,
                    risk_pct=round(base_risk * daily_state.risk_multiplier, 4),
                )

        minimum = self.minimum_score_for_session(session_name)
        return SetupRiskAssessment(
            score=score,
            grade="NO_TRADE",
            permission="reject",
            session_name=normalized_session,
            score_floor=minimum,
            score_ceiling=100,
            risk_range=(0.0, 0.0),
            base_risk_pct=0.0,
            risk_pct=0.0,
        )

    def minimum_score_for_session(self, session_name: str) -> int:
        if session_name == "off_session":
            return self.settings.off_session_score_threshold_c
        return self.settings.normal_score_threshold_c

    def score_threshold_for_grade(self, grade: str, session_name: str) -> int:
        normalized_session = "off_session" if session_name == "off_session" else "normal_session"
        for tier_grade, floor, *_ in self._risk_tiers_for_session(normalized_session):
            if tier_grade == grade:
                return floor
        return self.minimum_score_for_session(session_name)

    def _risk_tiers_for_session(self, normalized_session: str) -> list[tuple[str, int, float, float, str]]:
        if normalized_session == "off_session":
            return [
                (
                    "A+",
                    self.settings.off_session_score_threshold_aplus,
                    self.settings.off_session_risk_aplus_min_pct,
                    self.settings.off_session_risk_aplus_max_pct,
                    "elite off-session entry",
                ),
                (
                    "A",
                    self.settings.off_session_score_threshold_a,
                    self.settings.off_session_risk_a_min_pct,
                    self.settings.off_session_risk_a_max_pct,
                    "selective off-session entry",
                ),
                (
                    "B",
                    self.settings.off_session_score_threshold_b,
                    self.settings.off_session_risk_b_min_pct,
                    self.settings.off_session_risk_b_max_pct,
                    "small off-session entry",
                ),
                (
                    "C",
                    self.settings.off_session_score_threshold_c,
                    self.settings.off_session_risk_c_min_pct,
                    self.settings.off_session_risk_c_max_pct,
                    "tiny off-session entry",
                ),
            ]
        return [
            (
                "A+",
                self.settings.normal_score_threshold_aplus,
                self.settings.normal_risk_aplus_min_pct,
                self.settings.normal_risk_aplus_max_pct,
                "aggressive entry allowed",
            ),
            (
                "A",
                self.settings.normal_score_threshold_a,
                self.settings.normal_risk_a_min_pct,
                self.settings.normal_risk_a_max_pct,
                "normal entry",
            ),
            (
                "B",
                self.settings.normal_score_threshold_b,
                self.settings.normal_risk_b_min_pct,
                self.settings.normal_risk_b_max_pct,
                "small entry",
            ),
            (
                "C",
                self.settings.normal_score_threshold_c,
                self.settings.normal_risk_c_min_pct,
                self.settings.normal_risk_c_max_pct,
                "tiny entry",
            ),
        ]

    @staticmethod
    def _interpolate_risk(score: int, floor: int, ceiling: int, risk_min: float, risk_max: float) -> float:
        if ceiling <= floor:
            return round(risk_max, 4)
        capped_score = min(max(score, floor), ceiling)
        fraction = (capped_score - floor) / (ceiling - floor)
        return round(risk_min + (risk_max - risk_min) * fraction, 4)

    def calculate_position_size(
        self,
        account_equity: float,
        risk_pct: float,
        entry_price: float,
        stop_loss: float,
        leverage: int = 1,
        fee_bps: float | None = None,
        slippage_bps: float | None = None,
        min_notional: float = 5.0,
    ) -> PositionSizeResult:
        reasons: list[str] = []
        if account_equity <= 0:
            return PositionSizeResult(0, 0, 0, risk_pct, leverage, False, ["account equity must be positive"])
        stop_distance = abs(entry_price - stop_loss)
        if entry_price <= 0 or stop_distance <= 0:
            return PositionSizeResult(0, 0, 0, risk_pct, leverage, False, ["invalid entry or stop distance"])

        leverage = min(max(1, leverage), self.settings.max_leverage)
        risk_amount = account_equity * (risk_pct / 100)
        cost_bps = (fee_bps if fee_bps is not None else self.settings.fee_rate_bps) * 2
        cost_bps += slippage_bps if slippage_bps is not None else self.settings.slippage_bps
        adjusted_stop_distance = stop_distance + entry_price * (cost_bps / 10_000)
        quantity = risk_amount / adjusted_stop_distance
        # Exposure caps are based on actual position notional. Leverage controls
        # margin usage, but it must not inflate the maximum exposure gate.
        max_notional = account_equity * (self.settings.max_coin_exposure_pct / 100)
        notional = min(quantity * entry_price, max_notional)
        quantity = notional / entry_price

        valid = quantity > 0 and notional >= min_notional
        if not valid:
            reasons.append("position is below exchange minimum order size")
        if leverage > self.settings.default_leverage and self.settings.max_leverage <= leverage:
            reasons.append("leverage capped by max leverage guard")

        return PositionSizeResult(
            quantity=round(quantity, 8),
            notional=round(notional, 4),
            risk_amount=round(risk_amount, 4),
            risk_pct=risk_pct,
            leverage=leverage,
            valid=valid,
            reasons=reasons,
        )

    def evaluate_trade_permission(self, request: TradePermissionRequest) -> RiskDecision:
        reasons: list[str] = []
        daily_state = self.evaluate_daily_state(
            request.daily_pnl_pct,
            request.consecutive_losses,
            recent_consecutive_losses=request.recent_consecutive_losses,
        )
        setup_threshold = request.setup_score_threshold or self.settings.normal_score_threshold_c

        checks = [
            (request.bot_enabled, "bot is disabled"),
            (self._live_trading_allowed(request.live_requested), "live trading is not explicitly enabled"),
            (request.exchange_connected, "exchange is not connected"),
            (not daily_state.daily_hard_shutdown, "daily hard loss limit reached"),
            (not daily_state.loss_streak_shutdown, "loss-streak stop reached for this session"),
            (request.open_trades < self.settings.max_concurrent_trades, "max concurrent trades reached"),
            (request.market_regime.lower() in self._tradable_regimes(), "market regime is not tradable"),
            (request.session_tradable, "current session is not tradable"),
            (request.coin_in_watchlist, "coin is not in approved top-50 watchlist"),
            (request.liquid_enough, "coin is not liquid enough"),
            (request.spread_bps <= self.settings.max_spread_bps, "spread is too wide"),
            (request.expected_net_profit_pct > 0, "expected move does not beat fees and slippage"),
            (request.setup_score >= setup_threshold, f"setup score is below required threshold {setup_threshold}"),
            (request.setup_grade != "NO_TRADE", "setup grade is not tradable"),
            (request.btc_eth_confirmed or not request.leader_confirmation_required, "BTC/ETH confirmation is invalid"),
            (request.risk_reward >= self.settings.min_risk_reward, "risk-reward is below minimum"),
            (request.position_size_valid, "position size is invalid"),
            (request.order_size_valid, "order size is below exchange minimum"),
        ]
        for passed, reason in checks:
            if not passed:
                reasons.append(reason)

        if daily_state.aplus_only and request.setup_grade != "A+":
            reasons.append("A+ setup required after daily drawdown")
        if daily_state.cooldown_required:
            reasons.append("cooldown required after consecutive losses")

        return RiskDecision(
            allowed=not reasons,
            action="place_order" if not reasons else "reject",
            reasons=reasons or ["all pre-trade checks passed"],
        )

    def _live_trading_allowed(self, live_requested: bool) -> bool:
        if not live_requested:
            return True
        if not self.settings.live_trading_enabled:
            return False
        if self.settings.trading_mode == TradingMode.LIVE_FUTURES and not self.settings.futures_trading_confirmed:
            return False
        return True

    def _tradable_regimes(self) -> set[str]:
        regimes = {"good", "strong", "hot"}
        if self.settings.allow_unclear_regime_trading:
            regimes.add("unclear")
        return regimes
