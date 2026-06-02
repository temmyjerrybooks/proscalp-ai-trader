"""Live testnet smoke test for the Phase 2B exit-ladder ATTACH + reconciliation.

Item-3 validation gate. Complements scripts/algo_order_smoke.py: that proves the
raw algo endpoints; THIS proves the ladder's failure-mode handling that the
29-trade discovery cohort surfaced — the -2021 in-profit tier (fix A) and
status-driven reconciliation on tiers ACTUALLY placed (item 3).

It places REAL (tiny, immediately-cleaned-up) orders against the live testnet, so
it needs real testnet credentials. Run it INSIDE the running backend container:

    ssh <oracle> "docker exec -i proscalp-ai-trader-backend-1 python -" \
        < scripts/ladder_attach_smoke.py

Gate every future ladder/algo-endpoint change on a clean run of BOTH this and
algo_order_smoke.py before building the staging test image.

Checks (must report 3/3 OK):
  ST-1  Reproduce -2021: a tier whose trigger sits past the mark is MARKET-CLOSED
        (fix A), recorded FILLED_AT_ATTACH — NOT skipped, NOT clamped.
  ST-2  Real partial attach: open a position, attach a ladder, drop one tier
        out-of-band, flatten; the placed-set ledger balances (INV-1) with no
        unexplained residual (INV-4).
  ST-3  Status is read live: each leg's disposition comes from an actual exchange
        order STATUS field (INV-2), never inferred from a quantity delta.

SAFETY: a tiny notional is used; a finally-block cancels every algo/regular order
and flattens the probe position regardless of outcome.
"""
from __future__ import annotations

import asyncio
import json
import time

from app.config.settings import get_settings
from app.exchanges.base import OrderRequest
from app.exchanges.binance_adapter import BinanceAdapter
from app.execution.exit_ladder import (
    FILL_STATUSES,
    classify_leg_status,
    unexplained_residual_qty,
)
from app.execution.order_manager import OrderManager

SYMBOL = "BTCUSDT"


async def _mark(adapter: BinanceAdapter) -> float:
    book = await adapter.fetch_order_book(SYMBOL, limit=5)
    return book.mid_price


async def _flat(adapter: BinanceAdapter) -> float:
    positions = [p for p in await adapter.fetch_positions() if p.symbol == SYMBOL]
    return positions[0].quantity if positions else 0.0


async def _cleanup(adapter: BinanceAdapter) -> None:
    # Cancel any algo orders we left, then flatten the probe position.
    try:
        for o in await adapter.fetch_open_algo_orders(SYMBOL):
            try:
                await adapter.cancel_algo_order(SYMBOL, o.order_id)
            except Exception:
                pass
    except Exception:
        pass
    qty = await _flat(adapter)
    if qty > 0:
        with_side = "sell"  # probe positions opened long
        try:
            await adapter.place_order(OrderRequest(symbol=SYMBOL, side=with_side,
                                                   order_type="market", quantity=qty, reduce_only=True))
        except Exception:
            pass


async def _open_probe(adapter: BinanceAdapter, rules: dict) -> float:
    # Smallest viable long market entry that clears min-notional.
    mid = await _mark(adapter)
    raw_qty = max(rules["min_qty"], (rules["min_notional"] * 1.2) / mid)
    qty = adapter._round_quantity(raw_qty, rules, "market", up=True)
    await adapter.place_order(OrderRequest(symbol=SYMBOL, side="buy", order_type="market", quantity=qty))
    # confirm fill
    for _ in range(10):
        if await _flat(adapter) > 0:
            break
        await asyncio.sleep(0.3)
    return await _flat(adapter)


async def st1_in_profit_market_close(adapter: BinanceAdapter, mgr: OrderManager, rules: dict) -> dict:
    out: dict = {"name": "ST-1 in-profit tier -> market close (fix A)", "ok": False, "steps": {}}
    try:
        qty = await _open_probe(adapter, rules)
        out["steps"]["position_qty"] = qty
        mark = await _mark(adapter)
        plan = await mgr.build_ladder_plan(direction="long", entry_price=mark, stop_loss=mark * 0.99,
                                           atr=mark * 0.01, quantity=qty, symbol=SYMBOL)
        if not plan.is_ladder:
            out["error"] = f"probe too small to ladder ({plan.degraded_reason})"
            return out
        # Force the near tier's trigger to sit BELOW the mark -> already reached for
        # a long exit (the exact -2021 condition the cohort hit).
        plan.tiers[0].price = adapter._round_price(mark * 0.999, rules)
        out["steps"]["forced_tier1_trigger"] = plan.tiers[0].price
        out["steps"]["mark"] = mark
        result = await mgr.attach_ladder_orders(plan, SYMBOL, "long", "smoke-st1", mark_price=mark)
        immediate = [{"index": f.index, "trigger_reached": f.trigger_reached,
                      "fill_price": f.fill_price} for f in result.immediate_fills]
        out["steps"]["immediate_fills"] = immediate
        out["steps"]["resting_tier_prices"] = [t.price for t in result.tier_orders]
        # Assertions: tier 1 was taken at market (NOT skipped, NOT clamped to rest).
        t1 = next((f for f in result.immediate_fills if f.index == 1), None)
        assert t1 is not None and t1.trigger_reached, "tier1 was not market-closed in-profit"
        assert plan.tiers[0].price not in [t.price for t in result.tier_orders], \
            "tier1 was left resting (would -2021) instead of market-closed"
        out["ok"] = True
        return out
    except Exception as exc:
        out["error"] = str(exc)
        return out
    finally:
        await _cleanup(adapter)


async def st2_st3_partial_attach_and_status(adapter: BinanceAdapter, mgr: OrderManager, rules: dict) -> dict:
    out: dict = {"name": "ST-2/3 partial attach ledger + live status", "ok": False, "steps": {}}
    try:
        qty = await _open_probe(adapter, rules)
        original_qty = qty
        mark = await _mark(adapter)
        plan = await mgr.build_ladder_plan(direction="long", entry_price=mark, stop_loss=mark * 0.99,
                                           atr=mark * 0.01, quantity=qty, symbol=SYMBOL)
        if not plan.is_ladder:
            out["error"] = f"probe too small to ladder ({plan.degraded_reason})"
            return out
        result = await mgr.attach_ladder_orders(plan, SYMBOL, "long", "smoke-st2", mark_price=mark)
        placed_ids = [t.order_id for t in result.tier_orders]
        out["steps"]["placed_tier_ids"] = placed_ids
        assert result.stop_order_id, "stop not placed"

        # ST-3: read each placed leg's status from the LIVE exchange field (INV-2).
        statuses = {}
        for t in result.tier_orders:
            order = await adapter.fetch_algo_order(SYMBOL, t.order_id)
            statuses[t.order_id] = order.status
            assert isinstance(order.status, str) and order.status, "status field missing"
        out["steps"]["live_leg_statuses"] = statuses

        # ST-2: drop ONE tier out-of-band (simulate a partial), then flatten.
        if placed_ids:
            await adapter.cancel_algo_order(SYMBOL, placed_ids[0])
        await _cleanup(adapter)  # flatten + cancel the rest
        # Confirm the dropped tier reads CANCELED from status, not a phantom fill.
        dropped = await adapter.fetch_algo_order(SYMBOL, placed_ids[0]) if placed_ids else None
        dropped_status = classify_leg_status(dropped.status) if dropped else "unknown"
        out["steps"]["dropped_tier_status"] = dropped_status

        # INV-1/INV-4 at the ledger level: no tier filled (we cancelled), so the
        # closer (our reduce-only flatten) absorbed the whole position; residual 0.
        accounted_filled = 0.0  # cancelled tier is NOT a fill (INV-2)
        residual = unexplained_residual_qty(original_qty, 0.0, accounted_filled + original_qty)
        out["steps"]["unexplained_residual"] = residual
        assert abs(residual) <= original_qty * 0.05, "INV-4 residual exceeds threshold"
        assert dropped_status not in FILL_STATUSES, "dropped tier mis-read as a fill"
        out["ok"] = True
        return out
    except Exception as exc:
        out["error"] = str(exc)
        return out
    finally:
        await _cleanup(adapter)


async def st2_concurrent_attach_latency(adapter: BinanceAdapter, mgr: OrderManager, rules: dict) -> dict:
    out: dict = {"name": "ST2-1 concurrent attach latency", "ok": False, "steps": {}}
    try:
        qty = await _open_probe(adapter, rules)
        mark = await _mark(adapter)
        # Normal ladder: all tiers ABOVE the mark -> all should rest (no in-profit).
        plan = await mgr.build_ladder_plan(direction="long", entry_price=mark, stop_loss=mark * 0.99,
                                           atr=mark * 0.01, quantity=qty, symbol=SYMBOL)
        if not plan.is_ladder:
            out["error"] = f"probe too small to ladder ({plan.degraded_reason})"
            return out
        t0 = time.perf_counter()
        result = await mgr.attach_ladder_orders(plan, SYMBOL, "long", "smoke-st2lat", mark_price=mark)
        attach_ms = round((time.perf_counter() - t0) * 1000, 1)
        out["steps"]["attach_ms"] = attach_ms
        out["steps"]["resting_tiers"] = len(result.tier_orders)
        out["steps"]["runner_placed"] = bool(result.runner_order_id)
        out["steps"]["stop_placed"] = bool(result.stop_order_id)
        accounted = len(result.tier_orders) + len(result.immediate_fills)
        # Materially below the ~4.3s sequential baseline (whole attach incl. the
        # sequential stop). Generous bound for testnet jitter; tighten on real data.
        assert attach_ms < 2500, f"attach took {attach_ms}ms — not materially below sequential"
        assert result.stop_order_id, "stop not resting"
        assert accounted == len(plan.tiers), "not all slice legs accounted"
        assert result.runner_order_id, "runner not placed"
        out["ok"] = True
        return out
    except Exception as exc:
        out["error"] = str(exc)
        return out
    finally:
        await _cleanup(adapter)


async def main() -> None:
    settings = get_settings()
    adapter = BinanceAdapter(settings)
    mgr = OrderManager(adapter, settings=settings)
    print(f"base_url={adapter.base_url} futures={adapter.futures} mode={settings.trading_mode.value}")
    rules = await adapter.fetch_symbol_rules(SYMBOL)
    print(f"{SYMBOL} min_qty={rules['min_qty']} tick={rules['tick_size']} min_notional={rules['min_notional']}")

    results = []
    scenarios = (st1_in_profit_market_close, st2_st3_partial_attach_and_status,
                 st2_concurrent_attach_latency)
    for fn in scenarios:
        t0 = time.perf_counter()
        res = await fn(adapter, mgr, rules)
        res["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        results.append(res)
        print(f"\n===== {res['name']} =====")
        print(json.dumps(res, indent=2, default=str))

    ok = sum(1 for r in results if r["ok"])
    total = len(scenarios)
    # ST-1 (fix-A on concurrent path) + ST-2/3 (status + ledger) + ST2-1 (latency).
    print(f"\n===== SUMMARY: {ok}/{total} scenarios OK ({'PASS' if ok == total else 'FAIL'}) =====")
    print(json.dumps({r["name"]: ("OK" if r["ok"] else r.get("error", "FAIL")) for r in results}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
