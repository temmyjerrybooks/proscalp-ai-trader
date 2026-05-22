from __future__ import annotations

import asyncio
import csv
import io
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.database.db import AsyncSessionLocal, get_db
from app.database.models import MarketRegime, RiskEvent, SetupScore, Signal, Trade
from app.exchanges.base import Candle, ExchangeAdapter
from app.exchanges.factory import create_exchange_adapter
from app.signals.followup import evaluate_signal_follow_up
from app.services.bot_runner import bot_runner
from app.sessions.session_manager import SessionManager

router = APIRouter(prefix="/api", tags=["signals"])
SignalDecision = Literal["all", "accepted", "rejected"]
SignalSide = Literal["all", "long", "short"]
_FOLLOWUP_CANDLE_CACHE: dict[tuple[str, str, int, int, int], tuple[datetime, list[Candle]]] = {}
CSV_EXPORT_CHUNK_SIZE = 500
CSV_EXPORT_CANDLE_PAGE_LIMIT = 1500


@router.get("/signals")
async def signals(
    limit: int = Query(300, ge=1, le=500),
    include_followup: bool = True,
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    result = await db.execute(select(Signal).order_by(desc(Signal.created_at)).limit(limit))
    rows = result.scalars().all()
    if not rows:
        return [
            {
                "symbol": "ETHUSDT",
                "setup_type": "VWAP reclaim scalp",
                "confidence_score": 84,
                "setup_score": 84,
                "grade": "A",
                "direction": "long",
                "reason_for_entry": ["paper demo: VWAP reclaimed with volume"],
                "rejection_reasons": [],
                "decision_reasons": ["paper demo: VWAP reclaimed with volume"],
                "reason_summary": "paper demo: VWAP reclaimed with volume",
                "decision": "accepted",
                "accepted": True,
            },
            {
                "symbol": "DOGEUSDT",
                "setup_type": "Momentum continuation scalp",
                "confidence_score": 62,
                "setup_score": 62,
                "grade": "NO_TRADE",
                "direction": "long",
                "reason_for_entry": [],
                "rejection_reasons": ["setup score below B threshold", "spread is too wide"],
                "decision_reasons": ["setup score below B threshold", "spread is too wide"],
                "reason_summary": "setup score below B threshold; spread is too wide",
                "decision": "rejected",
                "accepted": False,
            },
        ]

    return await _serialize_signal_rows(
        db,
        rows,
        risk_event_limit=min(1000, max(300, limit * 3)),
        include_followup=include_followup,
    )


@router.get("/signals/search")
async def signal_search(
    decision: SignalDecision = "all",
    symbol: str | None = Query(None, min_length=1, max_length=32),
    setup: str | None = Query(None, min_length=1, max_length=80),
    side: SignalSide = "all",
    execution_status: str = Query("all", max_length=40),
    follow_up_status: str = Query("all", max_length=40),
    verdict: str = Query("all", max_length=40),
    settled_only: bool = False,
    start_date: str | None = Query(None, description="Signal start date, YYYY-MM-DD or ISO datetime"),
    end_date: str | None = Query(None, description="Signal end date, YYYY-MM-DD or ISO datetime"),
    min_score: int | None = Query(None, ge=0, le=100),
    max_score: int | None = Query(None, ge=0, le=100),
    current_cycle: bool = False,
    include_followup: bool = True,
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0, le=10000),
    db: AsyncSession = Depends(get_db),
) -> dict:
    normalized_symbol = _normalize_symbol(symbol)
    normalized_setup = _normalize_setup(setup)
    effective_current_cycle = _effective_current_cycle(current_cycle, start_date, end_date)
    filter_options = _signal_filter_options(
        decision=decision,
        symbol=normalized_symbol,
        setup=normalized_setup,
        side=side,
        execution_status=execution_status,
        follow_up_status=follow_up_status,
        verdict=verdict,
        settled_only=settled_only,
        start_date=start_date,
        end_date=end_date,
        min_score=min_score,
        max_score=max_score,
        current_cycle=effective_current_cycle,
    )
    rows, source_window = await _load_signal_rows(
        db,
        normalized_symbol=normalized_symbol,
        normalized_setup=normalized_setup,
        side=side,
        start_date=start_date,
        end_date=end_date,
        current_cycle=effective_current_cycle,
        trackable_only=settled_only,
        source_limit=min(10_000, max(10_000 if settled_only else 2_000, offset + limit * 6)),
    )
    if not rows:
        return _signal_page([], total=0, limit=limit, offset=offset, source_window=0, filters=filter_options)
    serialized = await _serialize_signal_rows(
        db,
        rows,
        risk_event_limit=1000,
        include_followup=include_followup,
        legacy_risk_fallback=not settled_only,
    )

    filtered = _apply_signal_filters(
        serialized,
        decision=decision,
        symbol=normalized_symbol,
        setup=normalized_setup,
        side=side,
        execution_status=execution_status,
        follow_up_status=follow_up_status,
        verdict=verdict,
        settled_only=settled_only,
        min_score=min_score,
        max_score=max_score,
    )
    page = filtered[offset : offset + limit]

    return _signal_page(
        page,
        total=len(filtered),
        limit=limit,
        offset=offset,
        source_window=source_window,
        filters=filter_options,
    )


@router.get("/signals/report.csv")
async def signal_report_csv(
    decision: SignalDecision = "all",
    symbol: str | None = Query(None, min_length=1, max_length=32),
    setup: str | None = Query(None, min_length=1, max_length=80),
    side: SignalSide = "all",
    execution_status: str = Query("all", max_length=40),
    follow_up_status: str = Query("all", max_length=40),
    verdict: str = Query("all", max_length=40),
    settled_only: bool = False,
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    min_score: int | None = Query(None, ge=0, le=100),
    max_score: int | None = Query(None, ge=0, le=100),
    current_cycle: bool = False,
) -> StreamingResponse:
    normalized_symbol = _normalize_symbol(symbol)
    normalized_setup = _normalize_setup(setup)
    effective_current_cycle = _effective_current_cycle(current_cycle, start_date, end_date)
    filename = _signal_report_filename(decision, start_date, end_date)
    return StreamingResponse(
        _stream_signal_report_csv(
            decision=decision,
            symbol=normalized_symbol,
            setup=normalized_setup,
            side=side,
            execution_status=execution_status,
            follow_up_status=follow_up_status,
            verdict=verdict,
            settled_only=settled_only,
            start_date=start_date,
            end_date=end_date,
            min_score=min_score,
            max_score=max_score,
            current_cycle=effective_current_cycle,
        ),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Accel-Buffering": "no",
        },
    )


async def _stream_signal_report_csv(
    *,
    decision: SignalDecision,
    symbol: str | None,
    setup: str | None,
    side: SignalSide,
    execution_status: str,
    follow_up_status: str,
    verdict: str,
    settled_only: bool,
    start_date: str | None,
    end_date: str | None,
    min_score: int | None,
    max_score: int | None,
    current_cycle: bool,
) -> AsyncIterator[str]:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=_signal_csv_fieldnames(), lineterminator="\n")
    writer.writeheader()
    yield _drain_csv_buffer(buffer)

    async with AsyncSessionLocal() as db:
        settings = get_settings()
        followup_provider = _FollowUpCandleProvider(settings)
        async for rows in _iter_signal_row_chunks(
            db,
            normalized_symbol=symbol,
            normalized_setup=setup,
            side=side,
            start_date=start_date,
            end_date=end_date,
            current_cycle=current_cycle,
            trackable_only=settled_only,
            chunk_size=CSV_EXPORT_CHUNK_SIZE,
        ):
            serialized = await _serialize_signal_rows(
                db,
                rows,
                risk_event_limit=max(2000, len(rows) * 6),
                include_followup=True,
                followup_provider=followup_provider,
                legacy_risk_fallback=False,
            )
            filtered = _apply_signal_filters(
                serialized,
                decision=decision,
                symbol=symbol,
                setup=setup,
                side=side,
                execution_status=execution_status,
                follow_up_status=follow_up_status,
                verdict=verdict,
                settled_only=settled_only,
                min_score=min_score,
                max_score=max_score,
            )
            for row in filtered:
                writer.writerow(_signal_csv_row(row))
                if buffer.tell() >= 64_000:
                    yield _drain_csv_buffer(buffer)
            db.expunge_all()
            if buffer.tell():
                yield _drain_csv_buffer(buffer)


async def _iter_signal_row_chunks(
    db: AsyncSession,
    *,
    normalized_symbol: str | None,
    normalized_setup: str | None,
    side: SignalSide,
    start_date: str | None,
    end_date: str | None,
    current_cycle: bool,
    trackable_only: bool,
    chunk_size: int,
) -> AsyncIterator[list[Signal]]:
    if current_cycle:
        signal_ids = bot_runner.current_signal_ids()
        for index in range(0, len(signal_ids), chunk_size):
            id_batch = signal_ids[index : index + chunk_size]
            if not id_batch:
                continue
            query = _signal_base_query(
                normalized_symbol=normalized_symbol,
                normalized_setup=normalized_setup,
                side=side,
                start_date=start_date,
                end_date=end_date,
                current_cycle=False,
                trackable_only=trackable_only,
            ).where(Signal.id.in_(id_batch))
            result = await db.execute(query.order_by(desc(Signal.created_at), desc(Signal.id)))
            rows = list(result.scalars().all())
            if rows:
                yield rows
        return

    last_created_at: datetime | None = None
    last_id: str | None = None
    while True:
        query = _signal_base_query(
            normalized_symbol=normalized_symbol,
            normalized_setup=normalized_setup,
            side=side,
            start_date=start_date,
            end_date=end_date,
            current_cycle=False,
            trackable_only=trackable_only,
        )
        if last_created_at and last_id:
            query = query.where(
                or_(
                    Signal.created_at < last_created_at,
                    and_(Signal.created_at == last_created_at, Signal.id < last_id),
                )
            )
        result = await db.execute(query.order_by(desc(Signal.created_at), desc(Signal.id)).limit(chunk_size))
        rows = list(result.scalars().all())
        if not rows:
            break
        yield rows
        last_created_at = rows[-1].created_at
        last_id = rows[-1].id


def _drain_csv_buffer(buffer: io.StringIO) -> str:
    value = buffer.getvalue()
    buffer.seek(0)
    buffer.truncate(0)
    return value


async def _load_signal_rows(
    db: AsyncSession,
    *,
    normalized_symbol: str | None,
    normalized_setup: str | None,
    side: SignalSide,
    start_date: str | None,
    end_date: str | None,
    current_cycle: bool,
    trackable_only: bool,
    source_limit: int,
) -> tuple[list[Signal], int]:
    query = _signal_base_query(
        normalized_symbol=normalized_symbol,
        normalized_setup=normalized_setup,
        side=side,
        start_date=start_date,
        end_date=end_date,
        current_cycle=False,
        trackable_only=trackable_only,
    )
    if current_cycle:
        signal_ids = bot_runner.current_signal_ids()
        if not signal_ids:
            return [], 0
        query = query.where(Signal.id.in_(signal_ids))
    result = await db.execute(query.order_by(desc(Signal.created_at)).limit(source_limit))
    rows = list(result.scalars().all())
    return rows, len(rows)


def _signal_base_query(
    *,
    normalized_symbol: str | None,
    normalized_setup: str | None,
    side: SignalSide,
    start_date: str | None,
    end_date: str | None,
    current_cycle: bool,
    trackable_only: bool,
):
    start_at = _parse_signal_bound(start_date, end=False)
    end_at = _parse_signal_bound(end_date, end=True)
    query = select(Signal)
    if current_cycle:
        signal_ids = bot_runner.current_signal_ids()
        if not signal_ids:
            return query.where(Signal.id.in_([]))
        query = query.where(Signal.id.in_(signal_ids))
    if normalized_symbol:
        query = query.where(Signal.symbol == normalized_symbol)
    if normalized_setup:
        query = query.where(Signal.setup_name.ilike(f"%{normalized_setup}%"))
    if side != "all":
        query = query.where(Signal.direction == side)
    if trackable_only:
        query = query.where(Signal.accepted.is_(True), Signal.entry_price > 0, Signal.stop_loss > 0)
    if start_at:
        query = query.where(Signal.created_at >= start_at)
    if end_at:
        query = query.where(Signal.created_at < end_at)
    return query


async def _serialize_signal_rows(
    db: AsyncSession,
    rows: list[Signal],
    risk_event_limit: int = 1000,
    include_followup: bool = True,
    followup_provider: "_FollowUpCandleProvider | None" = None,
    legacy_risk_fallback: bool = True,
) -> list[dict]:
    if not rows:
        return []

    settings = get_settings()
    signal_ids = [row.id for row in rows]
    created_times = [row.created_at for row in rows if row.created_at]
    window_start = min(created_times) - timedelta(minutes=10) if created_times else None
    window_end = max(created_times) + timedelta(minutes=60) if created_times else None
    score_result = await db.execute(select(SetupScore).where(SetupScore.signal_id.in_(signal_ids)))
    score_by_signal_id = {row.signal_id: row for row in score_result.scalars().all() if row.signal_id}

    risk_query = select(RiskEvent).where(
        RiskEvent.event_type.in_(["setup_rejected", "trade_rejected", "market_data_error"])
    )
    if signal_ids and window_start and window_end:
        exact_risk_clause = RiskEvent.payload["signal_id"].as_string().in_(signal_ids)
        risk_query = risk_query.where(
            or_(exact_risk_clause, and_(RiskEvent.created_at >= window_start, RiskEvent.created_at <= window_end))
            if legacy_risk_fallback
            else exact_risk_clause
        )
    risk_result = await db.execute(risk_query.order_by(desc(RiskEvent.created_at)))
    risk_events = list(risk_result.scalars().all())
    risk_events_by_signal_id: dict[str, list[RiskEvent]] = {}
    legacy_risk_events: list[RiskEvent] = []
    for event in risk_events:
        signal_id = str((event.payload or {}).get("signal_id") or "")
        if signal_id:
            risk_events_by_signal_id.setdefault(signal_id, []).append(event)
        else:
            legacy_risk_events.append(event)

    trade_query = select(Trade)
    if signal_ids:
        trade_query = trade_query.where(Trade.extra["signal_id"].as_string().in_(signal_ids))
    elif window_start and window_end:
        trade_query = trade_query.where(Trade.opened_at >= window_start, Trade.opened_at <= window_end)
    trade_result = await db.execute(trade_query.order_by(desc(Trade.opened_at)))
    trade_by_signal_id = {
        str(extra.get("signal_id")): trade
        for trade in trade_result.scalars().all()
        if (extra := (trade.extra or {})).get("signal_id")
    }
    context_by_signal_id = await _signal_context_by_id(db, rows, trade_by_signal_id, settings)
    if include_followup and settings.signal_followup_enabled:
        followup_candles = (
            await followup_provider.candles_by_symbol(rows)
            if followup_provider
            else await _followup_candles_by_symbol(rows, settings)
        )
    else:
        followup_candles = {}
    followup_now = datetime.now(timezone.utc)

    response = []
    for row in rows:
        score = score_by_signal_id.get(row.id)
        risk_reasons = _matching_risk_reasons(row, risk_events_by_signal_id.get(row.id, []) or legacy_risk_events)
        setup_score = score.total_score if score else int(round(row.confidence_score))
        trade = trade_by_signal_id.get(row.id)
        execution_status = _execution_status(row, score, risk_reasons, trade, settings)
        accepted = _bot_decision_accepted(execution_status)
        decision_reasons = _decision_reasons(row, score, risk_reasons, execution_status)
        signal_context = context_by_signal_id.get(row.id, {"signal_session": "unknown", "signal_regime": "unknown"})
        follow_up = (
            evaluate_signal_follow_up(
                row,
                trade,
                followup_candles.get(row.symbol, []),
                decision_accepted=accepted,
                now=followup_now,
                horizon_minutes=settings.signal_followup_window_minutes,
                min_move_pct=settings.signal_followup_min_move_pct,
            )
            if include_followup and settings.signal_followup_enabled
            else None
        )
        response.append(
            {
                "id": row.id,
                "created_at": row.created_at,
                "symbol": row.symbol,
                "setup_type": row.setup_name,
                "confidence_score": row.confidence_score,
                "setup_score": setup_score,
                "grade": score.grade if score else ("NO_TRADE" if not row.accepted else "strategy"),
                "direction": row.direction,
                "reason_for_entry": row.reasons_for_entry,
                "rejection_reasons": row.rejection_reasons,
                "decision_reasons": decision_reasons,
                "reason_summary": "; ".join(decision_reasons),
                "decision": "accepted" if accepted else "rejected",
                "strategy_status": "accepted" if row.accepted else "rejected",
                "execution_status": execution_status,
                "signal_session": signal_context["signal_session"],
                "signal_regime": signal_context["signal_regime"],
                "trade_id": trade.id if trade else None,
                "trade_status": trade.status if trade else None,
                "follow_up": follow_up,
                "accepted": accepted,
            }
        )
    return response


def _signal_page(
    items: list[dict],
    total: int,
    limit: int,
    offset: int,
    source_window: int,
    filters: dict,
) -> dict:
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_next": offset + limit < total,
        "source_window": source_window,
        "filters": filters,
    }


def _normalize_symbol(symbol: str | None) -> str | None:
    if not symbol:
        return None
    clean = symbol.upper().replace("/", "").replace("-", "").strip()
    return clean or None


def _normalize_setup(setup: str | None) -> str | None:
    if not setup:
        return None
    clean = " ".join(setup.strip().split())
    return clean or None


def _decision_matches(row: dict, decision: SignalDecision) -> bool:
    return decision == "all" or row.get("decision") == decision


def _signal_filter_options(
    *,
    decision: SignalDecision,
    symbol: str | None,
    setup: str | None,
    side: SignalSide,
    execution_status: str,
    follow_up_status: str,
    verdict: str,
    settled_only: bool,
    start_date: str | None,
    end_date: str | None,
    min_score: int | None,
    max_score: int | None,
    current_cycle: bool,
) -> dict:
    return {
        "decision": decision,
        "symbol": symbol,
        "setup": setup,
        "side": side,
        "execution_status": _normalize_status_filter(execution_status),
        "follow_up_status": _normalize_status_filter(follow_up_status),
        "verdict": _normalize_status_filter(verdict),
        "settled_only": settled_only,
        "start_date": start_date,
        "end_date": end_date,
        "min_score": min_score,
        "max_score": max_score,
        "current_cycle": current_cycle,
    }


def _effective_current_cycle(current_cycle: bool, start_date: str | None, end_date: str | None) -> bool:
    return current_cycle and not (bool(start_date and start_date.strip()) or bool(end_date and end_date.strip()))


def _apply_signal_filters(
    rows: list[dict],
    decision: SignalDecision,
    symbol: str | None,
    setup: str | None,
    side: SignalSide = "all",
    execution_status: str = "all",
    follow_up_status: str = "all",
    verdict: str = "all",
    settled_only: bool = False,
    min_score: int | None = None,
    max_score: int | None = None,
) -> list[dict]:
    return [
        row
        for row in rows
        if _decision_matches(row, decision)
        and _symbol_matches(row, symbol)
        and _setup_matches(row, setup)
        and _side_matches(row, side)
        and _score_matches(row, min_score, max_score)
        and _status_matches(row.get("execution_status"), execution_status)
        and _status_matches((row.get("follow_up") or {}).get("status"), follow_up_status)
        and _status_matches((row.get("follow_up") or {}).get("verdict"), verdict)
        and _settled_matches(row, settled_only)
    ]


def _side_matches(row: dict, side: SignalSide) -> bool:
    return side == "all" or row.get("direction") == side


def _score_matches(row: dict, min_score: int | None, max_score: int | None) -> bool:
    score = row.get("setup_score")
    if score is None:
        score = row.get("confidence_score", 0)
    try:
        numeric_score = float(score)
    except (TypeError, ValueError):
        return False
    if min_score is not None and numeric_score < min_score:
        return False
    if max_score is not None and numeric_score > max_score:
        return False
    return True


def _status_matches(value: object, selected: str) -> bool:
    normalized = _normalize_status_filter(selected)
    if normalized == "all":
        return True
    return _normalize_status_filter(str(value or "")) == normalized


def _settled_matches(row: dict, settled_only: bool) -> bool:
    if not settled_only:
        return True
    return bool((row.get("follow_up") or {}).get("settled"))


def _normalize_status_filter(value: str | None) -> str:
    if not value:
        return "all"
    return value.strip().lower().replace(" ", "_").replace("-", "_") or "all"


def _symbol_matches(row: dict, symbol: str | None) -> bool:
    return symbol is None or row.get("symbol") == symbol


def _setup_matches(row: dict, setup: str | None) -> bool:
    if setup is None:
        return True
    return setup.lower() in str(row.get("setup_type", "")).lower()


def _parse_signal_bound(value: str | None, end: bool) -> datetime | None:
    if not value:
        return None
    clean = value.strip()
    try:
        parsed_date = date.fromisoformat(clean)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(clean.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"Invalid signal date filter: {value}") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        parsed = parsed.astimezone(timezone.utc)
        return parsed + timedelta(microseconds=1) if end else parsed
    user_timezone = ZoneInfo(get_settings().user_timezone)
    bound = datetime.combine(parsed_date, time.min, tzinfo=user_timezone)
    if end:
        bound += timedelta(days=1)
    return bound.astimezone(timezone.utc)


async def _signal_context_by_id(
    db: AsyncSession,
    rows: list[Signal],
    trade_by_signal_id: dict[str, Trade],
    settings,
) -> dict[str, dict[str, str]]:
    if not rows:
        return {}
    regime_rows = await _regimes_for_signal_window(db, rows)
    session_manager = SessionManager(settings)
    response: dict[str, dict[str, str]] = {}
    for row in rows:
        trade = trade_by_signal_id.get(row.id)
        extra = trade.extra if trade else {}
        trade_session = (extra or {}).get("entry_session")
        trade_regime = (extra or {}).get("entry_regime")
        regime = str(trade_regime or _regime_at_signal(row, regime_rows) or "unknown")
        session = str(trade_session or session_manager.trading_session(now=row.created_at, regime=regime).name)
        response[row.id] = {"signal_session": session, "signal_regime": regime}
    return response


async def _regimes_for_signal_window(db: AsyncSession, rows: list[Signal]) -> list[MarketRegime]:
    created_times = [row.created_at for row in rows if row.created_at]
    if not created_times:
        return []
    start = min(created_times) - timedelta(hours=6)
    end = max(created_times) + timedelta(minutes=10)
    result = await db.execute(
        select(MarketRegime)
        .where(MarketRegime.created_at >= start, MarketRegime.created_at <= end)
        .order_by(MarketRegime.created_at)
        .limit(2000)
    )
    return list(result.scalars().all())


def _regime_at_signal(signal: Signal, regimes: list[MarketRegime]) -> str | None:
    created_at = signal.created_at
    if not created_at:
        return None
    eligible = [regime for regime in regimes if regime.created_at <= created_at + timedelta(minutes=1)]
    if not eligible:
        return None
    return eligible[-1].regime


@dataclass
class _CachedCandleRange:
    start_at: datetime
    end_at: datetime
    candles_by_time: dict[datetime, Candle]

    def merge(self, candles: list[Candle], start_at: datetime, end_at: datetime) -> None:
        self.start_at = min(self.start_at, start_at)
        self.end_at = max(self.end_at, end_at)
        for candle in candles:
            self.candles_by_time[_aware_datetime(candle.timestamp)] = candle

    def candles(self) -> list[Candle]:
        return [self.candles_by_time[key] for key in sorted(self.candles_by_time)]


class _FollowUpCandleProvider:
    """Shared candle cache for one report export.

    Signal exports can span thousands of rows. Reusing the same provider across
    chunks prevents repeated OHLCV downloads for overlapping symbols and time
    windows while keeping the CSV itself uncapped.
    """

    def __init__(self, settings) -> None:
        self.settings = settings
        self.adapter = create_exchange_adapter(settings)
        self.timeframe = settings.signal_followup_timeframe
        self.candle_step = _timeframe_delta(self.timeframe)
        self.limit = _export_candle_page_limit(settings)
        self.horizon = timedelta(minutes=max(1, int(settings.signal_followup_window_minutes)))
        self.cache: dict[str, _CachedCandleRange] = {}
        self.mark_cache: dict[str, Candle] = {}

    async def candles_by_symbol(self, rows: list[Signal]) -> dict[str, list[Candle]]:
        rows_by_symbol: dict[str, list[Signal]] = {}
        for row in rows:
            if row.entry_price and row.entry_price > 0 and row.created_at:
                rows_by_symbol.setdefault(row.symbol, []).append(row)
        if not rows_by_symbol:
            return {}

        now = datetime.now(timezone.utc)
        missing_ranges: list[tuple[str, datetime, datetime]] = []
        response: dict[str, list[Candle]] = {}

        for symbol, symbol_rows in rows_by_symbol.items():
            start_at, end_at = self._required_range(symbol_rows, now)
            fetch_start, fetch_end = self._expanded_fetch_range(start_at, end_at, now)
            cached = self.cache.get(symbol)
            if cached is None:
                self.cache[symbol] = _CachedCandleRange(start_at=fetch_start, end_at=fetch_end, candles_by_time={})
                missing_ranges.append((symbol, fetch_start, fetch_end))
            else:
                if fetch_start < cached.start_at:
                    missing_ranges.append((symbol, fetch_start, cached.start_at - self.candle_step))
                if fetch_end > cached.end_at:
                    missing_ranges.append((symbol, cached.end_at + self.candle_step, fetch_end))
                    cached.end_at = fetch_end
                cached.start_at = min(cached.start_at, fetch_start)

        if missing_ranges:
            await asyncio.gather(*(self._fetch_and_cache(symbol, start_at, end_at) for symbol, start_at, end_at in missing_ranges))

        for symbol, symbol_rows in rows_by_symbol.items():
            start_at, end_at = self._required_range(symbol_rows, now)
            cached = self.cache.get(symbol)
            candles = cached.candles() if cached else []
            if end_at >= now - self.candle_step:
                candles = candles + await self._mark_candle(symbol, now)
            response[symbol] = [
                candle
                for candle in candles
                if _aware_datetime(candle.timestamp) >= start_at and _aware_datetime(candle.timestamp) <= end_at
            ]

        return response

    def _required_range(self, rows: list[Signal], now: datetime) -> tuple[datetime, datetime]:
        created_times = [_aware_datetime(row.created_at) for row in rows if row.created_at]
        start_at = min(created_times) - self.candle_step
        end_at = min(now, max(created_times) + self.horizon + self.candle_step)
        return start_at, end_at

    def _expanded_fetch_range(self, start_at: datetime, end_at: datetime, now: datetime) -> tuple[datetime, datetime]:
        if end_at >= now - self.candle_step:
            return start_at, end_at
        start_day = datetime.combine(start_at.date(), time.min, tzinfo=timezone.utc)
        end_day = datetime.combine(end_at.date() + timedelta(days=1), time.min, tzinfo=timezone.utc)
        return start_day, min(end_day, now)

    async def _fetch_and_cache(self, symbol: str, start_at: datetime, end_at: datetime) -> None:
        if end_at < start_at:
            return
        try:
            candles = await _fetch_candle_range(
                self.adapter,
                symbol,
                self.timeframe,
                start_at,
                end_at,
                self.limit,
                self.candle_step,
            )
        except Exception:
            candles = []
        cached = self.cache.setdefault(symbol, _CachedCandleRange(start_at=start_at, end_at=end_at, candles_by_time={}))
        cached.merge(list(candles), start_at, end_at)

    async def _mark_candle(self, symbol: str, now: datetime) -> list[Candle]:
        cached = self.mark_cache.get(symbol)
        if cached and (now - _aware_datetime(cached.timestamp)).total_seconds() <= 10:
            return [cached]
        try:
            book = await self.adapter.fetch_order_book(symbol, limit=10)
        except Exception:
            return []
        if book.mid_price <= 0:
            return []
        candle = Candle(
            timestamp=now,
            open=book.mid_price,
            high=book.mid_price,
            low=book.mid_price,
            close=book.mid_price,
            volume=0.0,
        )
        self.mark_cache[symbol] = candle
        return [candle]


async def _followup_candles_by_symbol(rows: list[Signal], settings) -> dict[str, list[Candle]]:
    if not settings.signal_followup_enabled:
        return {}
    rows_by_symbol: dict[str, list[Signal]] = {}
    for row in rows:
        if row.entry_price and row.entry_price > 0 and row.created_at:
            rows_by_symbol.setdefault(row.symbol, []).append(row)
    if not rows_by_symbol:
        return {}

    now = datetime.now(timezone.utc)
    timeframe = settings.signal_followup_timeframe
    limit = max(50, min(int(settings.signal_followup_candle_limit), CSV_EXPORT_CANDLE_PAGE_LIMIT))
    cache_seconds = max(5, int(settings.signal_followup_cache_seconds))
    horizon = timedelta(minutes=max(1, int(settings.signal_followup_window_minutes)))
    candle_step = _timeframe_delta(timeframe)
    response: dict[str, list[Candle]] = {}
    missing: list[tuple[str, datetime, datetime, tuple[str, str, int, int, int]]] = []

    for symbol, symbol_rows in rows_by_symbol.items():
        created_times = [_aware_datetime(row.created_at) for row in symbol_rows]
        start_at = min(created_times) - candle_step
        end_at = min(now, max(created_times) + horizon + candle_step)
        key = (
            symbol,
            timeframe,
            limit,
            int(start_at.replace(second=0, microsecond=0).timestamp()),
            int(end_at.replace(second=0, microsecond=0).timestamp()),
        )
        cached = _FOLLOWUP_CANDLE_CACHE.get(key)
        if cached and (now - cached[0]).total_seconds() <= cache_seconds:
            response[symbol] = cached[1]
        else:
            missing.append((symbol, start_at, end_at, key))

    if not missing:
        return response

    adapter = create_exchange_adapter(settings)

    async def fetch(symbol: str, start_at: datetime, end_at: datetime, key: tuple[str, str, int, int, int]) -> tuple[str, list[Candle]]:
        try:
            candles = await _fetch_candle_range(adapter, symbol, timeframe, start_at, end_at, limit, candle_step)
        except Exception:
            candles = []
        candles = list(candles)
        if end_at >= now - candle_step:
            try:
                book = await adapter.fetch_order_book(symbol, limit=10)
                if book.mid_price > 0:
                    candles.append(
                        Candle(
                            timestamp=now,
                            open=book.mid_price,
                            high=book.mid_price,
                            low=book.mid_price,
                            close=book.mid_price,
                            volume=0.0,
                        )
                    )
            except Exception:
                pass
        _FOLLOWUP_CANDLE_CACHE[key] = (now, candles)
        return symbol, candles

    results = await asyncio.gather(*(fetch(symbol, start_at, end_at, key) for symbol, start_at, end_at, key in missing))
    response.update(dict(results))
    return response


async def _fetch_candle_range(
    adapter: ExchangeAdapter,
    symbol: str,
    timeframe: str,
    start_at: datetime,
    end_at: datetime,
    limit: int,
    candle_step: timedelta,
) -> list[Candle]:
    if end_at <= start_at:
        return []
    candles_by_time: dict[datetime, Candle] = {}
    cursor = start_at
    while cursor <= end_at:
        chunk_end = min(end_at, cursor + candle_step * max(1, limit - 1))
        chunk = await adapter.fetch_ohlcv_range(
            symbol,
            timeframe,
            start_time=cursor,
            end_time=chunk_end,
            limit=limit,
        )
        for candle in chunk:
            candles_by_time[_aware_datetime(candle.timestamp)] = candle
        if chunk:
            last_time = max(_aware_datetime(candle.timestamp) for candle in chunk)
            next_cursor = last_time + candle_step
            cursor = next_cursor if next_cursor > cursor else chunk_end + candle_step
        else:
            cursor = chunk_end + candle_step
    return [candles_by_time[key] for key in sorted(candles_by_time)]


def _timeframe_delta(timeframe: str) -> timedelta:
    normalized = timeframe.strip().lower()
    if normalized.endswith("m"):
        return timedelta(minutes=max(1, int(normalized[:-1] or "1")))
    if normalized.endswith("h"):
        return timedelta(hours=max(1, int(normalized[:-1] or "1")))
    if normalized.endswith("d"):
        return timedelta(days=max(1, int(normalized[:-1] or "1")))
    return timedelta(minutes=1)


def _export_candle_page_limit(settings) -> int:
    configured = int(settings.signal_followup_candle_limit)
    return max(50, min(max(configured, CSV_EXPORT_CANDLE_PAGE_LIMIT), CSV_EXPORT_CANDLE_PAGE_LIMIT))


def _aware_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _matching_risk_reasons(signal: Signal, risk_events: list[RiskEvent]) -> list[str]:
    reasons: list[str] = []
    for event in risk_events:
        payload = event.payload or {}
        signal_id = payload.get("signal_id")
        if signal_id and signal_id != signal.id:
            continue
        if signal_id == signal.id:
            if event.message and event.message not in reasons:
                reasons.append(event.message)
            if len(reasons) >= 3:
                break
            continue
        if payload.get("symbol") != signal.symbol:
            continue
        setup = payload.get("setup")
        if setup and setup != signal.setup_name:
            continue
        if event.created_at and signal.created_at:
            distance_seconds = abs((event.created_at - signal.created_at).total_seconds())
            if distance_seconds > 300:
                continue
        if event.message and event.message not in reasons:
            reasons.append(event.message)
        if len(reasons) >= 3:
            break
    return reasons


def _decision_reasons(
    signal: Signal,
    score: SetupScore | None,
    risk_reasons: list[str],
    execution_status: str,
) -> list[str]:
    if not signal.accepted:
        reasons = list(signal.rejection_reasons or [])
        return reasons or [f"{signal.setup_name} conditions were not confirmed on the selected timeframe"]
    if risk_reasons:
        return risk_reasons
    if execution_status == "not_selected":
        return [
            "setup passed strategy scoring, but the bot did not take it in this cycle "
            "because another setup had priority, order-per-cycle was reached, or order cooldown was active"
        ]
    if score and score.total_score < get_settings().normal_score_threshold_c:
        return ["setup score below C threshold"]
    return list(signal.reasons_for_entry or ["strategy conditions accepted"])


def _execution_status(
    signal: Signal,
    score: SetupScore | None,
    risk_reasons: list[str],
    trade: Trade | None,
    settings,
) -> str:
    if trade:
        if trade.status in {"open", "pending"}:
            return trade.status
        return "closed"
    if not signal.accepted:
        return "strategy_rejected"
    if risk_reasons:
        return "blocked"
    if score and score.total_score < settings.normal_score_threshold_c:
        return "score_rejected"
    return "not_selected"


def _bot_decision_accepted(execution_status: str) -> bool:
    return execution_status in {"open", "pending", "closed", "executed"}


def _signal_csv_fieldnames() -> list[str]:
    return [
        "created_at",
        "symbol",
        "setup",
        "direction",
        "setup_score",
        "grade",
        "signal_decision",
        "strategy_status",
        "execution_status",
        "trade_id",
        "trade_status",
        "signal_session",
        "signal_regime",
        "follow_up_status",
        "follow_up_settled",
        "decision_quality",
        "follow_up_pnl_pct",
        "max_favorable_pct",
        "max_adverse_pct",
        "follow_up_exit_reason",
        "follow_up_summary",
        "reasons",
        "entry_reasons",
        "rejection_reasons",
    ]


def _signal_csv_row(row: dict) -> dict:
    follow_up = row.get("follow_up") or {}
    return {
        "created_at": _iso(row.get("created_at")),
        "symbol": row.get("symbol"),
        "setup": row.get("setup_type"),
        "direction": row.get("direction"),
        "setup_score": row.get("setup_score"),
        "grade": row.get("grade"),
        "signal_decision": row.get("decision"),
        "strategy_status": row.get("strategy_status"),
        "execution_status": row.get("execution_status"),
        "trade_id": row.get("trade_id"),
        "trade_status": row.get("trade_status"),
        "signal_session": row.get("signal_session"),
        "signal_regime": row.get("signal_regime"),
        "follow_up_status": follow_up.get("status"),
        "follow_up_settled": follow_up.get("settled"),
        "decision_quality": follow_up.get("verdict"),
        "follow_up_pnl_pct": follow_up.get("pnl_pct"),
        "max_favorable_pct": follow_up.get("max_favorable_pct"),
        "max_adverse_pct": follow_up.get("max_adverse_pct"),
        "follow_up_exit_reason": follow_up.get("exit_reason"),
        "follow_up_summary": follow_up.get("summary"),
        "reasons": row.get("reason_summary"),
        "entry_reasons": "; ".join(row.get("reason_for_entry") or []),
        "rejection_reasons": "; ".join(row.get("rejection_reasons") or []),
    }


def _iso(value: object) -> str:
    return value.isoformat() if isinstance(value, datetime) else ""


def _signal_report_filename(decision: str, start_date: str | None, end_date: str | None) -> str:
    start = (start_date or "all").replace(":", "-")
    end = (end_date or date.today().isoformat()).replace(":", "-")
    return f"proscalp_signals_{decision}_{start}_to_{end}.csv"
