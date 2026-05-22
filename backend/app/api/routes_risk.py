from __future__ import annotations

from fastapi import APIRouter

from app.config.settings import get_settings
from app.risk.risk_engine import RiskEngine

router = APIRouter(prefix="/api/risk", tags=["risk"])


@router.get("/status")
async def risk_status() -> dict:
    settings = get_settings()
    engine = RiskEngine(settings)
    state = engine.evaluate_daily_state(daily_pnl_pct=0.0, consecutive_losses=0)
    return {
        "daily_loss_limit_pct": settings.daily_hard_loss_limit_pct,
        "risk_per_trade": {
            "A+": [settings.normal_risk_aplus_min_pct, settings.normal_risk_aplus_max_pct],
            "A": [settings.normal_risk_a_min_pct, settings.normal_risk_a_max_pct],
            "B": [settings.normal_risk_b_min_pct, settings.normal_risk_b_max_pct],
            "C": [settings.normal_risk_c_min_pct, settings.normal_risk_c_max_pct],
        },
        "off_session_risk_per_trade": {
            "A+": [settings.off_session_risk_aplus_min_pct, settings.off_session_risk_aplus_max_pct],
            "A": [settings.off_session_risk_a_min_pct, settings.off_session_risk_a_max_pct],
            "B": [settings.off_session_risk_b_min_pct, settings.off_session_risk_b_max_pct],
            "C": [settings.off_session_risk_c_min_pct, settings.off_session_risk_c_max_pct],
        },
        "max_concurrent_trades": settings.max_concurrent_trades,
        "exposure_limits": {
            "total_pct": settings.max_total_exposure_pct,
            "total_strong_pct": settings.max_total_exposure_strong_pct,
            "total_hot_pct": settings.max_total_exposure_hot_pct,
            "coin_pct": settings.max_coin_exposure_pct,
            "session_pct": settings.max_session_exposure_pct,
            "session_off_session_pct": settings.max_session_exposure_off_session_pct,
            "session_normal_pct": settings.max_session_exposure_normal_pct,
            "session_strong_pct": settings.max_session_exposure_strong_pct,
            "session_hot_pct": settings.max_session_exposure_hot_pct,
            "open_risk_pct": settings.max_open_risk_pct,
            "open_risk_off_session_pct": settings.max_open_risk_off_session_pct,
            "open_risk_strong_pct": settings.max_open_risk_strong_pct,
            "open_risk_hot_pct": settings.max_open_risk_hot_pct,
        },
        "trade_score_thresholds": {
            "session": settings.normal_score_threshold_c,
            "off_session": settings.off_session_score_threshold_c,
            "C": settings.normal_score_threshold_c,
            "B": settings.normal_score_threshold_b,
            "A": settings.normal_score_threshold_a,
            "A+": settings.normal_score_threshold_aplus,
            "off_session_C": settings.off_session_score_threshold_c,
            "off_session_B": settings.off_session_score_threshold_b,
            "off_session_A": settings.off_session_score_threshold_a,
            "off_session_A+": settings.off_session_score_threshold_aplus,
        },
        "off_session_trading_enabled": settings.off_session_trading_enabled,
        "allow_unclear_regime_trading": settings.allow_unclear_regime_trading,
        "loss_streak": {
            "reduce_after": settings.consecutive_loss_reduce_after,
            "pause_after": settings.consecutive_loss_pause_after,
            "stop_after": settings.consecutive_loss_stop_after,
            "stop_scope": settings.consecutive_loss_stop_scope,
            "cooldown_minutes": settings.loss_cooldown_minutes,
            "min_abs_pnl": settings.loss_streak_min_abs_pnl,
        },
        "kill_switch": state.hard_shutdown,
        "warnings": state.reasons,
        "live_trading_enabled": settings.live_trading_enabled,
        "futures_confirmed": settings.futures_trading_confirmed,
    }
