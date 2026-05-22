from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter
from sqlalchemy import desc, func, select

from app.config.settings import get_settings
from app.database.db import AsyncSessionLocal
from app.database.models import Order, RiskEvent, Signal, TelegramAlert, Trade
from app.exchanges.factory import create_exchange_adapter
from app.services.accounting import daily_pnl_snapshot
from app.services.bot_runner import _safe_error_message, bot_runner
from app.sessions.session_manager import SessionManager

router = APIRouter(prefix="/api", tags=["dashboard"])


@router.get("/dashboard")
async def dashboard() -> dict:
    settings = get_settings()
    session = SessionManager(settings).active_session(regime="good")
    async with AsyncSessionLocal() as db:
        active_trade_count = (
            await db.execute(select(func.count()).select_from(Trade).where(Trade.status.in_(["open", "pending"])))
        ).scalar_one()
        adapter = None
        exchange_positions = []
        if settings.trading_mode.value != "paper":
            adapter = create_exchange_adapter(settings)
            try:
                exchange_positions = await adapter.fetch_positions()
            except Exception:
                exchange_positions = []
        snapshot = await daily_pnl_snapshot(
            db,
            settings,
            adapter,
            exchange_positions=exchange_positions,
        )
    return {
        "account_equity": snapshot.account_equity,
        "account_balance": snapshot.account_balance,
        "account_equity_source": snapshot.equity_source,
        "daily_pnl_pct": snapshot.daily_pnl_pct,
        "daily_pnl": snapshot.daily_pnl,
        "open_trades": snapshot.open_trade_count if snapshot.open_trade_count else active_trade_count,
        "session_status": bot_runner.status.current_session if bot_runner.status.loop_task_active else session.name if session.active else "closed",
        "market_regime": bot_runner.status.current_regime if bot_runner.status.loop_task_active else "good",
        "active_exchange": settings.exchange.value,
        "active_mode": settings.trading_mode.value,
        "daily_profit_target_progress": max(0.0, min(100.0, snapshot.daily_pnl_pct / settings.daily_profit_target_pct * 100)),
        "daily_loss_limit_progress": max(
            0.0,
            min(100.0, abs(min(0.0, snapshot.daily_pnl_pct)) / abs(settings.daily_hard_loss_limit_pct) * 100),
        ),
        "bot": asdict(bot_runner.status),
    }


@router.get("/activity")
async def activity() -> list[dict]:
    async with AsyncSessionLocal() as db:
        alert_rows = (
            await db.execute(select(TelegramAlert).order_by(desc(TelegramAlert.created_at)).limit(40))
        ).scalars().all()
        risk_rows = (
            await db.execute(select(RiskEvent).order_by(desc(RiskEvent.created_at)).limit(40))
        ).scalars().all()
        order_rows = (
            await db.execute(select(Order).order_by(desc(Order.created_at)).limit(30))
        ).scalars().all()
        trade_rows = (
            await db.execute(select(Trade).order_by(desc(Trade.updated_at)).limit(30))
        ).scalars().all()
        signal_rows = (
            await db.execute(select(Signal).order_by(desc(Signal.created_at)).limit(30))
        ).scalars().all()

    events: list[dict] = []
    events.extend(
        {
            "id": row.id,
            "time": row.created_at,
            "source": "telegram",
            "type": row.alert_type,
            "severity": "info" if row.delivered else "warning",
            "symbol": _symbol_from_message(row.message),
            "message": _safe_error_message(row.message),
        }
        for row in alert_rows
    )
    events.extend(
        {
            "id": row.id,
            "time": row.created_at,
            "source": "risk",
            "type": row.event_type,
            "severity": row.severity,
            "symbol": row.payload.get("symbol"),
            "message": _risk_activity_message(row),
        }
        for row in risk_rows
    )
    events.extend(
        {
            "id": row.id,
            "time": row.created_at,
            "source": "order",
            "type": row.status,
            "severity": "info" if row.status in {"filled", "new", "partially_filled"} else "warning",
            "symbol": row.symbol,
            "message": f"{row.symbol} {row.side} {row.order_type} order {row.status}",
        }
        for row in order_rows
    )
    events.extend(
        {
            "id": row.id,
            "time": row.updated_at,
            "source": "trade",
            "type": row.status,
            "severity": "info" if row.status == "open" else "warning" if row.realized_pnl < 0 else "success",
            "symbol": row.symbol,
            "message": _trade_activity_message(row),
        }
        for row in trade_rows
    )
    events.extend(
        {
            "id": row.id,
            "time": row.created_at,
            "source": "signal",
            "type": "accepted" if row.accepted else "rejected",
            "severity": "success" if row.accepted else "muted",
            "symbol": row.symbol,
            "message": f"{row.symbol} {row.setup_name} score {round(row.confidence_score, 2)}",
        }
        for row in signal_rows
    )
    events.sort(key=lambda item: item["time"], reverse=True)
    return events[:100]


def _symbol_from_message(message: str) -> str | None:
    for token in message.replace(":", " ").replace(",", " ").split():
        cleaned = token.strip().upper()
        if cleaned.endswith("USDT") and 6 <= len(cleaned) <= 20:
            return cleaned
    return None


def _trade_activity_message(trade: Trade) -> str:
    extra = trade.extra or {}
    score = extra.get("setup_score")
    grade = extra.get("grade")
    risk = extra.get("risk_pct")
    assessment = ""
    if score is not None or grade or risk is not None:
        parts = []
        if score is not None:
            parts.append(f"score {score}")
        if grade:
            parts.append(f"grade {grade}")
        if risk is not None:
            parts.append(f"risk {float(risk):.2f}%")
        assessment = f" ({', '.join(parts)})"
    return f"{trade.symbol} {trade.side} {trade.setup_name} {trade.status}{assessment}"


def _risk_activity_message(event: RiskEvent) -> str:
    exposure = (event.payload or {}).get("exposure") or {}
    if not exposure:
        return _safe_error_message(event.message)
    current = exposure.get("current_session_notional_pct")
    candidate = exposure.get("candidate_notional_pct")
    total_risk = exposure.get("total_open_risk_pct")
    session_cap = exposure.get("session_cap_pct")
    risk_cap = exposure.get("open_risk_cap_pct")
    if current is None or candidate is None:
        return _safe_error_message(event.message)
    return (
        f"{_safe_error_message(event.message)} | session {float(current):.2f}% + candidate {float(candidate):.2f}% "
        f"(cap {float(session_cap):.2f}%), open risk {float(total_risk or 0):.2f}% "
        f"(cap {float(risk_cap or 0):.2f}%)"
    )
