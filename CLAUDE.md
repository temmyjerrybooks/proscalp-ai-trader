# CLAUDE.md

> Read this file first at the start of every session before doing other work.

## ⚑ CURRENT DEPLOYED STATE (2026-06-02, post-deploy) — AUTHORITATIVE

This block supersedes any older "reverted to single-TP / productive right now / first-flip-complete" wording further down. Where this conflicts with the Phase-2B activation sections below, **this is correct** — those sections are history.

**Prod runs the FIXED ladder code, shipped but INERT ("shipped, not armed").**
- Deployed: branch `phase-2b-ladder-fix`, app code **`e1ca615`**, gate-script fix **`15e478f`** (tip). Branch-fix symbols verified live in the running container: `tier_trigger_reached`, `classify_attach`, `unexplained_residual_qty`. Container **healthy**.
- **Three flags OFF by committed safe default** (NOT env overrides):
  - `five_tier_ladder_enabled = False`
  - `exchange_resting_exits_enabled = False`
  - `allow_unclear_regime_trading = False` — was `True` on the old `ad3a28e` runtime; **tightened by this deploy**. Unclear-regime trades are now BLOCKED unless explicitly opted in; arming it is a conscious decision, not an inherited default.
- **Trading loop STOPPED** — stopped manually via `/api/bot/stop` at ~**2026-06-01 18:42 UTC** (after the discovery cohort; the consecutive-loss guard had been rejecting trades, but the loop stop itself was a manual/API action, not a guard). It does **NOT auto-start on boot** ([main.py](backend/app/main.py) lifespan has no `bot.start()`); stays stopped until `/api/bot/start` is explicitly called.
- **Flat:** 0 positions / 0 algo orders.

> ⚠️ Earlier in this deployment, prod was found running the **buggy pre-fix ladder ARMED** (`ad3a28e` defaults `five_tier_ladder_enabled=True`/`exchange_resting_exits_enabled=True`, no `.env` override) — the believed ".env revert to ladder-OFF" never existed in prod. It was disarmed via `.env` (flags→false + backend restart) on 2026-06-02, then this fixed-code deploy made the safe state the committed default.

**Recovery / backup**
- Private remote `github.com/temmyjerrybooks/proscalp-ai-trader` (PRIVATE): `phase-2b-ladder-fix` tip `15e478f` (app `e1ca615`); `main` = `ad3a28e` (parent preserved).
- Rollback copy on Oracle: `/opt/proscalp-ai-trader/backend/app.bak-predeploy-15e478f` — **keep until the first armed low-concurrency cohort runs clean.**

**Validation done** (live testnet, isolated staging container, prod untouched): `algo_order_smoke.py` 3/3; `ladder_attach_smoke.py` 3/3 — the committed gate self-passes: real `-2021` provoked + caught via the race-fallback (`trigger_reached=False`, market-closed), full concurrent attach **~1479 ms (≈3× faster than the ~4.3 s sequential path)**, live status reads, dropped tier reads-as-CANCELED (non-fill).

**Known operational gaps (Phase-2C / before real volume):**
- **Deploy mechanism is improvised + fragile.** No `rsync` on the Windows shell; no repo auth on Oracle. Current method = full-replace `backend/app` from a tar'd clean tree (`rm -rf` + `cp -a`). An over-matching `--exclude` glob (`app/data`) crashed the first rebuild this deploy (diagnosed + recovered). **FIX BEFORE NEXT DEPLOY:** install `rsync` (use `-a --delete` per the deploy contract) OR put repo auth on Oracle for a clean `git clone` + build. Do not let the improvised full-replace become the default.
- **Cron monitor** (`/opt/proscalp-monitor`, every 15 min) is **Phase-2A-scoped: BLIND to ladder signals** — no `ladder_sync_anomaly` / partial-attach / 60–120 milestone escalation; only the in-app RiskEvent path carries ladder anomalies. Needs a ladder-aware monitor extension (external to the bot) before the ladder runs volume.
- **Milestone alerts:** 60/120 ladder-milestone Telegram alerts are **likely ABSENT** (the monitor has only a one-shot 50-trade milestone). Verify/build before relying on milestone alerting.
- **Stop/start API actions are UNATTRIBUTED** (no record of who/what called stop). Add attribution + logging before real volume.

**NEXT STEP — ARMING (separate, gated, NOT done).** In strict order, each operator-gated: (1) `/api/bot/start` the loop; (2) arm `exchange_resting_exits_enabled` + `five_tier_ladder_enabled` at **LOW concurrency** (NOT 2/cycle); (3) let the C2 partial-attach breaker gather its `min_sample=8` baseline before it can trip; (4) watch the first handful of live attaches; (5) only then consider ramping to 2/cycle and starting the 120-trade clock. **The 120-clock counts fresh from a CLEAN ladder going live; the discovery cohort's −$13.50 does NOT count.**

## Project overview

ProScalp is a Python crypto scalping bot. Stack: FastAPI + asyncio, SQLAlchemy + Postgres, Docker Compose, Binance via testnet/mainnet adapters (a Bybit adapter also exists). Deployed on Oracle Cloud (`ubuntu@92.5.76.247`, `/opt/proscalp-ai-trader/`); source is **Windows-canonical** at `c:\Users\PC\Scalping_Bot`. **Private git remote (2026-06-02):** `github.com/temmyjerrybooks/proscalp-ai-trader` (PRIVATE) — `origin`, authed via the machine's `gh` keyring. (Oracle has no repo auth — relevant to the deploy gap below.) **Trading mode: testnet only. Never deploy to mainnet without explicit operator approval in the current session.**

## Architecture map

- Entry: [backend/app/main.py](backend/app/main.py) — FastAPI app + lifespan `init_db`
- Core loop: [backend/app/services/bot_runner.py](backend/app/services/bot_runner.py) — scan→score→filter→execute→manage
- Strategies registry: [backend/app/strategies/__init__.py](backend/app/strategies/__init__.py) — 7 active setups
- Scoring: [backend/app/scoring/setup_score.py](backend/app/scoring/setup_score.py)
- Risk: [backend/app/risk/risk_engine.py](backend/app/risk/risk_engine.py)
- Regime: [backend/app/regime/detector.py](backend/app/regime/detector.py)
- Sessions: [backend/app/sessions/session_manager.py](backend/app/sessions/session_manager.py)
- Execution: [backend/app/execution/order_manager.py](backend/app/execution/order_manager.py), [position_manager.py](backend/app/execution/position_manager.py)
- Paper sim: [backend/app/paper_trading/simulator.py](backend/app/paper_trading/simulator.py) *(not wired into live loop — see Phase 2B)*
- Indicators: [backend/app/indicators/technical.py](backend/app/indicators/technical.py)
- Market data: [backend/app/data/market_data.py](backend/app/data/market_data.py)
- Followup / shadow PnL: [backend/app/signals/followup.py](backend/app/signals/followup.py) *(computed at export, not stored)*
- Settings: [backend/app/config/settings.py](backend/app/config/settings.py)
- CSV export: [backend/app/api/routes_signals.py](backend/app/api/routes_signals.py) → `/api/signals/report.csv`
- Tests: [backend/tests/](backend/tests/) *(mounted at runtime, not in the image)*
- Baseline doc: [docs/baseline-pre-phase-2a.md](docs/baseline-pre-phase-2a.md)

## Current state as of Phase 2A

**7 active strategies:** Asia-to-London continuation, VWAP reclaim, Breakout and retest, Liquidity sweep reversal, Momentum continuation, Range bounce, BTC-led altcoin continuation.

**3 disabled — do NOT re-enable without explicit approval:** EMA pullback (PF 0.21, n=22), London open breakout (PF 0.10, n=5), US open breakout (0 fires / 51,410 evals — file deleted).

**Phase 2A flag defaults in [settings.py](backend/app/config/settings.py):**
- `ema_pullback_enabled = False`
- `london_open_breakout_enabled = False`
- `aggression_mode_enabled = False`
- `force_limit_orders = True` *(IOC limit only; `market_order_min_score` retained but gated off)*
- `cap_risk_at_score = 75` *(scores ≥75 size at A-tier minimum)*

**Two grade systems** — always be explicit which one an analysis references:
- `SetupScoringEngine.grade` (fixed thresholds 85/75/65/55) → `setup_scores.grade` + trade `metadata->>'normal_grade'`. **This is the grade in the exported CSV.**
- `RiskEngine.assess_setup_score.grade` (session-aware) → trade `metadata->>'grade'`.

**Shadow→real PnL calibration:** real win rate ≈ shadow × **0.77**; subtract **0.20 pct-pts** per trade for round-trip fees+slippage (2×6 bps + 2×4 bps). No maker/taker split.

**Pre-Phase-2A baseline (71 testnet trades, 2026-05-16…19):** net **−$32.58**, win **35.2%**. Full breakdown: [docs/baseline-pre-phase-2a.md](docs/baseline-pre-phase-2a.md).

**Phase 2A evaluation (first 50 closes, 2026-05-23…24):** net **+$18.16**, win **56.0%**, PF **1.57**. Full breakdown: [docs/phase-2a-evaluation.md](docs/phase-2a-evaluation.md).

### Phase 2B Branch 1 — DEPLOYED 2026-05-28 (branch `phase-2b-foundation`, merged to main)

Foundation work (items 1–4 + clarifications + circuit-breaker) live on testnet. All new flags default OFF for trading-impact, ON for measurement. Deploy verified zero trading change (no protective orders, no market orders, legacy exit path unchanged); MFE/MAE + fee-aware shadow confirmed populating. Post-deploy trading anchor: ~+$12.68 / 154 closed / 51.9% win (since Phase 2A deploy).

- `exchange_resting_exits_enabled = False` — when ON (non-paper futures only), after entry fill posts `STOP_MARKET` + `TAKE_PROFIT_MARKET` (`closePosition=True`, `workingType=MARK_PRICE`) via `OrderManager.attach_protective_orders`. Polling loop switches to *sync-from-exchange* for resting trades. **No partial exits in Branch 1** — Branch 2 adds the 5-tier TP ladder.
- `mfe_mae_logging_enabled = True` — per-cycle `mfe_pnl` / `mae_pnl` tracked on `trade.extra` (no schema migration). Works for both exit paths.
- `paper_sim_wired_to_live_loop = False` — when ON, paper-mode exits route through `PaperTradingSimulator.update_price` (eliminates the split-brain where sim equity tracked only fees).
- `shadow_replay_fee_aware = True` — `follow_up.pnl_pct` is fee+slippage-net by default; `pnl_pct_gross` preserved for transparency.
- `startup_reconciliation_enabled = True` — runs once in `BotRunner.start()` when resting is ON. Three states: (1) reconciled, (2) `protective_orders_repaired`, (3) `orphan_orders_cancelled`. Identifies our orders by `client_order_id` prefix `proscalp-sl-` / `proscalp-tp-` (foreign orders never touched).
- `startup_adapter_test_enabled = True` — places + immediately cancels a far-from-market `STOP_MARKET` on BTCUSDT to validate the adapter wiring before the main loop starts. Failure logs loudly but does not block startup.
- `orphan_reconcile_stop_pct = 0.0035`, `orphan_reconcile_tp_levels = [0.003, 0.005, 0.008]` — formerly hardcoded in `_reconcile_pending_and_orphan_positions`, now settings (Branch 1 clarification A). Orphans receive protective orders when resting is ON.
- `protective_order_max_elapsed_ms = 2000` — `OrderManager.attach_protective_orders` is **sequential** (stop first, then TP) and warns via `protective_order_slow` when total elapsed exceeds this.
- **Circuit-breaker (refinement 2):** `protective_orders_failure_threshold = 3`, `protective_orders_failure_window_hours = 1`. If `attach_protective_orders` fails >= threshold times in any rolling window, `_use_exchange_resting_exits()` returns False for the rest of the UTC day (auto-resets at next UTC day-start). Emits a `protective_orders_circuit_breaker` RiskEvent + Telegram alert. Tracker state lives on the BotRunner singleton (`_protective_failures` deque, `_resting_disabled_until_utc_day` date). Branch 1 leaves the resting flag OFF, so this code path won't fire in production until Branch 2 — but the safety net is in place.

`PositionManager` (`execution/position_manager.py`) is still dead code; Branch 2 decision pending.

## Deployment mechanics

Windows source is canonical. Oracle target: `/opt/proscalp-ai-trader/`. **The deployment contract is `rsync -a --delete`** from Windows post-merge `main` to `/opt/proscalp-ai-trader/backend/` (NOT the legacy [scripts/deploy_oracle_from_windows.ps1](scripts/deploy_oracle_from_windows.ps1) — its `tar -xz` overlay doesn't delete removed files, leaving stale orphans). Always preview with `rsync -avn --delete …` and show the operator before the real run.

Production image: `proscalp-ai-trader-backend:latest`, built from [backend/Dockerfile](backend/Dockerfile), **Python 3.12.13**.

**Settled test-gate pattern (always before prod deploy):** stage in a separate dir (e.g. `/opt/proscalp-<phase>-test/`), `docker build -t proscalp:<phase>-test -f backend/Dockerfile backend/`, then run pytest **bind-mounting** `tests/` + `pyproject.toml` read-only into the sealed image (the Dockerfile only `COPY app ./app` — tests are intentionally not baked in, keeping prod lean). Verify hashes match between staging and Windows source before building.

**Live algo smoke test (mandatory gate for any adapter change touching algo/conditional orders):** the mock-only pytest suite was green while the live `/fapi/v1/order` endpoint was wrong (the 2026-05-30 `-4120` incident). So **before** building the staging test image, run [scripts/algo_order_smoke.py](scripts/algo_order_smoke.py) against the live testnet from inside the running backend container (creds stay in-container): `ssh <oracle> "docker exec -i proscalp-ai-trader-backend-1 python -" < scripts/algo_order_smoke.py`. It must report **3/3 lifecycles OK** (place→GET→cancel→GET for STOP_MARKET / TAKE_PROFIT_MARKET / TRAILING_STOP_MARKET). Mocks prove construction; only this proves exchange compatibility.

**Containers (docker compose):** `postgres`, `backend`, `frontend`, `nginx`.
- **Only `backend` is restarted for code deploys.** Never touch the other three.
- **Use `docker compose up -d --no-deps --build backend`.** `--no-deps`: without it, compose follows the `depends_on: postgres: service_healthy` chain and **recreates postgres** as a side effect (unnecessary churn; violates the "only-backend" rule — data survives via the named `postgres_data` volume, but don't). **`--build` is required**: the backend image bakes the app via Dockerfile `COPY app ./app` (it is **not** a runtime bind-mount — only `data`/`logs` are mounted), so rsync'd code only takes effect via an image rebuild. Without `--build`, compose restarts the **existing** image and **silently no-ops the deploy** (you'd be running old code while believing the deploy succeeded). Verify post-deploy by hashing files *inside* the running container. (Lessons from Phase 2A + Branch 2 deploys.)

**DB raw-SQL gotcha:** `trades.extra` maps to column literally named `metadata`. Use `trades.metadata->>'grade'`.

**SSH access:** key at `C:\Users\PC\Downloads\ssh_keys\ssh-key-2026-05-14.key`, host `ubuntu@92.5.76.247`.

## Next planned work (Phase 2B)

**Branch 1 — `phase-2b-foundation`:** DEPLOYED 2026-05-28 (merged to main). Measurement-only active; trading-impact flags off until Branch 2.

**Branch 2 — `phase-2b-exits`:** **DEPLOYED 2026-05-29 (gated)** — merged to main (`--no-ff` merge `4dd1e6e`, tags `phase-2b-branch2-deployed` / `pre-phase-2b-branch2`) and live on testnet with **both new flags default False** (zero trading-impact; deploy verified by in-container hash match + a clean 24h negative-check window — no ladder/protective/order activity, no errors). Implemented per commit `081ab43` on config slice `66d23b5`. BE+ stop, 5-tier TP ladder (4×20% tiers + 20% trailing runner), stop progression (BE+ → +0.2% → +0.5% → +1.0%), trailing runner, time-based exits (partial 15min / full 45min), slippage anomaly logging. Signals/regime/volume are explicitly OUT (Phase 2C). Full suite **150** passing. Activation is the staged operator-triggered flag sequence below — code is inert until then.

**Exit-ladder architecture (Branch 2):**
- Pure decision core: [backend/app/execution/exit_ladder.py](backend/app/execution/exit_ladder.py) — ATR-based tier prices, stop-progression, BE+ arm, runner callback-rate clamp, time-exit decisions. **Side-effect-free, Decimal math** (no float-noise tick errors). No I/O — exhaustively unit-tested.
- Effecting layer: `OrderManager.attach_ladder_orders` / `advance_ladder_stop` / `replace_ladder_tiers` / `cancel_orders`.
- Per-cycle management: `BotRunner._sync_ladder_trades` (routes ahead of the Branch 1 single-TP path when `ladder_active` on the trade).
- ATR captured at entry from the scored context (5m primary, 3m fallback, then `|entry−stop|`), persisted as `trade.extra["entry_atr"]`.

**Six-order exchange layout (per ladder position, all `workingType=MARK_PRICE`):**
1. **1× `STOP_MARKET`, `closePosition=True`** — the marching stop. closePosition auto-sizes to whatever remains, so tier fills never require resizing it — only re-placing at a higher `stopPrice`. Advanced via **place-new-then-cancel-old** (brief two-stop overlap, never unprotected).
2. **4× `TAKE_PROFIT_MARKET`, `reduceOnly=True`, quantity=20% each** — the TP tiers (closePosition can't express partial sizes, so tiers must be reduceOnly quantity orders).
3. **1× `TRAILING_STOP_MARKET`, `reduceOnly=True`, quantity=20%, `activationPrice`=TP4 price** — the runner; Binance auto-activates trailing when price reaches TP4.

**Stop progression (worst-of, on tier fills — never moves backward, never set at/above mark):** BE+ arms on first 0.5×ATR favorable move → stop to entry+20bps; TP1 fill → +0.2%; TP2 → +0.5%; TP3 → +1.0%; TP4 → runner active, stop floor stays at +1.0%. If the warranted top rung would sit above the current mark, the stop advances to the **highest feasible lower rung** instead of deferring entirely.

**Min-notional Option A (graceful degradation), per `tp_tier_min_notional_usdt`:** pick the largest piece-count C∈[2..5] where every piece clears min-qty + min-notional. C=5 → **full** ladder (4 tiers + runner); C∈{2,3,4} → **reduced** (C−1 tiers + runner); C<2 → **single** whole-position TP via the Branch 1 path (still protected, never naked). The ladder sync only manages full/reduced; single routes to Branch 1's single-TP sync.

**Two-step activation sequence (NEVER simultaneous):** flip `exchange_resting_exits_enabled` ON **first** to validate the simpler single-stop+TP resting path; once stable, flip `five_tier_ladder_enabled` ON **second**. Both default False. A `start()` consistency guard disables the ladder in-memory (logs `ladder_flag_inconsistent`) if ladder-on is ever seen while resting-off. The `_use_ladder_exits()` gate also respects the Branch 1 protective-orders circuit-breaker.

**Branch 2 ladder settings (defaults, in [settings.py](backend/app/config/settings.py)):** `five_tier_ladder_enabled=False`, `tp_tier_atr_multipliers=[0.3,0.6,1.0,1.6]` (**literal 14-period ATR**, not R), `tp_tier_size_pct=[0.2,0.2,0.2,0.2]`, `tp_tier_min_notional_usdt=5.0`, `be_plus_activation_atr_mult=0.5`, `be_plus_offset_bps=20.0`, `stop_ladder_pct=[0.0,0.002,0.005,0.010]`, `runner_trail_atr_mult=0.55`, `runner_trail_callback_clamp=[0.1,10.0]`, `time_exit_partial_minutes=15`, `time_exit_partial_pct=0.5`, `time_exit_full_minutes=45`, `slippage_anomaly_bps=30.0`.

**Audit:** every state transition emits a `RiskEvent` — `ladder_attached`, `ladder_tier_filled`, `ladder_slippage_anomaly`, `ladder_be_plus_armed`, `ladder_stop_advanced`, `ladder_runner_active`, `ladder_time_partial`, `ladder_time_full`, `ladder_min_notional_degraded`, and `ladder_trade_closed` (carries `counts_toward_150=True` — this is the queryable signal for the 150-trade metric).

**Known observability tripwire — `ladder_sync_anomaly`:** tier-fill detection treats "tier order absent from a fresh `fetch_open_orders` for the symbol" as a fill (position-closed-entirely is a separate branch; a `fetch_open_orders` exception bails the cycle so a transient API error never marks a false fill). Two accepted-for-testnet tradeoffs: (1) an out-of-band *cancel* would look like a fill; (2) no position-qty cross-check on the primary path. The tripwire makes these empirically visible: once per cycle, after that cycle's fills are booked, it does one extra `fetch_positions` and compares the live exchange quantity to `ladder_base_quantity − Σ(filled tier qty)`; if they diverge by **>5%** of the base it emits a `warning` `ladder_sync_anomaly` (payload: tiers, exchange_quantity vs expected_remaining, per-tier `fetch_order_status`, cumulative filled qty, drift_pct, timestamp). It is **observational only — never aborts detection**. `ladder_base_quantity` is the current ladder's entry qty, reset on the 15-min re-ladder so the partial time-exit doesn't trip it; the check runs once per cycle (not per tier) so multi-tier fills in one poll don't false-positive. **Acceptance bar: if anomaly count stays at 0 over 50+ ladder trades on testnet, the detection heuristic is confirmed sound; if it fires, the payload names which trades/tiers to investigate and signals whether to upgrade to a full position-qty cross-check.**

> **Branch 2 success criterion:** judged on a fully-closed-ladder-trade count with **positive net PnL = success**, counted at final exit (the `ladder_trade_closed` audit event; one trade = one position with all tiers + runner done). Pre-activation trades are "before" comparison only. If exits are clean but the bot is still negative at the verdict mark, the signal layer becomes the prime suspect and Phase 2C focuses there. **The count and start-point were refined at deploy time — see Pending Activation Sequence below (the judged clock is now 120 ladder trades from the *ladder* flip, not 150 from the resting flip).**

### Resting exits — 2026-05-31 activation (HISTORY — superseded, see top "CURRENT DEPLOYED STATE" block)

> ⚠️ **SUPERSEDED.** As of 2026-06-02 prod runs the fixed code with `exchange_resting_exits_enabled=False` and `five_tier_ladder_enabled=False` (both safe defaults), loop stopped. The narrative below is the historical 2026-05-31 activation, retained for context only — it is NOT the current runtime state.

**`exchange_resting_exits_enabled = True` is live in production (commit `3f527bc`).** On the 2026-05-31 re-activation the canary (`startup_adapter_test`) **passed** on `/fapi/v1/algoOrder` (place→GET→cancel→GET-confirm), and 3-state reconciliation attached real algo STOP_MARKET + TAKE_PROFIT_MARKET orders to open positions (verified exchange-side via `fetch_open_algo_orders`). The ladder stays OFF (`five_tier_ladder_enabled=False`). Two operational learnings from the activation:
- **`-2021 "Order would immediately trigger"` is an expected, graceful case.** A position whose pre-existing stop now sits on the wrong side of the market can't be a resting order by definition; the algo endpoint rejects it (same as `/fapi/v1/order` would), and that position **falls back to legacy polling**. A single such failure does **not** trip the circuit breaker (threshold 3/1h). Do not treat `-2021` reconciliation failures as a fault.
- **Attach latency watch:** the BCH reconciliation attach took **2373ms** (sequential place of 2 algo orders on testnet), exceeding the `protective_order_max_elapsed_ms=2000` *warn* threshold (soft warning, not a failure). If legitimate slow attaches accumulate, bump the threshold for algo-endpoint latency.

**Background — the 2026-05-30 first attempt failed and was rolled back the same day.** Flipping `exchange_resting_exits_enabled = True` triggered `startup_adapter_test` → Binance futures **testnet** (`demo-fapi.binance.com`) rejects `STOP_MARKET`/`TAKE_PROFIT_MARKET` on `/fapi/v1/order` with **`-4120` ("use the Algo Order API endpoints instead")**. The Branch 1 safety net worked exactly as designed: the probe order caught it (zero positions at risk), 3-state reconciliation's attach attempts on 4 open positions all failed cleanly (no orphan orders), the **circuit breaker tripped** (3 failures/1h), and the bot **self-degraded to legacy polling**. Flag **rolled back to `False`** (commit `d1927f4`, deployed image `66f79c2b7303`, verified). Bot kept running on legacy polling (operator decision — 4 open positions need management). Full write-up: [docs/phase-2b-activation-incident-2026-05-30.md](docs/phase-2b-activation-incident-2026-05-30.md).

**Adapter remediation — `phase-2b-adapter-algo-fix` + `-followup`: DEPLOYED 2026-05-31.** Investigation confirmed (Binance docs + live probe) a **mandatory, mainnet-wide migration effective 2025-12-09**: all conditional order types moved to the **Algo Order API**; `/fapi/v1/order` rejects them with `-4120`. `demo-fapi.binance.com` is the *current* testnet (the legacy URL would not help); the fix is forward-compatible to mainnet. **No ccxt** — the adapter is hand-rolled `httpx`. (Followup `-followup` merge `a25aa17` fixed two activation bugs — see below.)

**Algo Order adapter pattern (Approach B — explicit methods):**
- New `BinanceAdapter` methods: `place_algo_order` (`POST /fapi/v1/algoOrder`, `algoType=CONDITIONAL`), `cancel_algo_order` (`DELETE`), `fetch_algo_order` (`GET /fapi/v1/algoOrder`), `fetch_open_algo_orders` (**`GET /fapi/v1/openAlgoOrders`** — NOT `algoOpenOrders`; the intuitive/doc ordering is wrong and 404s with `-5000`. Verified by live probe; word transposition was the `-followup` Bug 2). Conditional types (STOP_MARKET / TAKE_PROFIT_MARKET / TRAILING_STOP_MARKET) route here; **entry orders (market/limit) stay on `place_order` → `/fapi/v1/order`, untouched.** `place_order` now **rejects** conditional types (guard against `-4120` recurrence).
- **Param renames** vs the old path: `stopPrice→triggerPrice`, `activationPrice→activatePrice`, `newClientOrderId→clientAlgoId`. Response id `algoId` and `algoStatus` are **normalized inside the adapter** to `OrderResult.order_id` / `.status`, so OrderManager/bot_runner never see the algoId/orderId distinction.
- **Cancel semantics:** success is **HTTP-status based** (2xx = cancelled). The testnet `DELETE` body returns a null `algoStatus`, so it is **not** trusted. Production cancels do NOT re-GET; only `startup_adapter_test` does the full place→GET→cancel→GET-confirm cycle (it's the verification path).
- **`clientAlgoId` pattern:** `proscalp-{trade8}-{role}-{nonce}` (trade id truncated to 10 hex for Binance's id-length limit; `role` ∈ `stop|tp|tp1..tp4|runner|test`). The `proscalp-` prefix is how startup reconciliation distinguishes our algo orders from foreign ones (foreign orders never touched).
- Rewired callers: `attach_protective_orders`, `attach_ladder_orders`/`replace_ladder_tiers`, `advance_ladder_stop` (now takes `trade_id`), `cancel_orders`/`cancel_protective_orders`; bot_runner `_ladder_detect_tier_fills` (`fetch_open_algo_orders` + `fetch_algo_order`), `_attribute_exchange_close`, `_startup_reconciliation`, `_startup_adapter_test`. **Known gap (follow-up):** `close_all_positions`/emergency paths still cancel only *regular* open orders — lingering algo orders are harmless once the position is flat (closePosition/reduceOnly can't fill), but should be added.
- **Live smoke test:** [scripts/algo_order_smoke.py](scripts/algo_order_smoke.py) — run inside the container against demo-fapi; validates the **full lifecycle incl. the LIST endpoint** (place → GET → **list (must appear)** → cancel → **list (must be gone)**) for all three types. **Mandatory staging-gate requirement (see below).**
- **`-followup` bug fixes (merge `a25aa17`, deployed 2026-05-31):** Bug 1 — `_startup_adapter_test` called `self._algo_client_id` (a staticmethod on `OrderManager`, not `BotRunner`) → AttributeError crashed the canary on the first real start; fixed + added the missing `_startup_adapter_test` unit test. Bug 2 — the `openAlgoOrders` endpoint above. Both shipped because mocks passed and the smoke test didn't exercise the LIST endpoint — now it does.

**First flip COMPLETE 2026-05-31** (resting exits live). `five_tier_ladder_enabled` remains `False`; the ladder code is complete and inert pending the second flip.

### Pending Activation Sequence (Branch 2) — HISTORY (superseded by the top block's ARMING sequence)

> ⚠️ **SUPERSEDED.** The 2026-06-01 second-flip ran the 29-trade discovery cohort that surfaced Bugs A/B; the cohort closed −$13.50, the loop was later stopped, and 2026-06-02 shipped the fixed code (ladder OFF). The authoritative go-forward arming steps are in the top "CURRENT DEPLOYED STATE" block. The sequence below is retained for historical context.

Activation is a **deliberate, staged, operator-triggered** sequence — never flip flags without an explicit operator request in-session:

- ~~**Day +1:** flip `exchange_resting_exits_enabled = True` only.~~ **✅ COMPLETE 2026-05-31** (commit `3f527bc`, after the adapter algo-fix + `-followup`). Resting exits + Branch 1 single-TP behavior live; ladder OFF. **Now in the ~20–25 closed-resting-trade validation window** (a Telegram milestone alert at ~20–25 trades is the trigger for the second-flip decision). This is validation, **not** part of the judged test.
- **Day +2–3 (after resting-orders proves clean):** operator flips **four** things together — **`five_tier_ladder_enabled = True`**, **`allow_unclear_regime_trading = True`**, **`bot_max_orders_per_cycle = 2`** (from 1), **and `bot_min_seconds_between_orders = 10`** (from 30). This starts the **120-trade judged clock**.
  - Rationale: the order-cap bump captures multi-signal cycles (volume bump; no change to per-trade economics or the correlated-exposure ceiling). `allow_unclear_regime_trading` widens trading to unclear regimes (separate lever, bundled by operator direction).
  - **⚠️ Coupled dependency — do not revert one without the other.** Raising `bot_max_orders_per_cycle` to 2 is a **silent no-op alone**: the per-cycle order loop ([bot_runner.py](backend/app/services/bot_runner.py) ~L611) breaks on `_order_cooldown_active()` after the first fill, and `bot_min_seconds_between_orders = 30` keeps that cooldown true for the rest of the ~10s cycle (just-placed order → ~0s elapsed < 30s → `break`). Lowering the cooldown to **10s** (≈ cycle length) removes the artificial bottleneck so the 2-per-cycle limit is reachable. Max order rate ≈ **12/min** — safely within Binance API limits. **Both must move together**; reverting either re-introduces the bottleneck.
  - **Aggregate caps deliberately left unchanged** (investigated 2026-05-30, not bugs — they are the intended design ceiling): `max_concurrent_trades=5`, `max_trades_per_day=50`, `max_trades_per_coin=1`, and the exposure caps (total ~35% / session ~39% / per-regime `max_open_risk_*`). After the cooldown is lifted, these become the natural binding ceiling on *sustained* volume — so throughput is gated by the ladder's **entry→full-exit speed** (how fast slots/exposure free up), which is one of the things the 120-trade test is meant to measure. There is **no** per-symbol or per-direction order cooldown; the only pace gate is the global `bot_min_seconds_between_orders`.
- **Verdict marks:** interim check at **60** closed ladder trades, full verdict at **120** closed ladder trades. Telegram milestone alerts at both. Count via `ladder_trade_closed` events (`counts_toward_150` flag — name retained from the original criterion; the operative target is now 120).

> **Criterion reconciliation:** the original recorded metric was "150 trades from the `exchange_resting_exits_enabled` flip." This sequence supersedes it: the resting flip is now a 20–25 trade smoke test, and the judged metric is **120 fully-closed ladder trades from the ladder flip**. The `counts_toward_150` payload key is left unchanged to avoid a code churn; treat "150" in that key name as historical.

**Branch 3 — `phase-2b-safety`:** not started. Flash-crash detection + daily reconciliation alert.

## Operational ground rules

Hard rules — do not violate without explicit operator approval *in the current session*:

- **Never deploy to mainnet.**
- **Never restart `postgres`, `nginx`, or `frontend` containers.** Only `backend` for code deploys.
- **Never delete** `.env` files or anything in `data/`, `logs/`, `backups/`.
- **Never modify code on `/opt/` directly.** Edits flow: Windows source → git commit → rsync deploy.
- **Never bypass the staging-dir test gate** before a production deploy (build test image, run pytest, smoke import — every time).
- **High-risk operations** (deploy, code changes, container restarts) require explicit operator approval at each step. **Low-risk operations** (read-only investigation, test runs, analysis) may proceed in batches.
- **Disclose mistakes transparently** rather than papering over them.

Default report length at STOP markers: **under 300 words.** Append detail only when asked or when something unexpected requires it.

## Known operational gaps

*Address after Phase 2A is stable:*

- ~~**No git remote**~~ — RESOLVED 2026-06-02: private remote `github.com/temmyjerrybooks/proscalp-ai-trader` (authed via the Windows machine's `gh` keyring). **Follow-up:** Oracle still has no repo auth — putting auth there would also close the improvised-deploy gap (clean `git clone` + build instead of tar full-replace).
- **No automated test in deploy pipeline** — staging+pytest is a manual gate.
- **Two grade systems** — reconcile to one to eliminate analysis confusion.
- **Indicator warm-up windows** — EMA200 on 120 candles is dominated by the seed; EMA50 marginal; leader `EMA21` at depth 80 under-warmed; VWAP is not session-anchored.

## Known Follow-ups (deferred to Phase 2C)

- **`close_all_positions` / emergency paths do not cancel algo orders** (ref: investigation in commit `c527bd3`). They cancel only *regular* `/fapi/v1/order` open orders; the resting protective/ladder orders are now algo orders (`/fapi/v1/algoOrder`). Financially harmless once the position is flat — a `closePosition`/`reduceOnly` algo order can't fill against zero exposure — but the leftover orders **count against Binance's per-symbol algo-order cap** until they expire/are cleared, which can block re-entry on that symbol. **Pre-mainnet requirement (hard gate):** `close_all_positions` (and `emergency_stop`) must cancel algo orders alongside regular orders via `fetch_open_algo_orders` + `cancel_algo_order` **before any mainnet deployment is considered.** Acceptable to leave on testnet.

---

Last updated: 2026-06-02 — ladder-fix (Bugs A/B + items 1–4) deployed INERT to prod; see top "CURRENT DEPLOYED STATE" block.
