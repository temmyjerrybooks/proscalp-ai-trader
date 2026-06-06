"""Shared types for the pluggable signal-engine layer.

`ScoredSignal` is the single currency of the signal layer: every engine
*produces* a list of them, and the execution path + the (userTrades-authoritative)
exit ladder *consume* them unchanged. It lives here — rather than in
``bot_runner`` — so new engines can construct it without importing the runner
(which would be a circular import). ``bot_runner`` re-exports it for back-compat.

``EngineContext`` is the immutable per-cycle input bundle handed to an engine's
``generate``: the data and market state the runner has already assembled for the
current cycle. Engines read from it; they never mutate runner state directly.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.exchanges.base import ExchangeAdapter
from app.regime.detector import RegimeResult
from app.scoring.setup_score import SetupScoreResult
from app.sessions.session_manager import SessionState
from app.strategies.base_strategy import Direction, StrategyContext, StrategySignal
from app.universe.top50_scanner import CoinCandidate


@dataclass(slots=True)
class ScoredSignal:
    """A strategy signal that passed scoring and is ready for execution.

    Whatever the engine, the downstream contract is identical: ``signal`` carries
    the entry/stop/TP geometry, ``score`` the setup score used for sizing/grading,
    ``signal_id`` the persisted Signal row, ``candidate`` the universe context, and
    ``context`` the per-symbol StrategyContext.
    """

    signal: StrategySignal
    score: SetupScoreResult
    signal_id: str
    candidate: CoinCandidate
    context: StrategyContext


@dataclass(slots=True)
class EngineContext:
    """Per-cycle inputs an engine needs to generate signals.

    This is everything ``run_cycle`` has already computed by the time signal
    generation runs (watchlist, regime, session, leader directions). Engines that
    need extra data (order flow, funding, open interest, news) fetch it themselves
    via ``adapter`` / their own data providers — keeping the runner agnostic to
    each engine's data dependencies.
    """

    db: AsyncSession
    adapter: ExchangeAdapter
    candidates: list[CoinCandidate]
    session: SessionState
    regime: RegimeResult
    btc_direction: Direction | None = None
    eth_direction: Direction | None = None
