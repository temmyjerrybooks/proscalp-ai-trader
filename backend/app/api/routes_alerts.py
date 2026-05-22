from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.alerts.telegram import TelegramAlertService
from app.database.db import get_db
from app.database.models import TelegramAlert
from app.services.bot_runner import _safe_error_message

router = APIRouter(prefix="/api/telegram", tags=["telegram"])


@router.get("/test")
async def telegram_test() -> dict:
    result = await TelegramAlertService().test()
    return asdict(result)


@router.get("/alerts")
async def telegram_alerts(db: AsyncSession = Depends(get_db)) -> list[dict]:
    result = await db.execute(select(TelegramAlert).order_by(desc(TelegramAlert.created_at)).limit(100))
    return [
        {
            "id": row.id,
            "alert_type": row.alert_type,
            "message": _safe_error_message(row.message),
            "delivered": row.delivered,
            "error": _safe_error_message(row.error) if row.error else None,
            "created_at": row.created_at,
        }
        for row in result.scalars().all()
    ]
