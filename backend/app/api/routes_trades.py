from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import TradingMode, get_settings
from app.database.db import get_db
from app.database.models import Trade
from app.exchanges.factory import create_exchange_adapter
from app.services.bot_runner import bot_runner

router = APIRouter(prefix="/api/trades", tags=["trades"])
HistoryOutcome = Literal["all", "positive", "negative", "open", "closed", "breakeven"]


@router.get("/open")
async def open_trades(db: AsyncSession = Depends(get_db)) -> list[dict]:
    settings = get_settings()
    positions = []
    exchange_error: Exception | None = None
    if settings.trading_mode != TradingMode.PAPER:
        try:
            positions = await create_exchange_adapter(settings).fetch_positions()
        except Exception as exc:
            exchange_error = exc
    position_by_symbol = {position.symbol: position for position in positions}

    result = await db.execute(
        select(Trade).where(Trade.status.in_(["open", "pending"])).order_by(desc(Trade.opened_at))
    )
    database_trades = []
    for trade in result.scalars().all():
        extra = trade.extra or {}
        database_trades.append(
            {
                "id": trade.id,
                "symbol": trade.symbol,
                "side": trade.side,
                "status": trade.status,
                "entry": position_by_symbol.get(trade.symbol).entry_price
                if trade.symbol in position_by_symbol
                else trade.entry_price,
                "mark_price": position_by_symbol.get(trade.symbol).mark_price
                if trade.symbol in position_by_symbol
                else None,
                "stop_loss": trade.stop_loss,
                "take_profit": trade.take_profit,
                "quantity": position_by_symbol.get(trade.symbol).quantity
                if trade.symbol in position_by_symbol
                else trade.quantity,
                "unrealized_pnl": position_by_symbol.get(trade.symbol).unrealized_pnl
                if trade.symbol in position_by_symbol
                else trade.unrealized_pnl,
                "realized_pnl": trade.realized_pnl,
                "setup": trade.setup_name,
                **_score_fields(extra),
                **_trade_context_fields(extra),
                "opened_at": trade.opened_at,
                "source": "database+exchange" if trade.symbol in position_by_symbol else "database",
            }
        )
    if settings.trading_mode == TradingMode.PAPER:
        return database_trades

    if exchange_error:
        database_trades.append(
            {
                "id": "exchange-error",
                "symbol": "EXCHANGE",
                "side": "error",
                "status": "error",
                "entry": 0,
                "mark_price": None,
                "stop_loss": 0,
                "take_profit": [],
                "quantity": 0,
                "unrealized_pnl": 0,
                "realized_pnl": 0,
                "setup": f"Unable to fetch exchange positions: {exchange_error}",
                "setup_score": None,
                "setup_grade": None,
                "risk_pct": None,
                "risk_amount": None,
                "score_permission": None,
                "score_session": None,
                "entry_session": "unknown",
                "entry_regime": "unknown",
                "source": "exchange_error",
            }
        )
        return database_trades

    database_symbols = {trade["symbol"] for trade in database_trades}
    exchange_positions = [
        {
            "id": f"exchange-{position.symbol}",
            "symbol": position.symbol,
            "side": position.side,
            "status": "open",
            "entry": position.entry_price,
            "mark_price": position.mark_price,
            "stop_loss": 0,
            "take_profit": [],
            "quantity": position.quantity,
            "unrealized_pnl": position.unrealized_pnl,
            "realized_pnl": 0,
            "setup": f"Exchange position, {position.leverage}x",
            **_empty_score_fields(score_permission="exchange-only position; stop risk estimated"),
            "entry_session": "exchange",
            "entry_regime": "unknown",
            "source": "exchange_estimated_risk",
        }
        for position in positions
        if position.symbol not in database_symbols
    ]
    return database_trades + exchange_positions


@router.get("/history")
async def trade_history(
    start_date: str | None = Query(None, description="Opened-at start date, YYYY-MM-DD or ISO datetime"),
    end_date: str | None = Query(None, description="Opened-at end date, YYYY-MM-DD or ISO datetime"),
    outcome: HistoryOutcome = "all",
    limit: int = Query(300, ge=1, le=2000),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    trades = await _filtered_trade_history(db, start_date, end_date, outcome, limit)
    return [_serialize_trade(trade) for trade in trades]


@router.get("/history.csv")
async def trade_history_csv(
    start_date: str | None = Query(None, description="Opened-at start date, YYYY-MM-DD or ISO datetime"),
    end_date: str | None = Query(None, description="Opened-at end date, YYYY-MM-DD or ISO datetime"),
    outcome: HistoryOutcome = "all",
    limit: int = Query(5000, ge=1, le=10000),
    db: AsyncSession = Depends(get_db),
) -> Response:
    trades = await _filtered_trade_history(db, start_date, end_date, outcome, limit)
    rows = [_serialize_trade(trade) for trade in trades]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=_csv_fieldnames())
    writer.writeheader()
    for row in rows:
        writer.writerow(_csv_row(row))
    filename = f"proscalp_trade_history_{outcome}_{date.today().isoformat()}.csv"
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/close-all")
async def close_all() -> dict:
    return await bot_runner.close_all_positions()


def _score_fields(extra: dict) -> dict:
    return {
        "setup_score": extra.get("setup_score"),
        "setup_grade": extra.get("grade"),
        "risk_pct": extra.get("risk_pct"),
        "risk_amount": extra.get("risk_amount"),
        "score_permission": extra.get("score_permission"),
        "score_session": extra.get("score_session"),
    }


def _trade_context_fields(extra: dict) -> dict:
    return {
        "entry_session": extra.get("entry_session") or extra.get("score_session") or "unknown",
        "entry_regime": extra.get("entry_regime") or "unknown",
    }


def _empty_score_fields(score_permission: str | None = None) -> dict:
    return {
        "setup_score": None,
        "setup_grade": None,
        "risk_pct": None,
        "risk_amount": None,
        "score_permission": score_permission,
        "score_session": None,
    }


async def _filtered_trade_history(
    db: AsyncSession,
    start_date: str | None,
    end_date: str | None,
    outcome: HistoryOutcome,
    limit: int,
) -> list[Trade]:
    start_at = _parse_history_bound(start_date, end=False)
    end_at = _parse_history_bound(end_date, end=True)
    query = select(Trade)
    if start_at:
        query = query.where(Trade.opened_at >= start_at)
    if end_at:
        query = query.where(Trade.opened_at < end_at)
    if outcome == "positive":
        query = query.where(Trade.realized_pnl > 0)
    elif outcome == "negative":
        query = query.where(Trade.realized_pnl < 0)
    elif outcome == "open":
        query = query.where(Trade.status.in_(["open", "pending"]))
    elif outcome == "closed":
        query = query.where(Trade.status == "closed")
    elif outcome == "breakeven":
        query = query.where(Trade.status == "closed", Trade.realized_pnl == 0)
    result = await db.execute(query.order_by(desc(Trade.opened_at)).limit(limit))
    return list(result.scalars().all())


def _serialize_trade(trade: Trade) -> dict:
    extra = trade.extra or {}
    return {
        "id": trade.id,
        "symbol": trade.symbol,
        "side": trade.side,
        "status": trade.status,
        "entry": trade.entry_price,
        "stop_loss": trade.stop_loss,
        "take_profit": trade.take_profit,
        "quantity": trade.quantity,
        "realized_pnl": trade.realized_pnl,
        "unrealized_pnl": trade.unrealized_pnl,
        "fees": trade.fees,
        "opened_at": trade.opened_at,
        "closed_at": trade.closed_at,
        "setup": trade.setup_name,
        **_score_fields(extra),
        **_trade_context_fields(extra),
        "close_reason": extra.get("close_reason"),
    }


def _parse_history_bound(value: str | None, end: bool) -> datetime | None:
    if not value:
        return None
    clean = value.strip()
    try:
        parsed_date = date.fromisoformat(clean)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(clean.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"Invalid date filter: {value}") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        parsed = parsed.astimezone(timezone.utc)
        return parsed + timedelta(microseconds=1) if end else parsed
    bound = datetime.combine(parsed_date, time.min, tzinfo=timezone.utc)
    return bound + timedelta(days=1) if end else bound


def _csv_fieldnames() -> list[str]:
    return [
        "opened_at",
        "closed_at",
        "symbol",
        "side",
        "status",
        "setup_score",
        "setup_grade",
        "risk_pct",
        "risk_amount",
        "entry_session",
        "entry_regime",
        "entry",
        "stop_loss",
        "quantity",
        "realized_pnl",
        "unrealized_pnl",
        "fees",
        "close_reason",
        "setup",
        "take_profit",
    ]


def _csv_row(row: dict) -> dict:
    return {
        "opened_at": _iso(row.get("opened_at")),
        "closed_at": _iso(row.get("closed_at")),
        "symbol": row.get("symbol"),
        "side": row.get("side"),
        "status": row.get("status"),
        "setup_score": row.get("setup_score"),
        "setup_grade": row.get("setup_grade"),
        "risk_pct": row.get("risk_pct"),
        "risk_amount": row.get("risk_amount"),
        "entry_session": row.get("entry_session"),
        "entry_regime": row.get("entry_regime"),
        "entry": row.get("entry"),
        "stop_loss": row.get("stop_loss"),
        "quantity": row.get("quantity"),
        "realized_pnl": row.get("realized_pnl"),
        "unrealized_pnl": row.get("unrealized_pnl"),
        "fees": row.get("fees"),
        "close_reason": row.get("close_reason"),
        "setup": row.get("setup"),
        "take_profit": json.dumps(row.get("take_profit") or {}, separators=(",", ":")),
    }


def _iso(value: object) -> str:
    return value.isoformat() if isinstance(value, datetime) else ""
