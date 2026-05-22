from __future__ import annotations

from app.strategies.base_strategy import StrategyContext
from app.strategies.london_breakout import LondonBreakoutStrategy


def test_strategy_signal_generation_london_breakout(trending_candles):
    candles = list(trending_candles)
    candles[-1].close = candles[-1].high + 2
    candles[-1].volume = candles[-2].volume * 3
    context = StrategyContext(
        symbol="ETHUSDT",
        candles_by_timeframe={"5m": candles},
        session_name="london",
        regime="strong",
        coin_strength_score=90,
        btc_direction="long",
        eth_direction="long",
        asian_high=candles[-2].high,
        asian_low=min(candle.low for candle in candles[-50:]),
    )

    signal = LondonBreakoutStrategy().evaluate(context)

    assert signal.accepted is True
    assert signal.direction == "long"
    assert signal.risk_reward_ratio >= 1.2
