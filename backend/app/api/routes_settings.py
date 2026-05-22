from __future__ import annotations

from fastapi import APIRouter

from app.config.settings import get_settings

router = APIRouter(prefix="/api", tags=["settings"])


@router.get("/settings")
async def settings() -> dict:
    current = get_settings()
    safe = current.model_dump()
    for secret in ["binance_api_key", "binance_api_secret", "bybit_api_key", "bybit_api_secret", "telegram_bot_token"]:
        safe[secret] = "***configured***" if safe.get(secret) else None
    return safe
