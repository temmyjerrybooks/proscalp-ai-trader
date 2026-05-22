from __future__ import annotations

from dataclasses import dataclass

from app.config.settings import Settings, get_settings


@dataclass(slots=True)
class SetupScoreInput:
    market_regime_score: float
    session_timing_score: float
    coin_strength_score: float
    volume_confirmation_score: float
    trend_alignment_score: float
    liquidity_orderbook_score: float
    risk_reward_score: float
    btc_eth_confirmation_score: float
    spread_slippage_score: float


@dataclass(slots=True)
class SetupScoreResult:
    total: int
    grade: str
    permission: str
    breakdown: dict[str, float]
    rejection_reasons: list[str]


class SetupScoringEngine:
    """Scores a trade setup from 0 to 100 using the requested weighting model."""

    weights = {
        "market_regime": 20,
        "session_timing": 15,
        "coin_strength": 15,
        "volume_confirmation": 10,
        "trend_alignment": 10,
        "liquidity_orderbook": 10,
        "risk_reward": 10,
        "btc_eth_confirmation": 5,
        "spread_slippage": 5,
    }

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def score(self, data: SetupScoreInput) -> SetupScoreResult:
        raw_breakdown = {
            "market_regime": data.market_regime_score,
            "session_timing": data.session_timing_score,
            "coin_strength": data.coin_strength_score,
            "volume_confirmation": data.volume_confirmation_score,
            "trend_alignment": data.trend_alignment_score,
            "liquidity_orderbook": data.liquidity_orderbook_score,
            "risk_reward": data.risk_reward_score,
            "btc_eth_confirmation": data.btc_eth_confirmation_score,
            "spread_slippage": data.spread_slippage_score,
        }
        weighted = {
            key: max(0.0, min(1.0, value / 100.0)) * self.weights[key]
            for key, value in raw_breakdown.items()
        }
        total = int(round(sum(weighted.values())))
        rejection_reasons: list[str] = []
        if total >= self.settings.normal_score_threshold_aplus:
            grade = "A+"
            permission = "aggressive entry allowed"
        elif total >= self.settings.normal_score_threshold_a:
            grade = "A"
            permission = "normal entry"
        elif total >= self.settings.normal_score_threshold_b:
            grade = "B"
            permission = "small entry"
        elif total >= self.settings.normal_score_threshold_c:
            grade = "C"
            permission = "tiny entry"
        else:
            grade = "NO_TRADE"
            permission = "reject"
            rejection_reasons.append("setup score below C threshold")

        weak_components = [key for key, value in raw_breakdown.items() if value < 45]
        if weak_components:
            rejection_reasons.extend(f"weak {component.replace('_', ' ')}" for component in weak_components)

        return SetupScoreResult(
            total=total,
            grade=grade,
            permission=permission,
            breakdown={key: round(value, 2) for key, value in weighted.items()},
            rejection_reasons=rejection_reasons,
        )
