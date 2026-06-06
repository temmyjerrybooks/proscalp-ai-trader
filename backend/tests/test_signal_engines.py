"""Pluggable signal-engine framework tests.

Locks the contract that makes engines switchable without touching execution:
- Classic is the registered default and a faithful pass-through to the existing
  ``_scan_for_signals`` (so ``mode="classic"`` is a no-op vs pre-framework).
- The registry resolves known modes, falls back to Classic on unknown/empty.
- The runner builds its engine from settings, switches at runtime, and reflects
  the active engine on its status.
- Every trade is stamped with ``extra['signal_engine']`` for per-engine attribution.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config.settings import Settings
from app.database.models import Trade
from app.services.bot_runner import BotRunner
from app.signal_engines import (
    ClassicEngine,
    EngineContext,
    ScoredSignal,
    SignalEngine,
    available_engines,
    engine_names,
    get_engine,
)
from app.signal_engines import registry
from app.strategies.base_strategy import StrategySignal


# --------------------------------------------------------------------------- fakes

class _FakeEngine(SignalEngine):
    name = "fakearb"
    description = "fake engine for tests"

    async def generate(self, runner, ctx):  # pragma: no cover - not exercised here
        return []


@pytest.fixture
def fake_engine():
    """Register a throwaway engine and remove it afterwards (no global pollution)."""
    registry.register_engine("fakearb", _FakeEngine)
    try:
        yield "fakearb"
    finally:
        registry._REGISTRY.pop("fakearb", None)


class _FakeDB:
    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None


def _signal() -> StrategySignal:
    return StrategySignal(
        setup_name="momentum_continuation",
        symbol="BTCUSDT",
        direction="long",
        entry_price=100.0,
        stop_loss=99.0,
        take_profit_levels=[101.0, 102.0, 103.0],
        trailing_stop=99.5,
        expected_move=2.0,
        risk_reward_ratio=2.0,
        confidence_score=80.0,
        accepted=True,
    )


def _scored() -> ScoredSignal:
    return ScoredSignal(
        signal=_signal(),
        score=SimpleNamespace(total=80, grade="A"),
        signal_id="sig-123",
        candidate=SimpleNamespace(approved=True, liquidity_score=90),
        context=SimpleNamespace(spread_bps=1.0),
    )


# --------------------------------------------------------------------------- registry

def test_classic_is_registered_default():
    assert "classic" in engine_names()
    engine = get_engine("classic")
    assert isinstance(engine, ClassicEngine)
    assert engine.name == "classic"


def test_get_engine_none_and_empty_fall_back_to_classic():
    assert get_engine(None).name == "classic"
    assert get_engine("").name == "classic"
    assert get_engine("   ").name == "classic"


def test_get_engine_unknown_falls_back_to_classic():
    assert get_engine("does-not-exist").name == "classic"


def test_get_engine_is_case_insensitive():
    assert get_engine("CLASSIC").name == "classic"


def test_available_engines_shape():
    engines = available_engines()
    names = {e["name"] for e in engines}
    assert "classic" in names
    for e in engines:
        assert e["description"]  # every engine advertises a non-empty description


def test_register_and_resolve(fake_engine):
    assert "fakearb" in engine_names()
    assert get_engine("fakearb").name == "fakearb"


# --------------------------------------------------------------------------- classic delegation

async def test_classic_engine_is_pass_through_to_scan_for_signals():
    """ClassicEngine must call the runner's existing scan with the ctx fields
    verbatim and return its result unchanged — proving mode=classic is a no-op."""
    sentinel = [object(), object()]
    captured: dict = {}

    async def fake_scan(**kwargs):
        captured.update(kwargs)
        return sentinel

    runner = SimpleNamespace(_scan_for_signals=fake_scan)
    ctx = EngineContext(
        db="DB",
        adapter="ADAPTER",
        candidates=["c1", "c2"],
        session="SESSION",
        regime="REGIME",
        btc_direction="long",
        eth_direction=None,
    )

    out = await ClassicEngine().generate(runner, ctx)

    assert out is sentinel
    assert captured == {
        "db": "DB",
        "adapter": "ADAPTER",
        "candidates": ["c1", "c2"],
        "session": "SESSION",
        "regime": "REGIME",
        "btc_direction": "long",
        "eth_direction": None,
    }


# --------------------------------------------------------------------------- runner integration

def test_settings_default_mode_is_classic():
    assert Settings().signal_engine_mode == "classic"


def test_runner_defaults_to_classic():
    runner = BotRunner(Settings())
    assert runner.engine.name == "classic"
    assert runner.signal_engine_mode() == "classic"
    assert runner.status.signal_engine == "classic"


def test_runner_honors_settings_mode(fake_engine):
    runner = BotRunner(Settings(signal_engine_mode="fakearb"))
    assert runner.engine.name == "fakearb"
    assert runner.status.signal_engine == "fakearb"


def test_runner_bad_settings_mode_falls_back(fake_engine):
    runner = BotRunner(Settings(signal_engine_mode="nope"))
    assert runner.engine.name == "classic"


def test_set_signal_engine_switches_and_reflects_on_status(fake_engine):
    runner = BotRunner(Settings())
    assert runner.engine.name == "classic"

    active = runner.set_signal_engine("fakearb")
    assert active == "fakearb"
    assert runner.engine.name == "fakearb"
    assert runner.status.signal_engine == "fakearb"

    # unknown switch is rejected back to classic, never leaves bot engine-less
    active = runner.set_signal_engine("ghost")
    assert active == "classic"
    assert runner.status.signal_engine == "classic"


# --------------------------------------------------------------------------- attribution

async def test_persist_execution_stamps_active_engine(fake_engine):
    """Every trade records which engine generated it — the scoreboard's key."""
    runner = BotRunner(Settings())
    runner.set_signal_engine("fakearb")

    db = _FakeDB()
    report = SimpleNamespace(
        accepted=True,
        paper_position=SimpleNamespace(id="trade-1"),
        order_result=None,
    )
    setup_assessment = SimpleNamespace(
        grade="A",
        risk_pct=1.0,
        base_risk_pct=1.0,
        risk_range=(0.5, 1.5),
        score_floor=0,
        score_ceiling=100,
        permission="ok",
        session_name="london",
    )
    position_size = SimpleNamespace(risk_amount=5.0)

    trade = await runner._persist_execution(
        db=db,
        scored=_scored(),
        report=report,
        quantity=1.0,
        setup_assessment=setup_assessment,
        position_size=position_size,
        session=SimpleNamespace(name="london"),
        regime=SimpleNamespace(regime="trending_up"),
    )

    assert isinstance(trade, Trade)
    assert trade.extra["signal_engine"] == "fakearb"
    assert trade in db.added
