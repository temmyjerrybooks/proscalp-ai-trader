from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings, TradingMode
from app.database.models import Trade
from app.exchanges.base import ExchangeAdapter, Position


@dataclass(slots=True)
class DailyPnlSnapshot:
    day_start: datetime
    account_balance: float
    account_equity: float
    equity_source: str
    realized_pnl: float
    unrealized_pnl: float
    daily_pnl: float
    daily_pnl_pct: float
    open_trade_count: int


def utc_day_start(now: datetime | None = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    return current.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


async def account_balance_basis(
    settings: Settings,
    adapter: ExchangeAdapter | None,
) -> tuple[float, str]:
    if adapter is None or settings.trading_mode == TradingMode.PAPER:
        return settings.paper_starting_equity, "paper_starting_equity"
    try:
        balances = await adapter.fetch_balances()
        for asset in (settings.quote_asset, "USDT", "USDC", "USD"):
            match = next((balance for balance in balances if balance.asset == asset), None)
            if match and match.total > 0:
                return match.total, "exchange_balance"
    except Exception:
        return settings.paper_starting_equity, "fallback_paper_starting_equity"
    return settings.paper_starting_equity, "fallback_paper_starting_equity"


async def daily_pnl_snapshot(
    db: AsyncSession,
    settings: Settings,
    adapter: ExchangeAdapter | None,
    *,
    account_balance: float | None = None,
    exchange_positions: list[Position] | None = None,
    now: datetime | None = None,
) -> DailyPnlSnapshot:
    day_start = utc_day_start(now)
    if account_balance is None:
        account_balance, equity_source = await account_balance_basis(settings, adapter)
    else:
        equity_source = "provided_balance"

    positions = exchange_positions or []
    if adapter is not None and exchange_positions is None and settings.trading_mode != TradingMode.PAPER:
        try:
            positions = await adapter.fetch_positions()
        except Exception:
            positions = []

    active_result = await db.execute(select(Trade).where(Trade.status.in_(["open", "pending"])))
    active_trades = list(active_result.scalars().all())
    daily_result = await db.execute(
        select(Trade).where(or_(Trade.opened_at >= day_start, Trade.closed_at >= day_start))
    )
    daily_trades = list(daily_result.scalars().all())

    active_db_symbols = {trade.symbol for trade in active_trades}
    open_db_trades = [trade for trade in active_trades if trade.status == "open"]
    exchange_pnl_by_symbol = {position.symbol: position.unrealized_pnl for position in positions}
    exchange_only_positions = [position for position in positions if position.symbol not in active_db_symbols]

    realized_pnl = sum(float(trade.realized_pnl or 0.0) for trade in daily_trades)
    db_unrealized = sum(exchange_pnl_by_symbol.get(trade.symbol, trade.unrealized_pnl) for trade in open_db_trades)
    exchange_unrealized = sum(position.unrealized_pnl for position in exchange_only_positions)
    unrealized_pnl = db_unrealized + exchange_unrealized
    daily_pnl = realized_pnl + unrealized_pnl
    base_equity = max(account_balance, 1.0)
    daily_pnl_pct = daily_pnl / base_equity * 100
    account_equity = (
        settings.paper_starting_equity + daily_pnl
        if settings.trading_mode == TradingMode.PAPER
        else account_balance + unrealized_pnl
    )
    open_trade_count = len(active_db_symbols | {position.symbol for position in positions})

    return DailyPnlSnapshot(
        day_start=day_start,
        account_balance=account_balance,
        account_equity=account_equity,
        equity_source=equity_source,
        realized_pnl=realized_pnl,
        unrealized_pnl=unrealized_pnl,
        daily_pnl=daily_pnl,
        daily_pnl_pct=daily_pnl_pct,
        open_trade_count=open_trade_count,
    )


async def daily_trade_count(db: AsyncSession, day_start: datetime | None = None) -> int:
    start = day_start or utc_day_start()
    result = await db.execute(select(func.count()).select_from(Trade).where(Trade.opened_at >= start))
    return int(result.scalar_one() or 0)


async def consecutive_loss_count(
    db: AsyncSession,
    limit: int = 20,
    since: datetime | None = None,
    min_abs_pnl: float = 0.01,
    strategy_only: bool = True,
) -> int:
    query = select(Trade).where(Trade.status == "closed", Trade.closed_at.is_not(None))
    if since is not None:
        query = query.where(Trade.closed_at >= since)
    result = await db.execute(query.order_by(Trade.closed_at.desc()).limit(max(limit * 5, limit)))
    losses = 0
    considered = 0
    for trade in result.scalars().all():
        if strategy_only and not trade_counts_for_loss_streak(trade):
            continue
        pnl = float(trade.realized_pnl or 0.0)
        if abs(pnl) < min_abs_pnl:
            continue
        considered += 1
        if pnl < 0:
            losses += 1
            if considered >= limit:
                break
            continue
        break
    return losses


def trade_counts_for_loss_streak(trade: Trade) -> bool:
    """Return whether a closed trade is allowed to reset or extend the strategy loss streak."""
    extra = trade.extra or {}
    if extra.get("signal_id") or extra.get("setup_score") is not None:
        return True
    if trade.setup_name == "Exchange reconciled position":
        return False
    if extra.get("reconciled_orphan_position"):
        return False
    if extra.get("entry_session") == "unknown" and extra.get("entry_regime") == "unknown":
        return False
    return True
