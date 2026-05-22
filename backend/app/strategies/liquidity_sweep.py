from __future__ import annotations

from app.strategies.base_strategy import BaseStrategy, StrategyContext, StrategySignal


class LiquiditySweepStrategy(BaseStrategy):
    name = "Liquidity sweep reversal"

    def evaluate(self, context: StrategyContext) -> StrategySignal:
        candles = self.candles(context, "1m")
        snapshot = self.snapshot(context, "1m")
        if len(candles) < 40 or not snapshot:
            return self.reject(context, ["insufficient 1m candles for liquidity sweep"])
        last = candles[-1]
        prior = candles[-30:-1]
        prior_low = min(candle.low for candle in prior)
        prior_high = max(candle.high for candle in prior)
        swept_low = last.low < prior_low and last.close > prior_low and snapshot.body_wick_ratio < 1.5
        swept_high = last.high > prior_high and last.close < prior_high and snapshot.body_wick_ratio < 1.5
        if swept_low and snapshot.rsi > 35:
            return self.build_signal(
                context,
                "long",
                last.close,
                last.low,
                76 + (8 if context.order_book_imbalance > 0 else 0),
                ["liquidity swept below range", "price reclaimed prior low", "wick profile suggests reversal"],
            )
        if swept_high and context.allow_short and snapshot.rsi < 65:
            return self.build_signal(
                context,
                "short",
                last.close,
                last.high,
                76 + (8 if context.order_book_imbalance < 0 else 0),
                ["liquidity swept above range", "price rejected prior high", "wick profile suggests reversal"],
            )
        return self.reject(context, ["no sweep and reclaim pattern"])
