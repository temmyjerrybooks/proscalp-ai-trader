# CLAUDE.md

> Read this file first at the start of every session before doing other work.

## Project overview

ProScalp is a Python crypto scalping bot. Stack: FastAPI + asyncio, SQLAlchemy + Postgres, Docker Compose, Binance via testnet/mainnet adapters (a Bybit adapter also exists). Deployed on Oracle Cloud (`ubuntu@92.5.76.247`, `/opt/proscalp-ai-trader/`); source is **Windows-canonical** at `c:\Users\PC\Scalping_Bot` — the only place with git history. **No git remote yet** — single source of failure on the Windows machine. **Trading mode: testnet only. Never deploy to mainnet without explicit operator approval in the current session.**

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

### Pending Activation Sequence (Branch 2) — agreed 2026-05-29, NOT yet executed

Deployed gated (both new flags default False) on 2026-05-29. Activation is a **deliberate, staged, operator-triggered** sequence — never flip flags without an explicit operator request in-session:

- **Day +1 (24h after deploy monitor approval):** operator flips **`exchange_resting_exits_enabled = True` only**. Activates exchange-resting orders + Branch 1 single-TP behavior; **ladder stays OFF**. Monitor ~**20–25 trades** for clean resting-order operation. This is a validation phase, **not** part of the judged test.
- **Day +2–3 (after resting-orders proves clean):** operator flips **four** things together — **`five_tier_ladder_enabled = True`**, **`allow_unclear_regime_trading = True`**, **`bot_max_orders_per_cycle = 2`** (from 1), **and `bot_min_seconds_between_orders = 10`** (from 30). This starts the **120-trade judged clock**.
  - Rationale: the order-cap bump captures multi-signal cycles (volume bump; no change to per-trade economics or the correlated-exposure ceiling). `allow_unclear_regime_trading` widens trading to unclear regimes (separate lever, bundled by operator direction).
  - **⚠️ Coupled dependency — do not revert one without the other.** Raising `bot_max_orders_per_cycle` to 2 is a **silent no-op alone**: the per-cycle order loop ([bot_runner.py](backend/app/services/bot_runner.py) ~L611) breaks on `_order_cooldown_active()` after the first fill, and `bot_min_seconds_between_orders = 30` keeps that cooldown true for the rest of the ~10s cycle (just-placed order → ~0s elapsed < 30s → `break`). Lowering the cooldown to **10s** (≈ cycle length) removes the artificial bottleneck so the 2-per-cycle limit is reachable. Max order rate ≈ **12/min** — safely within Binance API limits. **Both must move together**; reverting either re-introduces the bottleneck.
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

- **No git remote** — Windows machine is a single point of failure. Set up a private GitHub/GitLab remote.
- **No automated test in deploy pipeline** — staging+pytest is a manual gate.
- **Two grade systems** — reconcile to one to eliminate analysis confusion.
- **Indicator warm-up windows** — EMA200 on 120 candles is dominated by the seed; EMA50 marginal; leader `EMA21` at depth 80 under-warmed; VWAP is not session-anchored.

---

Last updated: 2026-05-23 after Phase 2A merge
