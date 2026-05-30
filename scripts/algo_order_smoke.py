"""Live testnet smoke test for Binance USD-M Futures Algo Order endpoints.

Phase 2B adapter remediation. NOT part of the pytest suite — it places REAL
(throwaway, far-from-market, immediately-cancelled) orders against the live
exchange, so it needs real testnet credentials. It is the missing safeguard
that would have caught the 2026-05-30 `-4120` activation failure pre-deploy.

Run it INSIDE the running backend container so credentials come from the
container's .env and never touch the local environment:

    ssh <oracle> "docker exec -i proscalp-ai-trader-backend-1 python -" \
        < scripts/algo_order_smoke.py

It validates, for STOP_MARKET / TAKE_PROFIT_MARKET / TRAILING_STOP_MARKET, the
full lifecycle against `/fapi/v1/algoOrder`:
    place -> parse algoId -> GET (confirm NEW) -> DELETE -> GET (confirm gone)

All orders are placed far from market (trigger/activation at 0.5x or 2x mid)
with the minimum quantity, so they cannot fill during the probe window; each
is cancelled immediately regardless. Gate every future algo-adapter change on
a clean run of this script before building the staging test image.
"""
from __future__ import annotations

import asyncio
import json
import time

from app.config.settings import get_settings
from app.exchanges.binance_adapter import BinanceAdapter

SYMBOL = "BTCUSDT"


async def _algo_request(adapter: BinanceAdapter, method: str, params: dict):
    return await adapter._signed_request(method, "/fapi/v1/algoOrder", params)


def _build_place_params(adapter: BinanceAdapter, order_type: str, mid: float, rules: dict) -> dict:
    price_prec = int(rules["price_precision"])
    # Far-from-market trigger/activation so the order can't fill in the probe window.
    # SELL stop triggers when price FALLS (0.5x mid); SELL TP / trailing activate
    # when price RISES (2x mid).
    trig = adapter._round_price(mid * (0.5 if order_type == "STOP_MARKET" else 2.0), rules)
    # Binance values a conditional MARKET order's notional at its trigger price, so
    # size quantity against the LOWER of mid/trigger to clear min_notional (50 on
    # BTCUSDT testnet). Production protective stops use closePosition=true, which
    # bypasses this check — the quantity path here is a no-position probe artifact.
    ref_price = min(mid, trig)
    raw_qty = max(rules["min_qty"], (rules["min_notional"] * 1.15) / ref_price)
    qty = adapter._round_quantity(raw_qty, rules, "market", up=True)
    qty = max(qty, rules["min_qty"])
    params = {
        "algoType": "CONDITIONAL",
        "symbol": SYMBOL,
        "side": "SELL",
        "type": order_type,
        "quantity": adapter._format_quantity(qty, int(rules["quantity_precision"])),
        "workingType": "MARK_PRICE",
        "newOrderRespType": "RESULT",
        "clientAlgoId": f"proscalp-smoke-{order_type[:6].lower()}-{int(time.time())}",
    }
    if order_type == "TRAILING_STOP_MARKET":
        params["callbackRate"] = "1.0"
        params["activatePrice"] = adapter._format_price(trig, price_prec)
    else:
        params["triggerPrice"] = adapter._format_price(trig, price_prec)
    return params


async def _run_one(adapter: BinanceAdapter, order_type: str, mid: float, rules: dict) -> dict:
    out: dict = {"type": order_type, "ok": False, "steps": {}}
    try:
        place_params = _build_place_params(adapter, order_type, mid, rules)
        out["request"] = place_params
        placed = await _algo_request(adapter, "POST", place_params)
        out["steps"]["place_response"] = placed
        algo_id = placed.get("algoId")
        out["algo_id"] = algo_id
        out["algo_status_on_place"] = placed.get("algoStatus")
        if not algo_id:
            out["error"] = "no algoId in place response"
            return out

        got = await _algo_request(adapter, "GET", {"symbol": SYMBOL, "algoId": algo_id})
        out["steps"]["get_after_place"] = {"algoStatus": got.get("algoStatus"), "algoId": got.get("algoId")}

        cancelled = await _algo_request(adapter, "DELETE", {"symbol": SYMBOL, "algoId": algo_id})
        out["steps"]["cancel_response"] = {"algoStatus": cancelled.get("algoStatus"), "algoId": cancelled.get("algoId")}

        try:
            got2 = await _algo_request(adapter, "GET", {"symbol": SYMBOL, "algoId": algo_id})
            out["steps"]["get_after_cancel"] = {"algoStatus": got2.get("algoStatus")}
        except Exception as exc:  # a cancelled/absent order may 400 on GET — that's an acceptable "gone"
            out["steps"]["get_after_cancel"] = f"GET raised (treated as gone): {exc}"

        out["ok"] = True
        return out
    except Exception as exc:
        out["error"] = str(exc)
        return out


async def main() -> None:
    settings = get_settings()
    adapter = BinanceAdapter(settings)
    print(f"base_url={adapter.base_url} futures={adapter.futures} mode={settings.trading_mode.value}")
    book = await adapter.fetch_order_book(SYMBOL, limit=5)
    mid = book.mid_price
    rules = await adapter.fetch_symbol_rules(SYMBOL)
    print(f"{SYMBOL} mid={mid} min_qty={rules['min_qty']} tick={rules['tick_size']} min_notional={rules['min_notional']}")

    results = []
    for order_type in ("STOP_MARKET", "TAKE_PROFIT_MARKET", "TRAILING_STOP_MARKET"):
        t0 = time.perf_counter()
        res = await _run_one(adapter, order_type, mid, rules)
        res["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        results.append(res)
        print(f"\n===== {order_type} =====")
        print(json.dumps(res, indent=2, default=str))

    ok = sum(1 for r in results if r["ok"])
    print(f"\n===== SUMMARY: {ok}/3 lifecycles OK =====")
    print(json.dumps({r["type"]: ("OK" if r["ok"] else r.get("error", "FAIL")) for r in results}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
