from __future__ import annotations

from dataclasses import dataclass, field

from app.indicators.technical import IndicatorSnapshot


@dataclass(slots=True)
class RegimeInput:
    btc: IndicatorSnapshot
    eth: IndicatorSnapshot
    average_spread_bps: float
    moving_coin_ratio: float
    abnormal_wick_ratio: float
    correlation_risk: float
    btc_dominance_change: float = 0.0
    total_market_trend_score: float | None = None


@dataclass(slots=True)
class RegimeResult:
    regime: str
    score: int
    tradable: bool
    max_daily_trades: int
    reasons: list[str] = field(default_factory=list)


class MarketRegimeDetector:
    """Classifies market conditions from dangerous to hot."""

    def classify(self, data: RegimeInput) -> RegimeResult:
        score = 0
        reasons: list[str] = []

        btc_trend = data.btc.trend_strength_score
        eth_trend = data.eth.trend_strength_score
        if btc_trend >= 70 and eth_trend >= 65:
            score += 25
            reasons.append("BTC and ETH trend strength aligned")
        elif btc_trend >= 55 or eth_trend >= 55:
            score += 12
            reasons.append("partial BTC/ETH trend support")
        else:
            reasons.append("BTC/ETH trend support is weak")

        if data.btc.relative_volume > 1.2 or data.eth.relative_volume > 1.2:
            score += 15
            reasons.append("leader volume expansion")
        if data.btc.volatility_expansion > 1.1 or data.eth.volatility_expansion > 1.1:
            score += 15
            reasons.append("volatility expansion present")
        if data.moving_coin_ratio >= 0.55:
            score += 15
            reasons.append("broad market participation")
        elif data.moving_coin_ratio < 0.25:
            score -= 15
            reasons.append("few top coins are moving together")

        if data.average_spread_bps <= 6:
            score += 10
            reasons.append("spread conditions are clean")
        elif data.average_spread_bps > 15:
            score -= 20
            reasons.append("spread conditions are dangerous")

        if data.abnormal_wick_ratio > 0.35:
            score -= 20
            reasons.append("abnormal wick risk detected")
        if data.correlation_risk > 0.8:
            score -= 10
            reasons.append("correlation risk is elevated")
        if data.total_market_trend_score is not None:
            score += int((data.total_market_trend_score - 50) / 5)
        if data.btc.close < data.btc.ema_200 and data.eth.close < data.eth.ema_200:
            score -= 20
            reasons.append("BTC and ETH below EMA200")

        score = max(0, min(100, score + 30))
        if score < 20:
            regime, tradable, max_trades = "dangerous", False, 0
        elif score < 35:
            regime, tradable, max_trades = "bad", False, 5
        elif score < 50:
            regime, tradable, max_trades = "unclear", True, 8
        elif score < 68:
            regime, tradable, max_trades = "good", True, 30
        elif score < 84:
            regime, tradable, max_trades = "strong", True, 40
        else:
            regime, tradable, max_trades = "hot", True, 50

        return RegimeResult(regime=regime, score=score, tradable=tradable, max_daily_trades=max_trades, reasons=reasons)
