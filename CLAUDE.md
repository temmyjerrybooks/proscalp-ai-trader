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

### Phase 2B Branch 1 — IMPLEMENTED, NOT DEPLOYED (branch `phase-2b-foundation`)

Foundation work (items 1–4 + clarifications) merged-ready but awaiting staging+deploy approval. All new flags default OFF for trading-impact, ON for measurement.

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
- **Use `docker compose up -d --no-deps backend`.** Without `--no-deps`, compose follows the `depends_on: postgres: service_healthy` chain and **recreates postgres** as a side effect. Data survives via the named `postgres_data` volume, but it's unnecessary churn and violates the "only-backend" rule. (Lesson from Phase 2A deploy.)

**DB raw-SQL gotcha:** `trades.extra` maps to column literally named `metadata`. Use `trades.metadata->>'grade'`.

**SSH access:** key at `C:\Users\PC\Downloads\ssh_keys\ssh-key-2026-05-14.key`, host `ubuntu@92.5.76.247`.

## Next planned work (Phase 2B)

**Branch 1 — `phase-2b-foundation`:** IMPLEMENTED locally; awaiting staging+deploy approval. (Items 1-4 + clarifications A/B/C + startup adapter test.)

**Branch 2 — `phase-2b-exits`:** not started. Builds on Branch 1: BE+ stop, 5-tier TP ladder, stop progression, trailing stop on runner, time-based exits, slippage anomaly logging.

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
