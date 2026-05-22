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
