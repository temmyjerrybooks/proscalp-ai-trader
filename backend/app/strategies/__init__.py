"""Strategy implementations."""

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
    return [
        LondonBreakoutStrategy(),
        AsiaContinuationStrategy(),
        USOpenBreakoutStrategy(),
        VWAPReclaimStrategy(),
        EMAPullbackStrategy(),
        BreakoutRetestStrategy(),
        LiquiditySweepStrategy(),
        MomentumContinuationStrategy(),
        RangeBounceStrategy(),
        BTCLedAltcoinContinuationStrategy(),
    ]
