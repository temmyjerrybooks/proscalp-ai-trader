from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter

from app.services.bot_runner import bot_runner

router = APIRouter(prefix="/api/bot", tags=["bot"])


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
