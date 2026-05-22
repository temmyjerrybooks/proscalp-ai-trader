from __future__ import annotations

from app.strategies.base_strategy import BaseStrategy, StrategyContext, StrategySignal


class BreakoutRetestStrategy(BaseStrategy):
    name = "Breakout and retest scalp"

    def evaluate(self, context: StrategyContext) -> StrategySignal:
        candles = self.candles(context)
        snapshot = self.snapshot(context)
        if len(candles) < 35 or not snapshot:
            return self.reject(context, ["insufficient 5m candles for breakout retest"])
        last = candles[-1]
        previous_range = candles[-25:-5]
        resistance = max(candle.high for candle in previous_range)
        support = min(candle.low for candle in previous_range)
        retest_long = last.low <= resistance <= last.close and last.close > snapshot.vwap
        retest_short = last.high >= support >= last.close and last.close < snapshot.vwap and context.allow_short
        if retest_long and snapshot.relative_volume >= 0.9:
            return self.build_signal(
                context,
                "long",
                last.close,
                min(resistance, last.low),
                73 + min(snapshot.trend_strength_score / 5, 18),
                ["prior resistance retested as support", "price above VWAP", "volume acceptable"],
            )
        if retest_short and snapshot.relative_volume >= 0.9:
            return self.build_signal(
                context,
                "short",
                last.close,
                max(support, last.high),
                73 + min(snapshot.trend_strength_score / 5, 18),
                ["prior support retested as resistance", "price below VWAP", "volume acceptable"],
            )
        return self.reject(context, ["breakout retest level did not hold"])
