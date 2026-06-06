"""Stat-Arb engine — single-leg BTC-relative mean reversion.

Per cycle: fetch BTC as the reference, then for each approved candidate measure how
stretched its ``ln(alt/btc)`` spread is (z-score over a lookback). When |z| crosses
the entry threshold and the spread has started reverting, emit ONE outright signal
on the alt toward its mean (long if cheap vs BTC, short if rich).

It deliberately reuses the runner's existing helpers — ``_build_strategy_context``,
``_score_signal``, ``_persist_signal``/``_persist_score``, ``_minimum_score_for_session``,
``_risk_event`` — so the produced ``ScoredSignal`` is indistinguishable downstream:
same risk sizing, same (userTrades-authoritative) exit ladder, same attribution.
Only the *entry decision* differs. Stop is ATR-based; TP geometry comes from the
shared ``BaseStrategy.build_signal`` R-multiples.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from app.data.market_data import MarketDataService
from app.signal_engines.base import SignalEngine
from app.signal_engines.stat_arb_core import conviction_score, reversion_signal
from app.signal_engines.types import EngineContext, ScoredSignal
from app.strategies.base_strategy import BaseStrategy

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.services.bot_runner import BotRunner

logger = structlog.get_logger(__name__)

# Default stop fallback when ATR is unavailable (fraction of entry price).
_ATR_FALLBACK_PCT = 0.004


class _StatArbSignalBuilder(BaseStrategy):
    """Reuses the shared, tested entry-geometry (TP R-multiples, trailing, RR)."""

    name = "stat_arb_btc_reversion"
    preferred_timeframe = "5m"


_BUILDER = _StatArbSignalBuilder()


class StatArbEngine(SignalEngine):
    name = "stat_arb"
    description = "Single-leg BTC-relative mean reversion: fade alts stretched far from their ln(alt/BTC) mean (z-score), entering back toward the mean."

    async def generate(self, runner: "BotRunner", ctx: EngineContext) -> list[ScoredSignal]:
        settings = runner.settings
        tf = settings.stat_arb_timeframe
        lookback = settings.stat_arb_lookback
        entry_z = settings.stat_arb_entry_z
        reference = settings.stat_arb_reference_symbol
        candle_limit = max(120, lookback + 40)

        market = MarketDataService(ctx.adapter)

        # Reference leg: BTC closes on the working timeframe.
        try:
            btc_bundle = await market.fetch_bundle(reference, timeframes=[tf], candle_limit=candle_limit)
        except Exception as exc:
            await runner._risk_event(
                ctx.db, "warning", "market_data_error",
                f"stat_arb reference fetch failed: {exc}", {"symbol": reference},
            )
            return []
        btc_closes = [c.close for c in btc_bundle.candles_by_timeframe.get(tf, [])]
        if len(btc_closes) < lookback:
            await runner._risk_event(
                ctx.db, "info", "stat_arb_reference_short",
                f"stat_arb reference has {len(btc_closes)} < {lookback} bars; skipping cycle",
                {"symbol": reference, "bars": len(btc_closes), "lookback": lookback},
            )
            return []

        scored: list[ScoredSignal] = []
        active = [c for c in ctx.candidates if c.approved and c.symbol != reference]
        for candidate in active[: settings.bot_cycle_symbol_limit]:
            try:
                bundle = await market.fetch_bundle(
                    candidate.symbol,
                    timeframes=["15m", "5m", "3m", "1m"],
                    candle_limit=candle_limit,
                )
            except Exception as exc:
                await runner._risk_event(
                    ctx.db, "warning", "market_data_error", str(exc), {"symbol": candidate.symbol}
                )
                continue

            alt_closes = [c.close for c in bundle.candles_by_timeframe.get(tf, [])]
            decision = reversion_signal(
                alt_closes, btc_closes,
                lookback=lookback, entry_z=entry_z, require_turn=settings.stat_arb_require_turn,
            )
            if decision is None:
                continue
            if decision.direction == "short" and not (settings.market_type == "futures"):
                continue  # cannot short on spot

            entry = alt_closes[-1]
            snapshot = bundle.indicators_by_timeframe.get(tf) or bundle.indicators_by_timeframe.get("3m")
            atr = snapshot.atr if (snapshot and snapshot.atr > 0) else entry * _ATR_FALLBACK_PCT
            stop = entry - atr * settings.stat_arb_stop_atr_mult if decision.direction == "long" \
                else entry + atr * settings.stat_arb_stop_atr_mult
            confidence = conviction_score(decision.abs_z, entry_z)
            reasons = [
                f"alt {'cheap' if decision.direction == 'long' else 'rich'} vs {reference}",
                f"z={decision.z:+.2f} (entry {entry_z:.2f}), reverting",
            ]
            signal = _BUILDER.build_signal(
                _build_min_context(candidate.symbol, settings.market_type),
                decision.direction, entry, stop, confidence, reasons,
            )
            if not signal.accepted:
                continue

            signal_id = await runner._persist_signal(ctx.db, signal)
            context = runner._build_strategy_context(
                candidate, bundle, ctx.session, ctx.regime, ctx.btc_direction, ctx.eth_direction
            )
            score = runner._score_signal(signal, context, candidate, ctx.regime, ctx.session, bundle)
            await runner._persist_score(ctx.db, signal_id, signal, score)

            minimum_score = runner._minimum_score_for_session(ctx.session)
            if score.total < minimum_score:
                await runner._risk_event(
                    ctx.db, "info", "setup_rejected",
                    f"stat_arb setup score {score.total} below threshold {minimum_score}",
                    {"signal_id": signal_id, "symbol": signal.symbol, "setup": signal.setup_name,
                     "score": score.total, "z": round(decision.z, 3)},
                )
                continue

            scored.append(ScoredSignal(signal, score, signal_id, candidate, context))

        return scored


def _build_min_context(symbol: str, market_type: str):
    """Minimal StrategyContext for ``build_signal`` (it only reads candles on the
    reject path, which we never hit because risk>0 is guaranteed by an ATR stop)."""
    from app.strategies.base_strategy import StrategyContext

    return StrategyContext(
        symbol=symbol,
        candles_by_timeframe={},
        session_name="",
        regime="",
        coin_strength_score=0.0,
        allow_short=(market_type == "futures"),
    )
