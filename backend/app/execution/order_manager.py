from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4

import structlog

from app.config.settings import Settings, TradingMode, get_settings
from app.exchanges.base import ExchangeAdapter, OrderBook, OrderRequest, OrderResult
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
