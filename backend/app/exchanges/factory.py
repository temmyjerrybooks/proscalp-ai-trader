from __future__ import annotations

from app.config.settings import ExchangeName, Settings, get_settings
from app.exchanges.base import ExchangeAdapter
from app.exchanges.binance_adapter import BinanceAdapter
from app.exchanges.bybit_adapter import BybitAdapter


def create_exchange_adapter(settings: Settings | None = None) -> ExchangeAdapter:
    settings = settings or get_settings()
    if settings.exchange == ExchangeName.BYBIT:
        return BybitAdapter(settings)
    return BinanceAdapter(settings)
