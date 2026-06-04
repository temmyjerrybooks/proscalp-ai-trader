"""Phase 2B Branch 2: pure-logic tests for the exit-ladder core (exit_ladder.py)."""
from __future__ import annotations

import pytest

from app.config.settings import Settings
from app.execution.exit_ladder import (
    SymbolRules,
    build_ladder_plan,
    compute_target_stop,
    runner_callback_rate,
    should_arm_be_plus,
    time_exit_decision,
)


def _settings(**kw) -> Settings:
    return Settings(**kw)


# Permissive rules: tiny min so the full ladder is feasible for most quantities.
_RULES = SymbolRules(tick_size=0.01, step_size=0.0001, min_qty=0.0001, min_notional=5.0)


# ---------------------------------------------------------------- build_ladder_plan

def test_full_ladder_four_tiers_plus_runner_long():
    s = _settings()
    # entry 1000, ATR 10 (1%), qty 1.0 (notional 1000 — comfortably above 5*5).
    plan = build_ladder_plan(
        settings=s, direction="long", entry_price=1000.0, stop_loss=995.0,
        atr=10.0, quantity=1.0, rules=_RULES,
    )
    assert plan.mode == "full"
    assert plan.is_ladder
    assert [t.index for t in plan.tiers] == [1, 2, 3, 4]
    # tier prices = entry + mult*ATR for the production tuple [0.6,1.0,1.5,2.0] * 10
    assert [t.price for t in plan.tiers] == [1006.0, 1010.0, 1015.0, 1020.0]
    # 4 tiers @ 20% + runner 20%
    assert all(abs(t.quantity - 0.2) < 1e-9 for t in plan.tiers)
    assert abs(plan.runner_quantity - 0.2) < 1e-9
    assert plan.runner_activation_price == 1020.0  # final tier price
    assert plan.stop_price == 995.0


def test_full_ladder_short_side_prices_flip():
    s = _settings()
    plan = build_ladder_plan(
        settings=s, direction="short", entry_price=1000.0, stop_loss=1005.0,
        atr=10.0, quantity=1.0, rules=_RULES,
    )
    assert plan.mode == "full"
    assert [t.price for t in plan.tiers] == [994.0, 990.0, 985.0, 980.0]
    assert plan.runner_activation_price == 980.0


def test_min_notional_degrades_to_single_for_tiny_position():
    s = _settings()
    # qty 0.02 @ 1000 = $20 notional. 5 pieces of $4 < $5 floor; even 2 pieces of
    # $10 work, so this should REDUCE (not collapse). Use a smaller one for single.
    plan = build_ladder_plan(
        settings=s, direction="long", entry_price=1000.0, stop_loss=995.0,
        atr=10.0, quantity=0.008, rules=_RULES,  # $8 total < 2*5
    )
    assert plan.mode == "single"
    assert not plan.is_ladder
    assert plan.single_tp_price is not None
    assert plan.degraded_reason


def test_min_notional_reduced_mode_uses_fewer_tiers():
    s = _settings()
    # $20 total: supports 4 pieces of $5 (3 tiers + runner) but not 5.
    plan = build_ladder_plan(
        settings=s, direction="long", entry_price=1000.0, stop_loss=995.0,
        atr=10.0, quantity=0.02, rules=_RULES,
    )
    assert plan.mode == "reduced"
    assert plan.is_ladder
    assert len(plan.tiers) == 3  # 4 pieces -> 3 tiers + 1 runner
    assert plan.runner_quantity > 0


def test_quantity_conserved_runner_is_remainder():
    s = _settings()
    plan = build_ladder_plan(
        settings=s, direction="long", entry_price=1000.0, stop_loss=995.0,
        atr=10.0, quantity=1.0, rules=_RULES,
    )
    total = sum(t.quantity for t in plan.tiers) + plan.runner_quantity
    assert abs(total - 1.0) < 1e-6


def test_zero_atr_falls_back_to_risk_distance():
    s = _settings()
    plan = build_ladder_plan(
        settings=s, direction="long", entry_price=1000.0, stop_loss=990.0,
        atr=0.0, quantity=1.0, rules=_RULES,
    )
    # effective unit = |entry-stop| = 10; tier1 = 1000 + 0.6*10 = 1006
    assert plan.tiers[0].price == 1006.0


# ---------------------------------------------------------------- runner_callback_rate

def test_runner_callback_rate_clamped_low():
    s = _settings()  # clamp [0.1, 10.0], mult 0.55
    # tiny ATR -> raw below 0.1 -> clamp to 0.1
    assert runner_callback_rate(s, entry_price=1000.0, atr=0.1) == 0.1


def test_runner_callback_rate_clamped_high():
    s = _settings()
    # huge ATR -> raw above 10 -> clamp to 10.0
    assert runner_callback_rate(s, entry_price=1000.0, atr=1000.0) == 10.0


def test_runner_callback_rate_normal():
    s = _settings()
    # 0.55 * 20 / 1000 * 100 = 1.1
    assert runner_callback_rate(s, entry_price=1000.0, atr=20.0) == 1.1


# ---------------------------------------------------------------- should_arm_be_plus

def test_be_plus_arms_after_half_atr_move_long():
    s = _settings()  # be_plus_activation_atr_mult = 0.5
    assert should_arm_be_plus(direction="long", entry_price=1000.0, mark_price=1005.0, atr=10.0, settings=s)
    assert not should_arm_be_plus(direction="long", entry_price=1000.0, mark_price=1004.0, atr=10.0, settings=s)


def test_be_plus_arms_after_half_atr_move_short():
    s = _settings()
    assert should_arm_be_plus(direction="short", entry_price=1000.0, mark_price=995.0, atr=10.0, settings=s)
    assert not should_arm_be_plus(direction="short", entry_price=1000.0, mark_price=996.0, atr=10.0, settings=s)


# ---------------------------------------------------------------- compute_target_stop

def test_stop_advances_on_tier_fills_long():
    s = _settings()  # stop_ladder_pct [0.0, 0.002, 0.005, 0.010], be_plus_offset 20bps
    # TP2 filled, mark well above -> stop to entry + 0.5%
    adv = compute_target_stop(
        direction="long", entry_price=1000.0, current_stop=995.0, mark_price=1010.0,
        tiers_filled=2, be_plus_armed=True, rules=_RULES, settings=s,
    )
    assert adv.new_stop_price == 1005.0  # 1000 * 1.005
    assert adv.offset_pct == 0.005


def test_stop_worst_of_progression_never_loosens():
    s = _settings()
    # current stop already at 1005 (rung 2); tiers_filled=1 (rung 0.2% = 1002) -> no move.
    adv = compute_target_stop(
        direction="long", entry_price=1000.0, current_stop=1005.0, mark_price=1010.0,
        tiers_filled=1, be_plus_armed=True, rules=_RULES, settings=s,
    )
    assert adv.new_stop_price is None
    assert "not tighter" in adv.reason


def test_stop_deferred_when_only_warranted_rung_is_above_mark():
    s = _settings()
    # Only TP1 filled (rung +0.2% = 1002), BE+ not armed. Current stop 1000 so 1002
    # is tighter, but mark is 1001 -> 1002 would instantly trigger -> defer entirely.
    adv = compute_target_stop(
        direction="long", entry_price=1000.0, current_stop=1000.0, mark_price=1001.0,
        tiers_filled=1, be_plus_armed=False, rules=_RULES, settings=s,
    )
    assert adv.new_stop_price is None
    assert adv.deferred is True


def test_stop_advances_to_highest_feasible_rung_below_mark():
    s = _settings()
    # 3 tiers filled (top rung +1.0% = 1010) but mark 1008 -> top rung blocked.
    # Should still advance to the next-lower warranted rung (+0.5% = 1005).
    adv = compute_target_stop(
        direction="long", entry_price=1000.0, current_stop=995.0, mark_price=1008.0,
        tiers_filled=3, be_plus_armed=True, rules=_RULES, settings=s,
    )
    assert adv.new_stop_price == 1005.0
    assert adv.deferred is False


def test_stop_advances_short_side():
    s = _settings()
    adv = compute_target_stop(
        direction="short", entry_price=1000.0, current_stop=1005.0, mark_price=990.0,
        tiers_filled=2, be_plus_armed=True, rules=_RULES, settings=s,
    )
    assert adv.new_stop_price == 995.0  # 1000 * 0.995
    assert adv.deferred is False


def test_be_plus_only_moves_stop_to_offset():
    s = _settings()
    # No tiers filled, BE+ armed -> stop to entry + 20bps = 1002.
    adv = compute_target_stop(
        direction="long", entry_price=1000.0, current_stop=995.0, mark_price=1006.0,
        tiers_filled=0, be_plus_armed=True, rules=_RULES, settings=s,
    )
    assert adv.new_stop_price == 1002.0
    assert abs(adv.offset_pct - 0.002) < 1e-9


# ---------------------------------------------------------------- time_exit_decision

def test_time_exit_partial_then_full():
    s = _settings()  # partial 15min, full 45min
    assert time_exit_decision(10 * 60, partial_done=False, settings=s) is None
    assert time_exit_decision(15 * 60, partial_done=False, settings=s) == "partial"
    assert time_exit_decision(20 * 60, partial_done=True, settings=s) is None  # already done
    assert time_exit_decision(45 * 60, partial_done=True, settings=s) == "full"
    assert time_exit_decision(46 * 60, partial_done=False, settings=s) == "full"  # full wins
