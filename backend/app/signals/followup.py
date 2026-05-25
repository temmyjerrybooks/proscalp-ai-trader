from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.config.settings import get_settings
from app.database.models import Signal, Trade
from app.exchanges.base import Candle


def _shadow_cost_pct() -> float:
    """Phase 2B Branch 1: round-trip fee+slippage in percentage points.

    Shadow replays exit at the exact TP/stop with zero costs. To make their
    pnl_pct directly comparable to realized trades' pnl_pct, subtract
    2x fee_rate_bps + 2x slippage_bps. Calibrated empirically in the Phase 1.7
    cost study (≈0.20 pct-pts under current settings: 2*6 + 2*4 = 20 bps).
    Returns 0.0 when shadow_replay_fee_aware is False (legacy gross behavior).
    """
    s = get_settings()
    if not s.shadow_replay_fee_aware:
        return 0.0
    return (2.0 * float(s.fee_rate_bps) + 2.0 * float(s.slippage_bps)) / 100.0


def evaluate_signal_follow_up(
    signal: Signal,
    trade: Trade | None,
    candles: list[Candle],
    *,
    decision_accepted: bool,
    now: datetime | None = None,
    horizon_minutes: int = 180,
    min_move_pct: float = 0.15,
    allow_directional_fallback: bool = False,
) -> dict[str, Any]:
    """Evaluate whether a signal decision aged well.

    Closed/open real trades use actual PnL. Signals without an executed trade use
    a shadow replay from the signal's entry, stop, and first take-profit. Decision
    quality is only final when the actual trade closed or the shadow trade hit a
    stop/take-profit; open hypothetical outcomes stay pending so reports do not
    drift as new candles arrive.
    """
    evaluated_at = _aware(now or datetime.now(timezone.utc))
    created_at = _aware(signal.created_at)
    minutes_elapsed = max(0.0, (evaluated_at - created_at).total_seconds() / 60)
    horizon_minutes = max(1, int(horizon_minutes))

    if trade:
        return _actual_trade_follow_up(
            trade,
            decision_accepted=decision_accepted,
            evaluated_at=evaluated_at,
            minutes_elapsed=minutes_elapsed,
            horizon_minutes=horizon_minutes,
        )

    entry = float(signal.entry_price or 0)
    if entry <= 0:
        return _result(
            status="not_trackable",
            verdict="unknown",
            basis="untrackable",
            summary="Signal has no usable entry price for follow-up.",
            evaluated_at=evaluated_at,
            minutes_elapsed=minutes_elapsed,
            horizon_minutes=horizon_minutes,
            settled=False,
        )

    usable_candles = _candles_in_window(candles, created_at, evaluated_at, horizon_minutes)
    if not usable_candles:
        if not candles and minutes_elapsed >= 2:
            return _result(
                status="market_data_unavailable",
                verdict="unknown",
                basis="shadow",
                summary="No post-signal candles or mark-price fallback were available for follow-up.",
                evaluated_at=evaluated_at,
                minutes_elapsed=minutes_elapsed,
                horizon_minutes=horizon_minutes,
                settled=False,
            )
        return _result(
            status="waiting",
            verdict="pending",
            basis="shadow",
            summary="Waiting for post-signal candles before judging the decision.",
            evaluated_at=evaluated_at,
            minutes_elapsed=minutes_elapsed,
            horizon_minutes=horizon_minutes,
            settled=False,
        )

    target = _first_take_profit(signal.take_profit)
    stop = float(signal.stop_loss or 0)
    if _has_trade_plan(entry, stop, target):
        return _planned_shadow_follow_up(
            signal,
            usable_candles,
            target=float(target),
            decision_accepted=decision_accepted,
            evaluated_at=evaluated_at,
            minutes_elapsed=minutes_elapsed,
            horizon_minutes=horizon_minutes,
            min_move_pct=min_move_pct,
        )

    if not allow_directional_fallback:
        return _result(
            status="not_trackable",
            verdict="unknown",
            basis="untrackable",
            summary="Signal has no complete stop/take-profit plan, so no closed hypothetical trade can be replayed.",
            evaluated_at=evaluated_at,
            minutes_elapsed=minutes_elapsed,
            horizon_minutes=horizon_minutes,
            settled=False,
        )

    return _directional_shadow_follow_up(
        signal,
        usable_candles,
        decision_accepted=decision_accepted,
        evaluated_at=evaluated_at,
        minutes_elapsed=minutes_elapsed,
        horizon_minutes=horizon_minutes,
        min_move_pct=min_move_pct,
        basis="directional_shadow",
    )


def _actual_trade_follow_up(
    trade: Trade,
    *,
    decision_accepted: bool,
    evaluated_at: datetime,
    minutes_elapsed: float,
    horizon_minutes: int,
) -> dict[str, Any]:
    pnl = float(trade.realized_pnl if trade.status == "closed" else trade.unrealized_pnl or 0)
    notional = abs(float(trade.entry_price or 0) * float(trade.quantity or 0))
    pnl_pct = pnl / notional * 100 if notional > 0 else None
    if trade.status == "closed":
        polarity = _pnl_polarity(pnl)
        status = f"actual_{polarity}"
        reason = str((trade.extra or {}).get("close_reason") or "closed")
        summary = f"Actual trade closed {reason} with PnL {pnl:.2f}."
        settled = True
    else:
        polarity = _pnl_polarity(pnl)
        status = f"actual_open_{polarity}"
        summary = f"Actual trade is still open with unrealized PnL {pnl:.2f}."
        settled = False
    return _result(
        status=status,
        verdict=_decision_verdict(decision_accepted, polarity, closed=trade.status == "closed"),
        basis="actual_trade",
        summary=summary,
        pnl_pct=pnl_pct,
        exit_reason=(trade.extra or {}).get("close_reason") if trade.status == "closed" else None,
        evaluated_at=evaluated_at,
        minutes_elapsed=minutes_elapsed,
        horizon_minutes=horizon_minutes,
        settled=settled,
    )


def _planned_shadow_follow_up(
    signal: Signal,
    candles: list[Candle],
    *,
    target: float,
    decision_accepted: bool,
    evaluated_at: datetime,
    minutes_elapsed: float,
    horizon_minutes: int,
    min_move_pct: float,
) -> dict[str, Any]:
    entry = float(signal.entry_price)
    stop = float(signal.stop_loss)
    direction = signal.direction
    max_favorable, max_adverse = _favorable_adverse_pct(direction, entry, candles)

    cost_pct = _shadow_cost_pct()
    for candle in candles:
        stop_hit = _stop_hit(direction, stop, candle)
        target_hit = _target_hit(direction, target, candle)
        if stop_hit and target_hit:
            gross = _directional_pnl_pct(direction, entry, stop)
            return _result(
                status="would_lose",
                verdict=_decision_verdict(decision_accepted, "negative", closed=True),
                basis="planned_shadow",
                summary="Stop and target touched in the same candle; counted as stop first for conservative review.",
                pnl_pct=gross - cost_pct,
                pnl_pct_gross=gross,
                max_favorable_pct=max_favorable,
                max_adverse_pct=max_adverse,
                exit_reason="stop_loss_conservative",
                exit_price=stop,
                evaluated_at=evaluated_at,
                minutes_elapsed=minutes_elapsed,
                horizon_minutes=horizon_minutes,
                settled=True,
            )
        if stop_hit:
            gross = _directional_pnl_pct(direction, entry, stop)
            return _result(
                status="would_lose",
                verdict=_decision_verdict(decision_accepted, "negative", closed=True),
                basis="planned_shadow",
                summary="Shadow trade would have hit stop loss.",
                pnl_pct=gross - cost_pct,
                pnl_pct_gross=gross,
                max_favorable_pct=max_favorable,
                max_adverse_pct=max_adverse,
                exit_reason="stop_loss",
                exit_price=stop,
                evaluated_at=evaluated_at,
                minutes_elapsed=minutes_elapsed,
                horizon_minutes=horizon_minutes,
                settled=True,
            )
        if target_hit:
            gross = _directional_pnl_pct(direction, entry, target)
            return _result(
                status="would_win",
                verdict=_decision_verdict(decision_accepted, "positive", closed=True),
                basis="planned_shadow",
                summary="Shadow trade would have reached the first take-profit.",
                pnl_pct=gross - cost_pct,
                pnl_pct_gross=gross,
                max_favorable_pct=max_favorable,
                max_adverse_pct=max_adverse,
                exit_reason="take_profit_1",
                exit_price=target,
                evaluated_at=evaluated_at,
                minutes_elapsed=minutes_elapsed,
                horizon_minutes=horizon_minutes,
                settled=True,
            )

    max_favorable, max_adverse = _favorable_adverse_pct(signal.direction, entry, candles)
    return _result(
        status="still_running",
        verdict="pending",
        basis="planned_shadow",
        summary="Shadow trade did not hit stop loss or take-profit inside the follow-up window.",
        max_favorable_pct=max_favorable,
        max_adverse_pct=max_adverse,
        evaluated_at=evaluated_at,
        minutes_elapsed=minutes_elapsed,
        horizon_minutes=horizon_minutes,
        settled=False,
    )


def _directional_shadow_follow_up(
    signal: Signal,
    candles: list[Candle],
    *,
    decision_accepted: bool,
    evaluated_at: datetime,
    minutes_elapsed: float,
    horizon_minutes: int,
    min_move_pct: float,
    basis: str,
    summary_prefix: str | None = None,
) -> dict[str, Any]:
    entry = float(signal.entry_price)
    last_close = float(candles[-1].close)
    gross = _directional_pnl_pct(signal.direction, entry, last_close)
    cost_pct = _shadow_cost_pct()
    pnl_pct = gross - cost_pct
    max_favorable, max_adverse = _favorable_adverse_pct(signal.direction, entry, candles)
    if pnl_pct >= min_move_pct:
        polarity = "positive"
        status = "would_be_positive"
    elif pnl_pct <= -min_move_pct:
        polarity = "negative"
        status = "would_be_negative"
    else:
        polarity = "neutral"
        status = "neutral"
    prefix = summary_prefix or "No complete stop/target plan was available; using directional follow-up."
    summary = f"{prefix} Move from signal entry is {pnl_pct:.3f}%."
    return _result(
        status=status,
        verdict="pending",
        basis=basis,
        summary=summary,
        pnl_pct=pnl_pct,
        pnl_pct_gross=gross,
        max_favorable_pct=max_favorable,
        max_adverse_pct=max_adverse,
        exit_reason="mark_to_latest_candle",
        exit_price=last_close,
        evaluated_at=evaluated_at,
        minutes_elapsed=minutes_elapsed,
        horizon_minutes=horizon_minutes,
        settled=False,
    )


def _candles_in_window(
    candles: list[Candle],
    created_at: datetime,
    evaluated_at: datetime,
    horizon_minutes: int,
) -> list[Candle]:
    window_end = min(evaluated_at, created_at + timedelta(minutes=horizon_minutes))
    return [
        candle
        for candle in candles
        if _aware(candle.timestamp) > created_at and _aware(candle.timestamp) <= window_end
    ]


def _has_trade_plan(entry: float, stop: float, target: float | None) -> bool:
    if target is None:
        return False
    return entry > 0 and stop > 0 and abs(entry - stop) / entry > 0.00005 and abs(target - entry) / entry > 0.00005


def _first_take_profit(take_profit: Any) -> float | None:
    levels: list[Any]
    if isinstance(take_profit, dict):
        levels = list(take_profit.get("levels") or [])
    elif isinstance(take_profit, list):
        levels = take_profit
    else:
        levels = []
    for level in levels:
        try:
            value = float(level)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def _stop_hit(direction: str, stop: float, candle: Candle) -> bool:
    return candle.low <= stop if direction == "long" else candle.high >= stop


def _target_hit(direction: str, target: float, candle: Candle) -> bool:
    return candle.high >= target if direction == "long" else candle.low <= target


def _directional_pnl_pct(direction: str, entry: float, price: float) -> float:
    if entry <= 0:
        return 0.0
    raw = (price - entry) / entry * 100
    return round(raw if direction == "long" else -raw, 4)


def _favorable_adverse_pct(direction: str, entry: float, candles: list[Candle]) -> tuple[float | None, float | None]:
    if entry <= 0 or not candles:
        return None, None
    highs = [candle.high for candle in candles]
    lows = [candle.low for candle in candles]
    if direction == "long":
        favorable = (max(highs) - entry) / entry * 100
        adverse = (min(lows) - entry) / entry * 100
    else:
        favorable = (entry - min(lows)) / entry * 100
        adverse = (entry - max(highs)) / entry * 100
    return round(favorable, 4), round(adverse, 4)


def _pnl_polarity(pnl: float, tolerance: float = 0.01) -> str:
    if pnl > tolerance:
        return "positive"
    if pnl < -tolerance:
        return "negative"
    return "neutral"


def _decision_verdict(decision_accepted: bool, polarity: str, *, closed: bool) -> str:
    if polarity == "positive":
        return "good_acceptance" if decision_accepted else "bad_rejection"
    if polarity == "negative":
        return "bad_acceptance" if decision_accepted else "good_rejection"
    return "pending" if not closed else "neutral"


def _result(
    *,
    status: str,
    verdict: str,
    basis: str,
    summary: str,
    evaluated_at: datetime,
    minutes_elapsed: float,
    horizon_minutes: int,
    settled: bool,
    pnl_pct: float | None = None,
    pnl_pct_gross: float | None = None,
    max_favorable_pct: float | None = None,
    max_adverse_pct: float | None = None,
    exit_reason: str | None = None,
    exit_price: float | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "verdict": verdict,
        "basis": basis,
        "summary": summary,
        # Phase 2B Branch 1: pnl_pct is net (fee+slippage adjusted) when
        # shadow_replay_fee_aware=True; pnl_pct_gross preserves the original
        # cost-free value for transparency / audit.
        "pnl_pct": None if pnl_pct is None else round(float(pnl_pct), 4),
        "pnl_pct_gross": None if pnl_pct_gross is None else round(float(pnl_pct_gross), 4),
        "max_favorable_pct": max_favorable_pct,
        "max_adverse_pct": max_adverse_pct,
        "exit_reason": exit_reason,
        "exit_price": exit_price,
        "evaluated_at": evaluated_at,
        "minutes_elapsed": round(minutes_elapsed, 1),
        "horizon_minutes": horizon_minutes,
        "settled": settled,
    }


def _aware(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
