from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config.settings import Settings, TradingMode, get_settings
from app.exchanges.base import Balance, Candle, ExchangeAdapter, OrderBook, OrderRequest, OrderResult, Position, Ticker


class BybitAdapter(ExchangeAdapter):
    name = "bybit"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.base_url = (
            "https://api-testnet.bybit.com"
            if self.settings.trading_mode == TradingMode.TESTNET
            else "https://api.bybit.com"
        )
        self.category = "linear" if self.settings.market_type == "futures" else "spot"

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=12.0) as client:
            response = await client.request(method, path, **kwargs)
            response.raise_for_status()
            payload = response.json()
            if payload.get("retCode", 0) not in (0, "0"):
                raise RuntimeError(f"Bybit API error: {payload.get('retMsg')}")
            return payload

    async def _signed_request(
        self, method: str, path: str, params: dict[str, Any] | None = None
    ) -> Any:
        if not self.settings.live_trading_enabled and self.settings.trading_mode != TradingMode.TESTNET:
            raise PermissionError("Signed Bybit requests require TESTNET or LIVE_TRADING_ENABLED=true")
        if not self.settings.bybit_api_key or not self.settings.bybit_api_secret:
            raise PermissionError("Bybit API credentials are not configured")

        timestamp = str(int(time.time() * 1000))
        recv_window = "5000"
        payload = params or {}
        body = json.dumps(payload, separators=(",", ":")) if method.upper() != "GET" else ""
        query = "&".join(f"{key}={value}" for key, value in sorted(payload.items())) if method.upper() == "GET" else ""
        sign_payload = timestamp + self.settings.bybit_api_key + recv_window + (query or body)
        signature = hmac.new(
            self.settings.bybit_api_secret.encode(),
            sign_payload.encode(),
            hashlib.sha256,
        ).hexdigest()
        headers = {
            "X-BAPI-API-KEY": self.settings.bybit_api_key,
            "X-BAPI-SIGN": signature,
            "X-BAPI-SIGN-TYPE": "2",
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": recv_window,
            "Content-Type": "application/json",
        }
        kwargs: dict[str, Any] = {"headers": headers}
        if method.upper() == "GET":
            kwargs["params"] = payload
        else:
            kwargs["content"] = body
        return await self._request(method, path, **kwargs)

    async def fetch_tickers(self) -> list[Ticker]:
        raw = await self._request("GET", "/v5/market/tickers", params={"category": self.category})
        tickers: list[Ticker] = []
        for item in raw.get("result", {}).get("list", []):
            symbol = item.get("symbol", "")
            if not symbol.endswith(self.settings.quote_asset):
                continue
            last = float(item.get("lastPrice") or 0)
            high = float(item.get("highPrice24h") or last)
            low = float(item.get("lowPrice24h") or last)
            tickers.append(
                Ticker(
                    symbol=symbol,
                    last_price=last,
                    quote_volume=float(item.get("turnover24h") or 0),
                    price_change_pct=float(item.get("price24hPcnt") or 0) * 100,
                    high_price=high,
                    low_price=low,
                    exchange=self.name,
                )
            )
        return tickers

    async def fetch_order_book(self, symbol: str, limit: int = 50) -> OrderBook:
        raw = await self._request(
            "GET",
            "/v5/market/orderbook",
            params={"category": self.category, "symbol": symbol, "limit": limit},
        )
        result = raw.get("result", {})
        bids = [(float(price), float(qty)) for price, qty in result.get("b", [])]
        asks = [(float(price), float(qty)) for price, qty in result.get("a", [])]
        return OrderBook(symbol=symbol, bids=bids, asks=asks)

    async def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 200) -> list[Candle]:
        return await self.fetch_ohlcv_range(symbol, timeframe, limit=limit)

    async def fetch_ohlcv_range(
        self,
        symbol: str,
        timeframe: str,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 1000,
    ) -> list[Candle]:
        interval_map = {"1m": "1", "3m": "3", "5m": "5", "15m": "15", "1h": "60", "4h": "240", "1d": "D"}
        params: dict[str, Any] = {
            "category": self.category,
            "symbol": symbol,
            "interval": interval_map.get(timeframe, timeframe),
            "limit": min(max(1, limit), 1000),
        }
        if start_time:
            params["start"] = int(start_time.timestamp() * 1000)
        if end_time:
            params["end"] = int(end_time.timestamp() * 1000)
        raw = await self._request(
            "GET",
            "/v5/market/kline",
            params=params,
        )
        candles = []
        for item in reversed(raw.get("result", {}).get("list", [])):
            candles.append(
                Candle(
                    timestamp=datetime.fromtimestamp(int(item[0]) / 1000, tz=timezone.utc),
                    open=float(item[1]),
                    high=float(item[2]),
                    low=float(item[3]),
                    close=float(item[4]),
                    volume=float(item[5]),
                )
            )
        return candles

    async def fetch_balances(self) -> list[Balance]:
        account_type = "UNIFIED"
        raw = await self._signed_request("GET", "/v5/account/wallet-balance", {"accountType": account_type})
        balances: list[Balance] = []
        for account in raw.get("result", {}).get("list", []):
            for coin in account.get("coin", []):
                balances.append(
                    Balance(
                        asset=coin.get("coin", ""),
                        free=float(coin.get("availableToWithdraw") or coin.get("walletBalance") or 0),
                        locked=0.0,
                        total=float(coin.get("walletBalance") or 0),
                    )
                )
        return balances

    async def place_order(self, request: OrderRequest) -> OrderResult:
        payload: dict[str, Any] = {
            "category": self.category,
            "symbol": request.symbol,
            "side": "Buy" if request.side == "buy" else "Sell",
            "orderType": "Market" if request.order_type == "market" else "Limit",
            "qty": self._format_number(request.quantity),
            "reduceOnly": request.reduce_only,
        }
        if request.order_type == "limit":
            payload["price"] = self._format_number(request.price or 0)
            payload["timeInForce"] = request.time_in_force or "GTC"
        if request.client_order_id:
            payload["orderLinkId"] = request.client_order_id
        raw = await self._signed_request("POST", "/v5/order/create", payload)
        result = raw.get("result", {})
        return OrderResult(
            order_id=str(result.get("orderId")),
            symbol=request.symbol,
            status="new",
            side=request.side,
            order_type=request.order_type,
            quantity=request.quantity,
            raw=raw,
        )

    async def cancel_order(self, symbol: str, order_id: str) -> OrderResult:
        raw = await self._signed_request(
            "POST",
            "/v5/order/cancel",
            {"category": self.category, "symbol": symbol, "orderId": order_id},
        )
        return OrderResult(
            order_id=order_id,
            symbol=symbol,
            status="canceled",
            side="buy",
            order_type="limit",
            quantity=0.0,
            raw=raw,
        )

    async def fetch_open_orders(self, symbol: str | None = None) -> list[OrderResult]:
        payload: dict[str, Any] = {"category": self.category, "openOnly": 0}
        if symbol:
            payload["symbol"] = symbol
        raw = await self._signed_request("GET", "/v5/order/realtime", payload)
        return [
            OrderResult(
                order_id=str(item.get("orderId")),
                symbol=item.get("symbol", ""),
                status=str(item.get("orderStatus", "New")).lower(),
                side="buy" if item.get("side") == "Buy" else "sell",
                order_type=str(item.get("orderType", "Limit")).lower(),  # type: ignore[arg-type]
                quantity=float(item.get("qty") or 0),
                filled_quantity=float(item.get("cumExecQty") or 0),
                average_price=float(item.get("avgPrice") or 0) or None,
                raw=item,
            )
            for item in raw.get("result", {}).get("list", [])
        ]

    async def fetch_positions(self) -> list[Position]:
        if self.category == "spot":
            return []
        raw = await self._signed_request("GET", "/v5/position/list", {"category": self.category, "settleCoin": "USDT"})
        positions: list[Position] = []
        for item in raw.get("result", {}).get("list", []):
            size = float(item.get("size") or 0)
            if size <= 0:
                continue
            positions.append(
                Position(
                    symbol=item.get("symbol", ""),
                    side="long" if item.get("side") == "Buy" else "short",
                    quantity=size,
                    entry_price=float(item.get("avgPrice") or 0),
                    mark_price=float(item.get("markPrice") or 0),
                    unrealized_pnl=float(item.get("unrealisedPnl") or 0),
                    leverage=int(float(item.get("leverage") or 1)),
                    liquidation_price=float(item.get("liqPrice") or 0) or None,
                )
            )
        return positions

    async def close_position(self, symbol: str) -> OrderResult:
        positions = [position for position in await self.fetch_positions() if position.symbol == symbol]
        if not positions:
            return OrderResult(
                order_id="none",
                symbol=symbol,
                status="no_position",
                side="sell",
                order_type="market",
                quantity=0.0,
            )
        position = positions[0]
        side = "sell" if position.side == "long" else "buy"
        return await self.place_order(
            OrderRequest(
                symbol=symbol,
                side=side,  # type: ignore[arg-type]
                order_type="market",
                quantity=position.quantity,
                reduce_only=True,
            )
        )

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        if self.category == "spot":
            return True
        leverage = min(max(1, leverage), self.settings.max_leverage)
        await self._signed_request(
            "POST",
            "/v5/position/set-leverage",
            {
                "category": self.category,
                "symbol": symbol,
                "buyLeverage": str(leverage),
                "sellLeverage": str(leverage),
            },
        )
        return True

    @staticmethod
    def _format_number(value: float) -> str:
        return f"{value:.8f}".rstrip("0").rstrip(".")
