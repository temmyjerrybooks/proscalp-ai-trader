"""Mean-reversion strategy + engine tests.

Strategy: judges each coin on its OWN extension (no BTC), so the direction depends
only on the coin's price vs its own mean -> long when oversold+turning up, short
when overbought+turning down, nothing when flat; optional ranging filter skips
clean trends. Engine: runs it across the universe and emits ScoredSignals.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from unittest.mock import AsyncMock

from app.config.settings import Settings, TradingMode
from app.exchanges.base import Candle
from app.regime.detector import RegimeResult
from app.services.bot_runner import BotRunner
from app.sessions.session_manager import SessionState
from app.signal_engines import EngineContext, MeanReversionEngine, get_engine
from app.strategies.base_strategy import StrategyContext
from app.strategies.mean_reversion_scalp import MeanReversionScalp, efficiency_ratio
from app.universe.top50_scanner import CoinCandidate


def _candles(closes: list[float]) -> list[Candle]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        Candle(timestamp=start + timedelta(minutes=5 * i), open=c, high=c + 0.01,
               low=c - 0.01, close=c, volume=1000.0)
        for i, c in enumerate(closes)
    ]


def _ctx(closes: list[float], allow_short: bool = True) -> StrategyContext:
    return StrategyContext(
        symbol="ETHUSDT", candles_by_timeframe={"5m": _candles(closes)},
        session_name="london", regime="good", coin_strength_score=80.0,
        allow_short=allow_short,
    )


OVERSOLD = [100.0] * 40 + [99.7, 99.5, 99.6]      # dropped far below mean, last bar up
OVERBOUGHT = [100.0] * 40 + [100.3, 100.5, 100.4]  # spiked above mean, last bar down
FLAT = [100.0] * 43


# ------------------------------------------------------------------ strategy

def test_long_when_oversold_and_turning_up():
    sig = MeanReversionScalp(entry_z=2.0).evaluate(_ctx(OVERSOLD))
    assert sig.accepted and sig.direction == "long"
    assert sig.stop_loss < sig.entry_price            # long stop below
    assert sig.take_profit_levels[-1] > sig.entry_price  # TP toward (higher) mean


def test_short_when_overbought_and_turning_down():
    sig = MeanReversionScalp(entry_z=2.0).evaluate(_ctx(OVERBOUGHT))
    assert sig.accepted and sig.direction == "short"
    assert sig.stop_loss > sig.entry_price
    assert sig.take_profit_levels[-1] < sig.entry_price


def test_flat_market_rejects():
    assert MeanReversionScalp(entry_z=2.0).evaluate(_ctx(FLAT)).accepted is False


def test_short_blocked_on_spot():
    sig = MeanReversionScalp(entry_z=2.0).evaluate(_ctx(OVERBOUGHT, allow_short=False))
    assert sig.accepted is False


def test_efficiency_ratio_trend_vs_chop():
    trend = [100.0 + i for i in range(21)]
    chop = [100.0 + (1 if i % 2 else -1) for i in range(21)]
    assert efficiency_ratio(trend, 20) > 0.9
    assert efficiency_ratio(chop, 20) < 0.3


def test_ranging_filter_skips_clean_trend():
    trend_then_tick = [100.0 - i * 0.3 for i in range(42)] + [100.0 - 41 * 0.3 + 0.1]
    ctx = _ctx(trend_then_tick)
    # no filter: oversold + last bar up -> would go long
    assert MeanReversionScalp(entry_z=2.0, er_max=None).evaluate(ctx).accepted is True
    # filter on: it's a clean trend (high ER) -> skipped
    assert MeanReversionScalp(entry_z=2.0, er_max=0.3).evaluate(ctx).accepted is False


# ------------------------------------------------------------------ engine + registry

def test_registry_and_flags():
    eng = get_engine("mean_reversion")
    assert eng.name == "mean_reversion"
    assert eng.requires_leader_confirmation is False
    assert eng.prefer_maker_entry is True


class _FakeMarketAdapter:
    def __init__(self, closes: list[float]):
        self._candles = _candles(closes)

    async def fetch_ohlcv(self, symbol, timeframe, limit=220):
        return self._candles

    async def fetch_order_book(self, symbol, limit=50):
        return None


def _candidate(symbol="ETHUSDT") -> CoinCandidate:
    return CoinCandidate(
        symbol=symbol, rank=1, score=90.0, quote_volume=1e9, spread_bps=2.0,
        volatility_pct=1.0, liquidity_score=90.0, market_cap_rank=2, approved=True, reasons=[],
    )


def _session() -> SessionState:
    return SessionState(name="london", active=True, tradable=True, aggression_mode=False,
                        start_utc=None, end_utc=None, user_time=None, notes="")


def _runner() -> BotRunner:
    runner = BotRunner(Settings(signal_engine_mode="mean_reversion", trading_mode=TradingMode.TESTNET))
    runner._persist_signal = AsyncMock(return_value="sig-1")
    runner._persist_score = AsyncMock()
    runner._risk_event = AsyncMock()
    return runner


async def test_engine_emits_long_for_oversold_alt():
    runner = _runner()
    ctx = EngineContext(
        db=object(), adapter=_FakeMarketAdapter(OVERSOLD), candidates=[_candidate()],
        session=_session(), regime=RegimeResult("good", 85, True, 8, []),
        btc_direction="long", eth_direction=None,
    )
    out = await MeanReversionEngine().generate(runner, ctx)
    assert len(out) == 1
    assert out[0].signal.direction == "long"
    assert out[0].signal.setup_name == "mean_reversion_scalp"
    runner._persist_signal.assert_awaited()


async def test_engine_emits_nothing_when_flat():
    runner = _runner()
    ctx = EngineContext(
        db=object(), adapter=_FakeMarketAdapter(FLAT), candidates=[_candidate()],
        session=_session(), regime=RegimeResult("good", 85, True, 8, []),
        btc_direction="long", eth_direction=None,
    )
    out = await MeanReversionEngine().generate(runner, ctx)
    assert out == []
