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


# ---- Phase 2B adapter remediation: Algo Order (/fapi/v1/algoOrder) param generation ----

import pytest  # noqa: E402
from app.config.settings import TradingMode  # noqa: E402
from app.exchanges.base import OrderRequest  # noqa: E402

_RULES = {
    "tick_size": 0.1, "step_size": 0.001, "market_step_size": 0.001,
    "min_qty": 0.001, "min_notional": 5.0,
    "quantity_precision": 3.0, "price_precision": 1.0,
}


def _algo_adapter(monkeypatch, captured: dict, *, response: dict | None = None):
    adapter = BinanceAdapter(Settings(trading_mode=TradingMode.TESTNET, market_type="futures"))
    async def _fake_rules(_): return _RULES
    async def _fake_signed(method, path, params=None):
        captured["method"] = method; captured["path"] = path; captured["params"] = dict(params or {})
        return response if response is not None else {"algoId": 555, "algoStatus": "NEW", "clientAlgoId": "x"}
    monkeypatch.setattr(adapter, "fetch_symbol_rules", _fake_rules)
    monkeypatch.setattr(adapter, "_signed_request", _fake_signed)
    return adapter


@pytest.mark.asyncio
async def test_place_algo_stop_closePosition_no_quantity(monkeypatch):
    """closePosition stop => Algo endpoint, triggerPrice (NOT stopPrice), NO quantity,
    no reduceOnly. Defensive against the -4164 condition."""
    captured: dict = {}
    adapter = _algo_adapter(monkeypatch, captured)
    result = await adapter.place_algo_order(OrderRequest(
        symbol="BTCUSDT", side="sell", order_type="stop_market",
        stop_price=49995.07, close_position=True, working_type="MARK_PRICE",
        client_order_id="proscalp-67e80a53-stop-ab12",
    ))
    assert captured["method"] == "POST"
    assert captured["path"] == "/fapi/v1/algoOrder"
    p = captured["params"]
    assert p["algoType"] == "CONDITIONAL"
    assert p["type"] == "STOP_MARKET" and p["side"] == "SELL"
    assert p["triggerPrice"] == "49995"  # renamed from stopPrice, rounded to tick 0.1
    assert "stopPrice" not in p
    assert p["closePosition"] == "true"
    assert p["workingType"] == "MARK_PRICE"
    assert p["clientAlgoId"] == "proscalp-67e80a53-stop-ab12"
    assert "newClientOrderId" not in p
    assert "quantity" not in p       # closePosition => no quantity (the -4164 guard)
    assert "reduceOnly" not in p
    assert result.order_id == "555"  # algoId normalized to order_id
    assert result.status == "new"    # algoStatus normalized to status


@pytest.mark.asyncio
async def test_place_algo_tp_tier_reduceonly_has_quantity(monkeypatch):
    captured: dict = {}
    adapter = _algo_adapter(monkeypatch, captured)
    await adapter.place_algo_order(OrderRequest(
        symbol="BTCUSDT", side="sell", order_type="take_profit_market",
        quantity=0.05, stop_price=50250.0, reduce_only=True, working_type="MARK_PRICE",
    ))
    p = captured["params"]
    assert p["type"] == "TAKE_PROFIT_MARKET"
    assert p["quantity"] == "0.05"
    assert p["reduceOnly"] == "true"
    assert "closePosition" not in p
    assert p["triggerPrice"] == "50250"


@pytest.mark.asyncio
async def test_place_algo_trailing_has_callback_activate_no_trigger(monkeypatch):
    captured: dict = {}
    adapter = _algo_adapter(monkeypatch, captured)
    await adapter.place_algo_order(OrderRequest(
        symbol="BTCUSDT", side="sell", order_type="trailing_stop_market",
        quantity=0.02, callback_rate=0.6, activation_price=50500.0,
        reduce_only=True, working_type="MARK_PRICE",
    ))
    p = captured["params"]
    assert p["type"] == "TRAILING_STOP_MARKET"
    assert p["callbackRate"] == "0.6"
    assert p["activatePrice"] == "50500"   # renamed from activationPrice
    assert "activationPrice" not in p
    assert p["quantity"] == "0.02"
    assert p["reduceOnly"] == "true"
    assert "triggerPrice" not in p          # trailing uses activatePrice, not triggerPrice


@pytest.mark.asyncio
async def test_place_algo_stop_without_trigger_raises(monkeypatch):
    captured: dict = {}
    adapter = _algo_adapter(monkeypatch, captured)
    with pytest.raises(ValueError, match="requires stop_price"):
        await adapter.place_algo_order(OrderRequest(
            symbol="BTCUSDT", side="sell", order_type="stop_market", close_position=True))


@pytest.mark.asyncio
async def test_place_algo_trailing_without_callback_raises(monkeypatch):
    captured: dict = {}
    adapter = _algo_adapter(monkeypatch, captured)
    with pytest.raises(ValueError, match="requires callback_rate"):
        await adapter.place_algo_order(OrderRequest(
            symbol="BTCUSDT", side="sell", order_type="trailing_stop_market", quantity=0.02))


@pytest.mark.asyncio
async def test_place_order_rejects_conditional_types():
    """Entry path must refuse conditional types (they 400 with -4120 on /fapi/v1/order)."""
    adapter = BinanceAdapter(Settings(trading_mode=TradingMode.TESTNET, market_type="futures"))
    for ot in ("stop_market", "take_profit_market", "trailing_stop_market"):
        with pytest.raises(ValueError, match="use place_algo_order"):
            await adapter.place_order(OrderRequest(symbol="BTCUSDT", side="sell", order_type=ot, quantity=0.01))


@pytest.mark.asyncio
async def test_cancel_algo_order_success_is_http_based(monkeypatch):
    """DELETE returns a null algoStatus on testnet; success must come from HTTP 2xx,
    so the result status is 'canceled' regardless of the body."""
    captured: dict = {}
    adapter = _algo_adapter(monkeypatch, captured, response={"algoId": 555, "algoStatus": None})
    result = await adapter.cancel_algo_order("BTCUSDT", "555")
    assert captured["method"] == "DELETE"
    assert captured["path"] == "/fapi/v1/algoOrder"
    assert captured["params"] == {"symbol": "BTCUSDT", "algoId": "555"}
    assert result.status == "canceled"
    assert result.order_id == "555"


@pytest.mark.asyncio
async def test_fetch_algo_order_normalizes_fields(monkeypatch):
    captured: dict = {}
    resp = {"algoId": 999, "algoStatus": "FILLED", "orderType": "TAKE_PROFIT_MARKET",
            "side": "SELL", "avgPrice": "50250.5", "executedQty": "0.05", "quantity": "0.05"}
    adapter = _algo_adapter(monkeypatch, captured, response=resp)
    result = await adapter.fetch_algo_order("BTCUSDT", "999")
    assert captured["method"] == "GET" and captured["path"] == "/fapi/v1/algoOrder"
    assert result.order_id == "999"
    assert result.status == "filled"
    assert result.order_type == "take_profit_market"
    assert result.average_price == 50250.5


@pytest.mark.asyncio
async def test_fetch_open_algo_orders_normalizes_list(monkeypatch):
    captured: dict = {}
    resp = [{"algoId": 1, "algoStatus": "NEW", "symbol": "BTCUSDT", "clientAlgoId": "proscalp-x-stop-1"},
            {"algoId": 2, "algoStatus": "NEW", "symbol": "BTCUSDT", "clientAlgoId": "proscalp-x-tp1-2"}]
    adapter = _algo_adapter(monkeypatch, captured, response=resp)
    orders = await adapter.fetch_open_algo_orders("BTCUSDT")
    assert captured["path"] == "/fapi/v1/algoOpenOrders"
    assert [o.order_id for o in orders] == ["1", "2"]
    assert orders[0].raw["clientAlgoId"] == "proscalp-x-stop-1"  # preserved for reconciliation
