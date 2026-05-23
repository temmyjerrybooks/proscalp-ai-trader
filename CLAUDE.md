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

## Deployment mechanics

Windows source is canonical. Oracle target: `/opt/proscalp-ai-trader/`. **Deploy via `rsync -a --delete`** from Windows post-merge `main` to Oracle (NOT the legacy [scripts/deploy_oracle_from_windows.ps1](scripts/deploy_oracle_from_windows.ps1) — its tar-overlay doesn't delete removed files).

Production image: `proscalp-ai-trader-backend:latest`, built from [backend/Dockerfile](backend/Dockerfile), **Python 3.12.13**.

**Test gate (always before prod deploy):** stage in separate dir (e.g. `/opt/proscalp-phase2a-test/`), `docker build -t proscalp:<phase>-test -f backend/Dockerfile backend/`, run pytest **bind-mounting** `tests/` + `pyproject.toml` read-only (Dockerfile only copies `app/`; tests aren't baked in).

**Containers (docker compose):** `postgres`, `backend`, `frontend`, `nginx`. **Only restart `backend` for code deploys** — never touch the other three.

**DB raw-SQL gotcha:** `trades.extra` maps to column literally named `metadata`. Use `trades.metadata->>'grade'`.

**SSH access:** key at `C:\Users\PC\Downloads\ssh_keys\ssh-key-2026-05-14.key`, host `ubuntu@92.5.76.247`.

## Next planned work (Phase 2B)

1. Replace mid-price-polling exits with exchange-resting TP/SL orders (RF#3 — primary remaining bleed).
2. Wire `PaperTradingSimulator.update_price` into the live paper loop (currently only used by backtester — split-brain equity).
3. Make shadow replay fee-aware so `follow_up_pnl_pct` is directly comparable to realized.
4. Add measurement infrastructure (daily reconciliation, dashboards).

**Do NOT start Phase 2B until Phase 2A has accumulated ≥50 closed trades post-deploy AND the operator has reviewed the results.**

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
