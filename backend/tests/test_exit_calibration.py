"""Phase-2C exit-calibration tests (pure exit_ladder logic):
  - TP tier geometry: recalibrated production tuple [0.6,1.0,1.5,2.0], cost-aware
    TP1 floor (0.22%), strict monotonicity in low vol, short-side symmetry.
  - Trail-aware time-exit: favorable active runner exempt at 45m, forced at the
    90m hard ceiling, stalled runner not exempt.
  - The pure qty-drop gate.
"""
from __future__ import annotations

import pytest

from app.config.settings import Settings, TradingMode
from app.execution.exit_ladder import (
    SymbolRules,
    build_ladder_plan,
    qty_drop_corroborates,
    runner_still_favorable,
    time_exit_decision,
)

_RULES = SymbolRules(tick_size=0.01, step_size=0.0001, min_qty=0.0001, min_notional=5.0)


def _settings(**kw):
    return Settings(trading_mode=TradingMode.TESTNET, market_type="futures", **kw)


# ----------------------------------------------------------------- geometry

def test_geometry_normal_vol_production_tuple():
    s = _settings()
    assert s.tp_tier_atr_multipliers == [0.6, 1.0, 1.5, 2.0]          # production default
    # atr = 1% of price -> the 0.22% floor does NOT bind.
    plan = build_ladder_plan(settings=s, direction="long", entry_price=1000.0,
                             stop_loss=995.0, atr=10.0, quantity=1.0, rules=_RULES)
    assert plan.mode == "full"
    dists = [round(t.price - 1000.0, 4) for t in plan.tiers]
    assert dists == [6.0, 10.0, 15.0, 20.0]                           # [0.6,1.0,1.5,2.0] x 10
    assert plan.runner_activation_price == plan.tiers[-1].price       # runner activates at TP4
    assert dists == sorted(dists) and len(set(dists)) == 4            # strictly increasing


def test_geometry_low_vol_tp1_floored_and_monotonic():
    s = _settings()
    # atr = 0.1% of price: every raw move (0.6..2.0) < floor (0.22% * 1000 = 2.2)
    # so TP1 floors to 2.2 and the monotonic guard nudges the rest strictly beyond.
    plan = build_ladder_plan(settings=s, direction="long", entry_price=1000.0,
                             stop_loss=999.0, atr=1.0, quantity=1.0, rules=_RULES)
    dists = [round(t.price - 1000.0, 4) for t in plan.tiers]
    assert dists[0] == pytest.approx(2.2)                             # TP1 floored to 0.22%
    assert all(d >= 2.2 for d in dists)
    assert dists == sorted(dists) and len(set(dists)) == 4            # no collapse/collision


def test_geometry_short_symmetry():
    s = _settings()
    plan = build_ladder_plan(settings=s, direction="short", entry_price=1000.0,
                             stop_loss=1005.0, atr=10.0, quantity=1.0, rules=_RULES)
    dists = [round(1000.0 - t.price, 4) for t in plan.tiers]         # tiers BELOW entry
    assert dists == [6.0, 10.0, 15.0, 20.0]
    assert all(t.price < 1000.0 for t in plan.tiers)


# ----------------------------------------------------------- trail-aware time-exit

def test_time_exit_partial_then_full():
    s = _settings()
    assert time_exit_decision(10 * 60, False, s) is None             # before partial window
    assert time_exit_decision(15 * 60, False, s) == "partial"        # partial at 15m
    assert time_exit_decision(45 * 60, True, s) == "full"            # full at 45m (no runner)


def test_time_exit_favorable_runner_exempt_until_hard_ceiling():
    s = _settings()
    kw = dict(runner_active=True, mark_price=1025.0,                 # mark beyond activation
              runner_activation_price=1020.0, direction="long")
    assert time_exit_decision(45 * 60, True, s, **kw) is None        # exempt at 45m
    assert time_exit_decision(89 * 60, True, s, **kw) is None        # still exempt < 90m
    assert time_exit_decision(90 * 60, True, s, **kw) == "full"      # hard ceiling forces close


def test_time_exit_stalled_runner_not_exempt():
    s = _settings()
    kw = dict(runner_active=True, mark_price=1018.0,                 # NOT beyond activation
              runner_activation_price=1020.0, direction="long")
    assert time_exit_decision(45 * 60, True, s, **kw) == "full"      # stalled -> not exempt
    assert time_exit_decision(45 * 60, True, s, runner_active=False) == "full"  # no runner


def test_runner_still_favorable_direction_aware():
    assert runner_still_favorable("long", 1025.0, 1020.0) is True
    assert runner_still_favorable("long", 1015.0, 1020.0) is False
    assert runner_still_favorable("short", 980.0, 985.0) is True
    assert runner_still_favorable("short", 990.0, 985.0) is False
    assert runner_still_favorable(None, 1.0, 1.0) is False           # missing data -> not favorable
    assert runner_still_favorable("long", None, 1020.0) is False


# ----------------------------------------------------------- pure qty-drop gate

def test_qty_drop_corroborates_pure():
    # full corroborating drop
    assert qty_drop_corroborates(original_qty=1.0, exchange_qty=0.8,
                                 accounted_prior=0.0, filled_qty=0.2, tol_frac=0.05) is True
    # second leg in the same poll: prior 0.2 booked, position shed 0.4 total
    assert qty_drop_corroborates(original_qty=1.0, exchange_qty=0.6,
                                 accounted_prior=0.2, filled_qty=0.2, tol_frac=0.05) is True
    # status says filled but the position never dropped -> reject
    assert qty_drop_corroborates(original_qty=1.0, exchange_qty=1.0,
                                 accounted_prior=0.0, filled_qty=0.2, tol_frac=0.05) is False
    # tolerance band: 0.16 drop >= 0.20 - 0.05 = 0.15 -> ok; 0.14 < 0.15 -> reject
    assert qty_drop_corroborates(original_qty=1.0, exchange_qty=0.84,
                                 accounted_prior=0.0, filled_qty=0.2, tol_frac=0.05) is True
    assert qty_drop_corroborates(original_qty=1.0, exchange_qty=0.86,
                                 accounted_prior=0.0, filled_qty=0.2, tol_frac=0.05) is False
    # degenerate original -> never corroborates
    assert qty_drop_corroborates(original_qty=0.0, exchange_qty=0.0,
                                 accounted_prior=0.0, filled_qty=0.2, tol_frac=0.05) is False
