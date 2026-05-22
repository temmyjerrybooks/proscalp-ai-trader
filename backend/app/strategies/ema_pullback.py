from __future__ import annotations

from app.strategies.base_strategy import BaseStrategy, StrategyContext, StrategySignal


class EMAPullbackStrategy(BaseStrategy):
    name = "EMA pullback scalp"

    def evaluate(self, context: StrategyContext) -> StrategySignal:
        candles = self.candles(context)
        snapshot = self.snapshot(context)
        if len(candles) < 50 or not snapshot:
            return self.reject(context, ["insufficient 5m candles for EMA pullback"])
        last = candles[-1]
        near_ema = abs(last.close - snapshot.ema_21) / last.close * 100 <= 0.35
        bull_stack = snapshot.ema_9 > snapshot.ema_21 > snapshot.ema_50 and last.close > snapshot.ema_21
        bear_stack = snapshot.ema_9 < snapshot.ema_21 < snapshot.ema_50 and last.close < snapshot.ema_21 and context.allow_short
        if near_ema and bull_stack and snapshot.trend_strength_score >= 62:
            return self.build_signal(
                context,
                "long",
                last.close,
                min(snapshot.ema_50, last.low),
                72 + min(snapshot.trend_strength_score / 4, 20),
                ["pullback held EMA21", "EMA trend stack is bullish", "trend strength is sufficient"],
            )
        if near_ema and bear_stack and snapshot.trend_strength_score >= 62:
            return self.build_signal(
                context,
                "short",
                last.close,
                max(snapshot.ema_50, last.high),
                72 + min(snapshot.trend_strength_score / 4, 20),
                ["pullback rejected EMA21", "EMA trend stack is bearish", "trend strength is sufficient"],
            )
        return self.reject(context, ["EMA pullback is not clean"])
