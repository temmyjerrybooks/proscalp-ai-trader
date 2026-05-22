from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, time, timedelta, timezone
from math import ceil

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.backtesting.backtester import Backtester
from app.database.db import get_db
from app.database.models import BacktestResult
from app.exchanges.base import Candle
from app.exchanges.factory import create_exchange_adapter
from app.strategies import default_strategies
from app.strategies.ema_pullback import EMAPullbackStrategy

router = APIRouter(prefix="/api/backtesting", tags=["backtesting"])


class BacktestRequest(BaseModel):
    symbol: str = "BTCUSDT"
    strategy: str = "EMA pullback scalp"
    timeframe: str = "5m"
    limit: int = 500
    session_name: str = "london"
    start_date: str | None = None
    end_date: str | None = None


@router.post("/run")
async def run_backtest(payload: BacktestRequest, db: AsyncSession = Depends(get_db)) -> dict:
    strategy = next(
        (item for item in default_strategies() if item.name == payload.strategy),
        EMAPullbackStrategy(enabled=True),  # backtests evaluate the setup regardless of live enablement
    )
    adapter = create_exchange_adapter()
    start_time = _parse_backtest_bound(payload.start_date, end=False)
    end_time = _parse_backtest_bound(payload.end_date, end=True)
    candle_limit = _candle_limit(payload.timeframe, payload.limit, start_time, end_time)
    try:
        candles = await adapter.fetch_ohlcv_range(
            payload.symbol,
            payload.timeframe,
            start_time=start_time,
            end_time=end_time,
            limit=candle_limit,
        )
    except Exception:
        candles = _synthetic_candles(candle_limit)
    candles = _filter_candles(candles, start_time, end_time)
    if not candles:
        raise HTTPException(status_code=422, detail="No candles available for the selected backtest window")
    report = Backtester().run(payload.symbol, candles, strategy, session_name=payload.session_name)
    payload_report = asdict(report)
    db.add(
        BacktestResult(
            symbol=payload.symbol,
            strategy_name=payload.strategy,
            date_range={
                "start_date": payload.start_date,
                "end_date": payload.end_date,
                "timeframe": payload.timeframe,
                "candles": len(candles),
            },
            total_trades=report.total_trades,
            win_rate=report.win_rate,
            profit_factor=report.profit_factor,
            max_drawdown=report.max_drawdown,
            expectancy=report.expectancy,
            results=payload_report,
        )
    )
    await db.commit()
    return {**payload_report, "candles": len(candles)}


def _parse_backtest_bound(value: str | None, end: bool) -> datetime | None:
    if not value:
        return None
    clean = value.strip()
    try:
        parsed_date = date.fromisoformat(clean)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(clean.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"Invalid backtest date: {value}") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    bound = datetime.combine(parsed_date, time.min, tzinfo=timezone.utc)
    return bound + timedelta(days=1) if end else bound


def _candle_limit(
    timeframe: str,
    requested_limit: int,
    start_time: datetime | None,
    end_time: datetime | None,
) -> int:
    requested = min(max(50, requested_limit), 1500)
    if not start_time or not end_time:
        return requested
    minutes = _timeframe_minutes(timeframe)
    window_minutes = max(1, (end_time - start_time).total_seconds() / 60)
    needed = ceil(window_minutes / minutes) + 20
    return min(max(requested, needed), 1500)


def _timeframe_minutes(timeframe: str) -> int:
    mapping = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}
    return mapping.get(timeframe, 5)


def _filter_candles(
    candles: list[Candle],
    start_time: datetime | None,
    end_time: datetime | None,
) -> list[Candle]:
    filtered = candles
    if start_time:
        filtered = [candle for candle in filtered if candle.timestamp >= start_time]
    if end_time:
        filtered = [candle for candle in filtered if candle.timestamp < end_time]
    return filtered


def _synthetic_candles(limit: int) -> list[Candle]:
    start = datetime.now(timezone.utc) - timedelta(minutes=limit * 5)
    candles: list[Candle] = []
    price = 100.0
    for index in range(limit):
        price += 0.05 if index % 7 else -0.12
        candles.append(
            Candle(
                timestamp=start + timedelta(minutes=index * 5),
                open=price - 0.04,
                high=price + 0.18,
                low=price - 0.16,
                close=price,
                volume=1000 + index * 3,
            )
        )
    return candles
