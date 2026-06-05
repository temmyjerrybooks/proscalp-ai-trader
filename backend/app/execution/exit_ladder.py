"""Phase 2B Branch 2 — pure-logic core for the 5-tier exit ladder.

This module is deliberately side-effect-free: every function takes plain values
and returns plain values / dataclasses. All exchange I/O, persistence and audit
logging live in OrderManager / BotRunner. Keeping the decision logic pure makes
the ladder exhaustively unit-testable without a live exchange.

The ladder, per the approved Branch 2 plan:

  - 4 fixed take-profit tiers at ``entry ± mult_i × ATR`` (literal 14-period ATR,
    captured at entry), each ``tp_tier_size_pct`` of the original quantity.
  - A trailing runner on the remaining quantity (Binance TRAILING_STOP_MARKET),
    activating at the final tier price.
  - A single marching STOP_MARKET (closePosition) that ratchets up on tier fills:
      * BE+ arms on the first 0.5×ATR favorable move  -> entry ± be_plus_offset
      * TP1 fill -> stop_ladder_pct[1]  (+0.2%)
      * TP2 fill -> stop_ladder_pct[2]  (+0.5%)
      * TP3 fill -> stop_ladder_pct[3]  (+1.0%)
      * TP4 fill -> runner active; stop floor stays at the final rung
    Worst-of-progression: the stop only ever tightens in profit, never moves
    backward, and is never set at/above the current mark (which the exchange
    would immediately trigger).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal

from app.config.settings import Settings


@dataclass(slots=True)
class SymbolRules:
    """The subset of Binance symbol filters the ladder math needs."""
    tick_size: float
    step_size: float
    min_qty: float
    min_notional: float


@dataclass(slots=True)
class TierSpec:
    """One take-profit tier of the ladder."""
    index: int  # 1-based tier number
    price: float
    quantity: float


@dataclass(slots=True)
class LadderPlan:
    """The computed shape of a ladder for one entry.

    ``mode`` is one of:
      - "full":     4 tiers + runner (the designed shape)
      - "reduced":  fewer tiers + runner (graceful min-notional degradation)
      - "single":   one whole-position TP, no partials/runner (smallest positions)
    """
    mode: str
    direction: str
    entry_price: float
    atr: float
    total_quantity: float
    stop_price: float
    tiers: list[TierSpec] = field(default_factory=list)
    runner_quantity: float = 0.0
    runner_activation_price: float | None = None
    runner_callback_rate: float | None = None
    single_tp_price: float | None = None  # only set in "single" mode
    degraded_reason: str | None = None

    @property
    def is_ladder(self) -> bool:
        """True when this plan uses partial tiers (full or reduced), i.e. the
        Branch 2 ladder sync path applies. 'single' uses the Branch 1 path."""
        return self.mode in ("full", "reduced")


def _round_step(value: float, step: float, *, up: bool) -> float:
    if step <= 0:
        return value
    dv = Decimal(str(value))
    ds = Decimal(str(step))
    rounding = ROUND_CEILING if up else ROUND_DOWN
    return float((dv / ds).to_integral_value(rounding=rounding) * ds)


def _round_price(price: float, rules: SymbolRules) -> float:
    return _round_step(price, rules.tick_size, up=False)


def _round_qty(qty: float, rules: SymbolRules) -> float:
    return _round_step(qty, rules.step_size, up=False)


def _tier_price(entry: float, direction: str, atr: float, multiplier: float) -> float:
    # Decimal arithmetic avoids binary float noise (e.g. 1000*1.005 -> 1004.9999...)
    # that would otherwise truncate a clean tick to the wrong side on rounding.
    move = Decimal(str(multiplier)) * Decimal(str(atr))
    base = Decimal(str(entry))
    return float(base + move if direction == "long" else base - move)


def _apply_move(entry: float, direction: str, move: Decimal) -> float:
    """Price at a Decimal distance ``move`` from entry, on the take-profit side."""
    base = Decimal(str(entry))
    return float(base + move if direction == "long" else base - move)


def _tier_price_floored(
    entry: float, direction: str, atr: float, multiplier: float, min_distance_pct: float
) -> float:
    """Phase-2C TASK 2 cost-aware floor: tier price at max(multiplier*ATR,
    min_distance_pct*entry) from entry. Used for the single-TP path; the multi-tier
    loop in build_ladder_plan additionally enforces strict monotonicity across tiers
    so a low-vol floor clamp can never collapse two tiers onto the same distance."""
    move = max(Decimal(str(multiplier)) * Decimal(str(atr)),
               Decimal(str(min_distance_pct)) * Decimal(str(entry)))
    return _apply_move(entry, direction, move)


def _pct_price(entry: float, offset: float, direction: str) -> float:
    """entry adjusted by a fractional offset, computed in Decimal."""
    factor = Decimal("1") + Decimal(str(offset)) if direction == "long" else Decimal("1") - Decimal(str(offset))
    return float(Decimal(str(entry)) * factor)


def runner_callback_rate(settings: Settings, entry_price: float, atr: float) -> float:
    """Trailing distance as a Binance callbackRate percent, clamped to the
    configured bounds. callbackRate = trail_distance / price × 100."""
    lo, hi = settings.runner_trail_callback_clamp
    if entry_price <= 0:
        return lo
    raw = settings.runner_trail_atr_mult * atr / entry_price * 100.0
    return round(min(max(raw, lo), hi), 1)


def build_ladder_plan(
    *,
    settings: Settings,
    direction: str,
    entry_price: float,
    stop_loss: float,
    atr: float,
    quantity: float,
    rules: SymbolRules,
) -> LadderPlan:
    """Compute the ladder shape for one entry, applying min-notional Option A
    graceful degradation (full -> reduced -> single).

    ATR must be a positive absolute price distance. If it is non-positive the
    caller should not be building a ladder; we fall back to a single-TP plan
    using the final configured multiplier against the risk distance.
    """
    multipliers = list(settings.tp_tier_atr_multipliers)
    size_pcts = list(settings.tp_tier_size_pct)
    n_tiers = min(len(multipliers), len(size_pcts))
    min_piece_notional = max(settings.tp_tier_min_notional_usdt, rules.min_notional, 5.0)

    # Degenerate ATR -> use the risk distance as the move unit so tiers stay sane.
    effective_atr = atr if atr > 0 else abs(entry_price - stop_loss)

    def _single(reason: str) -> LadderPlan:
        tp_price = _round_price(
            _tier_price_floored(entry_price, direction, effective_atr,
                                multipliers[n_tiers - 1], settings.tp_tier_min_distance_pct),
            rules,
        )
        return LadderPlan(
            mode="single",
            direction=direction,
            entry_price=entry_price,
            atr=atr,
            total_quantity=quantity,
            stop_price=_round_price(stop_loss, rules),
            single_tp_price=tp_price,
            degraded_reason=reason,
        )

    total_notional = quantity * entry_price
    if total_notional <= 0 or effective_atr <= 0:
        return _single("non-positive notional or ATR")

    # Largest piece count C in [2 .. n_tiers+1] where every piece clears min_qty
    # and min-notional. C pieces == (C-1) TP tiers + 1 runner.
    best_c = 0
    for c in range(n_tiers + 1, 1, -1):
        piece_qty = _round_qty(quantity / c, rules)
        if piece_qty >= rules.min_qty and piece_qty * entry_price >= min_piece_notional:
            best_c = c
            break

    if best_c < 2:
        return _single("position too small to split into tiers + runner")

    n_active_tiers = best_c - 1
    full = n_active_tiers == n_tiers

    tiers: list[TierSpec] = []
    allocated = Decimal("0")
    prev_move = Decimal("0")
    min_move = Decimal(str(settings.tp_tier_min_distance_pct)) * Decimal(str(entry_price))
    tick = Decimal(str(rules.tick_size)) if rules.tick_size > 0 else Decimal("0")
    for i in range(n_active_tiers):
        if full:
            raw_qty = quantity * size_pcts[i]
        else:
            raw_qty = quantity / best_c
        tier_qty = _round_qty(raw_qty, rules)
        # Cost-aware floor + strict monotonicity (TASK 2): each tier sits at least
        # min_distance_pct from entry AND strictly beyond the previous tier, so a
        # low-vol floor clamp can't collapse two tiers onto the same distance
        # (e.g. a floored TP1 must never meet TP2).
        move = Decimal(str(multipliers[i])) * Decimal(str(effective_atr))
        if move < min_move:
            move = min_move
        if move <= prev_move:
            move = prev_move + (tick if tick > 0 else min_move)
        prev_move = move
        tier_price = _round_price(_apply_move(entry_price, direction, move), rules)
        tiers.append(TierSpec(index=i + 1, price=tier_price, quantity=tier_qty))
        allocated += Decimal(str(tier_qty))

    # Remainder in Decimal so accumulated float dust never silently drops a step.
    runner_qty = _round_qty(float(Decimal(str(quantity)) - allocated), rules)
    if runner_qty < rules.min_qty or runner_qty * entry_price < min_piece_notional:
        # Rounding pushed the runner below the floor; collapse to single TP.
        return _single("runner remainder below min-notional after rounding")

    runner_activation = tiers[-1].price
    return LadderPlan(
        mode="full" if full else "reduced",
        direction=direction,
        entry_price=entry_price,
        atr=atr,
        total_quantity=quantity,
        stop_price=_round_price(stop_loss, rules),
        tiers=tiers,
        runner_quantity=runner_qty,
        runner_activation_price=runner_activation,
        runner_callback_rate=runner_callback_rate(settings, entry_price, effective_atr),
        degraded_reason=None if full else f"reduced to {n_active_tiers} tiers + runner",
    )


def tier_trigger_reached(direction: str, tier_price: float, mark_price: float) -> bool:
    """True when a TP tier's trigger already sits on the *filled* side of the mark
    — i.e. the position has already reached this tier's target by the time we go
    to place it.

    Placing a resting TAKE_PROFIT_MARKET in this state is rejected by Binance with
    ``-2021 "Order would immediately trigger"``. This mirrors, inverted for the
    take-profit direction, the marching stop's never-at/above-market guard in
    ``compute_target_stop`` (``_safe_of_mark``):

      - long  exits SELL on a *rise*  -> reached when tier_price <= mark_price
      - short exits BUY  on a *fall*  -> reached when tier_price >= mark_price

    A non-positive mark (no usable price) is treated as "not reached" so the
    caller falls back to placing the resting order rather than market-dumping a
    slice on bad data.
    """
    if mark_price <= 0:
        return False
    return tier_price <= mark_price if direction == "long" else tier_price >= mark_price


def should_arm_be_plus(
    *, direction: str, entry_price: float, mark_price: float, atr: float, settings: Settings
) -> bool:
    """BE+ arms once price has moved be_plus_activation_atr_mult × ATR favorably."""
    if atr <= 0:
        return False
    trigger = settings.be_plus_activation_atr_mult * atr
    favorable = (mark_price - entry_price) if direction == "long" else (entry_price - mark_price)
    return favorable >= trigger


def _candidate_offsets(settings: Settings, tiers_filled: int, be_plus_armed: bool) -> list[float]:
    """All stop offset fractions currently warranted, ascending.

    stop_ladder_pct is indexed by tiers filled (index 0 == the 0-tier baseline).
    BE+ contributes its own offset (be_plus_offset_bps). Returning every warranted
    rung — not just the max — lets the caller pick the most protective rung that
    still sits safely below the current mark (so a high rung that would instantly
    trigger doesn't block a lower, still-valid advance)."""
    ladder = list(settings.stop_ladder_pct)
    offsets = {ladder[0] if ladder else 0.0}
    if be_plus_armed:
        offsets.add(settings.be_plus_offset_bps / 10_000.0)
    if len(ladder) > 1:
        for i in range(1, tiers_filled + 1):
            offsets.add(ladder[min(i, len(ladder) - 1)])
    return sorted(offsets)


@dataclass(slots=True)
class StopAdvance:
    """Result of compute_target_stop: a new stop to place, or a deferral."""
    new_stop_price: float | None
    offset_pct: float
    deferred: bool = False  # rung is above current mark; retry next cycle
    reason: str = ""


def compute_target_stop(
    *,
    direction: str,
    entry_price: float,
    current_stop: float,
    mark_price: float,
    tiers_filled: int,
    be_plus_armed: bool,
    rules: SymbolRules,
    settings: Settings,
) -> StopAdvance:
    """Decide whether the marching stop should move, honoring worst-of-progression
    (tighten only) and the never-at/above-market constraint. Among the warranted
    rungs, advance to the most protective one that is both tighter than the current
    stop and still safely on the correct side of the mark."""
    def _tighter(price: float) -> bool:
        return price > current_stop if direction == "long" else price < current_stop

    def _safe_of_mark(price: float) -> bool:
        return price < mark_price if direction == "long" else price > mark_price

    best_offset: float | None = None
    best_target: float | None = None
    any_tighter_blocked = False
    for offset in _candidate_offsets(settings, tiers_filled, be_plus_armed):
        target = _round_price(_pct_price(entry_price, offset, direction), rules)
        if not _tighter(target):
            continue
        if _safe_of_mark(target):
            # Ascending offsets => later feasible rung is the more protective one.
            best_offset, best_target = offset, target
        else:
            any_tighter_blocked = True

    if best_target is not None:
        return StopAdvance(best_target, best_offset or 0.0, reason="advance")
    if any_tighter_blocked:
        return StopAdvance(None, 0.0, deferred=True, reason="rung at/above current mark")
    return StopAdvance(None, 0.0, reason="not tighter than current stop")


# ----- Phase 2B ladder-fix item 3: status-driven reconciliation primitives -----
#
# These are pure (no I/O) so the reconciler's accounting can be unit-tested in
# isolation. The reconciler attributes fills from a leg's *terminal status*
# (INV-2) and measures anomalies as the quantity that left the position with no
# status to explain it (INV-4) — never from the planned ladder shape (INV-3).

# A leg's slice counts as a fill ONLY in these terminal states (INV-2).
FILL_STATUSES = frozenset({"filled", "partially_filled", "filled_at_attach"})
# Terminal non-fill states: the leg is gone but realized nothing on its own.
CANCEL_STATUSES = frozenset({"canceled", "cancelled", "expired", "rejected"})


def classify_leg_status(raw_status: str | None) -> str:
    """Normalize a raw exchange algo-order status into the reconciler vocabulary:
    ``filled | partially_filled | canceled | expired | rejected | resting``.

    Anything not explicitly recognized as a terminal state maps to ``resting`` —
    we never infer a fill from a leg merely being absent from the open set
    (INV-2 / code-review gate G-1).

    ``finished`` is the Binance USDⓈ-M algo-order TERMINAL fill state for a
    triggered conditional order — verified live 2026-06-02: a triggered TP tier
    reports ``algoStatus=FINISHED`` (its own avgPrice/executedQty null; the real
    fill is in ``actualPrice``/``actualQty`` on the same response, sourced by the
    adapter). ``triggered`` is retained defensively (a fired conditional)."""
    s = (raw_status or "").strip().lower()
    if s in ("filled", "finished", "triggered"):
        return "filled"
    if s in ("partially_filled", "partial"):
        return "partially_filled"
    if s in ("canceled", "cancelled"):
        return "canceled"
    if s == "expired":
        return "expired"
    if s == "rejected":
        return "rejected"
    return "resting"


def is_fill_status(status: str) -> bool:
    return status in FILL_STATUSES


def is_terminal_status(status: str) -> bool:
    return status in FILL_STATUSES or status in CANCEL_STATUSES


def unexplained_residual_qty(
    original_qty: float, exchange_qty: float, accounted_filled_qty: float
) -> float:
    """INV-4 metric. The quantity that left the position with no terminal status
    to account for it:

        observed_decrease   = original_qty - exchange_qty
        unexplained_residual = observed_decrease - accounted_filled_qty

    ``accounted_filled_qty`` is the sum of FILLED / PARTIALLY_FILLED /
    FILLED_AT_ATTACH leg quantities — i.e. status-backed fills only. A positive
    result means quantity exited the position that no fill explains (the canonical
    anomaly); a negative result means more was booked as filled than the position
    actually shed (a phantom-fill / status-vs-position disagreement). Callers
    compare ``abs(result) / original_qty`` against the configured threshold."""
    observed_decrease = original_qty - exchange_qty
    return observed_decrease - accounted_filled_qty


def qty_drop_corroborates(
    *,
    original_qty: float,
    exchange_qty: float,
    accounted_prior: float,
    filled_qty: float,
    tol_frac: float,
) -> bool:
    """Phase-2C TASK 1 qty-drop GATE. A FILL-status leg is corroborated only if the
    live position has actually shed enough quantity to cover this fill on top of
    everything already accounted:

        observed_drop = original_qty - exchange_qty
        required      = accounted_prior + filled_qty
        corroborated  = observed_drop >= required - tol_frac * original_qty

    ``accounted_prior`` is the Σ of fills booked BEFORE this leg, recomputed in the
    same sequential booking section — so two legs resolved in one poll window can
    never both book against the same observed_drop (the second sees the first's
    qty already in accounted_prior). A status that reports FILLED while the position
    has not dropped (observed_drop ~ 0) fails this and is rejected as a false fill
    (INV-2 stays: status alone never books — status AND a corroborating qty drop)."""
    if original_qty <= 0:
        return False
    observed_drop = original_qty - exchange_qty
    required = accounted_prior + filled_qty
    return observed_drop >= required - tol_frac * original_qty


# ---------------------------------------------------------------------------
# Phase-2C authoritative reconciliation from userTrades (the exchange's own fill
# ledger). This REPLACES inference from the eventually-consistent algo-order
# status surface (fetch_algo_order / fetch_open_algo_orders), which conflated
# "gone because FILLED" with "-2013 can't read" and oscillated between over- and
# under-booking. These functions are pure + side-effect-free (unit-tested).
# ---------------------------------------------------------------------------

def split_user_trades(
    rows: list[dict], *, entry_side: str, quote_asset: str = "USDT"
) -> dict:
    """Split raw Binance USDⓈ-M ``GET /fapi/v1/userTrades`` rows for ONE position
    into entry vs reducing fills and aggregate the authoritative ledger.

    ``entry_side`` is the position-OPENING side: ``SELL`` for a short, ``BUY`` for
    a long. Reducing fills are the opposite side. realized PnL is Binance-computed
    (``realizedPnl``, gross of fees); commission is summed over EVERY fill (entry +
    exits) so ``net_pnl`` is the true round-trip exchange net.

    Returns a dict:
      entry_qty, exit_qty          — Σ qty by role (the conservation operands)
      realized_pnl_gross           — Σ realizedPnl over reducing fills
      commission                   — Σ commission paid in ``quote_asset``
      commission_nonquote          — Σ commission paid in any OTHER asset (NOT
                                     netted — flagged so a BNB-fee settle can't
                                     silently corrupt the USDT net)
      net_pnl                      — realized_pnl_gross − commission
      reduce_fills                 — [{order_id, price, qty, realized_pnl, time}]
                                     in input order (for gradient mapping + audit)
    """
    es = (entry_side or "").upper()
    entry_qty = 0.0
    exit_qty = 0.0
    realized = 0.0
    commission_quote = 0.0
    commission_nonquote = 0.0
    reduce_fills: list[dict] = []
    for r in rows:
        side = str(r.get("side", "")).upper()
        qty = float(r.get("qty") or r.get("quantity") or 0.0)
        comm = float(r.get("commission") or 0.0)
        casset = str(r.get("commissionAsset") or "")
        if casset == quote_asset:
            commission_quote += comm
        elif comm:
            commission_nonquote += comm
        if side == es:
            entry_qty += qty
        else:
            rpnl = float(r.get("realizedPnl") or 0.0)
            exit_qty += qty
            realized += rpnl
            reduce_fills.append({
                "order_id": str(r.get("orderId") or ""),
                "price": float(r.get("price") or 0.0),
                "qty": qty,
                "realized_pnl": rpnl,
                "time": int(r.get("time") or 0),
            })
    return {
        "entry_qty": round(entry_qty, 12),
        "exit_qty": round(exit_qty, 12),
        "realized_pnl_gross": round(realized, 10),
        "commission": round(commission_quote, 10),
        "commission_nonquote": round(commission_nonquote, 10),
        "net_pnl": round(realized - commission_quote, 10),
        "reduce_fills": reduce_fills,
    }


def conservation_status(
    base_qty: float, exit_qty: float, position_qty: float, tol_frac: float
) -> tuple[str, float]:
    """Authoritative conservation law for a ladder position reconciled from
    userTrades. With ``base_qty`` = Σ entry fills, ``exit_qty`` = Σ reducing fills,
    and ``position_qty`` = the live exchange remainder, the identity is::

        base_qty == exit_qty + position_qty

    Returns ``(status, residual)`` where ``residual = base_qty - position_qty -
    exit_qty``:

      - ``ok``        : ``|residual| <= tol_frac*base_qty`` — everything reconciles.
      - ``shortfall`` : ``residual > tol`` — quantity left the position that
        userTrades has not surfaced YET (eventual-consistency lag). This SELF-HEALS
        once the fill posts, so callers grace it with a bounded re-check rather than
        alarming (the crucial difference from the ``-2013`` read that NEVER healed).
      - ``overcount`` : ``residual < -tol`` — more reducing qty than the position
        actually shed. Impossible for one position from its own fills → a genuine
        mismatch, alarm immediately.
    """
    if base_qty <= 0:
        return "ok", 0.0
    residual = base_qty - position_qty - exit_qty
    if abs(residual) <= tol_frac * base_qty:
        return "ok", round(residual, 12)
    return ("shortfall" if residual > 0 else "overcount"), round(residual, 12)


def classify_attach(
    *,
    stop_placed: bool,
    planned_tier_count: int,
    rested_tier_count: int,
    immediate_fill_count: int,
    planned_runner: bool,
    runner_placed: bool,
) -> str:
    """Phase 2B ladder-fix item 4 — classify one ladder attach for the breaker:

      - ``failed``  : the closePosition stop did not place (position would be naked).
      - ``partial`` : stop placed, but ≥1 planned slice leg neither rested nor was
                      market-closed at attach — i.e. genuinely dropped.
      - ``full``    : stop placed and every planned slice leg ended accounted.

    A tier market-closed by fix A (``immediate_fill_count``) is ACCOUNTED, not a
    degradation — it must not push an attach into ``partial``. Counts come from the
    attach's placed_set ground truth (rested + immediate), consistent with INV-3
    (G4-1) — never from the global ladder config."""
    if not stop_placed:
        return "failed"
    accounted = rested_tier_count + immediate_fill_count
    dropped = max(0, planned_tier_count - accounted)
    if planned_runner and not runner_placed:
        dropped += 1
    return "partial" if dropped > 0 else "full"


def runner_still_favorable(
    direction: str | None, mark_price: float | None, activation_price: float | None
) -> bool:
    """True when the mark is strictly beyond the runner's activation level in the
    trade direction (long: mark > activation; short: mark < activation) — i.e. the
    runner is still running our way at the instant the time-exit clock would fire.
    Defensive on missing data: unknown -> not favorable (so we don't exempt blind)."""
    if not direction or mark_price is None or activation_price is None or mark_price <= 0:
        return False
    return mark_price > activation_price if direction == "long" else mark_price < activation_price


def time_exit_decision(
    elapsed_seconds: float,
    partial_done: bool,
    settings: Settings,
    *,
    runner_active: bool = False,
    mark_price: float | None = None,
    runner_activation_price: float | None = None,
    direction: str | None = None,
) -> str | None:
    """Return "full", "partial", or None based on elapsed time since entry.

    Phase-2C TASK 2 — trail-aware exemption: at the ``time_exit_full`` mark, a
    still-favorable ACTIVE runner (``runner_still_favorable``) is exempted from the
    clock so the trailing stop can manage it — UNTIL ``time_exit_hard_ceiling_minutes``,
    past which the position is force-closed regardless. The hard ceiling is checked
    first so a sideways-bleeding runner the trail somehow misses can't live forever.
    """
    if elapsed_seconds >= settings.time_exit_hard_ceiling_minutes * 60:
        return "full"
    if elapsed_seconds >= settings.time_exit_full_minutes * 60:
        if runner_active and runner_still_favorable(direction, mark_price, runner_activation_price):
            return None  # exempt; the trailing stop manages it until the hard ceiling
        return "full"
    if not partial_done and elapsed_seconds >= settings.time_exit_partial_minutes * 60:
        return "partial"
    return None
