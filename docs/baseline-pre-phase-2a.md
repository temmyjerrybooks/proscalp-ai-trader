# Baseline — Pre Phase 2A (real testnet data)

Snapshot of the production **testnet** trading record used as the before-state for
Phase 2A ("stop the bleeding"). Pulled read-only from the Oracle Postgres on
2026-05-22. All figures are **realized** PnL on closed trades (not shadow).

## Headline numbers

| Metric | Value |
|---|---|
| Closed trades | **71** |
| Mode | testnet (all 71) |
| Net realized PnL | **−$32.58** |
| Win rate | **35.2%** |
| Median trade return | −0.21% (mean +0.40%, skewed by one +10% outlier) |
| Median trade duration | 48 min |
| Exit mix | stop_loss 50 (70%), final_take_profit 16 (23%), manual_close_all 4, emergency_stop 1 |

Population context (no realized PnL — distribution only): 514,100 signals,
33,371 scored, 27,182 strategy-accepted in the May 16–19 window. A `Signal` row is
persisted for every strategy-eval every ~10s cycle, so the original 23,889-row
audit CSV was a ~6% early slice, not the full population.

## By setup (realized)

| Setup | n | avg $ | sum $ | win % | profit factor |
|---|---|---|---|---|---|
| Exchange reconciled (orphan, not a strategy) | 15 | +0.41 | +6.19 | 60% | — |
| Momentum continuation | 10 | +0.57 | **+5.70** | 40% | **1.23** |
| Asia-to-London continuation | 8 | +0.49 | **+3.91** | 25% | **1.37** |
| VWAP reclaim | 1 | +0.15 | +0.15 | 100% | — |
| Range bounce | 1 | −0.03 | −0.03 | 0% | — |
| Liquidity sweep | 1 | −1.65 | −1.65 | 0% | — |
| BTC-led altcoin | 6 | −0.60 | −3.61 | 33% | 0.58 |
| Breakout & retest | 2 | −2.15 | −4.31 | 0% | — |
| **London open breakout** | 5 | −2.88 | **−14.37** | 20% | **0.10** |
| **EMA pullback** | 22 | −1.12 | **−24.57** | 27% | **0.21** |

## By grade (inverse predictivity confirmed, monotonic)

| Setup-engine grade | n | avg $ | sum $ | win % |
|---|---|---|---|---|
| (none) | 28 | −0.08 | −2.29 | 53.6% |
| A | 23 | −0.49 | −11.36 | 26.1% |
| A+ | 20 | −0.95 | −18.94 | 20.0% |

## By session (shadow finding inverted on real money)

| Session | n | sum $ | win % |
|---|---|---|---|
| off_session | 22 | +1.43 | 31.8% |
| asia | 10 | −1.05 | 20.0% |
| **london** | 11 | **−30.68** | **9.1%** |

## By regime

| Regime | n | sum $ | win % |
|---|---|---|---|
| strong | 23 | −2.69 | 21.7% |
| unclear | 3 | −6.26 | 0.0% |
| good | 16 | −20.22 | 31.3% |

## Signal accept rates by setup (full 514k population)

| Setup | accept rate |
|---|---|
| EMA pullback | 35.5% (18,252 accepted — 55% of all accepted) |
| Range bounce | 11.0% |
| BTC-led altcoin | 7.1% |
| Momentum continuation | 4.2% |
| Breakout & retest | 2.8% |
| Asia-to-London | 2.5% |
| VWAP reclaim | 0.7% |
| London open breakout | 0.6% |
| Liquidity sweep | 0.4% |
| **US open breakout** | **0.0% (0 of 51,410 — dead)** |

## Phase 2A targets derived from this baseline

1. Disable **EMA pullback** (PF 0.21, dominant negative, 55% of signal volume).
2. Disable **London open breakout** (PF 0.10, not salvageable).
3. Remove **US open breakout** (0 fires — dead code).
4. Cap **score→risk** at 75 (A+ = 20% win, sizes up on losers).
5. Force **IOC limit orders** (kill market routing that pays full slippage on A+).
6. Disable **aggression mode** (London-open aggression is the worst real cell).
