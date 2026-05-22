from __future__ import annotations

import pytest

from app.alerts.telegram import TelegramAlertService
from app.config.settings import Settings


@pytest.mark.asyncio
async def test_telegram_alert_mock_without_credentials():
    service = TelegramAlertService(Settings(telegram_bot_token=None, telegram_chat_id=None))
    result = await service.send("bot_started", "test")

    assert result.delivered is True
    assert result.error is not None
