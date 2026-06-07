"""Pluggable signal-engine layer.

Each engine is a switchable strategy family that produces ``ScoredSignal``s; the
runner selects one per cycle via ``signal_engine_mode``. Risk sizing, execution,
and the exit ladder are shared and engine-agnostic.
"""

from app.signal_engines.base import SignalEngine
from app.signal_engines.classic import ClassicEngine
from app.signal_engines.mean_reversion import MeanReversionEngine
from app.signal_engines.stat_arb import StatArbEngine
from app.signal_engines.registry import (
    available_engines,
    engine_names,
    get_engine,
    register_engine,
)
from app.signal_engines.types import EngineContext, ScoredSignal

__all__ = [
    "SignalEngine",
    "ClassicEngine",
    "StatArbEngine",
    "MeanReversionEngine",
    "EngineContext",
    "ScoredSignal",
    "available_engines",
    "engine_names",
    "get_engine",
    "register_engine",
]
