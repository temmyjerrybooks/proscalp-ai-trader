from __future__ import annotations

import asyncio
import re
import time as _time
from collections import deque
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from uuid import uuid4

import structlog
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.alerts.telegram import TelegramAlertService
from app.config.settings import Settings, TradingMode, get_settings
from app.data.market_data import MarketDataBundle, MarketDataService
from app.database.db import AsyncSessionLocal
from app.database.models import (
    CoinUniverse,
    MarketRegime,
    Order,
    RiskEvent,
    SetupScore,
    Signal,
    Trade,
    utc_now,
)
from app.execution.exit_ladder import compute_target_stop, should_arm_be_plus, time_exit_decision
from app.execution.order_manager import OrderManager
from app.exchanges.base import Candle, ExchangeAdapter, OrderRequest, Position
from app.exchanges.factory import create_exchange_adapter
from app.indicators.technical import build_indicator_snapshot
from app.paper_trading.simulator import PaperTradingSimulator
from app.portfolio.exposure_manager import ExposureManager, ExposurePosition
from app.regime.detector import MarketRegimeDetector, RegimeInput, RegimeResult
from app.risk.risk_engine import RiskEngine, TradePermissionRequest
from app.scoring.setup_score import SetupScoreInput, SetupScoreResult, SetupScoringEngine
from app.services.accounting import (
    account_balance_basis,
    consecutive_loss_count,
    daily_pnl_snapshot,
    daily_trade_count,
    trade_counts_for_loss_streak,
)
from app.sessions.session_manager import SessionManager, SessionState
from app.strategies import default_strategies
from app.strategies.base_strategy import Direction, StrategyContext, StrategySignal
from app.universe.top50_scanner import CoinCandidate, Top50Scanner

logger = structlog.get_logger(__name__)


def _safe_error_message(error: object) -> str:
    """Remove signed exchange query material before storing errors for the UI."""
    text = str(error)
    text = re.sub(r"([?&]signature=)[^&\s'\"]+", r"\1[redacted]", text)
    text = re.sub(r"([?&]timestamp=)\d+", r"\1[redacted]", text)
    text = re.sub(r"([?&]recvWindow=)\d+", r"\1[redacted]", text)
    text = re.sub(r"([?&]newClientOrderId=)[^&\s'\"]+", r"\1[redacted]", text)
    return text[:1000]


@dataclass(slots=True)
class BotRuntimeStatus:
    enabled: bool = False
    status: str = "stopped"
    mode: str = "paper"
    exchange: str = "binance"
    started_at: datetime | None = None
    stopped_at: datetime | None = None
    emergency_stopped: bool = False
    last_scan_count: int = 0
    loop_task_active: bool = False
    cycle_count: int = 0
    last_cycle_at: datetime | None = None
    last_signal_batch_at: datetime | None = None
    last_cycle_message: str = "Idle"
    last_signal_count: int = 0
    last_rejection_count: int = 0
    last_order_count: int = 0
    cycle_symbol_limit: int = 0
    strategy_count: int = 0
    current_session: str = "closed"
    current_regime: str = "unclear"
    open_trade_count: int = 0
    consecutive_losses: int = 0
    recent_consecutive_losses: int = 0
    loss_streak_scope_start: datetime | None = None
    loss_cooldown_since: datetime | None = None
    last_error: str | None = None
    messages: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ScoredSignal:
    signal: StrategySignal
    score: SetupScoreResult
    signal_id: str
    candidate: CoinCandidate
    context: StrategyContext


class BotRunner:
    """Supervised autonomous runtime for paper/testnet scalping.

    The loop is deliberately live-locked. It can run continuously in paper or
    testnet mode, but live modes still require explicit configuration and should
    be reviewed before any production use.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.status = BotRuntimeStatus(mode=self.settings.trading_mode.value, exchange=self.settings.exchange.value)
        self.alerts = TelegramAlertService(self.settings)
        self.paper = PaperTradingSimulator(
            starting_equity=self.settings.paper_starting_equity,
            fee_bps=self.settings.fee_rate_bps,
            slippage_bps=self.settings.slippage_bps,
        )
        self.risk_engine = RiskEngine(self.settings)
        self.scoring = SetupScoringEngine(self.settings)
        self.exposure = ExposureManager(self.settings)
        self.strategies = default_strategies()
        self.status.cycle_symbol_limit = self.settings.bot_cycle_symbol_limit
        self.status.strategy_count = len(self.strategies)
        self._task: asyncio.Task[None] | None = None
        self._last_order_at: datetime | None = None
        self._consecutive_losses = 0
        self._recent_consecutive_losses = 0
        self._loss_streak_scope_start: datetime | None = None
        self._loss_cooldown_since: datetime | None = None
        self._pending_signal_ids: list[str] = []
        self._last_signal_ids: list[str] = []
        # Phase 2B Branch 1 refinement 2 — protective-orders failure circuit-breaker.
        self._protective_failures: deque[datetime] = deque()
        self._resting_disabled_until_utc_day: date | None = None

    def current_signal_ids(self) -> list[str]:
        """Return the exact signal IDs from the last committed scan batch."""
        return list(self._last_signal_ids)

    async def start(self) -> BotRuntimeStatus:
        if not self.settings.autonomous_trading_enabled:
            self._remember("Autonomous trading loop is disabled by AUTONOMOUS_TRADING_ENABLED=false")
            self.status.status = "blocked"
            return self.status
        if self.settings.is_live_mode and not self.settings.live_trading_enabled:
            self._remember("Live trading requested but LIVE_TRADING_ENABLED=false")
            self.status.status = "blocked"
            return self.status
        if self.settings.trading_mode == TradingMode.LIVE_FUTURES and not self.settings.futures_trading_confirmed:
            self._remember("Live futures requested but FUTURES_TRADING_CONFIRMED=false")
            self.status.status = "blocked"
            return self.status
        if self._task and not self._task.done():
            self._remember("Bot loop already running")
            return self.status

        self.status.enabled = True
        self.status.status = "running"
        self.status.started_at = datetime.now(timezone.utc)
        self.status.stopped_at = None
        self.status.emergency_stopped = False
        self.status.last_error = None
        self._remember("Bot started; autonomous loop armed")

        # Phase 2B Branch 2: flag-consistency guard. The ladder REQUIRES resting
        # orders to function; ladder-on while resting-off is invalid. Disable the
        # ladder in-memory and log loudly rather than run a broken configuration.
        if self.settings.five_tier_ladder_enabled and not self.settings.exchange_resting_exits_enabled:
            logger.error(
                "ladder_flag_inconsistent",
                detail="five_tier_ladder_enabled=True requires exchange_resting_exits_enabled=True; "
                       "disabling ladder in-memory for this run",
            )
            self._remember("Config error: ladder enabled without resting exits; ladder disabled")
            self.settings.five_tier_ladder_enabled = False

        # Phase 2B Branch 1: startup reconciliation + adapter smoke test.
        # Wrapped so a failure logs loudly but does not block the main loop —
        # the loop's per-cycle sync will eventually pick up state divergence.
        try:
            await self._run_startup_checks()
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("startup_checks_failed", error=str(exc))
            self._remember(f"Startup checks failed: {exc}")

        self._task = asyncio.create_task(self._run_loop(), name="proscalp-autonomous-loop")
        logger.info("bot_started", mode=self.status.mode, exchange=self.status.exchange)
        await self.alerts.send("bot_started", f"ProScalp AI Trader started in {self.status.mode} mode")
        return self.status

    async def _run_startup_checks(self) -> None:
        """Phase 2B Branch 1: startup reconciliation + adapter smoke test.

        Only runs in non-paper futures mode. Either check is independently
        gated by its own settings flag.
        """
        if self.settings.trading_mode == TradingMode.PAPER or not self.settings.is_futures_mode:
            return
        adapter = create_exchange_adapter(self.settings)
        if self.settings.startup_adapter_test_enabled and self.settings.exchange_resting_exits_enabled:
            await self._startup_adapter_test(adapter)
        if self.settings.startup_reconciliation_enabled and self.settings.exchange_resting_exits_enabled:
            async with AsyncSessionLocal() as db:
                await self._startup_reconciliation(db, adapter)

    async def _startup_reconciliation(self, db: AsyncSession, adapter: ExchangeAdapter) -> None:
        """Phase 2B Branch 1 clarification C: 3-state startup reconciliation.

        State 1: DB open trade + matching protective orders -> log 'startup_reconciled'.
        State 2: DB open trade + no matching protective orders -> attach + 'protective_orders_repaired'.
        State 3: protective orders on exchange + no DB trade -> cancel + 'orphan_orders_cancelled'.
        """
        try:
            db_trades = await self._open_database_trades(db)
            exchange_positions = await adapter.fetch_positions()
            exchange_orders = await adapter.fetch_open_algo_orders()
        except Exception as exc:
            logger.error("startup_reconciliation_fetch_failed", error=str(exc))
            return

        position_symbols = {p.symbol for p in exchange_positions}
        protective_by_symbol: dict[str, list] = {}
        for order in exchange_orders:
            # Protective/ladder orders are algo orders; identified by the clientAlgoId
            # 'proscalp-' prefix. Foreign algo orders never match and are never touched.
            cid = str((order.raw or {}).get("clientAlgoId", ""))
            if cid.startswith("proscalp-"):
                protective_by_symbol.setdefault(order.symbol, []).append(order)
        db_open_symbols = {t.symbol for t in db_trades}

        reconciled = 0
        repaired = 0
        orphan_cancelled = 0
        manager = OrderManager(adapter, risk_engine=self.risk_engine, paper=self.paper, settings=self.settings)

        for trade in db_trades:
            if trade.symbol not in position_symbols:
                continue  # position closed externally; per-cycle sync will finalize
            existing = protective_by_symbol.get(trade.symbol, [])
            existing_ids = {o.order_id for o in existing}
            extra = trade.extra or {}
            stored = {extra.get("stop_order_id"), extra.get("take_profit_order_id")} - {None, ""}
            if stored and stored.issubset(existing_ids):
                reconciled += 1
                logger.info("startup_reconciled", trade_id=trade.id, symbol=trade.symbol)
                continue
            # State 2: missing one or both protective orders -> repair.
            levels = self._take_profit_levels(trade)
            synthetic = StrategySignal(
                setup_name=trade.setup_name,
                symbol=trade.symbol,
                direction=trade.side,  # type: ignore[arg-type]
                entry_price=trade.entry_price,
                stop_loss=trade.stop_loss,
                take_profit_levels=levels,
                trailing_stop=0.0,
                expected_move=0.0,
                risk_reward_ratio=0.0,
                confidence_score=0.0,
                accepted=True,
            )
            protective = await manager.attach_protective_orders(synthetic, trade.id)
            new_extra = dict(extra)
            new_extra["protective_orders_attached_ms"] = round(protective.elapsed_ms, 1)
            if protective.success:
                new_extra["exchange_resting_active"] = True
                new_extra["stop_order_id"] = protective.stop_order_id
                new_extra["take_profit_order_id"] = protective.take_profit_order_id
                repaired += 1
                logger.info(
                    "protective_orders_repaired",
                    trade_id=trade.id, symbol=trade.symbol,
                    stop_order_id=protective.stop_order_id,
                    take_profit_order_id=protective.take_profit_order_id,
                )
            else:
                new_extra["exchange_resting_active"] = False
                new_extra["protective_orders_failed"] = "; ".join(protective.reasons) or "unknown"
                logger.warning(
                    "startup_protective_repair_failed",
                    trade_id=trade.id, symbol=trade.symbol, reasons=protective.reasons,
                )
            trade.extra = new_extra
            await self._record_protective_attach_outcome(
                db, protective.success,
                context={"trade_id": trade.id, "symbol": trade.symbol, "source": "startup_repair"},
            )

        # State 3: orphan protective orders on the exchange with no DB counterpart.
        for symbol, orders in protective_by_symbol.items():
            if symbol in db_open_symbols:
                continue
            for order in orders:
                try:
                    await adapter.cancel_algo_order(symbol, order.order_id)
                    orphan_cancelled += 1
                    logger.info(
                        "orphan_orders_cancelled",
                        symbol=symbol, order_id=order.order_id,
                        client_order_id=(order.raw or {}).get("clientAlgoId"),
                    )
                except Exception as exc:
                    logger.warning(
                        "orphan_order_cancel_failed",
                        symbol=symbol, order_id=order.order_id, error=str(exc),
                    )

        await self._risk_event(
            db, "info", "startup_reconciliation",
            f"reconciled={reconciled} repaired={repaired} orphan_cancelled={orphan_cancelled}",
            {"reconciled": reconciled, "repaired": repaired, "orphan_cancelled": orphan_cancelled},
        )
        await db.commit()

    async def _startup_adapter_test(self, adapter: ExchangeAdapter) -> None:
        """Phase 2B (algo remediation): catch adapter bugs before they affect real positions.

        Runs the FULL algo-order lifecycle against the live exchange on BTCUSDT:
        place a far-from-market conditional STOP_MARKET via the Algo Order API ->
        GET to confirm it rested -> cancel -> GET to confirm it's gone. The trigger
        sits at mid * 0.5, so it cannot fill in the window. Unlike production cancels
        (which trust HTTP 2xx — see cancel_algo_order), this verification path does a
        confirmation GET *after* cancel because its whole job is to prove the round
        trip end-to-end. A quantity order (sized for min-notional) is used rather than
        closePosition, since startup usually runs with no BTC position. Logs loudly on
        failure but never blocks startup (degraded path falls back to legacy polling).
        """
        symbol = "BTCUSDT"
        try:
            book = await adapter.fetch_order_book(symbol, limit=5)
            mark = book.mid_price
            if mark <= 0:
                raise ValueError("mid price not available")
            rules = await adapter.fetch_symbol_rules(symbol)  # type: ignore[attr-defined]
        except Exception as exc:
            logger.warning("startup_adapter_test_skip", reason=f"book/rules fetch failed: {exc}")
            return

        trigger = mark * 0.5  # far below market; cannot trigger in the placement window
        # Notional is valued at the trigger for a conditional market order; size for
        # min-notional there (with buffer) so the probe order isn't rejected (-4164).
        quantity = max(float(rules["min_qty"]), (float(rules["min_notional"]) * 1.15) / trigger)
        request = OrderRequest(
            symbol=symbol,
            side="sell",
            order_type="stop_market",
            stop_price=trigger,
            quantity=quantity,
            working_type="MARK_PRICE",
            client_order_id=OrderManager._algo_client_id("startup", "test"),
        )
        start = _time.perf_counter()
        try:
            result = await adapter.place_algo_order(request)
        except Exception as exc:
            elapsed_ms = (_time.perf_counter() - start) * 1000.0
            logger.error(
                "startup_adapter_test_failed",
                stage="place", error=str(exc), elapsed_ms=round(elapsed_ms, 1),
            )
            return

        try:
            placed = await adapter.fetch_algo_order(symbol, result.order_id)
            logger.info("startup_adapter_test_placed", order_id=result.order_id, status=placed.status)
        except Exception as exc:
            logger.warning("startup_adapter_test_get_failed", stage="get_after_place",
                           order_id=result.order_id, error=str(exc))

        try:
            await adapter.cancel_algo_order(symbol, result.order_id)
        except Exception as exc:
            logger.error("startup_adapter_test_failed", stage="cancel",
                         order_id=result.order_id, error=str(exc))
            return

        # Confirmation GET (verification path only — production cancels skip this).
        confirmed = "unconfirmed"
        try:
            after = await adapter.fetch_algo_order(symbol, result.order_id)
            confirmed = after.status
        except Exception:
            confirmed = "gone"  # a 4xx GET on a cancelled order is an acceptable "gone"
        elapsed_ms = (_time.perf_counter() - start) * 1000.0
        logger.info(
            "startup_adapter_test_ok",
            order_id=result.order_id, cancel_confirmed=confirmed, elapsed_ms=round(elapsed_ms, 1),
        )

    async def stop(self) -> BotRuntimeStatus:
        self.status.enabled = False
        self.status.status = "stopped"
        self.status.stopped_at = datetime.now(timezone.utc)
        self._remember("Bot stopped")
        await self._cancel_loop()
        logger.info("bot_stopped")
        await self.alerts.send("bot_stopped", "ProScalp AI Trader stopped")
        return self.status

    async def emergency_stop(self) -> BotRuntimeStatus:
        self.status.enabled = False
        self.status.status = "emergency_stopped"
        self.status.emergency_stopped = True
        self.status.stopped_at = datetime.now(timezone.utc)
        self._remember("Emergency shutdown triggered")
        await self._cancel_loop()
        try:
            adapter = create_exchange_adapter(self.settings)
            if self.settings.trading_mode != TradingMode.PAPER:
                for position in await adapter.fetch_positions():
                    await adapter.close_position(position.symbol)
            self.paper.close_all({}, reason="emergency_close")
            async with AsyncSessionLocal() as db:
                await self._mark_open_trades_closed(db, "emergency_stop")
        except Exception as exc:  # pragma: no cover - defensive exchange branch
            self.status.last_error = str(exc)
            logger.error("emergency_close_failed", error=str(exc))
        logger.warning("emergency_shutdown")
        await self.alerts.send("emergency_shutdown", "Emergency shutdown triggered")
        return self.status

    async def close_all_positions(self, reason: str = "manual_close_all") -> dict[str, object]:
        """Flatten open positions and update local trade records.

        This is intentionally separate from emergency_stop: it closes risk, but
        it does not disable the autonomous loop.
        """
        adapter = create_exchange_adapter(self.settings)
        closed_orders: list[dict[str, object]] = []
        canceled_orders: list[dict[str, object]] = []
        errors: list[str] = []
        failed_close_symbols: set[str] = set()
        failed_cancel_symbols: set[str] = set()
        async with AsyncSessionLocal() as db:
            active_trades = await self._active_database_trades(db)
            exchange_positions = await self._safe_fetch_positions(adapter)
            open_orders = await self._safe_fetch_open_orders(adapter)
            price_by_symbol = await self._best_effort_mark_prices(adapter, active_trades, exchange_positions)

            if self.settings.trading_mode != TradingMode.PAPER:
                for order in open_orders:
                    try:
                        canceled = await adapter.cancel_order(order.symbol, order.order_id)
                        canceled_orders.append(
                            {"symbol": order.symbol, "order_id": order.order_id, "status": canceled.status}
                        )
                    except Exception as exc:
                        failed_cancel_symbols.add(order.symbol)
                        errors.append(f"{order.symbol} cancel failed: {exc}")

                symbols_to_close = {position.symbol for position in exchange_positions}
                symbols_to_close.update(trade.symbol for trade in active_trades if trade.status == "open")
                for symbol in sorted(symbols_to_close):
                    try:
                        result = await adapter.close_position(symbol)
                        closed_orders.append(
                            {
                                "symbol": symbol,
                                "order_id": result.order_id,
                                "status": result.status,
                                "quantity": result.quantity,
                            }
                        )
                    except Exception as exc:
                        failed_close_symbols.add(symbol)
                        errors.append(f"{symbol} close failed: {exc}")
            else:
                self.paper.close_all(price_by_symbol, reason=reason)

            for trade in active_trades:
                if trade.status == "pending":
                    if trade.symbol in failed_cancel_symbols:
                        continue
                    trade.status = "canceled"
                    trade.closed_at = utc_now()
                    trade.extra = {**(trade.extra or {}), "close_reason": f"{reason}_pending_cancel"}
                    continue
                if trade.symbol in failed_close_symbols:
                    continue
                price = price_by_symbol.get(trade.symbol, trade.entry_price)
                await self._finalize_trade_close(db, trade, price, reason, send_alert=False)

            db.add(
                RiskEvent(
                    severity="warning" if errors else "info",
                    event_type="manual_close_all",
                    message="Manual close-all completed" if not errors else "Manual close-all completed with errors",
                    payload={
                        "closed_orders": closed_orders,
                        "canceled_orders": canceled_orders,
                        "errors": errors,
                        "trade_count": len(active_trades),
                    },
                )
            )
            await db.commit()

        await self.alerts.send(
            "trade_closed",
            f"Manual close-all completed: {len(closed_orders)} exchange closes, {len(errors)} errors",
        )
        self._remember("Manual close-all executed")
        return {
            "accepted": True,
            "message": "Close-all completed" if not errors else "Close-all completed with errors",
            "closed_orders": closed_orders,
            "canceled_orders": canceled_orders,
            "errors": errors,
        }

    async def run_top50_scan(self, db: AsyncSession | None = None) -> list[CoinCandidate]:
        adapter = create_exchange_adapter(self.settings)
        scanner = Top50Scanner(adapter, self.settings)
        candidates = await scanner.scan(db=db)
        self.status.last_scan_count = len(candidates)
        logger.info("top50_scan_completed", count=len(candidates))
        await self.alerts.send("daily_top50_scan_completed", f"Top-50 scan completed: {len(candidates)} symbols")
        return candidates

    async def _run_loop(self) -> None:
        self.status.loop_task_active = True
        try:
            while self.status.enabled:
                await self.run_cycle()
                await asyncio.sleep(max(5, self.settings.bot_loop_interval_seconds))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - last-resort guard
            safe_error = _safe_error_message(exc)
            self.status.status = "error"
            self.status.last_error = safe_error
            self._remember(f"Autonomous loop crashed: {safe_error}")
            logger.exception("bot_loop_crashed", error=safe_error)
            await self.alerts.send("exchange_api_error", f"Bot loop crashed: {safe_error}")
        finally:
            self.status.loop_task_active = False

    async def run_cycle(self) -> dict[str, object]:
        adapter = create_exchange_adapter(self.settings)
        async with AsyncSessionLocal() as db:
            await self._manage_open_trades(db, adapter)
            candidates = await self._load_watchlist(db)
            if not candidates:
                candidates = await self.run_top50_scan(db)

            regime = await self._detect_regime(adapter, candidates)
            await self._persist_regime(db, regime)
            session = SessionManager(self.settings).trading_session(regime=regime.regime)
            self._update_cycle_status(session, regime)

            if not self.status.enabled:
                return {"status": "stopped"}
            if not self._regime_allows_new_trades(regime):
                return await self._cycle_wait(db, f"Market regime is {regime.regime}; trading paused")
            if not session.tradable:
                return await self._cycle_wait(db, "No tradable session is active")

            open_trades = await self._active_database_trades(db)
            exchange_positions = await self._safe_fetch_positions(adapter)
            open_orders = await self._safe_fetch_open_orders(adapter)
            await self._reconcile_pending_and_orphan_positions(db, exchange_positions, open_orders)
            open_trades = await self._active_database_trades(db)
            active_symbols = {trade.symbol for trade in open_trades}
            exchange_only_symbols = {position.symbol for position in exchange_positions if position.symbol not in active_symbols}
            active_symbol_count = len(active_symbols | exchange_only_symbols)
            account_equity = await self._account_equity(adapter)
            daily_snapshot = await daily_pnl_snapshot(
                db,
                self.settings,
                None if self.settings.trading_mode == TradingMode.PAPER else adapter,
                account_balance=account_equity,
                exchange_positions=exchange_positions,
            )
            self._loss_streak_scope_start = self._loss_streak_since(session, daily_snapshot.day_start)
            cooldown_cutoff = datetime.now(timezone.utc) - timedelta(minutes=max(1, self.settings.loss_cooldown_minutes))
            self._loss_cooldown_since = max(self._loss_streak_scope_start, cooldown_cutoff)
            self._consecutive_losses = await consecutive_loss_count(
                db,
                since=self._loss_streak_scope_start,
                min_abs_pnl=self.settings.loss_streak_min_abs_pnl,
            )
            self._recent_consecutive_losses = await consecutive_loss_count(
                db,
                since=self._loss_cooldown_since,
                min_abs_pnl=self.settings.loss_streak_min_abs_pnl,
            )
            self._update_loss_status()
            today_trade_count = await daily_trade_count(db, daily_snapshot.day_start)
            scan_only_reasons = self._scan_only_reasons(
                active_symbol_count=active_symbol_count,
                open_order_count=len(open_orders),
                today_trade_count=today_trade_count,
                daily_pnl_pct=daily_snapshot.daily_pnl_pct,
            )
            account_equity = daily_snapshot.account_equity
            btc_direction, eth_direction = await self._leader_directions(adapter)
            scored_signals = await self._scan_for_signals(
                db=db,
                adapter=adapter,
                candidates=candidates,
                session=session,
                regime=regime,
                btc_direction=btc_direction,
                eth_direction=eth_direction,
            )
            if not scored_signals:
                result = await self._cycle_wait(db, "No setup passed strategy scoring")
                self._publish_pending_signal_batch()
                return result
            if scan_only_reasons:
                message = f"Scan-only cycle: {'; '.join(scan_only_reasons)}"
                await self._risk_event(
                    db,
                    "info",
                    "scan_only",
                    message,
                    {
                        "signals": len(scored_signals),
                        "active_symbols": active_symbol_count,
                        "open_orders": len(open_orders),
                        "today_trade_count": today_trade_count,
                        "daily_pnl_pct": daily_snapshot.daily_pnl_pct,
                    },
                )
                result = await self._cycle_wait(db, message)
                self._publish_pending_signal_batch()
                return {**result, "signals": len(scored_signals), "orders": 0}

            orders_sent = 0
            for scored in scored_signals:
                if orders_sent >= self.settings.bot_max_orders_per_cycle:
                    break
                if self._order_cooldown_active():
                    break
                result = await self._try_execute_scored_signal(
                    db=db,
                    adapter=adapter,
                    scored=scored,
                    account_equity=account_equity,
                    daily_pnl_pct=daily_snapshot.daily_pnl_pct,
                    session=session,
                    regime=regime,
                    open_trades=open_trades,
                    exchange_positions=exchange_positions,
                )
                if result:
                    orders_sent += 1
                    self._last_order_at = datetime.now(timezone.utc)

            await db.commit()
            self._publish_pending_signal_batch()
            self.status.last_order_count = orders_sent
            message = f"Cycle complete: {len(scored_signals)} tradable signals, {orders_sent} orders"
            self.status.last_cycle_message = message
            self._remember(message)
            return {"status": "complete", "signals": len(scored_signals), "orders": orders_sent}

    async def _scan_for_signals(
        self,
        db: AsyncSession,
        adapter: ExchangeAdapter,
        candidates: list[CoinCandidate],
        session: SessionState,
        regime: RegimeResult,
        btc_direction: Direction | None,
        eth_direction: Direction | None,
    ) -> list[ScoredSignal]:
        market_data = MarketDataService(adapter)
        scored: list[ScoredSignal] = []
        signal_ids: list[str] = []
        signal_count = 0
        rejection_count = 0
        self.status.last_signal_batch_at = datetime.now(timezone.utc)
        active_candidates = [candidate for candidate in candidates if candidate.approved]
        for candidate in active_candidates[: self.settings.bot_cycle_symbol_limit]:
            try:
                bundle = await market_data.fetch_bundle(
                    candidate.symbol,
                    timeframes=["15m", "5m", "3m", "1m"],
                    candle_limit=120,
                )
            except Exception as exc:
                await self._risk_event(db, "warning", "market_data_error", str(exc), {"symbol": candidate.symbol})
                continue
            context = self._build_strategy_context(
                candidate=candidate,
                bundle=bundle,
                session=session,
                regime=regime,
                btc_direction=btc_direction,
                eth_direction=eth_direction,
            )
            best_for_symbol: ScoredSignal | None = None
            for strategy in self.strategies:
                signal = strategy.evaluate(context)
                signal_count += 1
                signal_id = await self._persist_signal(db, signal)
                signal_ids.append(signal_id)
                if not signal.accepted:
                    rejection_count += 1
                    continue
                score = self._score_signal(signal, context, candidate, regime, session, bundle)
                await self._persist_score(db, signal_id, signal, score)
                minimum_score = self._minimum_score_for_session(session)
                if score.total < minimum_score:
                    rejection_count += 1
                    reason = (
                        f"setup score {score.total} below off-session threshold {minimum_score}"
                        if session.name == "off_session"
                        else f"setup score {score.total} below session threshold {minimum_score}"
                    )
                    await self._risk_event(
                        db,
                        "info",
                        "setup_rejected",
                        "; ".join([reason, *score.rejection_reasons]),
                        {
                            "signal_id": signal_id,
                            "symbol": signal.symbol,
                            "setup": signal.setup_name,
                            "score": score.total,
                        },
                    )
                    continue
                item = ScoredSignal(signal, score, signal_id, candidate, context)
                if best_for_symbol is None or score.total > best_for_symbol.score.total:
                    best_for_symbol = item
            if best_for_symbol:
                scored.append(best_for_symbol)

        scored.sort(key=lambda item: item.score.total, reverse=True)
        self.status.last_signal_count = signal_count
        self.status.last_rejection_count = rejection_count
        self._pending_signal_ids = signal_ids
        return scored

    def _publish_pending_signal_batch(self) -> None:
        self._last_signal_ids = list(self._pending_signal_ids)
        self._pending_signal_ids = []

    async def _try_execute_scored_signal(
        self,
        db: AsyncSession,
        adapter: ExchangeAdapter,
        scored: ScoredSignal,
        account_equity: float,
        daily_pnl_pct: float,
        session: SessionState,
        regime: RegimeResult,
        open_trades: list[Trade],
        exchange_positions: list[Position],
    ) -> bool:
        signal = scored.signal
        if signal.symbol in {trade.symbol for trade in open_trades}:
            await self._risk_event(
                db,
                "info",
                "trade_rejected",
                "active database trade already exists",
                {**asdict(signal), "signal_id": scored.signal_id},
            )
            return False
        if signal.symbol in {position.symbol for position in exchange_positions}:
            await self._risk_event(
                db,
                "info",
                "trade_rejected",
                "active exchange position already exists",
                {**asdict(signal), "signal_id": scored.signal_id},
            )
            return False

        daily_state = self.risk_engine.evaluate_daily_state(
            daily_pnl_pct,
            self._consecutive_losses,
            recent_consecutive_losses=self._recent_consecutive_losses,
        )
        setup_assessment = self.risk_engine.assess_setup_score(scored.score.total, session.name, daily_state)
        risk_pct = setup_assessment.risk_pct
        position_size = self.risk_engine.calculate_position_size(
            account_equity=account_equity,
            risk_pct=risk_pct,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            leverage=self.settings.default_leverage,
        )
        side = "buy" if signal.direction == "long" else "sell"
        try:
            slippage_bps = await adapter.estimate_slippage(signal.symbol, side, position_size.quantity)
        except Exception:
            slippage_bps = self.settings.slippage_bps
        expected_net_profit_pct = self._expected_net_profit_pct(signal, scored.context.spread_bps, slippage_bps)
        order_size_valid = await adapter.validate_min_order_size(
            signal.symbol,
            position_size.quantity,
            signal.entry_price,
        )
        btc_eth_confirmed = self._leader_confirmation_valid(scored.context, signal.direction, scored.score)
        open_exposure = self._exposure_positions(open_trades, exchange_positions, session.name)
        exposure_decision = self.exposure.can_open(
            account_equity=account_equity,
            candidate_symbol=signal.symbol,
            candidate_side=signal.direction,
            candidate_notional=position_size.notional,
            candidate_open_risk=self._position_open_risk(
                entry_price=signal.entry_price,
                stop_loss=signal.stop_loss,
                quantity=position_size.quantity,
            ),
            session=session.name,
            market_regime=regime.regime,
            open_positions=open_exposure,
            btc_eth_confirmation=btc_eth_confirmed,
        )
        if not exposure_decision.allowed:
            await self._risk_event(
                db,
                "info",
                "trade_rejected",
                "; ".join(exposure_decision.reasons),
                {
                    "signal_id": scored.signal_id,
                    "symbol": signal.symbol,
                    "setup": signal.setup_name,
                    "exposure": exposure_decision.diagnostics,
                },
            )
            return False

        permission = TradePermissionRequest(
            bot_enabled=self.status.enabled,
            exchange_connected=True,
            live_requested=self.settings.is_live_mode,
            daily_pnl_pct=daily_pnl_pct,
            consecutive_losses=self._consecutive_losses,
            recent_consecutive_losses=self._recent_consecutive_losses,
            open_trades=self._active_symbol_count(open_trades, exchange_positions),
            market_regime=regime.regime,
            session_tradable=session.tradable,
            coin_in_watchlist=scored.candidate.approved,
            liquid_enough=scored.candidate.liquidity_score >= 40,
            spread_bps=scored.context.spread_bps,
            expected_net_profit_pct=expected_net_profit_pct,
            setup_score=scored.score.total,
            setup_grade=setup_assessment.grade,
            btc_eth_confirmed=btc_eth_confirmed,
            risk_reward=signal.risk_reward_ratio,
            position_size_valid=position_size.valid,
            order_size_valid=order_size_valid,
            setup_score_threshold=self._minimum_score_for_session(session),
        )
        if self.settings.trading_mode != TradingMode.PAPER and self.settings.is_futures_mode:
            try:
                await adapter.set_leverage(signal.symbol, position_size.leverage)
            except Exception as exc:
                await self._risk_event(
                    db,
                    "error",
                    "exchange_api_error",
                    f"failed to set leverage before order: {_safe_error_message(exc)}",
                    {
                        "signal_id": scored.signal_id,
                        "symbol": signal.symbol,
                        "setup": signal.setup_name,
                        "leverage": position_size.leverage,
                    },
                )
                return False

        manager = OrderManager(adapter, risk_engine=self.risk_engine, paper=self.paper, settings=self.settings)
        report = await manager.execute_signal(signal, permission, position_size.quantity)
        trade = await self._persist_execution(
            db,
            scored,
            report=report,
            quantity=position_size.quantity,
            setup_assessment=setup_assessment,
            position_size=position_size,
            session=session,
            regime=regime,
        )

        # Phase 2B: attach exchange-resting exits for live/testnet futures entries
        # that filled. Branch 2 (ladder flag ON) attaches the 5-tier ladder;
        # otherwise Branch 1's single stop+TP. Failure falls back to legacy
        # mid-price polling (logged loudly).
        if (
            report.accepted
            and trade is not None
            and trade.status == "open"
            and self._use_exchange_resting_exits()
            and report.order_result is not None
        ):
            if self._use_ladder_exits():
                await self._attach_ladder_exits(db, manager, signal, trade, scored)
            else:
                await self._attach_branch1_protective(db, manager, signal, trade)

        if not report.accepted:
            await self._risk_event(
                db,
                "info",
                "trade_rejected",
                "; ".join(report.reasons or report.risk_decision.reasons),
                {
                    "signal_id": scored.signal_id,
                    "symbol": signal.symbol,
                    "setup": signal.setup_name,
                    "score": scored.score.total,
                    "grade": setup_assessment.grade,
                    "risk_pct": setup_assessment.risk_pct,
                },
            )
            return False

        await self.alerts.send(
            "trade_opened",
            f"{self.settings.trading_mode.value.upper()} {signal.direction.upper()} {signal.symbol} "
            f"{signal.setup_name} score {scored.score.total} grade {setup_assessment.grade} "
            f"risk {setup_assessment.risk_pct:.2f}%",
        )
        return True

    def _use_exchange_resting_exits(self) -> bool:
        """Phase 2B Branch 1: are exchange-resting protective orders active?

        Requires the feature flag, futures market type, and a non-paper mode
        (testnet or live). Paper mode keeps the simulator authoritative.

        Circuit-breaker (refinement 2): if attach_protective_orders has failed
        >= protective_orders_failure_threshold times in the rolling
        protective_orders_failure_window_hours window, returns False for the
        rest of the UTC day, auto-resetting at the next UTC day-start.
        """
        if not (
            self.settings.exchange_resting_exits_enabled
            and self.settings.is_futures_mode
            and self.settings.trading_mode != TradingMode.PAPER
        ):
            return False
        today = datetime.now(timezone.utc).date()
        if self._resting_disabled_until_utc_day is None:
            return True
        if self._resting_disabled_until_utc_day == today:
            return False  # circuit-breaker tripped today
        # Past trip date is < today -> auto-reset.
        self._resting_disabled_until_utc_day = None
        return True

    async def _record_protective_attach_outcome(
        self, db: AsyncSession, success: bool, *, context: dict | None = None
    ) -> bool:
        """Record an attach_protective_orders outcome. Trips the circuit-breaker
        when failures within the rolling window meet the threshold.

        Returns True if this call tripped the breaker (so callers can dedupe alerts).
        """
        if success:
            return False
        now = datetime.now(timezone.utc)
        self._protective_failures.append(now)
        cutoff = now - timedelta(hours=max(1, self.settings.protective_orders_failure_window_hours))
        while self._protective_failures and self._protective_failures[0] < cutoff:
            self._protective_failures.popleft()
        if len(self._protective_failures) < self.settings.protective_orders_failure_threshold:
            return False
        # Trip the breaker.
        self._resting_disabled_until_utc_day = now.date()
        self._protective_failures.clear()
        message = (
            f"protective_orders circuit-breaker tripped: "
            f"{self.settings.protective_orders_failure_threshold} failures within "
            f"{self.settings.protective_orders_failure_window_hours}h; "
            f"exchange-resting exits auto-disabled until UTC midnight"
        )
        await self._risk_event(
            db, "warning", "protective_orders_circuit_breaker", message,
            {**(context or {}),
             "threshold": self.settings.protective_orders_failure_threshold,
             "window_hours": self.settings.protective_orders_failure_window_hours,
             "tripped_at_utc": now.isoformat()},
        )
        try:
            await self.alerts.send("protective_orders_circuit_breaker", message)
        except Exception as exc:  # pragma: no cover - alerting is best-effort
            logger.warning("circuit_breaker_alert_failed", error=str(exc))
        logger.warning(
            "protective_orders_circuit_breaker_tripped",
            threshold=self.settings.protective_orders_failure_threshold,
            window_hours=self.settings.protective_orders_failure_window_hours,
        )
        return True

    def _use_ladder_exits(self) -> bool:
        """Phase 2B Branch 2: is the full 5-tier ladder active? Requires the
        ladder flag AND all of the Branch 1 exchange-resting preconditions
        (and respects the circuit-breaker via _use_exchange_resting_exits)."""
        return self.settings.five_tier_ladder_enabled and self._use_exchange_resting_exits()

    def _entry_atr(self, scored: ScoredSignal) -> float:
        """Capture the literal 14-period ATR at entry (5m primary, 3m fallback).

        Persisted on the trade so the management loop can size BE+ arming, the
        runner trail, and tier checks consistently. Falls back to the risk
        distance |entry-stop| if no usable candle history is available."""
        context = scored.context
        for timeframe in ("5m", "3m"):
            candles = context.candles_by_timeframe.get(timeframe) or []
            if len(candles) >= 15:
                snapshot = build_indicator_snapshot(candles)
                if snapshot and snapshot.atr > 0:
                    return float(snapshot.atr)
        return abs(scored.signal.entry_price - scored.signal.stop_loss)

    async def _attach_branch1_protective(
        self, db: AsyncSession, manager: OrderManager, signal: StrategySignal, trade: Trade
    ) -> None:
        """Phase 2B Branch 1: single stop+TP exchange-resting orders (unchanged)."""
        protective = await manager.attach_protective_orders(signal, trade.id)
        extra = dict(trade.extra or {})
        extra["protective_orders_attached_ms"] = round(protective.elapsed_ms, 1)
        if protective.success:
            extra["exchange_resting_active"] = True
            extra["stop_order_id"] = protective.stop_order_id
            extra["take_profit_order_id"] = protective.take_profit_order_id
        else:
            extra["exchange_resting_active"] = False
            extra["protective_orders_failed"] = "; ".join(protective.reasons) or "unknown"
            await self._risk_event(
                db, "warning", "protective_orders_failed",
                "; ".join(protective.reasons) or "attach_protective_orders failed",
                {"trade_id": trade.id, "symbol": signal.symbol,
                 "stop_order_id": protective.stop_order_id,
                 "take_profit_order_id": protective.take_profit_order_id},
            )
        trade.extra = extra
        await self._record_protective_attach_outcome(
            db, protective.success,
            context={"trade_id": trade.id, "symbol": signal.symbol, "source": "entry_attach"},
        )

    async def _attach_ladder_exits(
        self, db: AsyncSession, manager: OrderManager,
        signal: StrategySignal, trade: Trade, scored: ScoredSignal,
    ) -> None:
        """Phase 2B Branch 2: attach the 5-tier ladder for a filled entry.

        Min-notional Option A: if the position is too small to split into tiers,
        gracefully degrade to the Branch 1 single stop+TP path (still protected,
        never naked). Circuit-breaker counts a missing STOP as the failure signal.
        """
        atr = self._entry_atr(scored)
        plan = await manager.build_ladder_plan(
            direction=signal.direction, entry_price=trade.entry_price,
            stop_loss=signal.stop_loss, atr=atr, quantity=trade.quantity, symbol=signal.symbol,
        )
        extra = dict(trade.extra or {})
        extra["entry_atr"] = round(atr, 10)

        if not plan.is_ladder:
            logger.info("ladder_min_notional_degraded", trade_id=trade.id,
                        symbol=signal.symbol, reason=plan.degraded_reason)
            await self._risk_event(
                db, "info", "ladder_min_notional_degraded",
                f"{signal.symbol} ladder degraded to single TP: {plan.degraded_reason}",
                {"trade_id": trade.id, "symbol": signal.symbol, "reason": plan.degraded_reason},
            )
            trade.extra = extra  # persist entry_atr before the Branch 1 fallback overwrites extra
            await self._attach_branch1_protective(db, manager, signal, trade)
            return

        result = await manager.attach_ladder_orders(plan, signal.symbol, signal.direction, trade.id)
        extra["protective_orders_attached_ms"] = round(result.elapsed_ms, 1)
        extra["ladder_mode"] = result.mode
        if result.ladder_active:
            extra["exchange_resting_active"] = True
            extra["ladder_active"] = True
            extra["stop_order_id"] = result.stop_order_id
            extra["tier_orders"] = [
                {"index": t.index, "order_id": t.order_id, "price": t.price,
                 "quantity": t.quantity, "filled": False}
                for t in result.tier_orders
            ]
            extra["runner_order_id"] = result.runner_order_id
            extra["runner_activation_price"] = plan.runner_activation_price
            extra["runner_callback_rate"] = plan.runner_callback_rate
            extra["tiers_filled"] = 0
            extra["be_plus_armed"] = False
            extra["runner_active"] = False
            extra["time_partial_done"] = False
            extra["original_quantity"] = trade.quantity
            extra["remaining_quantity"] = trade.quantity
            # Baseline for the tier-fill cross-check anomaly. Reset on the 15-min
            # re-ladder so the partial time-exit's own bookkeeping never trips it.
            extra["ladder_base_quantity"] = trade.quantity
            await self._risk_event(
                db, "info", "ladder_attached",
                f"{signal.symbol} {result.mode} ladder: {len(result.tier_orders)} tiers, "
                f"runner={'yes' if result.runner_order_id else 'no'}",
                {"trade_id": trade.id, "symbol": signal.symbol, "mode": result.mode,
                 "tiers": len(result.tier_orders), "stop_order_id": result.stop_order_id,
                 "runner_order_id": result.runner_order_id,
                 "runner_callback_rate": plan.runner_callback_rate},
            )
            if result.reasons:
                await self._risk_event(
                    db, "warning", "ladder_partial_attach", "; ".join(result.reasons),
                    {"trade_id": trade.id, "symbol": signal.symbol},
                )
        else:
            extra["exchange_resting_active"] = False
            extra["ladder_active"] = False
            extra["protective_orders_failed"] = "; ".join(result.reasons) or "ladder attach failed"
            await self._risk_event(
                db, "warning", "protective_orders_failed",
                "; ".join(result.reasons) or "attach_ladder_orders failed",
                {"trade_id": trade.id, "symbol": signal.symbol,
                 "stop_order_id": result.stop_order_id},
            )
        trade.extra = extra
        # The marching STOP is the safety-critical order; treat its absence as the
        # circuit-breaker failure signal (tier/runner partials are logged separately).
        await self._record_protective_attach_outcome(
            db, result.stop_order_id is not None,
            context={"trade_id": trade.id, "symbol": signal.symbol, "source": "entry_attach_ladder"},
        )

    def _use_paper_sim_wiring(self) -> bool:
        """Phase 2B Branch 1: in paper mode, route exits through the simulator
        instead of the mid-price polling logic. Eliminates the split-brain
        where the simulator's equity never moved on closes (only fees)."""
        return (
            self.settings.paper_sim_wired_to_live_loop
            and self.settings.trading_mode == TradingMode.PAPER
        )

    async def _persist_execution(
        self,
        db: AsyncSession,
        scored: ScoredSignal,
        report,
        quantity: float,
        setup_assessment,
        position_size,
        session: SessionState,
        regime: RegimeResult,
    ) -> Trade | None:
        signal = scored.signal
        trade: Trade | None = None
        if report.accepted and (report.paper_position or report.order_result):
            order_status = report.order_result.status if report.order_result else "filled"
            trade_status = "open"
            if report.order_result and report.order_result.order_type == "limit" and report.order_result.filled_quantity <= 0:
                trade_status = "pending"
            trade_data = dict(
                symbol=signal.symbol,
                side=signal.direction,
                exchange=self.settings.exchange.value,
                mode=self.settings.trading_mode.value,
                setup_name=signal.setup_name,
                entry_price=signal.entry_price,
                stop_loss=signal.stop_loss,
                take_profit={"levels": signal.take_profit_levels},
                quantity=quantity,
                status=trade_status,
                extra={
                    "signal_id": scored.signal_id,
                    "setup_score": scored.score.total,
                    "grade": setup_assessment.grade,
                    "normal_grade": scored.score.grade,
                    "risk_pct": setup_assessment.risk_pct,
                    "base_risk_pct": setup_assessment.base_risk_pct,
                    "risk_amount": position_size.risk_amount,
                    "risk_range": list(setup_assessment.risk_range),
                    "score_floor": setup_assessment.score_floor,
                    "score_ceiling": setup_assessment.score_ceiling,
                    "score_permission": setup_assessment.permission,
                    "score_session": setup_assessment.session_name,
                    "entry_session": session.name,
                    "entry_regime": regime.regime,
                    "exchange_order_id": report.order_result.order_id if report.order_result else None,
                    "order_status": order_status,
                    "original_quantity": quantity,
                    "remaining_quantity": quantity,
                    "tp1_hit": False,
                    "tp2_hit": False,
                    "break_even_moved": False,
                },
            )
            if report.paper_position:
                trade_data["id"] = report.paper_position.id
            trade = Trade(**trade_data)
            db.add(trade)
            await db.flush()

        if report.order_result:
            db.add(
                Order(
                    trade_id=trade.id if trade else None,
                    exchange_order_id=report.order_result.order_id,
                    symbol=signal.symbol,
                    side=report.order_result.side,
                    order_type=report.order_result.order_type,
                    price=signal.entry_price if report.order_result.order_type == "limit" else None,
                    quantity=report.order_result.quantity,
                    filled_quantity=report.order_result.filled_quantity,
                    status=report.order_result.status,
                    raw_response=report.order_result.raw,
                )
            )
        return trade

    async def _manage_open_trades(self, db: AsyncSession, adapter: ExchangeAdapter) -> None:
        trades = await self._open_database_trades(db)
        self.status.open_trade_count = len(trades)

        # Phase 2B: split trades across exit paths (most specific first).
        #   - ladder:    Branch 2 five-tier ladder (resting + ladder flag ON)
        #   - resting:   Branch 1 single stop+TP exchange-resting orders
        #   - paper_sim: paper simulator authoritative (paper mode + flag ON)
        #   - legacy:    original mid-price polling
        use_ladder = self._use_ladder_exits()
        use_resting = self._use_exchange_resting_exits()
        use_paper_sim = self._use_paper_sim_wiring()
        ladder_trades: list[Trade] = []
        resting_trades: list[Trade] = []
        paper_sim_trades: list[Trade] = []
        legacy_trades: list[Trade] = []
        for t in trades:
            extra = t.extra or {}
            if use_ladder and extra.get("ladder_active"):
                ladder_trades.append(t)
            elif use_resting and extra.get("exchange_resting_active"):
                resting_trades.append(t)
            elif use_paper_sim:
                paper_sim_trades.append(t)
            else:
                legacy_trades.append(t)

        if ladder_trades:
            await self._sync_ladder_trades(db, adapter, ladder_trades)
        if resting_trades:
            await self._sync_exchange_resting_trades(db, adapter, resting_trades)
        if paper_sim_trades:
            await self._sync_paper_simulator_trades(db, adapter, paper_sim_trades)

        for trade in legacy_trades:
            try:
                book = await adapter.fetch_order_book(trade.symbol, limit=20)
                price = book.mid_price
            except Exception:
                continue
            if price <= 0:
                continue
            trade.unrealized_pnl = self._unrealized_pnl(trade, price)
            self._update_mfe_mae(trade, trade.unrealized_pnl)
            await self._maybe_move_break_even(trade, price)
            close_reason = self._close_reason(trade, price)
            if close_reason:
                await self._close_trade(db, adapter, trade, price, close_reason)
                continue
            await self._maybe_partial_exit(db, adapter, trade, price)

    def _update_mfe_mae(self, trade: Trade, current_pnl: float) -> None:
        """Phase 2B Branch 1: track per-trade max favorable / adverse excursion.

        Stored in trade.extra so no schema migration. The polling loop calls
        this at most once per cycle (~10s); resolution is coarse but adequate
        for the next round of analysis. MFE >= 0, MAE <= 0 by construction.
        """
        if not self.settings.mfe_mae_logging_enabled:
            return
        extra = dict(trade.extra or {})
        prev_mfe = float(extra.get("mfe_pnl") or 0.0)
        prev_mae = float(extra.get("mae_pnl") or 0.0)
        extra["mfe_pnl"] = max(prev_mfe, float(current_pnl))
        extra["mae_pnl"] = min(prev_mae, float(current_pnl))
        extra["mfe_mae_updated_at"] = utc_now().isoformat()
        trade.extra = extra

    async def _sync_exchange_resting_trades(
        self, db: AsyncSession, adapter: ExchangeAdapter, trades: list[Trade]
    ) -> None:
        """Phase 2B Branch 1: for trades with exchange-resting protective orders,
        detect position closure on the exchange and finalize the DB Trade row.

        Sync logic:
          - position still present on the exchange -> nothing to do (still open)
          - position gone -> trade was closed by stop or TP (or external action).
            Query both order ids to attribute the close (fill_price + reason).
        """
        try:
            positions = await adapter.fetch_positions()
        except Exception as exc:
            logger.warning("sync_resting_fetch_positions_failed", error=str(exc))
            return
        open_symbols = {p.symbol for p in positions}
        positions_by_symbol = {p.symbol: p for p in positions}
        for trade in trades:
            if trade.symbol in open_symbols:
                # Still open — update unrealized PnL + MFE/MAE from the exchange's view.
                position = positions_by_symbol.get(trade.symbol)
                if position is not None:
                    trade.unrealized_pnl = float(position.unrealized_pnl)
                    self._update_mfe_mae(trade, trade.unrealized_pnl)
                continue
            extra = dict(trade.extra or {})
            stop_id = extra.get("stop_order_id")
            tp_id = extra.get("take_profit_order_id")
            fill_price, reason = await self._attribute_exchange_close(adapter, trade.symbol, stop_id, tp_id)
            if fill_price <= 0:
                try:
                    book = await adapter.fetch_order_book(trade.symbol, limit=20)
                    fill_price = book.mid_price
                except Exception:
                    fill_price = trade.entry_price
            close_reason = reason or "exchange_resting_close"
            await self._finalize_trade_close(db, trade, fill_price, close_reason)

    async def _sync_ladder_trades(
        self, db: AsyncSession, adapter: ExchangeAdapter, trades: list[Trade]
    ) -> None:
        """Phase 2B Branch 2: per-cycle management of five-tier ladder positions.

        For each ladder trade:
          - position gone from the exchange -> finalize + cancel leftover orders
          - still open -> detect tier fills, arm BE+, ratchet the marching stop,
            flag runner activation, and apply time-based exits.
        Every state transition emits an audit RiskEvent.
        """
        try:
            positions = await adapter.fetch_positions()
        except Exception as exc:
            logger.warning("sync_ladder_fetch_positions_failed", error=str(exc))
            return
        positions_by_symbol = {p.symbol: p for p in positions}
        manager = OrderManager(adapter, risk_engine=self.risk_engine, paper=self.paper, settings=self.settings)

        for trade in trades:
            extra = dict(trade.extra or {})
            position = positions_by_symbol.get(trade.symbol)
            if position is None:
                await self._finalize_ladder_close(db, adapter, manager, trade, extra)
                continue

            mark = float(position.mark_price) if (position.mark_price or 0) > 0 else 0.0
            if mark <= 0:
                try:
                    mark = (await adapter.fetch_order_book(trade.symbol, limit=20)).mid_price
                except Exception:
                    mark = trade.entry_price
            trade.unrealized_pnl = self._unrealized_pnl(trade, mark)
            self._update_mfe_mae(trade, trade.unrealized_pnl)
            finalized = await self._ladder_manage_open(db, adapter, manager, trade, extra, position, mark)
            if not finalized:
                trade.extra = extra

    async def _ladder_manage_open(
        self, db: AsyncSession, adapter: ExchangeAdapter, manager: OrderManager,
        trade: Trade, extra: dict, position: Position, mark: float,
    ) -> bool:
        """Manage an open ladder position for one cycle. Returns True if the trade
        was finalized (closed) this cycle, so the caller skips the extra write."""
        atr = float(extra.get("entry_atr") or 0.0) or abs(trade.entry_price - trade.stop_loss)

        # 1) Detect tier fills (resting TP order no longer open == filled).
        await self._ladder_detect_tier_fills(db, adapter, trade, extra, mark)
        tier_orders = extra.get("tier_orders") or []
        tiers_filled = sum(1 for t in tier_orders if t.get("filled"))
        extra["tiers_filled"] = tiers_filled
        # Keep trade.quantity synced to the exchange remainder so finalize_close
        # prices only the un-realized chunk (filled tiers already booked).
        trade.quantity = float(position.quantity)
        extra["remaining_quantity"] = float(position.quantity)

        # 2) Arm BE+ on the first favorable 0.5xATR move.
        be_plus_armed = bool(extra.get("be_plus_armed"))
        if not be_plus_armed and should_arm_be_plus(
            direction=trade.side, entry_price=trade.entry_price,
            mark_price=mark, atr=atr, settings=self.settings,
        ):
            be_plus_armed = True
            extra["be_plus_armed"] = True
            await self._risk_event(
                db, "info", "ladder_be_plus_armed",
                f"{trade.symbol} BE+ armed at mark {mark}",
                {"trade_id": trade.id, "symbol": trade.symbol, "mark": mark, "atr": atr},
            )

        # 3) Ratchet the marching stop (worst-of-progression, never above market).
        rules = await manager._symbol_rules(trade.symbol)
        advance = compute_target_stop(
            direction=trade.side, entry_price=trade.entry_price, current_stop=trade.stop_loss,
            mark_price=mark, tiers_filled=tiers_filled, be_plus_armed=be_plus_armed,
            rules=rules, settings=self.settings,
        )
        if advance.new_stop_price is not None:
            old_stop_id = extra.get("stop_order_id")
            new_id, issues = await manager.advance_ladder_stop(
                trade.symbol, trade.side, advance.new_stop_price, old_stop_id, trade.id,
            )
            if new_id:
                extra["stop_order_id"] = new_id
                trade.stop_loss = advance.new_stop_price
                await self._risk_event(
                    db, "info", "ladder_stop_advanced",
                    f"{trade.symbol} stop -> {advance.new_stop_price} (offset {advance.offset_pct:.4f})",
                    {"trade_id": trade.id, "symbol": trade.symbol, "old_stop_order_id": old_stop_id,
                     "new_stop_order_id": new_id, "new_stop_price": advance.new_stop_price,
                     "tiers_filled": tiers_filled, "be_plus_armed": be_plus_armed},
                )
            if issues:
                logger.warning("ladder_stop_cancel_issues", symbol=trade.symbol, issues=issues)
        elif advance.deferred:
            logger.debug("ladder_stop_deferred", symbol=trade.symbol, reason=advance.reason)

        # 4) Runner activation milestone (the order auto-activates via activationPrice).
        if not extra.get("runner_active") and tier_orders and tiers_filled >= len(tier_orders):
            extra["runner_active"] = True
            await self._risk_event(
                db, "info", "ladder_runner_active",
                f"{trade.symbol} all tiers filled; trailing runner active",
                {"trade_id": trade.id, "symbol": trade.symbol, "tiers_filled": tiers_filled},
            )

        # 5) Time-based exits.
        opened = trade.opened_at
        elapsed = (datetime.now(timezone.utc) - opened).total_seconds() if opened else 0.0
        decision = time_exit_decision(elapsed, bool(extra.get("time_partial_done")), self.settings)
        if decision == "full":
            await self._ladder_time_full_exit(db, adapter, manager, trade, extra, position, mark)
            return True
        if decision == "partial":
            await self._ladder_time_partial_exit(db, adapter, manager, trade, extra, position, mark, atr)
        return False

    async def _ladder_detect_tier_fills(
        self, db: AsyncSession, adapter: ExchangeAdapter, trade: Trade, extra: dict, mark: float,
    ) -> None:
        """Detect newly-filled TP tiers, book their realized PnL, and log slippage
        anomalies (actual fill vs the tier trigger price)."""
        tier_orders = extra.get("tier_orders") or []
        unfilled = [t for t in tier_orders if not t.get("filled") and t.get("order_id")]
        if not unfilled:
            return
        try:
            open_ids = {o.order_id for o in await adapter.fetch_open_algo_orders(trade.symbol)}
        except Exception as exc:
            logger.warning("sync_ladder_fetch_orders_failed", symbol=trade.symbol, error=str(exc))
            return
        # Cross-check baseline: the entry quantity of the *current* ladder (reset on
        # the 15-min re-ladder). Running tally of filled tier quantity, seeded with
        # tiers already booked in prior cycles.
        base_qty = float(extra.get("ladder_base_quantity") or extra.get("original_quantity") or trade.quantity)
        filled_qty = sum(float(t["quantity"]) for t in tier_orders if t.get("filled"))
        newly_filled: list[tuple[dict, str]] = []  # (tier, fetch_order_status)
        for tier in unfilled:
            if tier["order_id"] in open_ids:
                continue  # still resting
            # No longer on the book -> treat as filled. Attribute the actual fill
            # price when the adapter can report it; otherwise use the tier trigger.
            fill_price = float(tier["price"])
            order_status = "unknown"
            try:
                order = await adapter.fetch_algo_order(trade.symbol, tier["order_id"])
                order_status = order.status.lower()
                if order_status in ("filled", "triggered") and (order.average_price or 0) > 0:
                    fill_price = float(order.average_price)
            except NotImplementedError:
                order_status = "fetch_algo_order_unsupported"
            except Exception as exc:
                logger.debug("ladder_tier_fetch_failed", symbol=trade.symbol, error=str(exc))
            tier["filled"] = True
            tier["fill_price"] = fill_price
            filled_qty += float(tier["quantity"])
            newly_filled.append((tier, order_status))
            pnl = self._exit_pnl(trade, fill_price, float(tier["quantity"]))
            trade.realized_pnl = float(trade.realized_pnl or 0.0) + pnl
            await self._risk_event(
                db, "info", "ladder_tier_filled",
                f"{trade.symbol} TP{tier['index']} filled at {fill_price}",
                {"trade_id": trade.id, "symbol": trade.symbol, "tier": tier["index"],
                 "trigger_price": tier["price"], "fill_price": fill_price,
                 "quantity": tier["quantity"], "pnl": round(pnl, 6)},
            )
            slippage_bps = abs(fill_price - float(tier["price"])) / max(float(tier["price"]), 1e-12) * 10_000
            if slippage_bps > self.settings.slippage_anomaly_bps:
                await self._risk_event(
                    db, "warning", "ladder_slippage_anomaly",
                    f"{trade.symbol} TP{tier['index']} slippage {slippage_bps:.1f} bps",
                    {"trade_id": trade.id, "symbol": trade.symbol, "tier": tier["index"],
                     "trigger_price": tier["price"], "fill_price": fill_price,
                     "slippage_bps": round(slippage_bps, 1),
                     "threshold_bps": self.settings.slippage_anomaly_bps},
                )
        extra["tier_orders"] = tier_orders
        # Observational cross-check: run ONCE per cycle after all this cycle's fills
        # are booked. The exchange position reflects fills cumulatively, so checking
        # per-tier would false-positive whenever >1 tier fills in a single poll.
        if newly_filled:
            await self._ladder_sync_crosscheck(db, adapter, trade, newly_filled, base_qty, filled_qty)

    async def _ladder_sync_crosscheck(
        self, db: AsyncSession, adapter: ExchangeAdapter, trade: Trade,
        newly_filled: list[tuple[dict, str]], base_qty: float, filled_qty_to_date: float,
    ) -> None:
        """Observational tripwire for the cancel-vs-fill / no-cross-check tradeoffs.

        After this cycle's tier fills are booked, compare the live exchange position
        quantity against the filled-tier accounting. If they diverge by more than 5%
        of the ladder's base quantity, emit a `ladder_sync_anomaly` RiskEvent. This
        NEVER aborts detection — it is purely empirical evidence of whether the
        detection heuristic ever misfires in practice (target: zero anomalies over
        50+ trades). The extra fetch_positions call only happens on an actual fill.
        """
        if base_qty <= 0:
            return
        try:
            positions = await adapter.fetch_positions()
            exchange_qty = next((float(p.quantity) for p in positions if p.symbol == trade.symbol), 0.0)
        except Exception as exc:
            logger.debug("ladder_crosscheck_fetch_failed", symbol=trade.symbol, error=str(exc))
            return  # could not read position this cycle; observational only
        expected_remaining = base_qty - filled_qty_to_date
        drift = abs(exchange_qty - expected_remaining) / base_qty
        if drift > 0.05:
            tiers = [t["index"] for t, _ in newly_filled]
            statuses = {t["index"]: status for t, status in newly_filled}
            await self._risk_event(
                db, "warning", "ladder_sync_anomaly",
                f"{trade.symbol} tiers {tiers} sync drift {drift * 100:.1f}%: "
                f"exchange_qty={exchange_qty} expected_remaining={expected_remaining}",
                {"trade_id": trade.id, "symbol": trade.symbol, "tiers": tiers,
                 "exchange_quantity": exchange_qty, "expected_remaining": expected_remaining,
                 "fetch_order_status": statuses,
                 "filled_tier_quantities": round(filled_qty_to_date, 10),
                 "ladder_base_quantity": base_qty,
                 "drift_pct": round(drift * 100, 2),
                 "timestamp": utc_now().isoformat()},
            )

    async def _finalize_ladder_close(
        self, db: AsyncSession, adapter: ExchangeAdapter, manager: OrderManager,
        trade: Trade, extra: dict,
    ) -> None:
        """Position gone from the exchange: attribute the close, cancel any leftover
        ladder orders, finalize the DB row, and emit the 150-count audit event."""
        stop_id = extra.get("stop_order_id")
        runner_id = extra.get("runner_order_id")
        unfilled_tier_ids = [t.get("order_id") for t in (extra.get("tier_orders") or []) if not t.get("filled")]

        fill_price, reason = await self._attribute_exchange_close(adapter, trade.symbol, stop_id, runner_id)
        if reason == "take_profit_exchange":
            reason = "trailing_runner_exchange"
        if fill_price <= 0:
            try:
                fill_price = (await adapter.fetch_order_book(trade.symbol, limit=20)).mid_price
            except Exception:
                fill_price = trade.entry_price
        close_reason = reason or "ladder_resting_close"

        issues = await manager.cancel_orders(trade.symbol, [stop_id, runner_id, *unfilled_tier_ids])
        if issues:
            logger.info("ladder_close_cancel_issues", symbol=trade.symbol, issues=issues)

        extra["ladder_active"] = False
        trade.extra = extra
        await self._finalize_trade_close(db, trade, fill_price, close_reason)
        await self._ladder_record_closed(db, trade, close_reason)

    async def _ladder_time_partial_exit(
        self, db: AsyncSession, adapter: ExchangeAdapter, manager: OrderManager,
        trade: Trade, extra: dict, position: Position, mark: float, atr: float,
    ) -> None:
        """15-minute partial: cancel resting tiers + runner, market-close a fraction
        of the remainder, then re-place the ladder for the new remaining quantity.
        The marching stop is left intact (closePosition auto-sizes)."""
        remaining = float(position.quantity)
        if remaining <= 0:
            extra["time_partial_done"] = True
            return
        tier_orders = extra.get("tier_orders") or []
        cancel_ids = [t.get("order_id") for t in tier_orders if not t.get("filled")]
        cancel_ids.append(extra.get("runner_order_id"))
        await manager.cancel_orders(trade.symbol, cancel_ids)

        close_qty = remaining * self.settings.time_exit_partial_pct
        await self._send_reduce_only(adapter, trade, close_qty)
        pnl = self._exit_pnl(trade, mark, close_qty)
        trade.realized_pnl = float(trade.realized_pnl or 0.0) + pnl
        new_remaining = max(0.0, remaining - close_qty)
        trade.quantity = new_remaining

        plan = await manager.build_ladder_plan(
            direction=trade.side, entry_price=trade.entry_price, stop_loss=trade.stop_loss,
            atr=atr, quantity=new_remaining, symbol=trade.symbol,
        )
        re_laddered = plan.is_ladder
        if re_laddered:
            tiers, runner_id, reasons = await manager.replace_ladder_tiers(
                plan, trade.symbol, trade.side, trade.id,
            )
            extra["tier_orders"] = [
                {"index": t.index, "order_id": t.order_id, "price": t.price,
                 "quantity": t.quantity, "filled": False}
                for t in tiers
            ]
            extra["runner_order_id"] = runner_id
            extra["runner_activation_price"] = plan.runner_activation_price
            extra["runner_callback_rate"] = plan.runner_callback_rate
            extra["tiers_filled"] = 0
            extra["runner_active"] = False
        else:
            # Remainder too small to re-ladder; the marching stop alone protects it.
            extra["tier_orders"] = []
            extra["runner_order_id"] = None
        extra["time_partial_done"] = True
        extra["remaining_quantity"] = new_remaining
        # Re-baseline the cross-check against the reshaped position.
        extra["ladder_base_quantity"] = new_remaining
        await self._risk_event(
            db, "info", "ladder_time_partial",
            f"{trade.symbol} 15-min partial: closed {close_qty}, remaining {new_remaining}",
            {"trade_id": trade.id, "symbol": trade.symbol, "closed_qty": close_qty,
             "remaining": new_remaining, "pnl": round(pnl, 6), "re_laddered": re_laddered},
        )

    async def _ladder_time_full_exit(
        self, db: AsyncSession, adapter: ExchangeAdapter, manager: OrderManager,
        trade: Trade, extra: dict, position: Position, mark: float,
    ) -> None:
        """45-minute hard exit: cancel ALL remaining ladder orders and market-close
        the whole remaining position."""
        tier_ids = [t.get("order_id") for t in (extra.get("tier_orders") or []) if not t.get("filled")]
        await manager.cancel_orders(
            trade.symbol, [extra.get("stop_order_id"), extra.get("runner_order_id"), *tier_ids],
        )
        await self._send_reduce_only(adapter, trade, float(position.quantity))
        trade.quantity = float(position.quantity)
        extra["ladder_active"] = False
        trade.extra = extra
        await self._finalize_trade_close(db, trade, mark, "time_exit_full")
        await self._risk_event(
            db, "info", "ladder_time_full",
            f"{trade.symbol} 45-min full time exit",
            {"trade_id": trade.id, "symbol": trade.symbol, "mark": mark},
        )
        await self._ladder_record_closed(db, trade, "time_exit_full")

    async def _ladder_record_closed(self, db: AsyncSession, trade: Trade, reason: str) -> None:
        """Emit the audit event that operationalizes the 150-trade success metric:
        one fully-closed ladder position == one event with counts_toward_150=True."""
        await self._risk_event(
            db, "info", "ladder_trade_closed",
            f"{trade.symbol} ladder position fully closed: {reason}",
            {"trade_id": trade.id, "symbol": trade.symbol, "reason": reason,
             "realized_pnl": round(float(trade.realized_pnl or 0.0), 6),
             "ladder_mode": (trade.extra or {}).get("ladder_mode"),
             "counts_toward_150": True},
        )

    async def _sync_paper_simulator_trades(
        self, db: AsyncSession, adapter: ExchangeAdapter, trades: list[Trade]
    ) -> None:
        """Phase 2B Branch 1: paper-mode exits driven by PaperTradingSimulator.

        For each trade, fetch the current mid price, call paper.update_price,
        and translate any returned PaperFills into DB Trade row updates.
        Trade.id == PaperPosition.id (set in _persist_execution) so the
        mapping is direct. Eliminates the split-brain where the simulator's
        equity tracked only fees, not realized PnL.
        """
        for trade in trades:
            try:
                book = await adapter.fetch_order_book(trade.symbol, limit=20)
                price = book.mid_price
            except Exception:
                continue
            if price <= 0:
                continue
            trade.unrealized_pnl = self._unrealized_pnl(trade, price)
            self._update_mfe_mae(trade, trade.unrealized_pnl)

            fills = self.paper.update_price(trade.symbol, price)
            for fill in fills:
                if fill.trade_id != trade.id:
                    continue
                extra = dict(trade.extra or {})
                trade.realized_pnl = float(trade.realized_pnl or 0.0) + float(fill.pnl)
                trade.fees = float(trade.fees or 0.0) + float(fill.fee)
                if fill.reason == "stop_loss":
                    trade.quantity = 0.0
                    trade.unrealized_pnl = 0.0
                    trade.status = "closed"
                    trade.closed_at = utc_now()
                    extra["close_reason"] = "stop_loss"
                    extra["remaining_quantity"] = 0.0
                    trade.extra = extra
                    self._record_closed_trade_outcome(trade)
                elif fill.reason == "trailing_runner_target":
                    trade.quantity = 0.0
                    trade.unrealized_pnl = 0.0
                    trade.status = "closed"
                    trade.closed_at = utc_now()
                    extra["close_reason"] = "final_take_profit"
                    extra["remaining_quantity"] = 0.0
                    trade.extra = extra
                    self._record_closed_trade_outcome(trade)
                elif fill.reason in ("tp1", "tp2"):
                    trade.quantity = max(0.0, float(trade.quantity) - float(fill.quantity))
                    extra[f"{fill.reason}_hit"] = True
                    extra["remaining_quantity"] = trade.quantity
                    trade.extra = extra

    async def _attribute_exchange_close(
        self, adapter: ExchangeAdapter, symbol: str, stop_id: str | None, tp_id: str | None
    ) -> tuple[float, str]:
        """Query stop and TP orders; return (fill_price, close_reason).

        Returns (0.0, '') if neither order can be attributed (caller falls back
        to mark price + a generic close reason).
        """
        for order_id, reason in ((stop_id, "stop_loss_exchange"), (tp_id, "take_profit_exchange")):
            if not order_id:
                continue
            try:
                order = await adapter.fetch_algo_order(symbol, order_id)
            except NotImplementedError:
                return 0.0, ""
            except Exception:
                continue
            if order.status.lower() in ("filled", "triggered") and (order.average_price or 0) > 0:
                return float(order.average_price), reason
        return 0.0, ""

    async def _reconcile_pending_and_orphan_positions(self, db: AsyncSession, positions, open_orders) -> None:
        if self.settings.trading_mode == TradingMode.PAPER:
            return
        pending_result = await db.execute(select(Trade).where(Trade.status == "pending"))
        pending_trades = list(pending_result.scalars().all())
        open_symbols = {trade.symbol for trade in await self._open_database_trades(db)}
        pending_symbols = {trade.symbol for trade in pending_trades}
        order_symbols = {order.symbol for order in open_orders}

        for trade in pending_trades:
            position = next((item for item in positions if item.symbol == trade.symbol), None)
            if position:
                trade.status = "open"
                trade.entry_price = position.entry_price or trade.entry_price
                trade.quantity = position.quantity
                trade.extra = {
                    **(trade.extra or {}),
                    "reconciled_from_exchange": True,
                    "remaining_quantity": position.quantity,
                }
                open_symbols.add(trade.symbol)
            elif trade.symbol not in order_symbols:
                trade.status = "canceled"
                trade.closed_at = utc_now()
                trade.extra = {**(trade.extra or {}), "close_reason": "order_not_open_on_exchange"}

        for position in positions:
            if position.symbol in open_symbols or position.symbol in pending_symbols:
                continue
            # Phase 2B Branch 1: stop/TP defaults moved to settings (clarification A).
            stop_pct = self.settings.orphan_reconcile_stop_pct
            target_pct = list(self.settings.orphan_reconcile_tp_levels)
            if position.side == "long":
                stop_loss = position.entry_price * (1 - stop_pct)
                take_profit = [position.entry_price * (1 + pct) for pct in target_pct]
            else:
                stop_loss = position.entry_price * (1 + stop_pct)
                take_profit = [position.entry_price * (1 - pct) for pct in target_pct]
            take_profit_rounded = [round(price, 8) for price in take_profit]
            orphan_trade = Trade(
                symbol=position.symbol,
                side=position.side,
                exchange=self.settings.exchange.value,
                mode=self.settings.trading_mode.value,
                setup_name="Exchange reconciled position",
                entry_price=position.entry_price,
                stop_loss=round(stop_loss, 8),
                take_profit={"levels": take_profit_rounded},
                quantity=position.quantity,
                status="open",
                unrealized_pnl=position.unrealized_pnl,
                extra={
                    "reconciled_orphan_position": True,
                    "entry_session": "unknown",
                    "entry_regime": "unknown",
                    "original_quantity": position.quantity,
                    "remaining_quantity": position.quantity,
                    "tp1_hit": False,
                    "tp2_hit": False,
                    "break_even_moved": False,
                },
            )
            db.add(orphan_trade)
            await db.flush()

            # Per clarification A: when exchange-resting exits are ON, orphans
            # also get protective orders attached (so they participate in the
            # same exit path as regular entries).
            if self._use_exchange_resting_exits():
                from app.execution.order_manager import OrderManager
                from app.strategies.base_strategy import StrategySignal
                adapter_for_attach = create_exchange_adapter(self.settings)
                manager = OrderManager(
                    adapter_for_attach, risk_engine=self.risk_engine,
                    paper=self.paper, settings=self.settings,
                )
                synthetic_signal = StrategySignal(
                    setup_name="Exchange reconciled position",
                    symbol=position.symbol,
                    direction=position.side,
                    entry_price=position.entry_price,
                    stop_loss=round(stop_loss, 8),
                    take_profit_levels=take_profit_rounded,
                    trailing_stop=0.0,
                    expected_move=0.0,
                    risk_reward_ratio=0.0,
                    confidence_score=0.0,
                    accepted=True,
                )
                protective = await manager.attach_protective_orders(synthetic_signal, orphan_trade.id)
                extra = dict(orphan_trade.extra or {})
                extra["protective_orders_attached_ms"] = round(protective.elapsed_ms, 1)
                if protective.success:
                    extra["exchange_resting_active"] = True
                    extra["stop_order_id"] = protective.stop_order_id
                    extra["take_profit_order_id"] = protective.take_profit_order_id
                else:
                    extra["exchange_resting_active"] = False
                    extra["protective_orders_failed"] = "; ".join(protective.reasons) or "unknown"
                orphan_trade.extra = extra
                await self._record_protective_attach_outcome(
                    db, protective.success,
                    context={"trade_id": orphan_trade.id, "symbol": position.symbol, "source": "orphan_reconcile"},
                )

            await self._risk_event(
                db,
                "warning",
                "exchange_position_reconciled",
                "Exchange position had no local trade record; attached protective management",
                {"symbol": position.symbol, "side": position.side, "quantity": position.quantity},
            )

    async def _maybe_partial_exit(self, db: AsyncSession, adapter: ExchangeAdapter, trade: Trade, price: float) -> None:
        levels = self._take_profit_levels(trade)
        if len(levels) < 2:
            return
        extra = dict(trade.extra or {})
        original_quantity = float(extra.get("original_quantity") or trade.quantity)
        if not extra.get("tp1_hit") and self._target_hit(trade.side, price, levels[0]):
            quantity = min(trade.quantity, original_quantity * 0.4)
            await self._reduce_trade(db, adapter, trade, price, quantity, "tp1")
            extra["tp1_hit"] = True
        if not extra.get("tp2_hit") and self._target_hit(trade.side, price, levels[1]):
            quantity = min(trade.quantity, original_quantity * 0.3)
            await self._reduce_trade(db, adapter, trade, price, quantity, "tp2")
            extra["tp2_hit"] = True
        extra["remaining_quantity"] = trade.quantity
        trade.extra = extra

    async def _reduce_trade(
        self,
        db: AsyncSession,
        adapter: ExchangeAdapter,
        trade: Trade,
        price: float,
        quantity: float,
        reason: str,
    ) -> None:
        if quantity <= 0:
            return
        await self._send_reduce_only(adapter, trade, quantity)
        pnl = self._exit_pnl(trade, price, quantity)
        trade.quantity = max(0.0, trade.quantity - quantity)
        trade.realized_pnl += pnl
        db.add(
            RiskEvent(
                severity="info",
                event_type="partial_exit",
                message=f"{trade.symbol} {reason} partial exit",
                payload={"trade_id": trade.id, "quantity": quantity, "pnl": pnl},
            )
        )
        await self.alerts.send(
            "trade_partially_closed",
            f"{trade.symbol} partial close at {reason} {self._trade_assessment_label(trade)}",
        )

    async def _close_trade(
        self,
        db: AsyncSession,
        adapter: ExchangeAdapter,
        trade: Trade,
        price: float,
        reason: str,
    ) -> None:
        await self._send_reduce_only(adapter, trade, trade.quantity)
        await self._finalize_trade_close(db, trade, price, reason)

    async def _finalize_trade_close(
        self,
        db: AsyncSession,
        trade: Trade,
        price: float,
        reason: str,
        *,
        send_alert: bool = True,
    ) -> None:
        trade.realized_pnl += self._exit_pnl(trade, price, trade.quantity)
        trade.unrealized_pnl = 0.0
        trade.status = "closed"
        trade.closed_at = utc_now()
        trade.extra = {**(trade.extra or {}), "close_reason": reason}
        self._record_closed_trade_outcome(trade)
        if not send_alert:
            return
        alert_type = "stop_loss_hit" if reason == "stop_loss" else "take_profit_hit"
        label = self._trade_assessment_label(trade)
        await self.alerts.send(alert_type, f"{trade.symbol} closed by {reason} {label}")
        await self.alerts.send("trade_closed", f"{trade.symbol} trade closed: {reason} {label}")

    def _record_closed_trade_outcome(self, trade: Trade) -> None:
        if not trade_counts_for_loss_streak(trade):
            return
        pnl = float(trade.realized_pnl or 0.0)
        if abs(pnl) < self.settings.loss_streak_min_abs_pnl:
            return
        if pnl < 0:
            self._consecutive_losses += 1
            self._recent_consecutive_losses += 1
        elif pnl > 0:
            self._consecutive_losses = 0
            self._recent_consecutive_losses = 0
        self._update_loss_status()

    async def _send_reduce_only(self, adapter: ExchangeAdapter, trade: Trade, quantity: float) -> None:
        if self.settings.trading_mode == TradingMode.PAPER:
            return
        side = "sell" if trade.side == "long" else "buy"
        with suppress(Exception):
            await adapter.place_order(
                OrderRequest(
                    symbol=trade.symbol,
                    side=side,  # type: ignore[arg-type]
                    order_type="market",
                    quantity=quantity,
                    reduce_only=True,
                )
            )

    async def _cycle_wait(self, db: AsyncSession, message: str) -> dict[str, object]:
        await db.commit()
        self.status.last_order_count = 0
        self.status.last_cycle_message = message
        self._remember(message)
        return {"status": "waiting", "message": message}

    async def _load_watchlist(self, db: AsyncSession) -> list[CoinCandidate]:
        latest_result = await db.execute(select(func.max(CoinUniverse.scan_date)))
        latest = latest_result.scalar_one_or_none()
        if not latest:
            return []
        result = await db.execute(
            select(CoinUniverse)
            .where(CoinUniverse.scan_date == latest)
            .order_by(CoinUniverse.rank)
            .limit(self.settings.top_coin_limit)
        )
        rows = result.scalars().all()
        self.status.last_scan_count = len(rows)
        return [
            CoinCandidate(
                symbol=row.symbol,
                rank=row.rank,
                score=row.score,
                quote_volume=row.quote_volume,
                spread_bps=row.spread_bps,
                volatility_pct=row.volatility_pct,
                liquidity_score=row.liquidity_score,
                market_cap_rank=None,
                approved=row.approved,
                reasons=row.reasons,
            )
            for row in rows
        ]

    async def _detect_regime(self, adapter: ExchangeAdapter, candidates: list[CoinCandidate]) -> RegimeResult:
        try:
            btc_candles, eth_candles = await asyncio.gather(
                adapter.fetch_ohlcv("BTCUSDT", "15m", limit=120),
                adapter.fetch_ohlcv("ETHUSDT", "15m", limit=120),
            )
            btc = build_indicator_snapshot(btc_candles)
            eth = build_indicator_snapshot(eth_candles)
            average_spread = (
                sum(candidate.spread_bps for candidate in candidates[:10]) / max(1, len(candidates[:10]))
            )
            moving_ratio = (
                sum(1 for candidate in candidates[:20] if candidate.volatility_pct >= self.settings.min_volatility_pct)
                / max(1, len(candidates[:20]))
            )
            data = RegimeInput(
                btc=btc,
                eth=eth,
                average_spread_bps=average_spread,
                moving_coin_ratio=moving_ratio,
                abnormal_wick_ratio=self._abnormal_wick_ratio(btc_candles[-30:] + eth_candles[-30:]),
                correlation_risk=0.35,
            )
            return MarketRegimeDetector().classify(data)
        except Exception as exc:
            logger.warning("regime_detection_failed", error=str(exc))
            return RegimeResult("unclear", 45, True, 8, [f"regime detector fallback: {exc}"])

    async def _persist_regime(self, db: AsyncSession, regime: RegimeResult) -> None:
        db.add(
            MarketRegime(
                regime=regime.regime,
                tradable=regime.tradable,
                score=regime.score,
                reasons=regime.reasons,
                inputs={"source": "autonomous_loop"},
            )
        )

    def _build_strategy_context(
        self,
        candidate: CoinCandidate,
        bundle: MarketDataBundle,
        session: SessionState,
        regime: RegimeResult,
        btc_direction: Direction | None,
        eth_direction: Direction | None,
    ) -> StrategyContext:
        five_minute = bundle.candles_by_timeframe.get("5m", [])
        fifteen_minute = bundle.candles_by_timeframe.get("15m", [])
        asian_high, asian_low = self._session_range(fifteen_minute, time(0, 0), time(4, 0))
        intraday_high = max((candle.high for candle in five_minute[-30:]), default=None)
        intraday_low = min((candle.low for candle in five_minute[-30:]), default=None)
        book = bundle.order_book
        return StrategyContext(
            symbol=candidate.symbol,
            candles_by_timeframe=bundle.candles_by_timeframe,
            session_name=session.name,
            regime=regime.regime,
            coin_strength_score=candidate.score,
            btc_direction=btc_direction,
            eth_direction=eth_direction,
            asian_high=asian_high,
            asian_low=asian_low,
            intraday_high=intraday_high,
            intraday_low=intraday_low,
            spread_bps=book.spread_bps if book else candidate.spread_bps,
            order_book_imbalance=book.imbalance() if book else 0.0,
            allow_short=self.settings.market_type == "futures",
        )

    def _score_signal(
        self,
        signal: StrategySignal,
        context: StrategyContext,
        candidate: CoinCandidate,
        regime: RegimeResult,
        session: SessionState,
        bundle: MarketDataBundle,
    ) -> SetupScoreResult:
        snapshot = bundle.indicators_by_timeframe.get("5m") or bundle.indicators_by_timeframe.get("3m")
        volume_score = min(100.0, (snapshot.relative_volume * 65) if snapshot else signal.confidence_score)
        trend_score = max(snapshot.trend_strength_score if snapshot else 0.0, signal.confidence_score)
        rr_score = min(100.0, signal.risk_reward_ratio / max(self.settings.min_risk_reward, 0.1) * 70)
        leader_state = self._leader_state(context, signal.direction)
        leader_score = {"aligned": 100.0, "neutral": 70.0, "conflict": 15.0}[leader_state]
        spread_score = max(0.0, 100.0 - context.spread_bps * 8)
        session_score = (
            100.0
            if session.aggression_mode
            else self.settings.off_session_timing_score
            if session.name == "off_session"
            else 82.0
            if session.tradable
            else 0.0
        )
        return self.scoring.score(
            SetupScoreInput(
                market_regime_score=regime.score,
                session_timing_score=session_score,
                coin_strength_score=candidate.score,
                volume_confirmation_score=volume_score,
                trend_alignment_score=trend_score,
                liquidity_orderbook_score=candidate.liquidity_score,
                risk_reward_score=rr_score,
                btc_eth_confirmation_score=leader_score,
                spread_slippage_score=spread_score,
            )
        )

    def _minimum_score_for_session(self, session: SessionState) -> int:
        return self.risk_engine.minimum_score_for_session(session.name)

    async def _persist_signal(self, db: AsyncSession, signal: StrategySignal) -> str:
        row = Signal(
            symbol=signal.symbol,
            setup_name=signal.setup_name,
            direction=signal.direction,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            take_profit={"levels": signal.take_profit_levels},
            confidence_score=signal.confidence_score,
            accepted=signal.accepted,
            reasons_for_entry=signal.reasons_for_entry,
            rejection_reasons=signal.rejection_reasons,
        )
        db.add(row)
        await db.flush()
        return row.id

    async def _persist_score(
        self,
        db: AsyncSession,
        signal_id: str,
        signal: StrategySignal,
        score: SetupScoreResult,
    ) -> None:
        db.add(
            SetupScore(
                signal_id=signal_id,
                symbol=signal.symbol,
                total_score=score.total,
                grade=score.grade,
                breakdown=score.breakdown,
            )
        )

    async def _risk_event(
        self,
        db: AsyncSession,
        severity: str,
        event_type: str,
        message: str,
        payload: dict,
    ) -> None:
        db.add(RiskEvent(severity=severity, event_type=event_type, message=_safe_error_message(message), payload=payload))

    async def _open_database_trades(self, db: AsyncSession) -> list[Trade]:
        result = await db.execute(select(Trade).where(Trade.status == "open").order_by(desc(Trade.opened_at)))
        return list(result.scalars().all())

    async def _active_database_trades(self, db: AsyncSession) -> list[Trade]:
        result = await db.execute(
            select(Trade).where(Trade.status.in_(["open", "pending"])).order_by(desc(Trade.opened_at))
        )
        return list(result.scalars().all())

    async def _mark_open_trades_closed(self, db: AsyncSession, reason: str) -> None:
        for trade in await self._open_database_trades(db):
            trade.status = "closed"
            trade.closed_at = utc_now()
            trade.extra = {**(trade.extra or {}), "close_reason": reason}
        await db.commit()

    async def _safe_fetch_positions(self, adapter: ExchangeAdapter) -> list[Position]:
        if self.settings.trading_mode == TradingMode.PAPER:
            return []
        try:
            return await adapter.fetch_positions()
        except Exception as exc:
            logger.warning("fetch_positions_failed", error=str(exc))
            return []

    async def _safe_fetch_open_orders(self, adapter: ExchangeAdapter):
        if self.settings.trading_mode == TradingMode.PAPER:
            return []
        try:
            return await adapter.fetch_open_orders()
        except Exception as exc:
            logger.warning("fetch_open_orders_failed", error=str(exc))
            return []

    async def _best_effort_mark_prices(
        self,
        adapter: ExchangeAdapter,
        trades: list[Trade],
        exchange_positions: list[Position],
    ) -> dict[str, float]:
        prices = {
            position.symbol: position.mark_price or position.entry_price
            for position in exchange_positions
            if (position.mark_price or position.entry_price) > 0
        }
        for trade in trades:
            if trade.symbol in prices:
                continue
            try:
                book = await adapter.fetch_order_book(trade.symbol, limit=20)
                if book.mid_price > 0:
                    prices[trade.symbol] = book.mid_price
                    continue
            except Exception:
                pass
            prices[trade.symbol] = trade.entry_price
        return prices

    async def _account_equity(self, adapter: ExchangeAdapter) -> float:
        if self.settings.trading_mode == TradingMode.PAPER:
            return self.paper.equity
        try:
            account_balance, _ = await account_balance_basis(self.settings, adapter)
            return account_balance
        except Exception as exc:
            logger.warning("account_equity_fallback", error=str(exc))
        return self.settings.paper_starting_equity

    async def _leader_directions(self, adapter: ExchangeAdapter) -> tuple[Direction | None, Direction | None]:
        service = MarketDataService(adapter)
        btc, eth = await asyncio.gather(
            service.leader_direction("BTCUSDT"),
            service.leader_direction("ETHUSDT"),
            return_exceptions=True,
        )
        return (
            btc if btc in {"long", "short"} else None,
            eth if eth in {"long", "short"} else None,
        )

    def _exposure_positions(
        self,
        open_trades: list[Trade],
        exchange_positions: list[Position],
        session_name: str,
    ) -> list[ExposurePosition]:
        trades_by_symbol = {trade.symbol: trade for trade in open_trades}
        exposures_by_symbol = {}
        for trade in open_trades:
            exposures_by_symbol[trade.symbol] = ExposurePosition(
                symbol=trade.symbol,
                side=trade.side,
                notional=trade.quantity * trade.entry_price,
                session=self._trade_entry_session(trade),
                open_risk=self._trade_open_risk(trade),
                source="database",
            )

        for position in exchange_positions:
            trade = trades_by_symbol.get(position.symbol)
            if trade:
                exposures_by_symbol[position.symbol] = ExposurePosition(
                    symbol=position.symbol,
                    side=position.side,
                    notional=position.quantity * position.mark_price,
                    session=self._trade_entry_session(trade),
                    open_risk=self._position_open_risk(trade.entry_price, trade.stop_loss, position.quantity),
                    source="database+exchange",
                )
                continue
            exposures_by_symbol[position.symbol] = ExposurePosition(
                symbol=position.symbol,
                side=position.side,
                notional=position.quantity * position.mark_price,
                session=session_name,
                open_risk=self._exchange_only_open_risk(position),
                source="exchange_estimated_risk",
            )
        return list(exposures_by_symbol.values())

    @staticmethod
    def _trade_entry_session(trade: Trade) -> str:
        extra = trade.extra or {}
        return str(extra.get("entry_session") or "unknown")

    @staticmethod
    def _trade_open_risk(trade: Trade) -> float:
        return BotRunner._position_open_risk(trade.entry_price, trade.stop_loss, trade.quantity)

    @staticmethod
    def _position_open_risk(entry_price: float, stop_loss: float, quantity: float) -> float:
        return max(0.0, abs(entry_price - stop_loss) * max(0.0, quantity))

    @staticmethod
    def _exchange_only_open_risk(position: Position) -> float:
        # Without a local stop, assume a conservative 1% adverse move for exposure gating.
        return max(0.0, position.quantity * position.mark_price * 0.01)

    @staticmethod
    def _active_symbol_count(open_trades: list[Trade], exchange_positions: list[Position]) -> int:
        return len({trade.symbol for trade in open_trades} | {position.symbol for position in exchange_positions})

    def _loss_streak_since(self, session: SessionState, day_start: datetime) -> datetime:
        if self.settings.consecutive_loss_stop_scope == "day" or session.name == "off_session":
            return day_start
        return max(day_start, session.start_utc)

    def _update_loss_status(self) -> None:
        self.status.consecutive_losses = self._consecutive_losses
        self.status.recent_consecutive_losses = self._recent_consecutive_losses
        self.status.loss_streak_scope_start = self._loss_streak_scope_start
        self.status.loss_cooldown_since = self._loss_cooldown_since

    def _scan_only_reasons(
        self,
        *,
        active_symbol_count: int,
        open_order_count: int,
        today_trade_count: int,
        daily_pnl_pct: float,
    ) -> list[str]:
        reasons: list[str] = []
        if active_symbol_count >= self.settings.max_concurrent_trades:
            reasons.append("max concurrent trade limit reached")
        if open_order_count >= self.settings.max_concurrent_trades:
            reasons.append("open order limit reached")
        if today_trade_count >= self.settings.max_trades_per_day:
            reasons.append("daily trade limit reached")
        if daily_pnl_pct <= self.settings.daily_hard_loss_limit_pct:
            reasons.append("daily hard loss limit reached")
        if daily_pnl_pct >= self.settings.daily_profit_target_pct:
            reasons.append("daily profit target reached; protecting gains")
        return reasons

    def _update_cycle_status(self, session: SessionState, regime: RegimeResult) -> None:
        self.status.cycle_count += 1
        self.status.last_cycle_at = datetime.now(timezone.utc)
        self.status.current_session = session.name if session.active or session.tradable else "closed"
        self.status.current_regime = regime.regime
        self.status.last_error = None

    def _order_cooldown_active(self) -> bool:
        if self._last_order_at is None:
            return False
        elapsed = (datetime.now(timezone.utc) - self._last_order_at).total_seconds()
        return elapsed < self.settings.bot_min_seconds_between_orders

    def _leader_confirmed(self, context: StrategyContext, direction: Direction) -> bool:
        return self._leader_state(context, direction) == "aligned"

    def _leader_confirmation_valid(
        self,
        context: StrategyContext,
        direction: Direction,
        score: SetupScoreResult,
    ) -> bool:
        leader_state = self._leader_state(context, direction)
        if leader_state == "aligned":
            return True
        if leader_state == "conflict":
            return False
        return score.total >= self.risk_engine.score_threshold_for_grade("A", context.session_name)

    def _regime_allows_new_trades(self, regime: RegimeResult) -> bool:
        if not regime.tradable:
            return False
        return regime.regime != "unclear" or self.settings.allow_unclear_regime_trading

    def _leader_state(self, context: StrategyContext, direction: Direction) -> str:
        leaders = [leader for leader in (context.btc_direction, context.eth_direction) if leader is not None]
        if any(leader == direction for leader in leaders):
            return "aligned"
        if any(leader != direction for leader in leaders):
            return "conflict"
        return "neutral"

    def _expected_net_profit_pct(self, signal: StrategySignal, spread_bps: float, slippage_bps: float) -> float:
        gross_pct = signal.expected_move / signal.entry_price * 100 if signal.entry_price else 0.0
        cost_pct = (self.settings.fee_rate_bps * 2 + slippage_bps + spread_bps) / 100
        return gross_pct - cost_pct

    def _take_profit_levels(self, trade: Trade) -> list[float]:
        take_profit = trade.take_profit or {}
        if isinstance(take_profit, dict):
            return [float(item) for item in take_profit.get("levels", [])]
        if isinstance(take_profit, list):
            return [float(item) for item in take_profit]
        return []

    def _close_reason(self, trade: Trade, price: float) -> str | None:
        if trade.side == "long" and price <= trade.stop_loss:
            return "stop_loss"
        if trade.side == "short" and price >= trade.stop_loss:
            return "stop_loss"
        levels = self._take_profit_levels(trade)
        if levels and self._target_hit(trade.side, price, levels[-1]):
            return "final_take_profit"
        return None

    def _target_hit(self, side: str, price: float, target: float) -> bool:
        return price >= target if side == "long" else price <= target

    async def _maybe_move_break_even(self, trade: Trade, price: float) -> None:
        extra = dict(trade.extra or {})
        if extra.get("break_even_moved"):
            return
        risk = abs(trade.entry_price - trade.stop_loss)
        if risk <= 0:
            return
        profit = price - trade.entry_price if trade.side == "long" else trade.entry_price - price
        if profit >= risk * self.settings.break_even_trigger_r:
            trade.stop_loss = trade.entry_price
            extra["break_even_moved"] = True
            trade.extra = extra

    def _unrealized_pnl(self, trade: Trade, price: float) -> float:
        return round(self._exit_pnl(trade, price, trade.quantity), 4)

    @staticmethod
    def _trade_assessment_label(trade: Trade) -> str:
        extra = trade.extra or {}
        score = extra.get("setup_score")
        grade = extra.get("grade")
        risk_pct = extra.get("risk_pct")
        parts = []
        if score is not None:
            parts.append(f"score {score}")
        if grade:
            parts.append(f"grade {grade}")
        if risk_pct is not None:
            parts.append(f"risk {float(risk_pct):.2f}%")
        return f"({' '.join(parts)})" if parts else ""

    def _exit_pnl(self, trade: Trade, price: float, quantity: float) -> float:
        gross = (price - trade.entry_price) * quantity
        if trade.side == "short":
            gross *= -1
        fee = price * quantity * self.settings.fee_rate_bps / 10_000
        return gross - fee

    def _session_range(self, candles: list[Candle], start: time, end: time) -> tuple[float | None, float | None]:
        filtered = [candle for candle in candles if start <= candle.timestamp.time().replace(tzinfo=None) <= end]
        if not filtered:
            return None, None
        return max(candle.high for candle in filtered), min(candle.low for candle in filtered)

    def _abnormal_wick_ratio(self, candles: list[Candle]) -> float:
        if not candles:
            return 0.0
        abnormal = 0
        for candle in candles:
            body = abs(candle.close - candle.open)
            wick = candle.high - candle.low
            if wick > 0 and body / wick < 0.25:
                abnormal += 1
        return abnormal / len(candles)

    async def _cancel_loop(self) -> None:
        task = self._task
        self._task = None
        if task and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self.status.loop_task_active = False

    def _remember(self, message: str) -> None:
        self.status.messages.append(message)
        self.status.messages = self.status.messages[-20:]


bot_runner = BotRunner()
