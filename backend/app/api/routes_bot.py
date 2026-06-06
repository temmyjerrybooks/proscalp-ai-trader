from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter
from pydantic import BaseModel

from app.signal_engines import available_engines
from app.services.bot_runner import bot_runner

router = APIRouter(prefix="/api/bot", tags=["bot"])


class SignalEngineRequest(BaseModel):
    mode: str


@router.get("/status")
async def bot_status() -> dict:
    return asdict(bot_runner.status)


@router.post("/start")
async def start_bot() -> dict:
    return asdict(await bot_runner.start())


@router.post("/stop")
async def stop_bot() -> dict:
    return asdict(await bot_runner.stop())


@router.post("/emergency-stop")
async def emergency_stop() -> dict:
    return asdict(await bot_runner.emergency_stop())


@router.get("/signal-engines")
async def list_signal_engines() -> dict:
    """Available signal engines (for the UI dropdown) + the currently active mode."""
    return {
        "active": bot_runner.signal_engine_mode(),
        "engines": available_engines(),
    }


@router.post("/signal-engine")
async def set_signal_engine(request: SignalEngineRequest) -> dict:
    """Switch the active signal engine (in-memory; effective next cycle).

    Returns the resolved active mode — which may differ from the requested mode if
    an unknown name was given (the registry falls back to 'classic').
    """
    active = bot_runner.set_signal_engine(request.mode)
    return {"requested": request.mode, "active": active, "engines": available_engines()}
