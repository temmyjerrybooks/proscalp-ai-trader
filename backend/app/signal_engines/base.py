"""The pluggable signal-engine interface.

A ``SignalEngine`` is a self-contained strategy family. The runner selects one
engine per cycle (by ``signal_engine_mode``) and asks it for signals; everything
downstream — risk sizing, execution, the exit ladder, attribution — is shared and
engine-agnostic. Switching engines therefore changes *only* how entry signals are
generated, never how positions are sized, protected, or exited.

Engines receive the ``BotRunner`` so they can reuse its vetted helpers
(``_build_strategy_context``, ``_score_signal``, ``_persist_signal``,
``_risk_event``, …) rather than re-implementing scoring/persistence. The runner
is passed as an argument (not stored) and type-hinted under ``TYPE_CHECKING`` to
avoid a circular import.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from app.signal_engines.types import EngineContext, ScoredSignal

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.services.bot_runner import BotRunner


class SignalEngine(ABC):
    """Base class for a switchable strategy family.

    Subclasses set ``name`` (the stable key used by the setting / API / UI and the
    value stamped onto each trade's ``extra['signal_engine']`` for attribution)
    and ``description`` (human-readable, surfaced in the UI dropdown), and
    implement ``generate``.
    """

    name: str = "base"
    description: str = ""

    @abstractmethod
    async def generate(self, runner: "BotRunner", ctx: EngineContext) -> list[ScoredSignal]:
        """Return the cycle's tradable, scored signals (best per symbol).

        Must be side-effect-light: persisting Signal/Score rows and emitting
        RiskEvents via the runner's helpers is expected, but the engine must not
        place orders or mutate open trades — execution is the runner's job.
        """
        raise NotImplementedError
