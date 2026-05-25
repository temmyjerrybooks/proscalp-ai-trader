from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal


Side = Literal["buy", "sell"]
Direction = Literal["long", "short"]
# Phase 2B: stop_market / take_profit_market are exchange-resting protective orders
# (Binance Futures STOP_MARKET / TAKE_PROFIT_MARKET).
OrderType = Literal["market", "limit", "stop_market", "take_profit_market"]
TimeInForce = Literal["GTC", "IOC", "FOK", "GTX"]
# Binance Futures workingType: CONTRACT_PRICE (last) or MARK_PRICE (mark).
# MARK_PRICE is generally safer for protective orders (resistant to thin-book wicks).
WorkingType = Literal["CONTRACT_PRICE", "MARK_PRICE"]


@dataclass(slots=True)
class Balance:
    asset: str
    free: float
    locked: float = 0.0
    total: float = 0.0


@dataclass(slots=True)
class Ticker:
    symbol: str
    last_price: float
    quote_volume: float
    price_change_pct: float
    high_price: float
    low_price: float
    exchange: str

    @property
    def volatility_pct(self) -> float:
        if self.last_price <= 0:
            return 0.0
        return abs(self.high_price - self.low_price) / self.last_price * 100


@dataclass(slots=True)
class OrderBook:
    symbol: str
    bids: list[tuple[float, float]]
    asks: list[tuple[float, float]]

    @property
    def best_bid(self) -> float:
        return self.bids[0][0] if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0][0] if self.asks else 0.0

    @property
    def mid_price(self) -> float:
        if self.best_bid and self.best_ask:
            return (self.best_bid + self.best_ask) / 2
        return self.best_bid or self.best_ask

    @property
    def spread_bps(self) -> float:
        if self.mid_price <= 0:
            return 10_000.0
        return (self.best_ask - self.best_bid) / self.mid_price * 10_000

    def depth_usdt(self, levels: int = 10) -> float:
        bid_depth = sum(price * qty for price, qty in self.bids[:levels])
        ask_depth = sum(price * qty for price, qty in self.asks[:levels])
        return bid_depth + ask_depth

    def imbalance(self, levels: int = 10) -> float:
        bid_depth = sum(price * qty for price, qty in self.bids[:levels])
        ask_depth = sum(price * qty for price, qty in self.asks[:levels])
        total = bid_depth + ask_depth
        return 0.0 if total <= 0 else (bid_depth - ask_depth) / total


@dataclass(slots=True)
class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(slots=True)
class OrderRequest:
    symbol: str
    side: Side
    order_type: OrderType
    quantity: float = 0.0  # may be 0 when close_position=True (whole-position protective order)
    price: float | None = None
    reduce_only: bool = False
    client_order_id: str | None = None
    time_in_force: TimeInForce | None = None
    # Phase 2B — for stop_market / take_profit_market:
    stop_price: float | None = None
    close_position: bool = False  # Binance Futures: closes the whole position when trigger fires
    working_type: WorkingType | None = None  # defaults to exchange default (CONTRACT_PRICE) if None


@dataclass(slots=True)
class OrderResult:
    order_id: str
    symbol: str
    status: str
    side: Side
    order_type: OrderType
    quantity: float
    filled_quantity: float = 0.0
    average_price: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Position:
    symbol: str
    side: Direction
    quantity: float
    entry_price: float
    mark_price: float
    unrealized_pnl: float = 0.0
    leverage: int = 1
    liquidation_price: float | None = None


class ExchangeAdapter(ABC):
    """Common contract for Binance and Bybit implementations."""

    name: str

    @abstractmethod
    async def fetch_balances(self) -> list[Balance]:
        raise NotImplementedError

    @abstractmethod
    async def fetch_tickers(self) -> list[Ticker]:
        raise NotImplementedError

    @abstractmethod
    async def fetch_order_book(self, symbol: str, limit: int = 50) -> OrderBook:
        raise NotImplementedError

    @abstractmethod
    async def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 200) -> list[Candle]:
        raise NotImplementedError

    async def fetch_ohlcv_range(
        self,
        symbol: str,
        timeframe: str,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 1000,
    ) -> list[Candle]:
        return await self.fetch_ohlcv(symbol, timeframe, limit=limit)

    @abstractmethod
    async def place_order(self, request: OrderRequest) -> OrderResult:
        raise NotImplementedError

    @abstractmethod
    async def cancel_order(self, symbol: str, order_id: str) -> OrderResult:
        raise NotImplementedError

    @abstractmethod
    async def fetch_open_orders(self, symbol: str | None = None) -> list[OrderResult]:
        raise NotImplementedError

    @abstractmethod
    async def fetch_positions(self) -> list[Position]:
        raise NotImplementedError

    @abstractmethod
    async def close_position(self, symbol: str) -> OrderResult:
        raise NotImplementedError

    @abstractmethod
    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        raise NotImplementedError

    async def fetch_order(self, symbol: str, order_id: str) -> OrderResult:
        """Query a specific order by id (e.g. to attribute the fill of a closed protective order).

        Phase 2B Branch 1: optional capability. Default raises; adapters that
        support it (Binance) override. Sync logic that needs precise close
        attribution should handle NotImplementedError as a degraded path.
        """
        raise NotImplementedError(f"{self.name} adapter does not implement fetch_order")

    async def get_fees(self, symbol: str) -> dict[str, float]:
        return {"maker_bps": 2.0, "taker_bps": 6.0}

    async def estimate_slippage(self, symbol: str, side: Side, quantity: float) -> float:
        book = await self.fetch_order_book(symbol, limit=50)
        levels = book.asks if side == "buy" else book.bids
        remaining = quantity
        notional = 0.0
        filled = 0.0
        for price, available in levels:
            take = min(remaining, available)
            notional += take * price
            filled += take
            remaining -= take
            if remaining <= 0:
                break
        if filled <= 0 or book.mid_price <= 0:
            return 10_000.0
        average_price = notional / filled
        return abs(average_price - book.mid_price) / book.mid_price * 10_000

    async def validate_min_order_size(self, symbol: str, quantity: float, price: float) -> bool:
        return quantity > 0 and quantity * price >= 5.0
