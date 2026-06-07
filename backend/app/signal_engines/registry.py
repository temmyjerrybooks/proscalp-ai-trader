"""Registry / factory for switchable signal engines.

One place maps a ``signal_engine_mode`` string to a concrete engine. The runner
resolves its engine through here; the API exposes ``available_engines()`` to
populate the UI dropdown. Adding an engine is a single registration line.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

import structlog

from app.signal_engines.base import SignalEngine
from app.signal_engines.classic import ClassicEngine
from app.signal_engines.mean_reversion import MeanReversionEngine
from app.signal_engines.stat_arb import StatArbEngine

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.config.settings import Settings

logger = structlog.get_logger(__name__)

DEFAULT_MODE = "classic"

# name -> zero-arg factory. Engines are cheap stateless objects, built on demand.
_REGISTRY: dict[str, Callable[[], SignalEngine]] = {
    "classic": ClassicEngine,
    "stat_arb": StatArbEngine,
    "mean_reversion": MeanReversionEngine,
}


def register_engine(name: str, factory: Callable[[], SignalEngine]) -> None:
    """Register an engine factory under ``name`` (idempotent overwrite)."""
    _REGISTRY[name] = factory


def engine_names() -> list[str]:
    return list(_REGISTRY)


def available_engines() -> list[dict[str, str]]:
    """``[{name, description}]`` for every registered engine — for the UI dropdown."""
    out: list[dict[str, str]] = []
    for name, factory in _REGISTRY.items():
        engine = factory()
        out.append({"name": name, "description": engine.description})
    return out


def get_engine(mode: str | None, settings: "Settings | None" = None) -> SignalEngine:
    """Resolve a mode to an engine instance.

    Unknown / empty modes fall back to the Classic engine (logged) so a bad
    setting can never leave the bot with no signal source.
    """
    key = (mode or DEFAULT_MODE).strip().lower()
    factory = _REGISTRY.get(key)
    if factory is None:
        logger.warning("signal_engine_unknown_mode", requested=mode, fallback=DEFAULT_MODE)
        factory = _REGISTRY[DEFAULT_MODE]
    return factory()
