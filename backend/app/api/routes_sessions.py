from __future__ import annotations

from fastapi import APIRouter

from app.config.settings import get_settings
from app.sessions.session_manager import SessionManager

router = APIRouter(prefix="/api", tags=["sessions"])


@router.get("/sessions")
async def sessions() -> list[dict]:
    manager = SessionManager(get_settings())
    return [
        {
            "name": session.name,
            "active": session.active,
            "tradable": session.tradable,
            "aggression_mode": session.aggression_mode,
            "start_utc": session.start_utc,
            "end_utc": session.end_utc,
            "user_time": session.user_time,
            "session_high": None,
            "session_low": None,
            "notes": session.notes,
        }
        for session in manager.current_sessions(regime="good")
    ]
