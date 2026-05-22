from __future__ import annotations

from enum import Enum
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ExchangeName(str, Enum):
    BINANCE = "binance"
    BYBIT = "bybit"


class TradingMode(str, Enum):
    PAPER = "paper"
    TESTNET = "testnet"
    LIVE_SPOT = "live_spot"
    LIVE_FUTURES = "live_futures"


class Settings(BaseSettings):
    """Application settings loaded from environment variables.

    The defaults intentionally keep the bot in paper mode. Live trading requires
    multiple explicit flags so a deployment cannot accidentally place real orders.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "ProScalp AI Trader"
    environment: str = "local"
    log_level: str = "INFO"
    database_url: str = "sqlite+aiosqlite:///./data/proscalp.db"
    cors_origins: str = "http://localhost:5173,http://localhost:8080"

    user_timezone: str = "Africa/Lagos"
    exchange: ExchangeName = ExchangeName.BINANCE
    trading_mode: TradingMode = TradingMode.PAPER
    market_type: Literal["spot", "futures"] = "futures"

    live_trading_enabled: bool = False
    futures_trading_confirmed: bool = False
    paper_starting_equity: float = 10_000.0
    quote_asset: str = "USDT"

    binance_api_key: str | None = None
    binance_api_secret: str | None = None
    binance_spot_testnet_base_url: str = "https://testnet.binance.vision"
    binance_futures_testnet_base_url: str = "https://demo-fapi.binance.com"
    binance_futures_legacy_testnet_base_url: str = "https://testnet.binancefuture.com"
    bybit_api_key: str | None = None
    bybit_api_secret: str | None = None

    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

    top_coin_limit: int = 50
    min_24h_quote_volume: float = 25_000_000.0
    max_spread_bps: float = 8.0
    min_order_book_depth_usdt: float = 250_000.0
    min_volatility_pct: float = 0.25
    max_volatility_pct: float = 18.0

    max_concurrent_trades: int = 5
    min_concurrent_trades: int = 3
    max_trades_per_day: int = 50
    normal_day_trade_goal_low: int = 15
    normal_day_trade_goal_high: int = 30
    max_trades_per_coin: int = 1

    daily_soft_profit_target_pct: float = 3.0
    daily_strong_profit_target_pct: float = 5.0
    daily_profit_target_pct: float = 10.0
    daily_hard_loss_limit_pct: float = -4.0
    daily_warning_loss_pct: float = -1.5
    daily_reduce_size_loss_pct: float = -2.0
    daily_aplus_only_loss_pct: float = -3.0
    consecutive_loss_reduce_after: int = 2
    consecutive_loss_pause_after: int = 3
    consecutive_loss_stop_after: int = 4
    consecutive_loss_stop_scope: Literal["session", "day"] = "day"
    loss_cooldown_minutes: int = 45
    loss_streak_min_abs_pnl: float = 0.01

    normal_risk_aplus_min_pct: float = 0.40
    normal_risk_aplus_max_pct: float = 0.50
    normal_risk_a_min_pct: float = 0.30
    normal_risk_a_max_pct: float = 0.40
    normal_risk_b_min_pct: float = 0.18
    normal_risk_b_max_pct: float = 0.29
    normal_risk_c_min_pct: float = 0.08
    normal_risk_c_max_pct: float = 0.15

    off_session_risk_aplus_min_pct: float = 0.35
    off_session_risk_aplus_max_pct: float = 0.45
    off_session_risk_a_min_pct: float = 0.25
    off_session_risk_a_max_pct: float = 0.35
    off_session_risk_b_min_pct: float = 0.15
    off_session_risk_b_max_pct: float = 0.25
    off_session_risk_c_min_pct: float = 0.05
    off_session_risk_c_max_pct: float = 0.12

    default_leverage: int = 2
    max_leverage: int = 5
    max_total_exposure_pct: float = 35.0
    max_total_exposure_strong_pct: float = 45.0
    max_total_exposure_hot_pct: float = 50.0
    max_coin_exposure_pct: float = 10.0
    max_session_exposure_pct: float = 22.0
    max_session_exposure_off_session_pct: float = 25.0
    max_session_exposure_normal_pct: float = 35.0
    max_session_exposure_strong_pct: float = 40.0
    max_session_exposure_hot_pct: float = 50.0
    max_open_risk_pct: float = 1.5
    max_open_risk_strong_pct: float = 2.0
    max_open_risk_hot_pct: float = 2.5
    max_open_risk_off_session_pct: float = 1.25
    min_risk_reward: float = 1.2
    break_even_trigger_r: float = 0.8
    trade_stagnation_minutes: int = 45

    normal_score_threshold_c: int = 55
    normal_score_threshold_b: int = 65
    normal_score_threshold_a: int = 75
    normal_score_threshold_aplus: int = 85
    off_session_trading_enabled: bool = True
    off_session_score_threshold_c: int = 60
    off_session_score_threshold_b: int = 70
    off_session_score_threshold_a: int = 80
    off_session_score_threshold_aplus: int = 90
    off_session_timing_score: float = 65.0
    allow_unclear_regime_trading: bool = False

    # Phase 2A — strategy enablement (disabled setups are dropped from the live
    # registry and self-reject in evaluate(); see docs/baseline-pre-phase-2a.md).
    ema_pullback_enabled: bool = False
    london_open_breakout_enabled: bool = False

    asia_session_start_utc: str = "00:00"
    asia_session_end_utc: str = "04:00"
    london_session_start_utc: str = "07:00"
    london_session_end_utc: str = "10:30"
    new_york_session_start_utc: str = "13:30"
    new_york_session_end_utc: str = "16:30"

    order_timeout_seconds: int = 20
    market_order_min_score: int = 88
    scalp_limit_time_in_force: Literal["GTC", "IOC", "FOK"] = "IOC"
    aggressive_limit_slippage_bps: float = 2.5
    max_signal_price_drift_bps: float = 12.0
    fee_rate_bps: float = 6.0
    slippage_bps: float = 4.0
    autonomous_trading_enabled: bool = True
    bot_loop_interval_seconds: int = 10
    bot_cycle_symbol_limit: int = 25
    bot_max_orders_per_cycle: int = 1
    bot_min_seconds_between_orders: int = 30
    signal_followup_enabled: bool = True
    signal_followup_timeframe: str = "1m"
    signal_followup_candle_limit: int = 500
    signal_followup_window_minutes: int = 180
    signal_followup_min_move_pct: float = 0.15
    signal_followup_cache_seconds: int = 30

    @field_validator("max_concurrent_trades")
    @classmethod
    def clamp_concurrency(cls, value: int) -> int:
        return max(1, min(value, 5))

    @field_validator("max_leverage")
    @classmethod
    def clamp_max_leverage(cls, value: int) -> int:
        return max(1, min(value, 20))

    @property
    def is_live_mode(self) -> bool:
        return self.trading_mode in {TradingMode.LIVE_SPOT, TradingMode.LIVE_FUTURES}

    @property
    def is_futures_mode(self) -> bool:
        return self.trading_mode == TradingMode.LIVE_FUTURES or self.market_type == "futures"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
