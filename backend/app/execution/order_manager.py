from __future__ import annotations

import time
from dataclasses import dataclass, field
from uuid import uuid4

import structlog

from app.config.settings import Settings, TradingMode, get_settings
from app.exchanges.base import ExchangeAdapter, OrderBook, OrderRequest, OrderResult, WorkingType
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
                limit_price = await self._aggressive_limit_price(signal, side)
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
            time_in_force=self.settings.scalp_limit_time_in_force if order_type == "limit" else None,
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

    @staticmethod
    def _book_is_usable(book: OrderBook) -> bool:
        return book.best_bid > 0 and book.best_ask > 0 and book.best_ask >= book.best_bid

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
        stop_request = OrderRequest(
            symbol=signal.symbol,
            side=side,  # type: ignore[arg-type]
            order_type="stop_market",
            stop_price=signal.stop_loss,
            close_position=True,
            working_type=working_type,
            client_order_id=f"proscalp-sl-{uuid4().hex[:18]}",
        )
        try:
            stop_result = await self.adapter.place_order(stop_request)
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
                client_order_id=f"proscalp-tp-{uuid4().hex[:18]}",
            )
            try:
                tp_result = await self.adapter.place_order(tp_request)
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
                await self.adapter.cancel_order(symbol, order_id)
            except Exception as exc:
                issues.append(f"{label} {order_id}: {exc}")
        return issues
