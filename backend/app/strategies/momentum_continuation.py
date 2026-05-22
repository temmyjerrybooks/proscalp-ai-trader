from __future__ import annotations

from app.strategies.base_strategy import BaseStrategy, StrategyContext, StrategySignal


class MomentumContinuationStrategy(BaseStrategy):
    name = "Momentum continuation scalp"

    def evaluate(self, context: StrategyContext) -> StrategySignal:
        candles = self.candles(context, "3m")
        snapshot = self.snapshot(context, "3m")
        if len(candles) < 50 or not snapshot:
            return self.reject(context, ["insufficient 3m candles for momentum continuation"])
        last = candles[-1]
        bull = snapshot.macd_histogram > 0 and snapshot.ema_9 > snapshot.ema_21 and last.close > snapshot.vwap
        bear = snapshot.macd_histogram < 0 and snapshot.ema_9 < snapshot.ema_21 and last.close < snapshot.vwap and context.allow_short
        if bull and snapshot.relative_volume >= 1.3 and self.aligned_with_leaders(context, "long"):
            return self.build_signal(
                context,
                "long",
                last.close,
                min(snapshot.ema_21, last.low),
                86 + min(snapshot.relative_volume * 4, 10),
                ["momentum expansion", "MACD supports continuation", "BTC/ETH leader confirmation"],
            )
        if bear and snapshot.relative_volume >= 1.3 and self.aligned_with_leaders(context, "short"):
            return self.build_signal(
                context,
                "short",
                last.close,
                max(snapshot.ema_21, last.high),
                86 + min(snapshot.relative_volume * 4, 10),
                ["downside momentum expansion", "MACD supports continuation", "BTC/ETH leader confirmation"],
            )
        return self.reject(context, ["momentum continuation lacks volume or leader confirmation"])
