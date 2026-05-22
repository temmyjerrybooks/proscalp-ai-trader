from __future__ import annotations

from time import perf_counter
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.config.settings import TradingMode, get_settings
from app.exchanges.factory import create_exchange_adapter

router = APIRouter(prefix="/api/exchange", tags=["exchange"])


class TestOrderRequest(BaseModel):
    symbol: str = "BTCUSDT"
    side: Literal["buy", "sell"] = "buy"
    notional_usdt: float = Field(default=110.0, ge=5.0, le=150.0)
    offset_bps: float = Field(default=1_000.0, ge=200.0, le=3_000.0)


@router.get("/test")
async def exchange_test() -> dict:
    adapter = create_exchange_adapter()
    start = perf_counter()
    try:
        tickers = await adapter.fetch_tickers()
        return {
            "ok": True,
            "exchange": adapter.name,
            "ticker_count": len(tickers),
            "latency_ms": round((perf_counter() - start) * 1000, 2),
            "permissions": "public market data ok; signed trading requires env keys and live/testnet flags",
        }
    except Exception as exc:
        return {
            "ok": False,
            "exchange": adapter.name,
            "latency_ms": round((perf_counter() - start) * 1000, 2),
            "error": str(exc),
        }


@router.get("/private-test")
async def exchange_private_test() -> dict:
    """Verify signed API access without placing orders."""

    settings = get_settings()
    adapter = create_exchange_adapter(settings)
    start = perf_counter()
    if settings.trading_mode == TradingMode.PAPER:
        return {
            "ok": False,
            "exchange": adapter.name,
            "mode": settings.trading_mode.value,
            "message": "Private exchange test requires TRADING_MODE=testnet or live mode. Paper mode does not call signed exchange endpoints.",
        }
    try:
        balances = await adapter.fetch_balances()
        return {
            "ok": True,
            "exchange": adapter.name,
            "mode": settings.trading_mode.value,
            "balance_count": len(balances),
            "assets": [balance.asset for balance in balances[:10]],
            "latency_ms": round((perf_counter() - start) * 1000, 2),
            "message": "Signed API credentials are accepted. No orders were placed.",
        }
    except Exception as exc:
        return {
            "ok": False,
            "exchange": adapter.name,
            "mode": settings.trading_mode.value,
            "latency_ms": round((perf_counter() - start) * 1000, 2),
            "error": str(exc),
        }


@router.post("/test-order")
async def exchange_test_order(payload: TestOrderRequest) -> dict:
    """Place a far-from-market testnet limit order and cancel it immediately."""

    settings = get_settings()
    adapter = create_exchange_adapter(settings)
    start = perf_counter()
    if settings.trading_mode != TradingMode.TESTNET:
        return {
            "ok": False,
            "mode": settings.trading_mode.value,
            "message": "Test order is allowed only in TRADING_MODE=testnet.",
        }
    if settings.live_trading_enabled or settings.futures_trading_confirmed:
        return {
            "ok": False,
            "mode": settings.trading_mode.value,
            "message": "Refusing test order while live trading flags are enabled.",
        }
    if adapter.name != "binance":
        return {
            "ok": False,
            "exchange": adapter.name,
            "message": "Controlled test order currently supports Binance testnet only.",
        }

    order_result = None
    try:
        if hasattr(adapter, "set_leverage"):
            await adapter.set_leverage(payload.symbol, 1)
        request = await adapter.build_far_from_market_limit_order(
            symbol=payload.symbol.upper(),
            side=payload.side,
            target_notional=payload.notional_usdt,
            offset_bps=payload.offset_bps,
        )
        order_result = await adapter.place_order(request)
        cancel_result = await adapter.cancel_order(payload.symbol.upper(), order_result.order_id)
        return {
            "ok": True,
            "exchange": adapter.name,
            "mode": settings.trading_mode.value,
            "symbol": request.symbol,
            "side": request.side,
            "price": request.price,
            "quantity": request.quantity,
            "notional": round((request.price or 0) * request.quantity, 4),
            "order_status": order_result.status,
            "cancel_status": cancel_result.status,
            "latency_ms": round((perf_counter() - start) * 1000, 2),
            "message": "Testnet limit order was accepted and canceled. No live order was placed.",
        }
    except Exception as exc:
        if order_result is not None:
            try:
                await adapter.cancel_order(payload.symbol.upper(), order_result.order_id)
            except Exception:
                pass
        return {
            "ok": False,
            "exchange": adapter.name,
            "mode": settings.trading_mode.value,
            "latency_ms": round((perf_counter() - start) * 1000, 2),
            "error": str(exc),
        }
