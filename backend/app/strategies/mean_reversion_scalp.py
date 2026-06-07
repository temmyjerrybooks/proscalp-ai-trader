"""Mean-reversion scalp — fade a coin's own over-extension (no Bitcoin comparison).

This is the strategy proven on historical klines (own-mean reversion wins ~66-69%
of trades; net-positive at maker fees). It deliberately judges each coin against
ITS OWN recent mean — not vs BTC — which is what killed the stat_arb all-shorts
bias (a Bitcoin move no longer makes every coin signal the same direction).

Logic (per the working timeframe, default 5m):
- mean = SMA(lookback) of close; atr = ATR(atr_period)
- deviation in ATR units = (price - mean) / atr
- LONG  when price is >= entry_z ATR BELOW its mean AND the last bar turns up
- SHORT when price is >= entry_z ATR ABOVE its mean AND the last bar turns down
- take-profit ladders back toward the mean (40/70/100% of the gap); stop sits
  beyond the extreme by stop_atr * ATR.
- Optional ranging filter (Kaufman Efficiency Ratio): only trade when the market
  is choppy (ER <= er_max), since reversion fails in strong trends. Off by default
  (at maker fees more trades = more profit; the filter mainly helps at taker fees).
"""

from __future__ import annotations

from app.indicators.technical import atr as atr_series
from app.strategies.base_strategy import BaseStrategy, StrategyContext, StrategySignal


def efficiency_ratio(closes: list[float], n: int) -> float:
    """Kaufman Efficiency Ratio over the last ``n`` bars: |net move| / |sum of bar
    moves|. ~1.0 = strong clean trend; ~0.0 = choppy/ranging. Mean reversion wants low."""
    if len(closes) < n + 1:
        return 1.0
    seg = closes[-(n + 1):]
    direction = abs(seg[-1] - seg[0])
    volatility = sum(abs(seg[i] - seg[i - 1]) for i in range(1, len(seg)))
    return direction / volatility if volatility > 0 else 1.0


class MeanReversionScalp(BaseStrategy):
    name = "mean_reversion_scalp"
    preferred_timeframe = "5m"

    def __init__(
        self,
        lookback: int = 20,
        atr_period: int = 14,
        entry_z: float = 2.0,
        stop_atr: float = 1.0,
        er_n: int = 20,
        er_max: float | None = None,
        timeframe: str = "5m",
    ) -> None:
        self.lookback = lookback
        self.atr_period = atr_period
        self.entry_z = entry_z
        self.stop_atr = stop_atr
        self.er_n = er_n
        self.er_max = er_max  # None -> ranging filter disabled
        self.preferred_timeframe = timeframe

    def evaluate(self, context: StrategyContext) -> StrategySignal:
        candles = self.candles(context)
        need = max(self.lookback, self.er_n) + self.atr_period + 2
        if len(candles) < need:
            return self.reject(context, ["insufficient history for mean reversion"])
        closes = [c.close for c in candles]

        if self.er_max is not None and efficiency_ratio(closes, self.er_n) > self.er_max:
            return self.reject(context, ["market trending - reversion skipped"])

        mean = sum(closes[-self.lookback:]) / self.lookback
        atr_values = atr_series(candles, self.atr_period)
        if not atr_values or atr_values[-1] <= 0:
            return self.reject(context, ["no usable ATR"])
        atrv = atr_values[-1]
        price, prev = closes[-1], closes[-2]
        dev_atr = (price - mean) / atrv

        if dev_atr <= -self.entry_z and price > prev:
            direction = "long"
            stop = price - self.stop_atr * atrv
        elif dev_atr >= self.entry_z and price < prev:
            if not context.allow_short:
                return self.reject(context, ["short not allowed on spot"], "short")
            direction = "short"
            stop = price + self.stop_atr * atrv
        else:
            return self.reject(context, [f"no extension (dev={dev_atr:.2f} ATR)"])

        to_mean = mean - price
        take_profit = [round(price + frac * to_mean, 8) for frac in (0.4, 0.7, 1.0)]
        risk = abs(price - stop)
        if risk <= 0:
            return self.reject(context, ["invalid stop distance"], direction)
        expected_move = abs(take_profit[-1] - price)
        confidence = max(0.0, min(100.0, 50.0 + (abs(dev_atr) - self.entry_z) * 15.0))
        return StrategySignal(
            setup_name=self.name,
            symbol=context.symbol,
            direction=direction,
            entry_price=price,
            stop_loss=round(stop, 8),
            take_profit_levels=take_profit,
            trailing_stop=round(stop, 8),
            expected_move=round(expected_move, 8),
            risk_reward_ratio=round(expected_move / risk, 2),
            confidence_score=confidence,
            reasons_for_entry=[
                f"{'oversold' if direction == 'long' else 'overbought'} {abs(dev_atr):.2f} ATR vs own mean",
                "reverting toward mean",
            ],
            accepted=True,
        )
