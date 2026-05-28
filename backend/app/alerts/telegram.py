from __future__ import annotations

from dataclasses import dataclass

import httpx
import structlog

from app.config.settings import Settings, get_settings
from app.database.db import AsyncSessionLocal
from app.database.models import TelegramAlert

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class TelegramAlertResult:
    delivered: bool
    message: str
    error: str | None = None


class TelegramAlertService:
    alert_types = {
        "bot_started",
        "bot_stopped",
        "daily_top50_scan_completed",
        "new_signal_detected",
        "trade_opened",
        "trade_partially_closed",
        "trade_closed",
        "stop_loss_hit",
        "take_profit_hit",
        "daily_profit_target_reached",
        "daily_loss_warning",
        "daily_hard_loss_shutdown",
        "exchange_api_error",
        "order_failed",
        "emergency_shutdown",
        "session_aggression_mode_activated",
        "session_ended",
        "backtest_completed",
        "protective_orders_circuit_breaker",
    }

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def send(self, alert_type: str, message: str) -> TelegramAlertResult:
        if alert_type not in self.alert_types:
            result = TelegramAlertResult(False, message, f"unsupported alert type: {alert_type}")
            await self._persist(alert_type, result)
            return result
        if not self.settings.telegram_bot_token or not self.settings.telegram_chat_id:
            logger.info("telegram_alert_mocked", alert_type=alert_type, message=message)
            result = TelegramAlertResult(True, message, "telegram credentials not configured; mocked")
            await self._persist(alert_type, result)
            return result
        url = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/sendMessage"
        payload = {"chat_id": self.settings.telegram_chat_id, "text": message}
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
            logger.info("telegram_alert_sent", alert_type=alert_type)
            result = TelegramAlertResult(True, message)
            await self._persist(alert_type, result)
            return result
        except Exception as exc:
            logger.error("telegram_alert_failed", alert_type=alert_type, error=str(exc))
            result = TelegramAlertResult(False, message, str(exc))
            await self._persist(alert_type, result)
            return result

    async def test(self) -> TelegramAlertResult:
        return await self.send("bot_started", "ProScalp AI Trader Telegram test alert")

    async def _persist(self, alert_type: str, result: TelegramAlertResult) -> None:
        try:
            async with AsyncSessionLocal() as db:
                db.add(
                    TelegramAlert(
                        alert_type=alert_type,
                        message=result.message,
                        delivered=result.delivered,
                        error=result.error,
                    )
                )
                await db.commit()
        except Exception as exc:  # pragma: no cover - alert persistence must not block trading
            logger.warning("telegram_alert_persist_failed", alert_type=alert_type, error=str(exc))
