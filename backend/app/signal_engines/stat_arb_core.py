"""Pure decision core for the single-leg BTC-relative mean-reversion engine.

Edge thesis: a liquid alt and BTC move together, so the *relative* price
``ln(alt/btc)`` is mean-reverting around a slow level. When an alt gets stretched
far from that level (a high |z-score|), it tends to revert. We trade that
reversion with a single outright leg on the alt (the approved v1 scope) — long
when the alt is cheap vs BTC, short when it is rich.

This module is side-effect-free and float-based (statistics, not exchange I/O),
so it is exhaustively unit-testable in isolation, mirroring ``exit_ladder``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import fmean, pstdev


@dataclass(slots=True)
class StatArbDecision:
    direction: str        # "long" (alt cheap vs BTC) | "short" (alt rich vs BTC)
    z: float              # signed z-score of the log relative-strength spread
    abs_z: float
    spread_last: float    # last ln(alt/btc)
    spread_mean: float    # mean of the lookback window
    turned: bool          # latest bar already moving back toward the mean


def log_relative_spread(alt_closes: list[float], btc_closes: list[float]) -> list[float]:
    """Tail-aligned series of ``ln(alt/btc)``.

    The two close series are aligned from the most-recent end (they may differ in
    length); pairs with a non-positive price are dropped (can't take a log).
    """
    n = min(len(alt_closes), len(btc_closes))
    if n == 0:
        return []
    alt = alt_closes[-n:]
    btc = btc_closes[-n:]
    out: list[float] = []
    for ac, bc in zip(alt, btc):
        if ac > 0 and bc > 0:
            out.append(math.log(ac / bc))
    return out


def zscore_last(series: list[float], lookback: int) -> tuple[float, float, float] | None:
    """z-score of the last value vs the trailing ``lookback`` window (incl. last).

    Returns ``(z, mean, std)``, or ``None`` when undefined — too few points or a
    flat window (zero variance) where a z-score is meaningless.
    """
    if lookback < 2 or len(series) < lookback:
        return None
    window = series[-lookback:]
    mu = fmean(window)
    sd = pstdev(window)
    if sd <= 0:
        return None
    return ((series[-1] - mu) / sd, mu, sd)


def _turned_toward_mean(spread: list[float], direction: str) -> bool:
    """True if the latest bar moved back toward the mean (reversion has begun)."""
    if len(spread) < 2:
        return False
    last, prev = spread[-1], spread[-2]
    # short → spread (alt/btc) should be falling; long → it should be rising.
    return last < prev if direction == "short" else last > prev


def reversion_signal(
    alt_closes: list[float],
    btc_closes: list[float],
    *,
    lookback: int,
    entry_z: float,
    require_turn: bool = True,
) -> StatArbDecision | None:
    """Decide whether the alt is stretched enough vs BTC to fade it.

    Returns a ``StatArbDecision`` when |z| >= ``entry_z`` (and, if
    ``require_turn``, the spread has already started reverting), else ``None``.
    """
    spread = log_relative_spread(alt_closes, btc_closes)
    stats = zscore_last(spread, lookback)
    if stats is None:
        return None
    z, mu, _sd = stats
    if abs(z) < entry_z:
        return None
    direction = "short" if z > 0 else "long"
    turned = _turned_toward_mean(spread, direction)
    if require_turn and not turned:
        return None
    return StatArbDecision(
        direction=direction,
        z=z,
        abs_z=abs(z),
        spread_last=spread[-1],
        spread_mean=mu,
        turned=turned,
    )


def conviction_score(
    abs_z: float,
    entry_z: float,
    *,
    base: float = 60.0,
    per_sigma: float = 15.0,
    cap: float = 100.0,
) -> float:
    """Map |z| (>= entry_z) to a 0-100 conviction: ``base`` at the entry
    threshold, rising ``per_sigma`` per extra sigma, capped at ``cap``. Below the
    threshold returns 0 (no signal)."""
    if abs_z < entry_z:
        return 0.0
    return min(cap, base + (abs_z - entry_z) * per_sigma)
