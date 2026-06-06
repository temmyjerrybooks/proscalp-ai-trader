"""The Classic engine — today's 7-strategy scoring pipeline, unchanged.

This is a thin adapter over the runner's existing ``_scan_for_signals``: for each
approved candidate it runs every registered strategy, scores them, and keeps the
best-scoring setup per symbol. Wrapping it (rather than moving the logic) makes
``mode="classic"`` a *byte-for-byte no-op* versus the pre-framework behavior, so
the framework can ship inert and be proven equivalent before any new engine is
switched on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.signal_engines.base import SignalEngine
from app.signal_engines.types import EngineContext, ScoredSignal

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.services.bot_runner import BotRunner


class ClassicEngine(SignalEngine):
    name = "classic"
    description = "Original 7-setup scoring pipeline (Asia continuation, VWAP reclaim, breakout-retest, liquidity sweep, momentum, range bounce, BTC-led alt)."

    async def generate(self, runner: "BotRunner", ctx: EngineContext) -> list[ScoredSignal]:
        return await runner._scan_for_signals(
            db=ctx.db,
            adapter=ctx.adapter,
            candidates=ctx.candidates,
            session=ctx.session,
            regime=ctx.regime,
            btc_direction=ctx.btc_direction,
            eth_direction=ctx.eth_direction,
        )
