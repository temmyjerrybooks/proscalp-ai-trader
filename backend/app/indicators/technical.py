from __future__ import annotations

from dataclasses import dataclass
from statistics import mean, pstdev

from app.exchanges.base import Candle


@dataclass(slots=True)
class IndicatorSnapshot:
    close: float
    ema_9: float
    ema_21: float
    ema_50: float
    ema_200: float
    rsi: float
    macd: float
    macd_signal: float
    macd_histogram: float
    vwap: float
    atr: float
    bollinger_upper: float
    bollinger_middle: float
    bollinger_lower: float
    volume_ma: float
    support: float
    resistance: float
    body_wick_ratio: float
    volatility_expansion: float
    relative_volume: float
    trend_strength_score: float


def closes(candles: list[Candle]) -> list[float]:
    return [candle.close for candle in candles]


def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    multiplier = 2 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append((value - result[-1]) * multiplier + result[-1])
    return result


def sma(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    result: list[float] = []
    for index in range(len(values)):
        window = values[max(0, index - period + 1) : index + 1]
        result.append(mean(window))
    return result


def rsi(values: list[float], period: int = 14) -> list[float]:
    if len(values) < 2:
        return [50.0 for _ in values]
    gains = [0.0]
    losses = [0.0]
    for previous, current in zip(values, values[1:]):
        change = current - previous
        gains.append(max(change, 0.0))
        losses.append(abs(min(change, 0.0)))

    result: list[float] = []
    for index in range(len(values)):
        gain_window = gains[max(0, index - period + 1) : index + 1]
        loss_window = losses[max(0, index - period + 1) : index + 1]
        avg_gain = mean(gain_window) if gain_window else 0.0
        avg_loss = mean(loss_window) if loss_window else 0.0
        if avg_loss == 0:
            result.append(100.0 if avg_gain > 0 else 50.0)
        else:
            rs = avg_gain / avg_loss
            result.append(100 - (100 / (1 + rs)))
    return result


def macd(values: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[list[float], list[float], list[float]]:
    fast_ema = ema(values, fast)
    slow_ema = ema(values, slow)
    macd_line = [fast_value - slow_value for fast_value, slow_value in zip(fast_ema, slow_ema)]
    signal_line = ema(macd_line, signal)
    histogram = [line - sig for line, sig in zip(macd_line, signal_line)]
    return macd_line, signal_line, histogram


def vwap(candles: list[Candle]) -> list[float]:
    cumulative_pv = 0.0
    cumulative_volume = 0.0
    result: list[float] = []
    for candle in candles:
        typical = (candle.high + candle.low + candle.close) / 3
        cumulative_pv += typical * candle.volume
        cumulative_volume += candle.volume
        result.append(cumulative_pv / cumulative_volume if cumulative_volume else candle.close)
    return result


def atr(candles: list[Candle], period: int = 14) -> list[float]:
    if not candles:
        return []
    true_ranges = [candles[0].high - candles[0].low]
    for previous, current in zip(candles, candles[1:]):
        true_ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    return sma(true_ranges, period)


def bollinger_bands(values: list[float], period: int = 20, deviations: float = 2.0) -> tuple[list[float], list[float], list[float]]:
    upper: list[float] = []
    middle: list[float] = []
    lower: list[float] = []
    for index in range(len(values)):
        window = values[max(0, index - period + 1) : index + 1]
        mid = mean(window)
        deviation = pstdev(window) if len(window) > 1 else 0.0
        middle.append(mid)
        upper.append(mid + deviations * deviation)
        lower.append(mid - deviations * deviation)
    return upper, middle, lower


def volume_moving_average(candles: list[Candle], period: int = 20) -> list[float]:
    return sma([candle.volume for candle in candles], period)


def support_resistance(candles: list[Candle], lookback: int = 30) -> tuple[float, float]:
    if not candles:
        return 0.0, 0.0
    window = candles[-lookback:]
    return min(candle.low for candle in window), max(candle.high for candle in window)


def body_wick_ratio(candle: Candle) -> float:
    body = abs(candle.close - candle.open)
    total_range = max(candle.high - candle.low, 1e-12)
    wick = max(total_range - body, 1e-12)
    return body / wick


def volatility_expansion(candles: list[Candle], period: int = 20) -> float:
    atr_values = atr(candles, period)
    if len(atr_values) < 2 or atr_values[-2] == 0:
        return 1.0
    baseline = mean(atr_values[-period:]) if len(atr_values) >= period else mean(atr_values)
    return atr_values[-1] / baseline if baseline else 1.0


def relative_volume(candles: list[Candle], period: int = 20) -> float:
    if not candles:
        return 0.0
    volumes = [candle.volume for candle in candles]
    baseline = mean(volumes[-period - 1 : -1]) if len(volumes) > 1 else volumes[-1]
    return volumes[-1] / baseline if baseline else 0.0


def trend_strength_score(values: list[float]) -> float:
    if len(values) < 20:
        return 50.0
    ema9 = ema(values, 9)[-1]
    ema21 = ema(values, 21)[-1]
    ema50 = ema(values, 50)[-1]
    latest = values[-1]
    slope = (ema(values, 21)[-1] - ema(values, 21)[-10]) / max(latest, 1e-12) * 100
    stacked_bull = latest > ema9 > ema21 > ema50
    stacked_bear = latest < ema9 < ema21 < ema50
    score = 50.0 + min(max(slope * 6, -25), 25)
    if stacked_bull or stacked_bear:
        score += 20
    return max(0.0, min(100.0, score))


def build_indicator_snapshot(candles: list[Candle]) -> IndicatorSnapshot:
    if not candles:
        raise ValueError("At least one candle is required")
    price_values = closes(candles)
    macd_line, signal_line, histogram = macd(price_values)
    upper, middle, lower = bollinger_bands(price_values)
    support, resistance = support_resistance(candles)
    volume_ma = volume_moving_average(candles)
    atr_values = atr(candles)
    vwap_values = vwap(candles)
    rsi_values = rsi(price_values)
    return IndicatorSnapshot(
        close=price_values[-1],
        ema_9=ema(price_values, 9)[-1],
        ema_21=ema(price_values, 21)[-1],
        ema_50=ema(price_values, 50)[-1],
        ema_200=ema(price_values, 200)[-1],
        rsi=rsi_values[-1],
        macd=macd_line[-1],
        macd_signal=signal_line[-1],
        macd_histogram=histogram[-1],
        vwap=vwap_values[-1],
        atr=atr_values[-1],
        bollinger_upper=upper[-1],
        bollinger_middle=middle[-1],
        bollinger_lower=lower[-1],
        volume_ma=volume_ma[-1],
        support=support,
        resistance=resistance,
        body_wick_ratio=body_wick_ratio(candles[-1]),
        volatility_expansion=volatility_expansion(candles),
        relative_volume=relative_volume(candles),
        trend_strength_score=trend_strength_score(price_values),
    )
