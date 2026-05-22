from __future__ import annotations

from app.strategies.base_strategy import BaseStrategy, StrategyContext, StrategySignal


class VWAPReclaimStrategy(BaseStrategy):
    name = "VWAP reclaim scalp"

    def evaluate(self, context: StrategyContext) -> StrategySignal:
        candles = self.candles(context, "3m")
        snapshot = self.snapshot(context, "3m")
        if len(candles) < 25 or not snapshot:
            return self.reject(context, ["insufficient 3m candles for VWAP reclaim"])
        previous = candles[-2]
        last = candles[-1]
        reclaimed = previous.close < snapshot.vwap < last.close
        rejected = previous.close > snapshot.vwap > last.close and context.allow_short
        if reclaimed and snapshot.rsi > 48 and snapshot.relative_volume >= 1.1:
            return self.build_signal(
                context,
                "long",
                last.close,
                min(last.low, snapshot.vwap, snapshot.ema_21),
                74 + min(snapshot.relative_volume * 7, 16),
                ["price reclaimed VWAP", "RSI supports reclaim", "volume validates scalp attempt"],
            )
        if rejected and snapshot.rsi < 52 and snapshot.relative_volume >= 1.1:
            return self.build_signal(
                context,
                "short",
                last.close,
                max(last.high, snapshot.vwap, snapshot.ema_21),
                74 + min(snapshot.relative_volume * 7, 16),
                ["price lost VWAP", "RSI supports rejection", "volume validates scalp attempt"],
            )
        return self.reject(context, ["VWAP reclaim/rejection not confirmed"])
