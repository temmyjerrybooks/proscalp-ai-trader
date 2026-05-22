from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.database.db import get_db
from app.database.models import PerformanceMetric, Trade

router = APIRouter(prefix="/api", tags=["performance"])


@router.get("/performance")
async def performance(db: AsyncSession = Depends(get_db)) -> dict:
    closed_result = await db.execute(
        select(Trade).where(Trade.status == "closed").order_by(Trade.closed_at, Trade.opened_at).limit(5000)
    )
    closed_trades = list(closed_result.scalars().all())
    if closed_trades:
        return _performance_from_trades(closed_trades, starting_equity=get_settings().paper_starting_equity)

    result = await db.execute(select(PerformanceMetric).order_by(desc(PerformanceMetric.metric_date)).limit(1))
    metric = result.scalar_one_or_none()
    if metric:
        return {
            "win_rate": metric.win_rate,
            "profit_factor": metric.profit_factor,
            "average_win": metric.average_win,
            "average_loss": metric.average_loss,
            "max_drawdown": metric.max_drawdown,
            "best_strategy": metric.payload.get("best_strategy", "unknown"),
            "worst_strategy": metric.payload.get("worst_strategy", "unknown"),
            "pnl_chart": metric.payload.get("equity_curve", []),
            "total_trades": metric.payload.get("total_trades", 0),
            "total_pnl": metric.pnl,
        }
    return {
        "win_rate": 0,
        "profit_factor": 0,
        "average_win": 0,
        "average_loss": 0,
        "max_drawdown": 0,
        "best_strategy": "pending",
        "worst_strategy": "pending",
        "pnl_chart": [10_000, 10_000],
        "total_trades": 0,
        "total_pnl": 0,
    }


def _performance_from_trades(trades: list[Trade], starting_equity: float = 10_000.0) -> dict:
    pnl_values = [float(trade.realized_pnl or 0.0) for trade in trades]
    wins = [pnl for pnl in pnl_values if pnl > 0]
    losses = [pnl for pnl in pnl_values if pnl < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    equity = starting_equity
    peak = equity
    max_drawdown = 0.0
    equity_curve = [round(equity, 2)]
    strategy_pnl: dict[str, float] = {}

    for trade, pnl in zip(trades, pnl_values):
        equity += pnl
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - equity) / peak * 100)
        equity_curve.append(round(equity, 2))
        strategy_pnl[trade.setup_name] = strategy_pnl.get(trade.setup_name, 0.0) + pnl

    best_strategy = max(strategy_pnl, key=strategy_pnl.get) if strategy_pnl else "pending"
    worst_strategy = min(strategy_pnl, key=strategy_pnl.get) if strategy_pnl else "pending"
    profit_factor = gross_win / gross_loss if gross_loss > 0 else gross_win if gross_win > 0 else 0.0

    return {
        "win_rate": round(len(wins) / max(1, len(trades)) * 100, 2),
        "profit_factor": round(profit_factor, 3),
        "average_win": round(gross_win / max(1, len(wins)), 4),
        "average_loss": round(sum(losses) / max(1, len(losses)), 4),
        "max_drawdown": round(max_drawdown, 2),
        "best_strategy": best_strategy,
        "worst_strategy": worst_strategy,
        "pnl_chart": equity_curve,
        "total_trades": len(trades),
        "total_pnl": round(sum(pnl_values), 4),
    }
