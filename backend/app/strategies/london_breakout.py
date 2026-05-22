from __future__ import annotations

from app.config.settings import get_settings
from app.strategies.base_strategy import BaseStrategy, StrategyContext, StrategySignal


class LondonBreakoutStrategy(BaseStrategy):
    name = "London open breakout"

    def __init__(self, enabled: bool | None = None) -> None:
        # Defaults to the settings flag; callers (backtests/tests) may force-enable.
        self.enabled = get_settings().london_open_breakout_enabled if enabled is None else enabled

    def evaluate(self, context: StrategyContext) -> StrategySignal:
        if not self.enabled:
            return self.reject(context, ["London open breakout disabled by configuration"])
        candles = self.candles(context)
        snapshot = self.snapshot(context)
        if len(candles) < 30 or not snapshot:
            return self.reject(context, ["insufficient 5m candles for London breakout"])
        if context.session_name != "london":
            return self.reject(context, ["not London session"])
        if context.asian_high is None or context.asian_low is None:
            return self.reject(context, ["Asian high/low not available"])

        last = candles[-1]
        breakout_long = last.close > context.asian_high and snapshot.relative_volume >= 1.2
        breakout_short = last.close < context.asian_low and snapshot.relative_volume >= 1.2 and context.allow_short
        if breakout_long:
            stop = min(context.asian_high, last.low, snapshot.ema_21)
            confidence = 78 + min(snapshot.relative_volume * 6, 14) + (8 if self.aligned_with_leaders(context, "long") else 0)
            return self.build_signal(
                context,
                "long",
                last.close,
                stop,
                confidence,
                ["broke above Asian high", "relative volume confirms breakout", "London liquidity window active"],
            )
        if breakout_short:
            stop = max(context.asian_low, last.high, snapshot.ema_21)
            confidence = 78 + min(snapshot.relative_volume * 6, 14) + (8 if self.aligned_with_leaders(context, "short") else 0)
            return self.build_signal(
                context,
                "short",
                last.close,
                stop,
                confidence,
                ["broke below Asian low", "relative volume confirms breakdown", "London liquidity window active"],
            )
        return self.reject(context, ["no clean Asian range breakout", "volume confirmation missing"])
