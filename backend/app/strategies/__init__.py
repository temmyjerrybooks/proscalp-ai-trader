"""Strategy implementations."""

from app.config.settings import get_settings
from app.strategies.asia_continuation import AsiaContinuationStrategy
from app.strategies.btc_led_altcoin import BTCLedAltcoinContinuationStrategy
from app.strategies.breakout_retest import BreakoutRetestStrategy
from app.strategies.ema_pullback import EMAPullbackStrategy
from app.strategies.liquidity_sweep import LiquiditySweepStrategy
from app.strategies.london_breakout import LondonBreakoutStrategy
from app.strategies.momentum_continuation import MomentumContinuationStrategy
from app.strategies.range_bounce import RangeBounceStrategy
from app.strategies.us_open_breakout import USOpenBreakoutStrategy
from app.strategies.vwap_reclaim import VWAPReclaimStrategy


def default_strategies():
    """Active strategies for the live bot.

    Strategies disabled via settings flags (Phase 2A) are dropped from this
    registry so they are never evaluated or persisted. They remain importable
    for backtesting/tests via direct construction with enabled=True.
    """
    settings = get_settings()
    strategies = [
        AsiaContinuationStrategy(),
        USOpenBreakoutStrategy(),
        VWAPReclaimStrategy(),
        BreakoutRetestStrategy(),
        LiquiditySweepStrategy(),
        MomentumContinuationStrategy(),
        RangeBounceStrategy(),
        BTCLedAltcoinContinuationStrategy(),
    ]
    if settings.london_open_breakout_enabled:
        strategies.insert(0, LondonBreakoutStrategy())
    if settings.ema_pullback_enabled:
        strategies.append(EMAPullbackStrategy())
    return strategies
