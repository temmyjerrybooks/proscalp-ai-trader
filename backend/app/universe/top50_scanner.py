from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings, get_settings
from app.database.models import CoinUniverse
from app.exchanges.base import ExchangeAdapter, Ticker


@dataclass(slots=True)
class CoinCandidate:
    symbol: str
    rank: int
    score: float
    quote_volume: float
    spread_bps: float
    volatility_pct: float
    liquidity_score: float
    market_cap_rank: int | None
    approved: bool
    reasons: list[str] = field(default_factory=list)


class Top50Scanner:
    """Builds the daily tradable universe using valuation when available and exchange liquidity as fallback."""

    def __init__(self, adapter: ExchangeAdapter, settings: Settings | None = None) -> None:
        self.adapter = adapter
        self.settings = settings or get_settings()
        self._ambiguous_market_symbols: set[str] = set()

    async def scan(self, db: AsyncSession | None = None) -> list[CoinCandidate]:
        market_cap_ranks = await self._fetch_market_cap_ranks()
        tickers = await self.adapter.fetch_tickers()
        liquid_tickers = [
            ticker for ticker in tickers if ticker.quote_volume >= self.settings.min_24h_quote_volume
        ]
        liquid_tickers.sort(
            key=lambda ticker: (
                -(1000 - market_cap_ranks.get(self._base_asset(ticker.symbol), 1000)),
                -ticker.quote_volume,
            )
        )

        tickers_to_score = liquid_tickers[: max(80, self.settings.top_coin_limit)]
        semaphore = asyncio.Semaphore(10)

        async def score_with_throttle(ticker: Ticker) -> CoinCandidate:
            async with semaphore:
                return await self._score_ticker(ticker, market_cap_ranks)

        scored = await asyncio.gather(*(score_with_throttle(ticker) for ticker in tickers_to_score))
        approved = [candidate for candidate in scored if candidate.approved]
        fallback = [candidate for candidate in scored if not candidate.approved]
        candidates = approved[: self.settings.top_coin_limit]
        if len(candidates) < self.settings.top_coin_limit:
            candidates.extend(fallback[: self.settings.top_coin_limit - len(candidates)])

        candidates.sort(key=lambda item: item.score, reverse=True)
        ranked = [
            replace(candidate, rank=index + 1)
            for index, candidate in enumerate(candidates[: self.settings.top_coin_limit])
        ]
        if db:
            await self._persist(ranked, db)
        return ranked

    async def _score_ticker(
        self, ticker: Ticker, market_cap_ranks: dict[str, int]
    ) -> CoinCandidate:
        reasons: list[str] = []
        base_asset = self._base_asset(ticker.symbol)
        market_cap_rank = market_cap_ranks.get(base_asset)
        valuation_score = max(0, 100 - (market_cap_rank or 100) * 1.5) if market_cap_rank else 45
        volume_score = min(100, ticker.quote_volume / self.settings.min_24h_quote_volume * 35)
        volatility_score = 100 if self.settings.min_volatility_pct <= ticker.volatility_pct <= self.settings.max_volatility_pct else 35

        try:
            book = await self.adapter.fetch_order_book(ticker.symbol, limit=25)
            spread_bps = book.spread_bps
            depth = book.depth_usdt(levels=10)
            liquidity_score = min(100, depth / self.settings.min_order_book_depth_usdt * 100)
        except Exception as exc:  # pragma: no cover - network defensive branch
            spread_bps = 10_000.0
            liquidity_score = 0.0
            reasons.append(f"order book unavailable: {exc}")

        spread_score = max(0, 100 - spread_bps * 8)
        score = (
            valuation_score * 0.2
            + volume_score * 0.25
            + volatility_score * 0.2
            + liquidity_score * 0.25
            + spread_score * 0.1
        )
        approved = True
        if spread_bps > self.settings.max_spread_bps:
            approved = False
            reasons.append("spread too wide")
        if liquidity_score < 40:
            approved = False
            reasons.append("order book depth too thin")
        if not self.settings.min_volatility_pct <= ticker.volatility_pct <= self.settings.max_volatility_pct:
            approved = False
            reasons.append("volatility outside scalp range")
        if ticker.quote_volume < self.settings.min_24h_quote_volume:
            approved = False
            reasons.append("24h quote volume below minimum")
        if market_cap_rank:
            reasons.append(f"market cap rank {market_cap_rank}")
        elif base_asset in self._ambiguous_market_symbols:
            reasons.append("valuation ticker is ambiguous; ranked by exchange liquidity")
        else:
            reasons.append("valuation unavailable; ranked by exchange liquidity")

        return CoinCandidate(
            symbol=ticker.symbol,
            rank=0,
            score=round(score, 2),
            quote_volume=ticker.quote_volume,
            spread_bps=round(spread_bps, 4),
            volatility_pct=round(ticker.volatility_pct, 4),
            liquidity_score=round(liquidity_score, 2),
            market_cap_rank=market_cap_rank,
            approved=approved,
            reasons=reasons,
        )

    async def _persist(self, candidates: list[CoinCandidate], db: AsyncSession) -> None:
        scan_date = datetime.now(timezone.utc).date().isoformat()
        for candidate in candidates:
            db.add(
                CoinUniverse(
                    scan_date=scan_date,
                    symbol=candidate.symbol,
                    rank=candidate.rank,
                    score=candidate.score,
                    quote_volume=candidate.quote_volume,
                    spread_bps=candidate.spread_bps,
                    volatility_pct=candidate.volatility_pct,
                    liquidity_score=candidate.liquidity_score,
                    exchange=self.adapter.name,
                    approved=candidate.approved,
                    reasons=candidate.reasons,
                )
            )
        await db.commit()

    async def _fetch_market_cap_ranks(self) -> dict[str, int]:
        url = "https://api.coingecko.com/api/v3/coins/markets"
        params = {
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": 250,
            "page": 1,
            "sparkline": "false",
        }
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.get(url, params=params)
                response.raise_for_status()
                assets = response.json()
            grouped: dict[str, list[int]] = {}
            for asset in assets:
                symbol = str(asset.get("symbol") or "").upper()
                rank = asset.get("market_cap_rank")
                if not symbol or not rank:
                    continue
                grouped.setdefault(symbol, []).append(int(rank))
            self._ambiguous_market_symbols = {symbol for symbol, ranks in grouped.items() if len(ranks) > 1}
            return {symbol: ranks[0] for symbol, ranks in grouped.items() if len(ranks) == 1}
        except Exception:
            self._ambiguous_market_symbols = set()
            return {}

    def _base_asset(self, symbol: str) -> str:
        return symbol.removesuffix(self.settings.quote_asset).upper()
