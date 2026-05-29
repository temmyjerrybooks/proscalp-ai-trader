from __future__ import annotations

from app.config.settings import Settings
from app.exchanges.binance_adapter import BinanceAdapter


def test_binance_depth_limit_is_normalized_for_futures():
    adapter = BinanceAdapter(Settings(market_type="futures"))

    assert adapter._normalize_depth_limit(25) == 50
    assert adapter._normalize_depth_limit(5) == 5
    assert adapter._normalize_depth_limit(1200) == 1000


def test_binance_rounding_helpers():
    adapter = BinanceAdapter(Settings(market_type="futures"))

    assert adapter._round_to_step(101.239, 0.1, up=False) == 101.2
    assert adapter._round_to_step(101.239, 0.1, up=True) == 101.3
    assert adapter._round_to_step(0.00121, 0.001, up=True) == 0.002
    assert adapter._round_to_step(0.00642155, 0.001, up=False) == 0.006


def test_binance_quantity_rounding_respects_quantity_precision():
    adapter = BinanceAdapter(Settings(market_type="futures"))
    rules = {
        "step_size": 0.0001,
        "market_step_size": 0.0001,
        "min_qty": 1,
        "min_notional": 5,
        "tick_size": 0.0001,
        "quantity_precision": 0,
        "price_precision": 4,
    }

    quantity = adapter._round_quantity(4510.1816, rules, "market")

    assert quantity == 4510
    assert adapter._format_quantity(quantity, 0) == "4510"


def test_binance_symbol_rules_select_requested_symbol_not_first_result():
    symbols = [
        {"symbol": "BTCUSDT", "quantityPrecision": 4},
        {"symbol": "ETHUSDT", "quantityPrecision": 3},
    ]

    selected = BinanceAdapter._select_symbol_info(symbols, "ETHUSDT")

    assert selected is not None
    assert selected["quantityPrecision"] == 3


# ---- Phase 2B Branch 1: STOP_MARKET / TAKE_PROFIT_MARKET param generation ----

import pytest  # noqa: E402
from app.config.settings import TradingMode  # noqa: E402
from app.exchanges.base import OrderRequest  # noqa: E402

_RULES = {
    "tick_size": 0.1, "step_size": 0.001, "market_step_size": 0.001,
    "min_qty": 0.001, "min_notional": 5.0,
    "quantity_precision": 3.0, "price_precision": 1.0,
}


@pytest.mark.asyncio
async def test_place_stop_market_closePosition_sends_expected_params(monkeypatch):
    """closePosition=True => no quantity, stopPrice rounded, no reduceOnly, no price/timeInForce."""
    adapter = BinanceAdapter(Settings(trading_mode=TradingMode.TESTNET, market_type="futures"))
    async def _fake_rules(_): return _RULES
    captured: dict = {}
    async def _fake_signed(method, path, params=None):
        captured["method"] = method; captured["path"] = path; captured["params"] = dict(params or {})
        return {"orderId": 12345, "status": "NEW"}
    monkeypatch.setattr(adapter, "fetch_symbol_rules", _fake_rules)
    monkeypatch.setattr(adapter, "_signed_request", _fake_signed)
    req = OrderRequest(
        symbol="BTCUSDT", side="sell", order_type="stop_market",
        stop_price=49995.07, close_position=True, working_type="MARK_PRICE",
        client_order_id="proscalp-sl-abc",
    )
    result = await adapter.place_order(req)
    params = captured["params"]
    assert params["type"] == "STOP_MARKET"
    assert params["side"] == "SELL"
    assert params["stopPrice"] == "49995"  # rounded down to tick_size 0.1 (49995.0 then trailing zeros stripped)
    assert params["closePosition"] == "true"
    assert params["workingType"] == "MARK_PRICE"
    assert "quantity" not in params  # closePosition implies no quantity
    assert "price" not in params
    assert "timeInForce" not in params
    assert "reduceOnly" not in params  # suppressed when closePosition is True
    assert result.order_id == "12345"


@pytest.mark.asyncio
async def test_place_take_profit_market_uses_correct_type(monkeypatch):
    adapter = BinanceAdapter(Settings(trading_mode=TradingMode.TESTNET, market_type="futures"))
    async def _fake_rules(_): return _RULES
    captured: dict = {}
    async def _fake_signed(method, path, params=None):
        captured["params"] = dict(params or {}); return {"orderId": 1, "status": "NEW"}
    monkeypatch.setattr(adapter, "fetch_symbol_rules", _fake_rules)
    monkeypatch.setattr(adapter, "_signed_request", _fake_signed)
    req = OrderRequest(
        symbol="BTCUSDT", side="buy", order_type="take_profit_market",
        stop_price=51000.0, close_position=True, working_type="MARK_PRICE",
    )
    await adapter.place_order(req)
    assert captured["params"]["type"] == "TAKE_PROFIT_MARKET"
    assert captured["params"]["side"] == "BUY"
    assert captured["params"]["closePosition"] == "true"


@pytest.mark.asyncio
async def test_place_stop_market_without_stop_price_raises():
    adapter = BinanceAdapter(Settings(trading_mode=TradingMode.TESTNET, market_type="futures"))
    req = OrderRequest(symbol="BTCUSDT", side="sell", order_type="stop_market", close_position=True)
    with pytest.raises(ValueError, match="requires stop_price"):
        await adapter.place_order(req)


# ---- Phase 2B Branch 2: reduceOnly TP tier + TRAILING_STOP_MARKET runner params ----


@pytest.mark.asyncio
async def test_place_reduce_only_tp_tier_sends_quantity_and_reduceonly(monkeypatch):
    """A ladder TP tier is a reduceOnly quantity order (NOT closePosition) so it
    can express a partial size."""
    adapter = BinanceAdapter(Settings(trading_mode=TradingMode.TESTNET, market_type="futures"))
    async def _fake_rules(_): return _RULES
    captured: dict = {}
    async def _fake_signed(method, path, params=None):
        captured["params"] = dict(params or {}); return {"orderId": 7, "status": "NEW"}
    monkeypatch.setattr(adapter, "fetch_symbol_rules", _fake_rules)
    monkeypatch.setattr(adapter, "_signed_request", _fake_signed)
    req = OrderRequest(
        symbol="BTCUSDT", side="sell", order_type="take_profit_market",
        quantity=0.05, stop_price=50250.0, reduce_only=True, working_type="MARK_PRICE",
    )
    await adapter.place_order(req)
    params = captured["params"]
    assert params["type"] == "TAKE_PROFIT_MARKET"
    assert params["quantity"] == "0.05"
    assert params["reduceOnly"] == "true"
    assert "closePosition" not in params
    assert params["stopPrice"] == "50250"


@pytest.mark.asyncio
async def test_place_trailing_stop_market_sends_callback_and_activation(monkeypatch):
    adapter = BinanceAdapter(Settings(trading_mode=TradingMode.TESTNET, market_type="futures"))
    async def _fake_rules(_): return _RULES
    captured: dict = {}
    async def _fake_signed(method, path, params=None):
        captured["params"] = dict(params or {}); return {"orderId": 9, "status": "NEW"}
    monkeypatch.setattr(adapter, "fetch_symbol_rules", _fake_rules)
    monkeypatch.setattr(adapter, "_signed_request", _fake_signed)
    req = OrderRequest(
        symbol="BTCUSDT", side="sell", order_type="trailing_stop_market",
        quantity=0.02, callback_rate=0.6, activation_price=50500.0,
        reduce_only=True, working_type="MARK_PRICE",
    )
    await adapter.place_order(req)
    params = captured["params"]
    assert params["type"] == "TRAILING_STOP_MARKET"
    assert params["callbackRate"] == "0.6"
    assert params["activationPrice"] == "50500"
    assert params["quantity"] == "0.02"
    assert params["reduceOnly"] == "true"


@pytest.mark.asyncio
async def test_place_trailing_stop_market_without_callback_raises():
    adapter = BinanceAdapter(Settings(trading_mode=TradingMode.TESTNET, market_type="futures"))
    adapter._symbol_rules_cache["BTCUSDT"] = _RULES  # avoid the HTTP rules fetch
    req = OrderRequest(symbol="BTCUSDT", side="sell", order_type="trailing_stop_market", quantity=0.02)
    with pytest.raises(ValueError, match="requires callback_rate"):
        await adapter.place_order(req)
