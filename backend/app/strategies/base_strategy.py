from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.exchanges.base import Candle
from app.indicators.technical import IndicatorSnapshot, build_indicator_snapshot


Direction = Literal["long", "short"]


@dataclass(slots=True)
class StrategyContext:
    symbol: str
    candles_by_timeframe: dict[str, list[Candle]]
    session_name: str
    regime: str
    coin_strength_score: float
    btc_direction: Direction | None = None
    eth_direction: Direction | None = None
    asian_high: float | None = None
    asian_low: float | None = None
    intraday_high: float | None = None
    intraday_low: float | None = None
    spread_bps: float = 0.0
    order_book_imbalance: float = 0.0
    allow_short: bool = True


@dataclass(slots=True)
class StrategySignal:
    setup_name: str
    symbol: str
    direction: Direction
    entry_price: float
    stop_loss: float
    take_profit_levels: list[float]
    trailing_stop: float
    expected_move: float
    risk_reward_ratio: float
    confidence_score: float
    reasons_for_entry: list[str] = field(default_factory=list)
    rejection_reasons: list[str] = field(default_factory=list)
    accepted: bool = False


class BaseStrategy:
    name = "base"
    preferred_timeframe = "5m"

    def evaluate(self, context: StrategyContext) -> StrategySignal:
        raise NotImplementedError

    def candles(self, context: StrategyContext, timeframe: str | None = None) -> list[Candle]:
        return context.candles_by_timeframe.get(timeframe or self.preferred_timeframe, [])

    def snapshot(self, context: StrategyContext, timeframe: str | None = None) -> IndicatorSnapshot | None:
        candles = self.candles(context, timeframe)
        if not candles:
            return None
        return build_indicator_snapshot(candles)

    def reject(self, context: StrategyContext, reasons: list[str], direction: Direction = "long") -> StrategySignal:
        candles = self.candles(context)
        price = candles[-1].close if candles else 0.0
        return StrategySignal(
            setup_name=self.name,
            symbol=context.symbol,
            direction=direction,
            entry_price=price,
            stop_loss=price,
            take_profit_levels=[],
            trailing_stop=0.0,
            expected_move=0.0,
            risk_reward_ratio=0.0,
            confidence_score=0.0,
            rejection_reasons=reasons,
            accepted=False,
        )

    def build_signal(
        self,
        context: StrategyContext,
        direction: Direction,
        entry: float,
        stop: float,
        confidence: float,
        reasons: list[str],
    ) -> StrategySignal:
        risk = abs(entry - stop)
        if risk <= 0:
            return self.reject(context, ["invalid stop distance"], direction)
        if direction == "long":
            take_profit = [entry + risk * 1.2, entry + risk * 1.8, entry + risk * 2.6]
            trailing_stop = entry - risk * 0.55
        else:
            take_profit = [entry - risk * 1.2, entry - risk * 1.8, entry - risk * 2.6]
            trailing_stop = entry + risk * 0.55
        expected_move = abs(take_profit[1] - entry)
        risk_reward = expected_move / risk
        return StrategySignal(
            setup_name=self.name,
            symbol=context.symbol,
            direction=direction,
            entry_price=entry,
            stop_loss=stop,
            take_profit_levels=[round(price, 8) for price in take_profit],
            trailing_stop=round(trailing_stop, 8),
            expected_move=round(expected_move, 8),
            risk_reward_ratio=round(risk_reward, 2),
            confidence_score=max(0.0, min(100.0, confidence)),
            reasons_for_entry=reasons,
            accepted=True,
        )

    @staticmethod
    def aligned_with_leaders(context: StrategyContext, direction: Direction) -> bool:
        return context.btc_direction == direction or context.eth_direction == direction
