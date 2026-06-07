"""Mean-reversion engine — runs the MeanReversionScalp strategy across the universe.

Fades each coin's OWN over-extension (no BTC comparison), so a Bitcoin move no
longer makes everything signal one way. Reuses the runner's vetted scoring /
persistence / context helpers so its ScoredSignal is identical downstream (same
risk sizing, same userTrades exit ladder, same attribution).

Like stat_arb it's exempt from leader confirmation (it's not a trend-following
setup). The profitability lever — post-only maker entries — is a separate
execution change (see prefer_maker_entry), not the signal itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from app.data.market_data import MarketDataService
from app.signal_engines.base import SignalEngine
from app.signal_engines.types import EngineContext, ScoredSignal
from app.strategies.mean_reversion_scalp import MeanReversionScalp

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.services.bot_runner import BotRunner

logger = structlog.get_logger(__name__)


class MeanReversionEngine(SignalEngine):
    name = "mean_reversion"
    description = "Fade each coin's OWN over-extension (price vs its own ATR-scaled mean), reverting toward the mean. Best with post-only maker entries."
    # Reversion fades moves -> leader confirmation is N/A (same rationale as stat_arb).
    requires_leader_confirmation = False
    # Signals this engine's entries should rest as post-only (maker) limit orders.
    # The execution path honors this once post-only support ships; until then the
    # entry uses the standard (taker) path and the flag is a no-op.
    prefer_maker_entry = True

    def _strategy(self, settings) -> MeanReversionScalp:
        return MeanReversionScalp(
            lookback=settings.mr_lookback,
            atr_period=settings.mr_atr_period,
            entry_z=settings.mr_entry_z,
            stop_atr=settings.mr_stop_atr,
            er_n=settings.mr_er_n,
            er_max=(settings.mr_er_max if settings.mr_ranging_filter_enabled else None),
            timeframe=settings.mr_timeframe,
        )

    async def generate(self, runner: "BotRunner", ctx: EngineContext) -> list[ScoredSignal]:
        settings = runner.settings
        strategy = self._strategy(settings)
        market = MarketDataService(ctx.adapter)
        scored: list[ScoredSignal] = []

        active = [c for c in ctx.candidates if c.approved]
        for candidate in active[: settings.bot_cycle_symbol_limit]:
            try:
                bundle = await market.fetch_bundle(
                    candidate.symbol,
                    timeframes=["15m", "5m", "3m", "1m"],
                    candle_limit=120,
                )
            except Exception as exc:
                await runner._risk_event(
                    ctx.db, "warning", "market_data_error", str(exc), {"symbol": candidate.symbol}
                )
                continue

            context = runner._build_strategy_context(
                candidate, bundle, ctx.session, ctx.regime, ctx.btc_direction, ctx.eth_direction
            )
            signal = strategy.evaluate(context)
            signal_id = await runner._persist_signal(ctx.db, signal)
            if not signal.accepted:
                continue

            score = runner._score_signal(signal, context, candidate, ctx.regime, ctx.session, bundle)
            await runner._persist_score(ctx.db, signal_id, signal, score)

            minimum_score = runner._minimum_score_for_session(ctx.session)
            if score.total < minimum_score:
                await runner._risk_event(
                    ctx.db, "info", "setup_rejected",
                    f"mean_reversion setup score {score.total} below threshold {minimum_score}",
                    {"signal_id": signal_id, "symbol": signal.symbol, "setup": signal.setup_name,
                     "score": score.total},
                )
                continue

            scored.append(ScoredSignal(signal, score, signal_id, candidate, context))

        return scored
