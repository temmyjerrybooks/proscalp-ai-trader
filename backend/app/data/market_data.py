from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.exchanges.base import Candle, ExchangeAdapter, OrderBook
from app.indicators.technical import IndicatorSnapshot, build_indicator_snapshot


TIMEFRAMES = ["1d", "4h", "1h", "15m", "5m", "3m", "1m"]


@dataclass(slots=True)
class MarketDataBundle:
    symbol: str
    candles_by_timeframe: dict[str, list[Candle]]
    indicators_by_timeframe: dict[str, IndicatorSnapshot]
    order_book: OrderBook | None


class MarketDataService:
    def __init__(self, adapter: ExchangeAdapter) -> None:
        self.adapter = adapter

    async def fetch_bundle(
        self, symbol: str, timeframes: list[str] | None = None, candle_limit: int = 220
    ) -> MarketDataBundle:
        selected = timeframes or TIMEFRAMES
        candles_by_timeframe: dict[str, list[Candle]] = {}
        indicators_by_timeframe: dict[str, IndicatorSnapshot] = {}

        candle_results = await asyncio.gather(
            *(self.adapter.fetch_ohlcv(symbol, timeframe, limit=candle_limit) for timeframe in selected),
            return_exceptions=True,
        )
        for timeframe, result in zip(selected, candle_results, strict=True):
            if isinstance(result, Exception):
                candles: list[Candle] = []
            else:
                candles = result
            candles_by_timeframe[timeframe] = candles
            if candles:
                indicators_by_timeframe[timeframe] = build_indicator_snapshot(candles)
        try:
            order_book = await self.adapter.fetch_order_book(symbol, limit=50)
        except Exception:
            order_book = None
        return MarketDataBundle(symbol, candles_by_timeframe, indicators_by_timeframe, order_book)

    async def leader_direction(self, symbol: str) -> str | None:
        candles = await self.adapter.fetch_ohlcv(symbol, "15m", limit=80)
        if not candles:
            return None
        snapshot = build_indicator_snapshot(candles)
        if snapshot.close > snapshot.ema_21 and snapshot.ema_9 > snapshot.ema_21:
            return "long"
        if snapshot.close < snapshot.ema_21 and snapshot.ema_9 < snapshot.ema_21:
            return "short"
        return None
