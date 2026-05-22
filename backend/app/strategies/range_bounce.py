from __future__ import annotations

from app.strategies.base_strategy import BaseStrategy, StrategyContext, StrategySignal


class RangeBounceStrategy(BaseStrategy):
    name = "Range bounce scalp"

    def evaluate(self, context: StrategyContext) -> StrategySignal:
        candles = self.candles(context)
        snapshot = self.snapshot(context)
        if len(candles) < 40 or not snapshot:
            return self.reject(context, ["insufficient 5m candles for range bounce"])
        last = candles[-1]
        range_high = max(candle.high for candle in candles[-35:])
        range_low = min(candle.low for candle in candles[-35:])
        range_size = max(range_high - range_low, 1e-12)
        near_low = (last.close - range_low) / range_size <= 0.18
        near_high = (range_high - last.close) / range_size <= 0.18
        if near_low and snapshot.rsi <= 42 and last.close > last.open:
            return self.build_signal(
                context,
                "long",
                last.close,
                min(last.low, range_low),
                70 + (8 if context.order_book_imbalance > 0 else 0),
                ["price bounced near range low", "RSI supports mean reversion", "bullish close from support"],
            )
        if near_high and snapshot.rsi >= 58 and last.close < last.open and context.allow_short:
            return self.build_signal(
                context,
                "short",
                last.close,
                max(last.high, range_high),
                70 + (8 if context.order_book_imbalance < 0 else 0),
                ["price rejected near range high", "RSI supports mean reversion", "bearish close from resistance"],
            )
        return self.reject(context, ["range bounce conditions not present"])
