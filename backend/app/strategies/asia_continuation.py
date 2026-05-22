from __future__ import annotations

from app.strategies.base_strategy import BaseStrategy, StrategyContext, StrategySignal


class AsiaContinuationStrategy(BaseStrategy):
    name = "Asia-to-London continuation"

    def evaluate(self, context: StrategyContext) -> StrategySignal:
        candles = self.candles(context, "15m")
        snapshot = self.snapshot(context, "15m")
        if len(candles) < 40 or not snapshot:
            return self.reject(context, ["insufficient 15m candles for Asia continuation"])
        if context.session_name not in {"asia", "london"}:
            return self.reject(context, ["not Asia or London continuation window"])
        last = candles[-1]
        bull = last.close > snapshot.vwap and snapshot.ema_9 > snapshot.ema_21 > snapshot.ema_50
        bear = last.close < snapshot.vwap and snapshot.ema_9 < snapshot.ema_21 < snapshot.ema_50 and context.allow_short
        if bull and snapshot.relative_volume >= 1.05:
            return self.build_signal(
                context,
                "long",
                last.close,
                min(snapshot.ema_21, last.low),
                76 + min(snapshot.trend_strength_score / 5, 18),
                ["Asia trend structure continuing", "price holds above VWAP", "EMA stack supports long continuation"],
            )
        if bear and snapshot.relative_volume >= 1.05:
            return self.build_signal(
                context,
                "short",
                last.close,
                max(snapshot.ema_21, last.high),
                76 + min(snapshot.trend_strength_score / 5, 18),
                ["Asia downside structure continuing", "price holds below VWAP", "EMA stack supports short continuation"],
            )
        return self.reject(context, ["Asia trend is not clean enough for continuation"])
