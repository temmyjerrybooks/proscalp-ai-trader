# Phase 2B Activation Incident — 2026-05-30

**Status:** Resolved (flag rolled back). Branch 2 activation **paused** pending adapter remediation.
**Severity:** High (feature-blocking) / **Capital impact:** None (testnet; zero positions stranded, no orphan orders).

## Summary

The first step of the Branch 2 activation sequence — flipping
`exchange_resting_exits_enabled` `False → True` — failed immediately on bot start.
Binance USD-M **futures testnet** (`demo-fapi.binance.com`) **rejects** the
protective order types the feature depends on. The built-in safety mechanisms
caught it cleanly: a single failed test order, no positions at risk, automatic
degradation back to legacy polling.

## Root cause

`OrderManager.attach_protective_orders` (and the ladder's `attach_ladder_orders`)
place `STOP_MARKET` / `TAKE_PROFIT_MARKET` (and `TRAILING_STOP_MARKET`) via
`POST /fapi/v1/order`. The futures **testnet** rejects these with:

```
HTTP 400  code -4120
"Order type not supported for this endpoint. Please use the Algo Order API endpoints instead."
```

On Binance futures, conditional/algo order types (`STOP_MARKET`,
`TAKE_PROFIT_MARKET`, `TRAILING_STOP_MARKET`, especially with `closePosition=true`)
must go to the **Algo Order API** (`/fapi/v1/algo/futures/...`) on the testnet
environment, not the standard order endpoint. The adapter was written against
Binance **mainnet** behavior, where `/fapi/v1/order` accepts these types — so the
divergence only surfaced against the live testnet endpoint.

## What the safety mechanisms caught (all worked as designed)

1. **`startup_adapter_test`** (Branch 1) — placed one far-from-market `STOP_MARKET`
   on BTCUSDT and got `-4120`. Logged `startup_adapter_test_failed` with the exact
   exchange response. **Zero positions at risk** — this is a throwaway probe order.
2. **3-state startup reconciliation** — found 4 open positions in State 2 (DB
   position, no protective orders), attempted `attach_protective_orders` on each.
   All 4 failed with `-4120`, `stop_order_id: null`. **Clean rejections — no orphan
   orders created** on the exchange (failures happened at placement, nothing to
   leave behind). Trades flagged `exchange_resting_active=false` + the failure reason.
3. **Circuit breaker** (Branch 1 refinement 2) — 3 attach failures within the
   1-hour window tripped `protective_orders_circuit_breaker_tripped`, Telegram
   alert sent, `_use_exchange_resting_exits()` forced to `False` for the UTC day.
4. **Graceful degradation** — with resting exits disabled, all open positions and
   any new entries route to the **legacy mid-price polling** exit path. The bot
   continued managing the 4 open positions safely (pre-flip behavior).

Net: the layered Branch 1 safety net did exactly its job — surface the
incompatibility on a probe order, refuse to operate a broken path, and fall back
to the proven path without stranding anything.

## What we learned

- **Unit tests passing against mocks does not prove real-exchange compatibility.**
  The 150-test suite (incl. adapter param-shape tests) was green; the failure was
  an *endpoint-routing* requirement no mock exercised.
- **Testnet ≠ mainnet for algo order endpoints.** The adapter code was correct
  against mainnet docs but the testnet demands the Algo Order API. Any future
  adapter work must validate against the actual target endpoint, not docs alone.
- **The `startup_adapter_test` is load-bearing and worth keeping** — it converted
  a potential live-position failure into a harmless probe failure.
- The circuit breaker's **UTC-day auto-reset** means a flag left `True` would
  re-fail daily — hence the flag rollback (not just relying on the breaker).

## Remediation pointer (NOT yet started)

New branch **`phase-2b-adapter-algo-fix`** (investigation-first, same discipline as
Branches 1–2). Scope to investigate:

- Route `STOP_MARKET`, `TAKE_PROFIT_MARKET`, `TRAILING_STOP_MARKET` to the **Algo
  Order API** (`/fapi/v1/algo/futures/...`). Determine exact endpoint + parameter
  shape the testnet expects (and whether `closePosition`/`reduceOnly`/`quantity`
  semantics carry over).
- **Does the adapter call Binance directly or via ccxt?** (Current `BinanceAdapter`
  builds signed requests by hand against `/fapi/v1/order`.) If a ccxt path exists,
  check whether ccxt auto-routes these order types to the algo endpoints; if the
  adapter is hand-rolled, add explicit endpoint switching for conditional types.
- Confirm mainnet vs testnet endpoint divergence and gate the routing accordingly.
- Re-validate via the same staging gate + a **live testnet probe** before
  re-attempting the first flag flip.

## Disposition

- Flag rolled back: `exchange_resting_exits_enabled = True → False` (commit `d1927f4`),
  deployed (image `66f79c2b7303`), verified `False` in production.
- Bot kept running on legacy polling (operator decision) to manage the 4 open
  positions; restarted post-deploy (in-memory reset), operator to re-issue start.
- Branch 2 two-flip activation sequence **paused** until the adapter fix is built,
  tested, and deployed.

**Timeline (UTC, 2026-05-30):** flip deployed ~13:52 · bot start ~13:54 ·
`startup_adapter_test_failed` 13:54:44 · reconciliation attaches failed
13:54:47–54 · circuit breaker tripped 13:54:50 · rollback deployed ~14:5x.
