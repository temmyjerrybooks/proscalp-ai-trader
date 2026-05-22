from __future__ import annotations

from fastapi import APIRouter

from app.config.settings import get_settings
from app.strategies import default_strategies

router = APIRouter(prefix="/api/strategies", tags=["strategies"])


@router.get("")
async def strategies() -> list[dict]:
    settings = get_settings()
    return [
        {
            "name": strategy.name,
            "enabled": True,
            "score_threshold": settings.normal_score_threshold_c,
            "session_rules": [
                f"Normal session {settings.normal_score_threshold_c}+",
                f"Off-session {settings.off_session_score_threshold_c}+",
            ],
        }
        for strategy in default_strategies()
    ]
