from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.exchanges.base import ExchangeAdapter


@dataclass(slots=True)
class HealthStatus:
    ok: bool
    database: bool
    exchange: bool
    latency_ms: float
    details: dict


class HealthcheckService:
    async def check(self, db: AsyncSession, adapter: ExchangeAdapter | None = None) -> HealthStatus:
        start = perf_counter()
        database_ok = True
        exchange_ok = True
        details: dict = {}
        try:
            await db.execute(text("SELECT 1"))
        except Exception as exc:
            database_ok = False
            details["database_error"] = str(exc)
        if adapter:
            try:
                tickers = await adapter.fetch_tickers()
                details["ticker_count"] = len(tickers)
            except Exception as exc:
                exchange_ok = False
                details["exchange_error"] = str(exc)
        latency = (perf_counter() - start) * 1000
        return HealthStatus(database_ok and exchange_ok, database_ok, exchange_ok, round(latency, 2), details)
