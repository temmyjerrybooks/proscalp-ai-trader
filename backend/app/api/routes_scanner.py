from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.db import get_db
from app.database.models import CoinUniverse
from app.services.bot_runner import bot_runner

router = APIRouter(prefix="/api/scanner", tags=["scanner"])


@router.get("/top50")
async def top50(db: AsyncSession = Depends(get_db)) -> list[dict]:
    result = await db.execute(
        select(CoinUniverse).order_by(desc(CoinUniverse.scan_date), CoinUniverse.rank).limit(50)
    )
    rows = result.scalars().all()
    if rows:
        return [
            {
                "rank": row.rank,
                "symbol": row.symbol,
                "score": row.score,
                "liquidity_rating": row.liquidity_score,
                "spread_bps": row.spread_bps,
                "volume": row.quote_volume,
                "volatility_pct": row.volatility_pct,
                "trade_permission": "approved" if row.approved else "watch only",
                "reasons": row.reasons,
            }
            for row in rows
        ]
    return [
        {
            "rank": index + 1,
            "symbol": symbol,
            "score": 88 - index,
            "liquidity_rating": 90 - index,
            "spread_bps": 2.5 + index * 0.2,
            "volume": 150_000_000 - index * 1_500_000,
            "volatility_pct": 2.1 + index * 0.05,
            "trade_permission": "paper watchlist",
            "reasons": ["demo row until first live/testnet scan is run"],
        }
        for index, symbol in enumerate(["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT"])
    ]


@router.post("/run")
async def run_scan(db: AsyncSession = Depends(get_db)) -> list[dict]:
    candidates = await bot_runner.run_top50_scan(db)
    return [asdict(candidate) for candidate in candidates]
