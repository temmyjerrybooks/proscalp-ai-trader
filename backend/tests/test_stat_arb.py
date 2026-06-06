"""Stat-Arb engine tests.

Two layers:
- Pure decision core (``stat_arb_core``): spread construction, z-score guards, the
  long/short/no-signal logic, the reversion-turn gate, conviction mapping.
- Engine orchestration (``StatArbEngine.generate``): with a fake market adapter and
  a real ``BotRunner`` whose DB-touching helpers are patched, prove it emits a
  correctly-directed ``ScoredSignal`` for a stretched alt and nothing for a calm one.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from unittest.mock import AsyncMock

from app.config.settings import Settings, TradingMode
from app.exchanges.base import Candle
from app.regime.detector import RegimeResult
from app.services.bot_runner import BotRunner
from app.sessions.session_manager import SessionState
from app.signal_engines import EngineContext
from app.signal_engines.stat_arb import StatArbEngine
from app.signal_engines.stat_arb_core import (
    conviction_score,
    log_relative_spread,
    reversion_signal,
    zscore_last,
)
from app.universe.top50_scanner import CoinCandidate


# ------------------------------------------------------------------ pure core

def test_log_relative_spread_tail_aligns_unequal_lengths():
    alt = [1.0, 2.0, 4.0]
    btc = [10.0, 10.0]
    spread = log_relative_spread(alt, btc)
    # aligned to the shorter (2): ln(2/10), ln(4/10)
    assert len(spread) == 2
    assert spread[0] == pytest.approx(-1.6094379, abs=1e-5)


def test_log_relative_spread_skips_nonpositive():
    assert log_relative_spread([0.0, -1.0], [10.0, 10.0]) == []
    assert log_relative_spread([], [1.0]) == []


def test_zscore_last_none_when_short_or_flat():
    assert zscore_last([1.0, 1.0], 5) is None          # too few points
    assert zscore_last([3.0] * 10, 10) is None          # zero variance


def test_zscore_last_value():
    series = [0.0, 0.0, 0.0, 0.0, 4.0]
    out = zscore_last(series, 5)
    assert out is not None
    z, mu, sd = out
    assert mu == pytest.approx(0.8)
    assert z > 0  # last value well above the mean


def test_reversion_long_when_alt_cheap_and_turning_up():
    btc = [100.0] * 70
    alt = [10.0] * 68 + [9.0, 9.05]  # dropped vs btc (cheap), last bar ticks back up
    d = reversion_signal(alt, btc, lookback=60, entry_z=1.5, require_turn=True)
    assert d is not None
    assert d.direction == "long"
    assert d.z < 0 and d.abs_z >= 1.5
    assert d.turned is True


def test_reversion_short_when_alt_rich_and_turning_down():
    btc = [100.0] * 70
    alt = [10.0] * 68 + [11.0, 10.95]  # spiked vs btc (rich), last bar ticks back down
    d = reversion_signal(alt, btc, lookback=60, entry_z=1.5, require_turn=True)
    assert d is not None
    assert d.direction == "short"
    assert d.z > 0


def test_reversion_none_below_threshold():
    btc = [100.0] * 70
    alt = [10.0] * 69 + [10.001]  # essentially tracking btc
    assert reversion_signal(alt, btc, lookback=60, entry_z=2.0) is None


def test_reversion_require_turn_gate():
    btc = [100.0] * 70
    # alt cheap and STILL falling (moving away from mean) -> no signal when turn required
    alt = [10.0] * 68 + [9.1, 9.0]
    assert reversion_signal(alt, btc, lookback=60, entry_z=1.5, require_turn=True) is None
    # but with the gate off, the stretch alone qualifies
    d = reversion_signal(alt, btc, lookback=60, entry_z=1.5, require_turn=False)
    assert d is not None and d.direction == "long"


def test_conviction_score_monotonic_capped_and_zero_below():
    assert conviction_score(1.0, 2.0) == 0.0            # below threshold
    assert conviction_score(2.0, 2.0) == 60.0           # at threshold = base
    assert conviction_score(3.0, 2.0) == 75.0           # +15 per sigma
    assert conviction_score(100.0, 2.0) == 100.0        # capped


# ------------------------------------------------------------------ engine orchestration

def _candles(closes: list[float]) -> list[Candle]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    out: list[Candle] = []
    for i, close in enumerate(closes):
        out.append(
            Candle(
                timestamp=start + timedelta(minutes=5 * i),
                open=close,
                high=close * 1.002,
                low=close * 0.998,
                close=close,
                volume=1000.0,
            )
        )
    return out


class _FakeMarketAdapter:
    """Returns crafted OHLCV per symbol; no order book (engine falls back to spread)."""

    def __init__(self, series_by_symbol: dict[str, list[float]]):
        self._series = series_by_symbol

    async def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 220) -> list[Candle]:
        return _candles(self._series.get(symbol, []))

    async def fetch_order_book(self, symbol: str, limit: int = 50):
        return None


def _candidate(symbol: str) -> CoinCandidate:
    return CoinCandidate(
        symbol=symbol, rank=1, score=90.0, quote_volume=1e9, spread_bps=2.0,
        volatility_pct=1.0, liquidity_score=90.0, market_cap_rank=2, approved=True, reasons=[],
    )


def _session() -> SessionState:
    return SessionState(
        name="london", active=True, tradable=True, aggression_mode=False,
        start_utc=None, end_utc=None, user_time=None, notes="",
    )


def _runner(**overrides) -> BotRunner:
    settings = Settings(signal_engine_mode="stat_arb", trading_mode=TradingMode.TESTNET, **overrides)
    runner = BotRunner(settings)
    # Patch the only DB-touching helpers the engine calls; keep scoring/context real.
    runner._persist_signal = AsyncMock(return_value="sig-1")
    runner._persist_score = AsyncMock()
    runner._risk_event = AsyncMock()
    return runner


async def test_engine_emits_long_for_cheap_alt():
    runner = _runner(stat_arb_entry_z=1.5)
    adapter = _FakeMarketAdapter({
        "BTCUSDT": [100.0] * 70,
        "ETHUSDT": [10.0] * 68 + [9.0, 9.05],  # cheap vs btc, turning up
    })
    ctx = EngineContext(
        db=object(), adapter=adapter, candidates=[_candidate("ETHUSDT")],
        session=_session(), regime=RegimeResult("trending_up", 85, True, 8, []),
        btc_direction="long", eth_direction=None,
    )

    out = await StatArbEngine().generate(runner, ctx)

    assert len(out) == 1
    scored = out[0]
    assert scored.signal.symbol == "ETHUSDT"
    assert scored.signal.direction == "long"
    assert scored.signal.setup_name == "stat_arb_btc_reversion"
    assert scored.signal.stop_loss < scored.signal.entry_price  # long stop below entry
    assert scored.score.total >= runner._minimum_score_for_session(ctx.session)
    runner._persist_signal.assert_awaited_once()
    runner._persist_score.assert_awaited_once()


async def test_engine_emits_nothing_for_calm_alt():
    runner = _runner(stat_arb_entry_z=2.0)
    adapter = _FakeMarketAdapter({
        "BTCUSDT": [100.0] * 70,
        "ETHUSDT": [10.0] * 69 + [10.001],  # tracking btc -> no stretch
    })
    ctx = EngineContext(
        db=object(), adapter=adapter, candidates=[_candidate("ETHUSDT")],
        session=_session(), regime=RegimeResult("trending_up", 85, True, 8, []),
        btc_direction="long", eth_direction=None,
    )

    out = await StatArbEngine().generate(runner, ctx)
    assert out == []
    runner._persist_signal.assert_not_awaited()


async def test_engine_skips_reference_symbol():
    runner = _runner(stat_arb_entry_z=1.5)
    adapter = _FakeMarketAdapter({"BTCUSDT": [100.0] * 70})
    ctx = EngineContext(
        db=object(), adapter=adapter, candidates=[_candidate("BTCUSDT")],
        session=_session(), regime=RegimeResult("trending_up", 85, True, 8, []),
        btc_direction="long", eth_direction=None,
    )
    out = await StatArbEngine().generate(runner, ctx)
    assert out == []
