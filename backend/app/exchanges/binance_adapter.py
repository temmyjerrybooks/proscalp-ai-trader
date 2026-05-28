from __future__ import annotations

import hashlib
import hmac
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN
from typing import Any
from urllib.parse import urlencode

import httpx

from app.config.settings import Settings, TradingMode, get_settings
from app.exchanges.base import Balance, Candle, ExchangeAdapter, OrderBook, OrderRequest, OrderResult, Position, Ticker


class BinanceAdapter(ExchangeAdapter):
    name = "binance"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        futures = self.settings.market_type == "futures" or self.settings.trading_mode == TradingMode.LIVE_FUTURES
        if self.settings.trading_mode == TradingMode.TESTNET and futures:
            self.base_url = self.settings.binance_futures_testnet_base_url
        elif self.settings.trading_mode == TradingMode.TESTNET:
            self.base_url = self.settings.binance_spot_testnet_base_url
        elif futures:
            self.base_url = "https://fapi.binance.com"
        else:
            self.base_url = "https://api.binance.com"
        self.futures = futures
        self._symbol_rules_cache: dict[str, dict[str, float]] = {}
        self._time_offset_ms: int | None = None
        self._recv_window_ms = 10_000

    @property
    def public_prefix(self) -> str:
        return "/fapi/v1" if self.futures else "/api/v3"

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=12.0) as client:
            response = await client.request(method, path, **kwargs)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                detail = response.text[:500]
                raise httpx.HTTPStatusError(
                    f"{exc} | Binance response: {detail}",
                    request=exc.request,
                    response=exc.response,
                ) from exc
            return response.json()

    async def _signed_request(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        if not self.settings.live_trading_enabled and self.settings.trading_mode != TradingMode.TESTNET:
            raise PermissionError("Signed Binance requests require TESTNET or LIVE_TRADING_ENABLED=true")
        if not self.settings.binance_api_key or not self.settings.binance_api_secret:
            raise PermissionError("Binance API credentials are not configured")

        payload = dict(params or {})
        headers = {"X-MBX-APIKEY": self.settings.binance_api_key}
        payload.setdefault("recvWindow", self._recv_window_ms)
        for attempt in range(2):
            payload["timestamp"] = await self._timestamp_ms(force_sync=attempt > 0)
            query = urlencode(payload, doseq=True)
            signature = hmac.new(
                self.settings.binance_api_secret.encode(),
                query.encode(),
                hashlib.sha256,
            ).hexdigest()
            try:
                return await self._request(method, f"{path}?{query}&signature={signature}", headers=headers)
            except httpx.HTTPStatusError as exc:
                if attempt == 0 and self._is_timestamp_error(exc):
                    self._time_offset_ms = None
                    continue
                raise
        raise RuntimeError("Binance signed request failed after timestamp resync")

    async def _timestamp_ms(self, *, force_sync: bool = False) -> int:
        if force_sync or self._time_offset_ms is None:
            local_before = int(time.time() * 1000)
            raw = await self._request("GET", f"{self.public_prefix}/time")
            local_after = int(time.time() * 1000)
            server_time = int(raw.get("serverTime") or local_after)
            self._time_offset_ms = server_time - ((local_before + local_after) // 2)
        return int(time.time() * 1000) + int(self._time_offset_ms or 0)

    @staticmethod
    def _is_timestamp_error(exc: httpx.HTTPStatusError) -> bool:
        try:
            payload = exc.response.json()
        except ValueError:
            return "outside of the recvWindow" in exc.response.text
        return payload.get("code") == -1021 or "outside of the recvWindow" in str(payload.get("msg", ""))

    async def fetch_tickers(self) -> list[Ticker]:
        raw = await self._request("GET", f"{self.public_prefix}/ticker/24hr")
        tickers: list[Ticker] = []
        for item in raw:
            symbol = item.get("symbol", "")
            if not symbol.endswith(self.settings.quote_asset):
                continue
            tickers.append(
                Ticker(
                    symbol=symbol,
                    last_price=float(item.get("lastPrice") or item.get("weightedAvgPrice") or 0),
                    quote_volume=float(item.get("quoteVolume") or 0),
                    price_change_pct=float(item.get("priceChangePercent") or 0),
                    high_price=float(item.get("highPrice") or 0),
                    low_price=float(item.get("lowPrice") or 0),
                    exchange=self.name,
                )
            )
        return tickers

    async def fetch_order_book(self, symbol: str, limit: int = 50) -> OrderBook:
        normalized_limit = self._normalize_depth_limit(limit)
        raw = await self._request(
            "GET", f"{self.public_prefix}/depth", params={"symbol": symbol, "limit": normalized_limit}
        )
        bids = [(float(price), float(qty)) for price, qty in raw.get("bids", [])]
        asks = [(float(price), float(qty)) for price, qty in raw.get("asks", [])]
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
        max_limit = 1500 if self.futures else 1000
        params: dict[str, Any] = {
            "symbol": symbol,
            "interval": timeframe,
            "limit": min(max(1, limit), max_limit),
        }
        if start_time:
            params["startTime"] = int(start_time.timestamp() * 1000)
        if end_time:
            params["endTime"] = int(end_time.timestamp() * 1000)
        raw = await self._request(
            "GET",
            f"{self.public_prefix}/klines",
            params=params,
        )
        return [
            Candle(
                timestamp=datetime.fromtimestamp(item[0] / 1000, tz=timezone.utc),
                open=float(item[1]),
                high=float(item[2]),
                low=float(item[3]),
                close=float(item[4]),
                volume=float(item[5]),
            )
            for item in raw
        ]

    async def fetch_balances(self) -> list[Balance]:
        if self.futures:
            raw = await self._signed_request("GET", "/fapi/v2/balance")
            return [
                Balance(
                    asset=item["asset"],
                    free=float(item.get("availableBalance") or 0),
                    locked=0.0,
                    total=float(item.get("balance") or 0),
                )
                for item in raw
            ]
        raw = await self._signed_request("GET", "/api/v3/account")
        return [
            Balance(
                asset=item["asset"],
                free=float(item.get("free") or 0),
                locked=float(item.get("locked") or 0),
                total=float(item.get("free") or 0) + float(item.get("locked") or 0),
            )
            for item in raw.get("balances", [])
        ]

    async def place_order(self, request: OrderRequest) -> OrderResult:
        rules = await self.fetch_symbol_rules(request.symbol)
        is_protective = request.order_type in ("stop_market", "take_profit_market")
        is_whole_position = is_protective and request.close_position

        quantity = request.quantity
        if not is_whole_position:
            quantity = self._round_quantity(quantity, rules, request.order_type)
            if quantity < rules["min_qty"]:
                raise ValueError(
                    f"{request.symbol} quantity is below minimum after exchange rounding"
                )

        price = request.price
        if request.order_type == "limit" and price is not None:
            price = self._round_price(price, rules, up=False)
            if quantity * price < rules["min_notional"]:
                raise ValueError(
                    f"{request.symbol} order notional is below minimum after exchange rounding"
                )

        stop_price = request.stop_price
        if is_protective:
            if stop_price is None:
                raise ValueError(f"{request.order_type} requires stop_price")
            stop_price = self._round_price(stop_price, rules, up=False)

        order_type = request.order_type.upper()
        params: dict[str, Any] = {
            "symbol": request.symbol,
            "side": request.side.upper(),
            "type": order_type,
            "newClientOrderId": request.client_order_id,
            "newOrderRespType": "RESULT",
        }
        if not is_whole_position:
            params["quantity"] = self._format_quantity(quantity, int(rules["quantity_precision"]))
        if request.order_type == "limit":
            params["price"] = self._format_price(price or 0, int(rules["price_precision"]))
            params["timeInForce"] = request.time_in_force or "GTC"
        if is_protective:
            params["stopPrice"] = self._format_price(stop_price, int(rules["price_precision"]))
            if request.close_position:
                params["closePosition"] = "true"
            if request.working_type:
                params["workingType"] = request.working_type
        # Binance rejects reduceOnly + closePosition together; closePosition already implies it.
        if self.futures and request.reduce_only and not request.close_position:
            params["reduceOnly"] = "true"
        params = {key: value for key, value in params.items() if value is not None}
        path = "/fapi/v1/order" if self.futures else "/api/v3/order"
        raw = await self._signed_request("POST", path, params=params)
        return OrderResult(
            order_id=str(raw.get("orderId")),
            symbol=request.symbol,
            status=str(raw.get("status", "NEW")).lower(),
            side=request.side,
            order_type=request.order_type,
            quantity=quantity,
            filled_quantity=float(raw.get("executedQty") or 0),
            average_price=float(raw.get("avgPrice") or raw.get("price") or 0) or None,
            raw=raw,
        )

    async def cancel_order(self, symbol: str, order_id: str) -> OrderResult:
        path = "/fapi/v1/order" if self.futures else "/api/v3/order"
        raw = await self._signed_request("DELETE", path, params={"symbol": symbol, "orderId": order_id})
        return OrderResult(
            order_id=str(raw.get("orderId", order_id)),
            symbol=symbol,
            status=str(raw.get("status", "canceled")).lower(),
            side=str(raw.get("side", "BUY")).lower(),  # type: ignore[arg-type]
            order_type=str(raw.get("type", "LIMIT")).lower(),  # type: ignore[arg-type]
            quantity=float(raw.get("origQty") or 0),
            filled_quantity=float(raw.get("executedQty") or 0),
            raw=raw,
        )

    async def fetch_order(self, symbol: str, order_id: str) -> OrderResult:
        path = "/fapi/v1/order" if self.futures else "/api/v3/order"
        raw = await self._signed_request("GET", path, params={"symbol": symbol, "orderId": order_id})
        return OrderResult(
            order_id=str(raw.get("orderId", order_id)),
            symbol=raw.get("symbol", symbol),
            status=str(raw.get("status", "unknown")).lower(),
            side=str(raw.get("side", "BUY")).lower(),  # type: ignore[arg-type]
            order_type=str(raw.get("type", "LIMIT")).lower(),  # type: ignore[arg-type]
            quantity=float(raw.get("origQty") or 0),
            filled_quantity=float(raw.get("executedQty") or 0),
            average_price=float(raw.get("avgPrice") or 0) or None,
            raw=raw,
        )

    async def fetch_open_orders(self, symbol: str | None = None) -> list[OrderResult]:
        path = "/fapi/v1/openOrders" if self.futures else "/api/v3/openOrders"
        params = {"symbol": symbol} if symbol else {}
        raw = await self._signed_request("GET", path, params=params)
        return [
            OrderResult(
                order_id=str(item.get("orderId")),
                symbol=item.get("symbol", ""),
                status=str(item.get("status", "open")).lower(),
                side=str(item.get("side", "BUY")).lower(),  # type: ignore[arg-type]
                order_type=str(item.get("type", "LIMIT")).lower(),  # type: ignore[arg-type]
                quantity=float(item.get("origQty") or 0),
                filled_quantity=float(item.get("executedQty") or 0),
                raw=item,
            )
            for item in raw
        ]

    async def fetch_positions(self) -> list[Position]:
        if not self.futures:
            return []
        raw = await self._signed_request("GET", "/fapi/v2/positionRisk")
        positions: list[Position] = []
        for item in raw:
            qty = float(item.get("positionAmt") or 0)
            if qty == 0:
                continue
            positions.append(
                Position(
                    symbol=item["symbol"],
                    side="long" if qty > 0 else "short",
                    quantity=abs(qty),
                    entry_price=float(item.get("entryPrice") or 0),
                    mark_price=float(item.get("markPrice") or 0),
                    unrealized_pnl=float(item.get("unRealizedProfit") or 0),
                    leverage=int(item.get("leverage") or 1),
                    liquidation_price=float(item.get("liquidationPrice") or 0) or None,
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
        if not self.futures:
            return True
        leverage = min(max(1, leverage), self.settings.max_leverage)
        await self._signed_request("POST", "/fapi/v1/leverage", params={"symbol": symbol, "leverage": leverage})
        return True

    async def validate_min_order_size(self, symbol: str, quantity: float, price: float) -> bool:
        try:
            rules = await self.fetch_symbol_rules(symbol)
        except Exception:
            return quantity > 0 and quantity * price >= 5.0
        rounded_quantity = self._round_quantity(quantity, rules, "market")
        return rounded_quantity >= rules["min_qty"] and rounded_quantity * price >= rules["min_notional"]

    async def fetch_symbol_rules(self, symbol: str) -> dict[str, float]:
        cached = self._symbol_rules_cache.get(symbol)
        if cached:
            return cached
        raw = await self._request("GET", f"{self.public_prefix}/exchangeInfo", params={"symbol": symbol})
        symbols = raw.get("symbols", [])
        symbol_info = self._select_symbol_info(symbols, symbol)
        if not symbol_info:
            raise ValueError(f"{symbol} not found in Binance exchangeInfo")
        filters = {item.get("filterType"): item for item in symbol_info.get("filters", [])}
        price_filter = filters.get("PRICE_FILTER", {})
        lot_filter = filters.get("LOT_SIZE", {})
        market_lot_filter = filters.get("MARKET_LOT_SIZE", {}) or lot_filter
        min_notional_filter = filters.get("MIN_NOTIONAL", {}) or filters.get("NOTIONAL", {})
        quantity_precision = symbol_info.get("quantityPrecision")
        if quantity_precision is None:
            quantity_precision = symbol_info.get("baseAssetPrecision", 8)
        price_precision = symbol_info.get("pricePrecision")
        if price_precision is None:
            price_precision = symbol_info.get("quotePrecision", 8)
        rules = {
            "tick_size": float(price_filter.get("tickSize") or 0.01),
            "step_size": float(lot_filter.get("stepSize") or 0.001),
            "market_step_size": float(market_lot_filter.get("stepSize") or lot_filter.get("stepSize") or 0.001),
            "min_qty": float(lot_filter.get("minQty") or 0.001),
            "min_notional": float(
                min_notional_filter.get("notional")
                or min_notional_filter.get("minNotional")
                or 5.0
            ),
            "quantity_precision": float(int(quantity_precision)),
            "price_precision": float(int(price_precision)),
        }
        self._symbol_rules_cache[symbol] = rules
        return rules

    @staticmethod
    def _select_symbol_info(symbols: list[dict[str, Any]], symbol: str) -> dict[str, Any] | None:
        for item in symbols:
            if item.get("symbol") == symbol:
                return item
        return symbols[0] if len(symbols) == 1 else None

    async def build_far_from_market_limit_order(
        self,
        symbol: str,
        side: str,
        target_notional: float,
        offset_bps: float,
    ) -> OrderRequest:
        book = await self.fetch_order_book(symbol, limit=20)
        if book.best_bid <= 0 or book.best_ask <= 0:
            raise ValueError(f"{symbol} order book is not usable")
        rules = await self.fetch_symbol_rules(symbol)
        if side == "buy":
            raw_price = book.best_bid * (1 - offset_bps / 10_000)
            price = self._round_to_step(raw_price, rules["tick_size"], up=False)
        elif side == "sell":
            raw_price = book.best_ask * (1 + offset_bps / 10_000)
            price = self._round_to_step(raw_price, rules["tick_size"], up=True)
        else:
            raise ValueError("side must be buy or sell")

        min_notional = max(rules["min_notional"], 5.0)
        notional = max(target_notional, min_notional)
        quantity = self._round_to_step(notional / price, rules["step_size"], up=True)
        quantity = max(quantity, rules["min_qty"])
        if quantity * price < min_notional:
            quantity = self._round_to_step(min_notional / price, rules["step_size"], up=True)

        return OrderRequest(
            symbol=symbol,
            side=side,  # type: ignore[arg-type]
            order_type="limit",
            quantity=quantity,
            price=price,
            client_order_id=f"proscalp-test-{int(time.time())}",
        )

    @staticmethod
    def _format_quantity(value: float, precision: int = 8) -> str:
        text = f"{value:.{precision}f}"
        return text.rstrip("0").rstrip(".") if "." in text else text

    @staticmethod
    def _format_price(value: float, precision: int = 8) -> str:
        text = f"{value:.{precision}f}"
        return text.rstrip("0").rstrip(".") if "." in text else text

    def _normalize_depth_limit(self, limit: int) -> int:
        allowed = [5, 10, 20, 50, 100, 500, 1000]
        if not self.futures:
            allowed.append(5000)
        for candidate in allowed:
            if limit <= candidate:
                return candidate
        return allowed[-1]

    @staticmethod
    def _round_to_step(value: float, step: float, up: bool = False) -> float:
        decimal_value = Decimal(str(value))
        decimal_step = Decimal(str(step))
        rounding = ROUND_CEILING if up else ROUND_DOWN
        return float((decimal_value / decimal_step).to_integral_value(rounding=rounding) * decimal_step)

    def _round_quantity(self, value: float, rules: dict[str, float], order_type: str, up: bool = False) -> float:
        step = rules["market_step_size"] if order_type == "market" else rules["step_size"]
        if step <= 0:
            step = rules["step_size"]
        rounded = self._round_to_step(value, step, up=up)
        return self._round_to_precision(rounded, int(rules["quantity_precision"]), up=up)

    def _round_price(self, value: float, rules: dict[str, float], up: bool = False) -> float:
        rounded = self._round_to_step(value, rules["tick_size"], up=up)
        return self._round_to_precision(rounded, int(rules["price_precision"]), up=up)

    @staticmethod
    def _round_to_precision(value: float, precision: int, up: bool = False) -> float:
        decimal_value = Decimal(str(value))
        quantum = Decimal("1").scaleb(-max(0, precision))
        rounding = ROUND_CEILING if up else ROUND_DOWN
        return float(decimal_value.quantize(quantum, rounding=rounding))
