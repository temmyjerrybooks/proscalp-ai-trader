from __future__ import annotations

from app.strategies.base_strategy import BaseStrategy, StrategyContext, StrategySignal


class BTCLedAltcoinContinuationStrategy(BaseStrategy):
    name = "BTC-led altcoin continuation scalp"

    def evaluate(self, context: StrategyContext) -> StrategySignal:
        candles = self.candles(context, "3m")
        snapshot = self.snapshot(context, "3m")
        if len(candles) < 40 or not snapshot:
            return self.reject(context, ["insufficient 3m candles for BTC-led continuation"])
        last = candles[-1]
        if context.btc_direction == "long" and snapshot.ema_9 > snapshot.ema_21 and snapshot.relative_volume >= 1.15:
            return self.build_signal(
                context,
                "long",
                last.close,
                min(last.low, snapshot.ema_21),
                82 + min(context.coin_strength_score / 10, 10),
                ["BTC leading upside move", "altcoin trend aligns", "relative volume confirms participation"],
            )
        if (
            context.allow_short
            and context.btc_direction == "short"
            and snapshot.ema_9 < snapshot.ema_21
            and snapshot.relative_volume >= 1.15
        ):
            return self.build_signal(
                context,
                "short",
                last.close,
                max(last.high, snapshot.ema_21),
                82 + min(context.coin_strength_score / 10, 10),
                ["BTC leading downside move", "altcoin trend aligns", "relative volume confirms participation"],
            )
        return self.reject(context, ["BTC-led altcoin continuation not confirmed"])
