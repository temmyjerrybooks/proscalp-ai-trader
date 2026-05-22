from __future__ import annotations

from app.strategies.base_strategy import BaseStrategy, StrategyContext, StrategySignal


class USOpenBreakoutStrategy(BaseStrategy):
    name = "US open breakout"

    def evaluate(self, context: StrategyContext) -> StrategySignal:
        candles = self.candles(context)
        snapshot = self.snapshot(context)
        if len(candles) < 30 or not snapshot:
            return self.reject(context, ["insufficient 5m candles for US open breakout"])
        if context.session_name != "new_york":
            return self.reject(context, ["not New York session"])
        high = context.intraday_high or max(candle.high for candle in candles[-30:])
        low = context.intraday_low or min(candle.low for candle in candles[-30:])
        last = candles[-1]
        if last.close > high and snapshot.relative_volume >= 1.25:
            return self.build_signal(
                context,
                "long",
                last.close,
                min(high, snapshot.ema_21),
                80 + (10 if self.aligned_with_leaders(context, "long") else 0),
                ["US open range breakout", "relative volume expansion", "leader confirmation checked"],
            )
        if last.close < low and snapshot.relative_volume >= 1.25 and context.allow_short:
            return self.build_signal(
                context,
                "short",
                last.close,
                max(low, snapshot.ema_21),
                80 + (10 if self.aligned_with_leaders(context, "short") else 0),
                ["US open range breakdown", "relative volume expansion", "leader confirmation checked"],
            )
        return self.reject(context, ["no US open range break with volume"])
