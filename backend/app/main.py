from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import (
    routes_alerts,
    routes_backtesting,
    routes_bot,
    routes_dashboard,
    routes_exchange,
    routes_performance,
    routes_risk,
    routes_scanner,
    routes_sessions,
    routes_settings,
    routes_signals,
    routes_strategies,
    routes_trades,
)
from app.config.logging_config import configure_logging
from app.config.settings import get_settings
from app.database.db import AsyncSessionLocal, init_db
from app.services.healthcheck import HealthcheckService

settings = get_settings()
configure_logging(settings.log_level)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(routes_dashboard.router)
app.include_router(routes_scanner.router)
app.include_router(routes_trades.router)
app.include_router(routes_signals.router)
app.include_router(routes_risk.router)
app.include_router(routes_settings.router)
app.include_router(routes_strategies.router)
app.include_router(routes_sessions.router)
app.include_router(routes_performance.router)
app.include_router(routes_exchange.router)
app.include_router(routes_alerts.router)
app.include_router(routes_bot.router)
app.include_router(routes_backtesting.router)


@app.get("/health")
async def health() -> dict:
    async with AsyncSessionLocal() as db:
        status = await HealthcheckService().check(db)
    return asdict(status)


@app.get("/")
async def root() -> dict:
    return {"name": settings.app_name, "mode": settings.trading_mode.value, "health": "/health"}
