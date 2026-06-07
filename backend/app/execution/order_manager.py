from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from uuid import uuid4

import structlog

from app.config.settings import Settings, TradingMode, get_settings
from app.exchanges.base import ExchangeAdapter, OrderBook, OrderRequest, OrderResult, WorkingType
from app.execution.exit_ladder import LadderPlan, SymbolRules, build_ladder_plan, tier_trigger_reached
from app.paper_trading.simulator import PaperPosition, PaperTradingSimulator
from app.risk.risk_engine import RiskDecision, RiskEngine, TradePermissionRequest
from app.strategies.base_strategy import StrategySignal

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class ExecutionReport:
    accepted: bool
    mode: str
    risk_decision: RiskDecision
    order_result: OrderResult | None = None
    paper_position: PaperPosition | None = None
    reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ProtectiveOrdersResult:
    """Outcome of attach_protective_orders for an exchange-resting stop+TP pair."""
    stop_order_id: str | None
    take_profit_order_id: str | None
    elapsed_ms: float
    success: bool
    reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class TierOrder:
    """A placed take-profit tier, recorded so the sync loop can detect its fill."""
    index: int
    order_id: str
    price: float
    quantity: float


@dataclass(slots=True)
class ImmediateFill:
    """A tier whose trigger was already past the mark at placement time, so its
    slice was taken at MARKET immediately instead of left as a resting TP.

    Per the Phase 2B ladder-fix item 1: the position has *already* reached this
    tier's target, so a resting TAKE_PROFIT_MARKET would be rejected with -2021
    ("Order would immediately trigger"). Rather than skip the slice (abandoning it
    to the time-exit at a worse price) or silently clamp the trigger (a strategy
    change), we realize it now. The caller books the realized PnL and records the
    tier as filled. ``trigger_reached`` distinguishes the deliberate pre-check
    case from the ``-2021`` race-fallback (both market-close identically)."""
    index: int
    requested_price: float
    quantity: float
    fill_price: float
    trigger_reached: bool  # True: pre-check; False: -2021 race fallback


@dataclass(slots=True)
class LadderOrdersResult:
    """Outcome of attach_ladder_orders (Phase 2B Branch 2).

    The marching stop is mandatory: if it fails, nothing else is attempted and
    ``success`` is False. Tier/runner failures are captured in ``reasons`` but
    leave the position protected by the stop, so the position is never naked.
    """
    mode: str
    stop_order_id: str | None
    tier_orders: list[TierOrder]
    runner_order_id: str | None
    elapsed_ms: float
    success: bool  # stop + every tier (resting or immediately-filled) + runner all accounted
    reasons: list[str] = field(default_factory=list)
    # Tiers whose target was already reached at placement and were taken at MARKET
    # immediately (item 1). The caller books their realized PnL + marks them filled.
    immediate_fills: list[ImmediateFill] = field(default_factory=list)

    @property
    def ladder_active(self) -> bool:
        """True when the stop is live and at least one tier is accounted for —
        either resting on the book or already taken at market — enough for the
        Branch 2 ladder sync path to manage the position."""
        return self.stop_order_id is not None and (
            len(self.tier_orders) > 0 or len(self.immediate_fills) > 0
        )


class OrderManager:
    """Professional execution layer with paper/live separation and pre-trade checks."""

    def __init__(
        self,
        adapter: ExchangeAdapter,
        risk_engine: RiskEngine | None = None,
        paper: PaperTradingSimulator | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.adapter = adapter
        self.settings = settings or get_settings()
        self.risk_engine = risk_engine or RiskEngine(self.settings)
        self.paper = paper or PaperTradingSimulator(
            starting_equity=self.settings.paper_starting_equity,
            fee_bps=self.settings.fee_rate_bps,
            slippage_bps=self.settings.slippage_bps,
        )

    async def execute_signal(
        self,
        signal: StrategySignal,
        permission_request: TradePermissionRequest,
        quantity: float,
        maker: bool = False,
    ) -> ExecutionReport:
        decision = self.risk_engine.evaluate_trade_permission(permission_request)
        if not decision.allowed:
            logger.info("trade_rejected", symbol=signal.symbol, setup=signal.setup_name, reasons=decision.reasons)
            return ExecutionReport(False, self.settings.trading_mode.value, decision, reasons=decision.reasons)

        execution_score = max(signal.confidence_score, permission_request.setup_score)
        if self.settings.force_limit_orders:
            order_type = "limit"
        else:
            order_type = "market" if execution_score >= self.settings.market_order_min_score else "limit"
        if maker:
            order_type = "limit"  # post-only entries must rest as limit orders
        side = "buy" if signal.direction == "long" else "sell"
        client_order_id = f"proscalp-{uuid4().hex[:20]}"

        if self.settings.trading_mode == TradingMode.PAPER:
            paper_position = self.paper.open_position(
                signal.symbol,
                signal.direction,
                quantity,
                signal.entry_price,
                signal.stop_loss,
                signal.take_profit_levels,
            )
            logger.info("paper_trade_opened", symbol=signal.symbol, side=signal.direction, quantity=quantity)
            return ExecutionReport(True, "paper", decision, paper_position=paper_position)

        limit_price: float | None = None
        if order_type == "limit":
            try:
                limit_price = (
                    await self._maker_limit_price(signal, side)
                    if maker
                    else await self._aggressive_limit_price(signal, side)
                )
            except ValueError as exc:
                logger.info("trade_rejected", symbol=signal.symbol, setup=signal.setup_name, reasons=[str(exc)])
                return ExecutionReport(False, self.settings.trading_mode.value, decision, reasons=[str(exc)])

        request = OrderRequest(
            symbol=signal.symbol,
            side=side,  # type: ignore[arg-type]
            order_type=order_type,  # type: ignore[arg-type]
            quantity=quantity,
            price=limit_price if order_type == "limit" else None,
            client_order_id=client_order_id,
            time_in_force=(
                ("GTX" if maker else self.settings.scalp_limit_time_in_force)
                if order_type == "limit"
                else None
            ),
        )
        try:
            result = await self.adapter.place_order(request)
            logger.info("order_result", symbol=signal.symbol, status=result.status, order_id=result.order_id)
            immediate_limit = request.time_in_force in {"IOC", "FOK"}
            if (
                order_type == "limit"
                and result.filled_quantity <= 0
                and (immediate_limit or result.status in {"expired", "canceled", "cancelled", "rejected"})
            ):
                reason = "scalp limit was not confirmed as filled immediately"
                logger.info("trade_rejected", symbol=signal.symbol, setup=signal.setup_name, reasons=[reason])
                return ExecutionReport(False, self.settings.trading_mode.value, decision, result, reasons=[reason])
            return ExecutionReport(True, self.settings.trading_mode.value, decision, order_result=result)
        except Exception as exc:
            logger.error("order_failed", symbol=signal.symbol, error=str(exc))
            return ExecutionReport(False, self.settings.trading_mode.value, decision, reasons=[str(exc)])

    async def _aggressive_limit_price(self, signal: StrategySignal, side: str) -> float:
        book = await self.adapter.fetch_order_book(signal.symbol, limit=20)
        if not self._book_is_usable(book):
            raise ValueError("order book is not usable for immediate execution")

        reference = book.best_ask if side == "buy" else book.best_bid
        drift_bps = abs(reference - signal.entry_price) / max(signal.entry_price, 1e-12) * 10_000
        if drift_bps > self.settings.max_signal_price_drift_bps:
            raise ValueError(
                f"signal price drift is {drift_bps:.2f} bps; max is {self.settings.max_signal_price_drift_bps:.2f} bps"
            )

        buffer = self.settings.aggressive_limit_slippage_bps / 10_000
        if side == "buy":
            return book.best_ask * (1 + buffer)
        return book.best_bid * (1 - buffer)

    async def _maker_limit_price(self, signal: StrategySignal, side: str) -> float:
        """Post-only (maker) entry price: rest on the maker side of the book so the
        order provides liquidity and earns the maker fee. Paired with TIF=GTX, Binance
        rejects it outright if it would cross (i.e. it can never fill as taker). Guards
        against a stale signal price drifting too far from the live book."""
        book = await self.adapter.fetch_order_book(signal.symbol, limit=20)
        if not self._book_is_usable(book):
            raise ValueError("order book is not usable for maker entry")
        reference = book.best_bid if side == "buy" else book.best_ask
        drift_bps = abs(reference - signal.entry_price) / max(signal.entry_price, 1e-12) * 10_000
        if drift_bps > self.settings.max_signal_price_drift_bps:
            raise ValueError(
                f"maker price drift is {drift_bps:.2f} bps; max is {self.settings.max_signal_price_drift_bps:.2f} bps"
            )
        return book.best_bid if side == "buy" else book.best_ask

    @staticmethod
    def _book_is_usable(book: OrderBook) -> bool:
        return book.best_bid > 0 and book.best_ask > 0 and book.best_ask >= book.best_bid

    @staticmethod
    def _algo_client_id(trade_id: str, role: str) -> str:
        """Stable, traceable clientAlgoId for a protective/ladder order.

        Pattern: ``proscalp-{trade8}-{role}-{nonce}`` (role ∈ stop|tp|tp1..tp4|
        runner|test). The trade id is truncated to 10 hex chars to stay within
        Binance's clientId length limit while remaining traceable back to the
        source trade for reconciliation + incident analysis. The ``proscalp-``
        prefix is how reconciliation distinguishes our orders from foreign ones."""
        short = (trade_id or "").replace("-", "")[:10]
        return f"proscalp-{short}-{role}-{uuid4().hex[:4]}"

    async def cancel_and_replace(
        self,
        symbol: str,
        order_id: str,
        replacement: OrderRequest,
    ) -> OrderResult:
        await self.adapter.cancel_order(symbol, order_id)
        return await self.adapter.place_order(replacement)

    async def reconcile(self) -> dict[str, int]:
        open_orders = await self.adapter.fetch_open_orders()
        positions = await self.adapter.fetch_positions()
        logger.info("exchange_reconciled", open_orders=len(open_orders), positions=len(positions))
        return {"open_orders": len(open_orders), "positions": len(positions)}

    # ----- Phase 2B Branch 1: exchange-resting protective orders -----

    async def attach_protective_orders(
        self,
        signal: StrategySignal,
        trade_id: str,
        *,
        working_type: WorkingType = "MARK_PRICE",
    ) -> ProtectiveOrdersResult:
        """Place STOP_MARKET + TAKE_PROFIT_MARKET (closePosition=True) sequentially.

        Per Branch 1 clarification B: sequential (not parallel). Logs elapsed
        time on attachment; emits a `protective_order_slow` warning when total
        elapsed exceeds `settings.protective_order_max_elapsed_ms`. The result
        carries the order ids so the caller can persist them on the Trade row.
        """
        start = time.perf_counter()
        side = "sell" if signal.direction == "long" else "buy"
        reasons: list[str] = []
        stop_order_id: str | None = None
        take_profit_order_id: str | None = None

        # 1) Stop loss first — protects against the most damaging direction.
        #    Conditional types now route to the Algo Order API (Binance migration).
        stop_request = OrderRequest(
            symbol=signal.symbol,
            side=side,  # type: ignore[arg-type]
            order_type="stop_market",
            stop_price=signal.stop_loss,
            close_position=True,
            working_type=working_type,
            client_order_id=self._algo_client_id(trade_id, "stop"),
        )
        try:
            stop_result = await self.adapter.place_algo_order(stop_request)
            stop_order_id = stop_result.order_id
        except Exception as exc:
            reasons.append(f"stop_market placement failed: {exc}")
            logger.error(
                "protective_stop_failed",
                trade_id=trade_id, symbol=signal.symbol, error=str(exc),
            )

        # 2) Take profit at the final ladder rung — only if stop placement succeeded.
        final_tp = signal.take_profit_levels[-1] if signal.take_profit_levels else None
        if stop_order_id and final_tp:
            tp_request = OrderRequest(
                symbol=signal.symbol,
                side=side,  # type: ignore[arg-type]
                order_type="take_profit_market",
                stop_price=final_tp,
                close_position=True,
                working_type=working_type,
                client_order_id=self._algo_client_id(trade_id, "tp"),
            )
            try:
                tp_result = await self.adapter.place_algo_order(tp_request)
                take_profit_order_id = tp_result.order_id
            except Exception as exc:
                reasons.append(f"take_profit_market placement failed: {exc}")
                logger.error(
                    "protective_tp_failed",
                    trade_id=trade_id, symbol=signal.symbol, error=str(exc),
                )

        elapsed_ms = (time.perf_counter() - start) * 1000.0
        success = stop_order_id is not None and (take_profit_order_id is not None or final_tp is None)

        logger.info(
            "protective_orders_attached",
            trade_id=trade_id, symbol=signal.symbol,
            stop_order_id=stop_order_id, take_profit_order_id=take_profit_order_id,
            elapsed_ms=round(elapsed_ms, 1), success=success,
        )
        if elapsed_ms > self.settings.protective_order_max_elapsed_ms:
            logger.warning(
                "protective_order_slow",
                trade_id=trade_id, symbol=signal.symbol,
                elapsed_ms=round(elapsed_ms, 1),
                threshold_ms=self.settings.protective_order_max_elapsed_ms,
            )

        return ProtectiveOrdersResult(
            stop_order_id=stop_order_id,
            take_profit_order_id=take_profit_order_id,
            elapsed_ms=elapsed_ms,
            success=success,
            reasons=reasons,
        )

    async def cancel_protective_orders(
        self, symbol: str, stop_order_id: str | None, take_profit_order_id: str | None
    ) -> list[str]:
        """Cancel one or both protective orders. Returns issue strings (empty on full success).

        Safe to call when an order has already been auto-cancelled by the exchange
        (e.g. its sibling fired and closed the position) — cancel failures are
        captured as issues but never raise.
        """
        issues: list[str] = []
        for label, order_id in (("stop", stop_order_id), ("take_profit", take_profit_order_id)):
            if not order_id:
                continue
            try:
                await self.adapter.cancel_algo_order(symbol, order_id)
            except Exception as exc:
                issues.append(f"{label} {order_id}: {exc}")
        return issues

    # ----- Phase 2B Branch 2: 5-tier exit ladder -----

    async def _symbol_rules(self, symbol: str) -> SymbolRules:
        """Fetch exchange symbol filters for ladder sizing.

        Adapters that expose ``fetch_symbol_rules`` (Binance) are used directly;
        others fall back to permissive defaults so non-futures/test adapters work.
        """
        fetch = getattr(self.adapter, "fetch_symbol_rules", None)
        if fetch is not None:
            try:
                raw = await fetch(symbol)
                return SymbolRules(
                    tick_size=float(raw.get("tick_size", 0.01)),
                    step_size=float(raw.get("step_size", 0.001)),
                    min_qty=float(raw.get("min_qty", 0.001)),
                    min_notional=float(raw.get("min_notional", 5.0)),
                )
            except Exception as exc:
                logger.warning("ladder_symbol_rules_fallback", symbol=symbol, error=str(exc))
        return SymbolRules(tick_size=0.01, step_size=0.001, min_qty=0.001, min_notional=5.0)

    async def build_ladder_plan(
        self,
        *,
        direction: str,
        entry_price: float,
        stop_loss: float,
        atr: float,
        quantity: float,
        symbol: str,
    ) -> LadderPlan:
        rules = await self._symbol_rules(symbol)
        return build_ladder_plan(
            settings=self.settings,
            direction=direction,
            entry_price=entry_price,
            stop_loss=stop_loss,
            atr=atr,
            quantity=quantity,
            rules=rules,
        )

    async def attach_ladder_orders(
        self,
        plan: LadderPlan,
        symbol: str,
        direction: str,
        trade_id: str,
        *,
        mark_price: float = 0.0,
        working_type: WorkingType = "MARK_PRICE",
    ) -> LadderOrdersResult:
        """Place the full ladder: 1 marching STOP_MARKET (closePosition), N
        reduceOnly TAKE_PROFIT_MARKET tiers, and 1 reduceOnly TRAILING_STOP_MARKET
        runner. Sequential, stop first (never leave the position naked).

        ``mark_price`` is the current mark at placement time. Any tier whose target
        is already reached (``tier_trigger_reached``) is taken at MARKET immediately
        instead of left to be rejected with -2021 (item 1); those land in
        ``result.immediate_fills`` for the caller to book.

        Tier/runner placement failures are recorded but do not abort: the
        closePosition stop alone keeps the position protected.
        """
        if not plan.is_ladder:
            raise ValueError("attach_ladder_orders called with a non-ladder plan")
        start = time.perf_counter()
        exit_side = "sell" if direction == "long" else "buy"
        reasons: list[str] = []
        tier_orders: list[TierOrder] = []
        runner_order_id: str | None = None

        # 1) Marching stop first — mandatory.
        stop_request = OrderRequest(
            symbol=symbol,
            side=exit_side,  # type: ignore[arg-type]
            order_type="stop_market",
            stop_price=plan.stop_price,
            close_position=True,
            working_type=working_type,
            client_order_id=self._algo_client_id(trade_id, "stop"),
        )
        try:
            stop_result = await self.adapter.place_algo_order(stop_request)
            stop_order_id = stop_result.order_id
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            logger.error("ladder_stop_failed", trade_id=trade_id, symbol=symbol, error=str(exc))
            return LadderOrdersResult(
                mode=plan.mode, stop_order_id=None, tier_orders=[], runner_order_id=None,
                elapsed_ms=elapsed_ms, success=False,
                reasons=[f"stop_market placement failed: {exc}"],
            )

        # 2-3) Take-profit tiers (reduceOnly quantity orders — closePosition can't
        #      express partial sizes) plus the trailing runner on the remainder.
        tier_orders, runner_order_id, immediate_fills, place_reasons = await self._place_tiers_and_runner(
            plan, symbol, exit_side, direction, mark_price, trade_id, working_type
        )
        reasons.extend(place_reasons)

        elapsed_ms = (time.perf_counter() - start) * 1000.0
        # A tier is "accounted for" if it is resting on the book OR was taken at
        # market immediately (item 1) — both realize the slice as designed.
        success = (
            stop_order_id is not None
            and len(tier_orders) + len(immediate_fills) == len(plan.tiers)
            and runner_order_id is not None
        )
        logger.info(
            "ladder_orders_attached",
            trade_id=trade_id, symbol=symbol, mode=plan.mode,
            stop_order_id=stop_order_id, tiers=len(tier_orders),
            immediate_fills=len(immediate_fills),
            runner_order_id=runner_order_id, elapsed_ms=round(elapsed_ms, 1), success=success,
        )
        if elapsed_ms > self.settings.protective_order_max_elapsed_ms:
            logger.warning(
                "protective_order_slow", trade_id=trade_id, symbol=symbol,
                elapsed_ms=round(elapsed_ms, 1),
                threshold_ms=self.settings.protective_order_max_elapsed_ms,
            )
        return LadderOrdersResult(
            mode=plan.mode, stop_order_id=stop_order_id, tier_orders=tier_orders,
            runner_order_id=runner_order_id, elapsed_ms=elapsed_ms, success=success,
            reasons=reasons, immediate_fills=immediate_fills,
        )

    @staticmethod
    def _is_would_immediately_trigger(exc: Exception) -> bool:
        """Detect Binance -2021 ('Order would immediately trigger'). The adapter
        appends the raw Binance JSON body to the error string, so the code is
        visible there. Used as the race-fallback path for in-profit tiers (item 1)."""
        text = str(exc)
        return "-2021" in text or "immediately trigger" in text.lower()

    async def _market_close_slice(
        self, symbol: str, exit_side: str, quantity: float, mark_price: float
    ) -> float:
        """Realize one tier's slice at MARKET (reduceOnly). Returns the actual fill
        price when the adapter reports it, else the supplied mark. Raises on failure
        so the caller can record the slice as un-realized (it remains protected by
        the marching closePosition stop)."""
        result = await self.adapter.place_order(
            OrderRequest(
                symbol=symbol,
                side=exit_side,  # type: ignore[arg-type]
                order_type="market",
                quantity=quantity,
                reduce_only=True,
            )
        )
        return float(result.average_price or 0.0) or mark_price

    async def _timed(self, coro):
        """Wrap a single placement in a per-coroutine timeout so one hung request
        cannot stall the whole gather (INV2 timeout isolation). Timeouts surface as
        an exception to the gather and route as a hard-error gap."""
        return await asyncio.wait_for(coro, timeout=self.settings.ladder_attach_order_timeout_s)

    def _tp_request(self, symbol, exit_side, tier, working_type, trade_id) -> OrderRequest:
        return OrderRequest(
            symbol=symbol, side=exit_side, order_type="take_profit_market",  # type: ignore[arg-type]
            quantity=tier.quantity, stop_price=tier.price, reduce_only=True,
            working_type=working_type, client_order_id=self._algo_client_id(trade_id, f"tp{tier.index}"),
        )

    async def _place_tiers_and_runner(
        self,
        plan: LadderPlan,
        symbol: str,
        exit_side: str,
        direction: str,
        mark_price: float,
        trade_id: str,
        working_type: WorkingType,
    ) -> tuple[list[TierOrder], str | None, list[ImmediateFill], list[str]]:
        """Place the reduceOnly TP tiers + trailing runner CONCURRENTLY (item 2).

        The marching stop is placed+confirmed by the caller BEFORE this runs and is
        never in the gather (INV2-1). Here, wave 1 fires every tier + the runner at
        once via asyncio.gather(return_exceptions=True); results zip back to their
        planned legs in order (INV2-5) and route by outcome (INV2-3/4):
          - ok            -> RESTING TierOrder
          - in-profit (pre-check) / -2021 race -> MARKET-close the slice -> ImmediateFill
          - any other error / timeout          -> dropped-but-protected gap (reason)
        The returned structures are identical to the old sequential path (INV2-2)."""
        tier_orders: list[TierOrder] = []
        immediate_fills: list[ImmediateFill] = []
        runner_order_id: str | None = None
        reasons: list[str] = []

        # Build wave-1 coroutines with order-preserving metadata. In-profit tiers
        # (pre-check) go straight to a concurrent market close; the rest are resting
        # algo placements. The runner is the last leg.
        metas: list[tuple[str, object]] = []
        coros = []
        for tier in plan.tiers:
            if tier_trigger_reached(direction, tier.price, mark_price):
                metas.append(("tier_market", tier))
                coros.append(self._timed(self._market_close_slice(symbol, exit_side, tier.quantity, mark_price)))
            else:
                metas.append(("tier_rest", tier))
                coros.append(self._timed(self.adapter.place_algo_order(
                    self._tp_request(symbol, exit_side, tier, working_type, trade_id))))
        has_runner = plan.runner_quantity > 0 and plan.runner_callback_rate is not None
        if has_runner:
            metas.append(("runner", None))
            coros.append(self._timed(self.adapter.place_algo_order(OrderRequest(
                symbol=symbol, side=exit_side, order_type="trailing_stop_market",  # type: ignore[arg-type]
                quantity=plan.runner_quantity, callback_rate=plan.runner_callback_rate,
                activation_price=plan.runner_activation_price, reduce_only=True,
                working_type=working_type, client_order_id=self._algo_client_id(trade_id, "runner")))))

        results = await asyncio.gather(*coros, return_exceptions=True)

        fallback_tiers = []  # -2021 race losers needing a wave-2 market close
        for (kind, meta), res in zip(metas, results):
            if kind == "tier_market":
                tier = meta  # type: ignore[assignment]
                if isinstance(res, BaseException):
                    reasons.append(f"tier {tier.index} in-profit market close failed: {res}")
                    logger.error("ladder_tier_market_close_failed", trade_id=trade_id,
                                 symbol=symbol, tier=tier.index, error=str(res))
                else:
                    immediate_fills.append(ImmediateFill(index=tier.index, requested_price=tier.price,
                                                         quantity=tier.quantity, fill_price=res, trigger_reached=True))
                    logger.info("ladder_tier_market_filled", trade_id=trade_id, symbol=symbol,
                                tier=tier.index, reason="trigger_reached_at_placement",
                                trigger_price=tier.price, fill_price=res)
            elif kind == "tier_rest":
                tier = meta  # type: ignore[assignment]
                if isinstance(res, BaseException):
                    # Route by exception TYPE (INV2-4): -2021 -> fix-A market close;
                    # anything else (network / rate-limit / -4xxx / timeout) -> gap.
                    if isinstance(res, Exception) and self._is_would_immediately_trigger(res):
                        fallback_tiers.append(tier)
                    else:
                        reasons.append(f"tier {tier.index} placement failed: {res}")
                        logger.error("ladder_tier_failed", trade_id=trade_id, symbol=symbol,
                                     tier=tier.index, error=str(res))
                else:
                    tier_orders.append(TierOrder(index=tier.index, order_id=res.order_id,
                                                 price=tier.price, quantity=tier.quantity))
            else:  # runner
                if isinstance(res, BaseException):
                    reasons.append(f"runner placement failed: {res}")
                    logger.error("ladder_runner_failed", trade_id=trade_id, symbol=symbol, error=str(res))
                else:
                    runner_order_id = res.order_id

        # Wave 2: market-close the -2021 race losers, also concurrently. The race is
        # still possible inside the shrunken window, so fix-A handling stays on the
        # concurrent path (not just the old sequential one).
        if fallback_tiers:
            fb_results = await asyncio.gather(
                *[self._timed(self._market_close_slice(symbol, exit_side, t.quantity, mark_price))
                  for t in fallback_tiers],
                return_exceptions=True,
            )
            for tier, res in zip(fallback_tiers, fb_results):
                if isinstance(res, BaseException):
                    reasons.append(f"tier {tier.index} -2021 fallback market close failed: {res}")
                    logger.error("ladder_tier_market_close_failed", trade_id=trade_id,
                                 symbol=symbol, tier=tier.index, error=str(res))
                else:
                    immediate_fills.append(ImmediateFill(index=tier.index, requested_price=tier.price,
                                                         quantity=tier.quantity, fill_price=res, trigger_reached=False))
                    logger.info("ladder_tier_market_filled", trade_id=trade_id, symbol=symbol,
                                tier=tier.index, reason="2021_race_fallback",
                                trigger_price=tier.price, fill_price=res)

        # Stable tier ordering for deterministic downstream recording.
        tier_orders.sort(key=lambda t: t.index)
        immediate_fills.sort(key=lambda f: f.index)
        return tier_orders, runner_order_id, immediate_fills, reasons

    async def replace_ladder_tiers(
        self,
        plan: LadderPlan,
        symbol: str,
        direction: str,
        trade_id: str,
        *,
        mark_price: float = 0.0,
        working_type: WorkingType = "MARK_PRICE",
    ) -> tuple[list[TierOrder], str | None, list[ImmediateFill], list[str]]:
        """Re-place TP tiers + runner against a freshly-sized plan (used after the
        15-minute partial time-exit). Does NOT touch the marching stop, which is
        closePosition and auto-sizes to whatever quantity remains.

        Like attach, any already-reached tier is taken at market immediately
        (item 1) and surfaced in the returned ImmediateFill list."""
        exit_side = "sell" if direction == "long" else "buy"
        return await self._place_tiers_and_runner(
            plan, symbol, exit_side, direction, mark_price, trade_id, working_type
        )

    async def advance_ladder_stop(
        self,
        symbol: str,
        direction: str,
        new_stop_price: float,
        old_stop_order_id: str | None,
        trade_id: str,
        *,
        working_type: WorkingType = "MARK_PRICE",
    ) -> tuple[str | None, list[str]]:
        """Ratchet the marching stop up. Place the NEW algo stop first, then cancel
        the old one (a brief two-stop overlap is safe — both are closePosition — and
        guarantees the position is never momentarily unprotected).

        Returns (new_stop_order_id, issues). On placement failure the old stop is
        left in place (not cancelled) and new id is None.
        """
        exit_side = "sell" if direction == "long" else "buy"
        issues: list[str] = []
        try:
            new_result = await self.adapter.place_algo_order(
                OrderRequest(
                    symbol=symbol,
                    side=exit_side,  # type: ignore[arg-type]
                    order_type="stop_market",
                    stop_price=new_stop_price,
                    close_position=True,
                    working_type=working_type,
                    client_order_id=self._algo_client_id(trade_id, "stop"),
                )
            )
        except Exception as exc:
            logger.error("ladder_stop_advance_failed", symbol=symbol, error=str(exc))
            return None, [f"new stop placement failed: {exc}"]

        if old_stop_order_id:
            try:
                await self.adapter.cancel_algo_order(symbol, old_stop_order_id)
            except Exception as exc:
                issues.append(f"old stop {old_stop_order_id} cancel failed: {exc}")
        return new_result.order_id, issues

    async def cancel_orders(self, symbol: str, order_ids: list[str | None]) -> list[str]:
        """Cancel a batch of algo orders, capturing (never raising) per-order failures."""
        issues: list[str] = []
        for order_id in order_ids:
            if not order_id:
                continue
            try:
                await self.adapter.cancel_algo_order(symbol, order_id)
            except Exception as exc:
                issues.append(f"{order_id}: {exc}")
        return issues
