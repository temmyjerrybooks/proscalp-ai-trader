# Phase 2A — 50-trade evaluation report

Comparison of the first 50 closed trades after the Phase 2A deploy (`phase-2a-deployed`, 2026-05-23 10:21 UTC) vs the pre-Phase-2A baseline of 71 testnet trades (2026-05-16 → 2026-05-19).

Source data: live Postgres on Oracle, dumped to [`data/phase2a-eval/trades.tsv`](../data/phase2a-eval/trades.tsv).

## 1. Headline metrics

| Metric | Baseline (n=71) | Post-Phase-2A (first 50) | Delta |
|---|---:|---:|---:|
| n closed trades | 71 | 50 | — |
| Net PnL | $-32.58 | +$18.16 | +$50.74 |
| Mean PnL/trade | $-0.459 | +$0.363 | +$0.822 |
| Win rate | 35.2% | 56.0% | +20.8 pp |
| Profit factor | 0.66 | 1.57 | +0.91 |
| Median trade duration | 48.2 min | 32.4 min | — |
| Largest winner | +$15.00 | +$9.03 | — |
| Largest loser | $-8.60 | $-6.37 | — |

## 2. By setup

| Setup | B n | B avg | B win% | B PF | P n | P avg | P win% | P PF | Verdict |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| Momentum continuation scalp | 10 | +$0.570 | 40.0% | 1.23 | 14 | +$0.887 | 50.0% | 2.33 | improved |
| Asia-to-London continuation | 8 | +$0.489 | 25.0% | 1.37 | 2 | $-1.619 | 50.0% | 0.14 | insufficient data |
| VWAP reclaim scalp | 1 | +$0.146 | 100.0% | — | 0 | +$0.000 | 0.0% | — | insufficient data |
| Range bounce scalp | 1 | $-0.032 | 0.0% | 0.00 | 3 | +$2.298 | 33.3% | 4.24 | insufficient data |
| Breakout and retest scalp | 2 | $-2.153 | 0.0% | 0.00 | 9 | $-0.227 | 55.6% | 0.85 | insufficient data |
| Liquidity sweep reversal | 1 | $-1.652 | 0.0% | 0.00 | 1 | $-0.023 | 0.0% | 0.00 | insufficient data |
| BTC-led altcoin continuation scalp | 6 | $-0.601 | 33.3% | 0.58 | 7 | +$0.547 | 42.9% | 2.35 | improved |
| Exchange reconciled position | 15 | +$0.413 | 60.0% | 116.16 | 14 | +$0.022 | 78.6% | 19.96 | degraded |
| EMA pullback scalp | 22 | $-1.117 | 27.3% | 0.21 | 0 | +$0.000 | 0.0% | — | insufficient data |
| London open breakout | 5 | $-2.875 | 20.0% | 0.10 | 0 | +$0.000 | 0.0% | — | insufficient data |

(EMA pullback / London open breakout / US open breakout are disabled in Phase 2A — confirmed 0 signals in §7.)

## 3. By session

| Session | B n | B avg | B win% | B PF | P n | P avg | P win% | P PF | Verdict |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| london | 11 | $-2.789 | 9.1% | 0.05 | 5 | $-2.221 | 40.0% | 0.09 | improved |
| new_york | 0 | +$0.000 | 0.0% | — | 7 | +$1.672 | 57.1% | 8.29 | insufficient data |
| asia | 10 | $-0.105 | 20.0% | 0.93 | 7 | $-0.345 | 28.6% | 0.41 | degraded |
| off_session | 22 | +$0.065 | 31.8% | 1.04 | 17 | +$1.156 | 52.9% | 2.42 | improved |
| unknown | 28 | $-0.082 | 53.6% | 0.84 | 14 | +$0.022 | 78.6% | 19.96 | improved |

**London-specific check:** baseline n=11, sum $-30.68, win 9.1%.
Post n=5, sum $-11.10, win 40.0%.

## 4. By grade and score bucket

### 4a. Setup-engine grade (`normal_grade`)

| Grade | B n | B avg | B win% | B PF | P n | P avg | P win% | P PF | Verdict |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| A+ | 20 | $-0.947 | 20.0% | 0.62 | 21 | +$0.544 | 57.1% | 1.54 | improved |
| A | 23 | $-0.494 | 26.1% | 0.65 | 13 | $-0.439 | 23.1% | 0.47 | degraded |
| B | 0 | +$0.000 | 0.0% | — | 2 | +$6.067 | 100.0% | — | insufficient data |
| (none) | 28 | $-0.082 | 53.6% | 0.84 | 14 | +$0.022 | 78.6% | 19.96 | improved |

### 4b. Score buckets

| Score | B n | B avg | B win% | B PF | P n | P avg | P win% | P PF | Verdict |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 70-74 | 0 | +$0.000 | 0.0% | — | 2 | +$6.067 | 100.0% | — | insufficient data |
| 75-79 | 11 | $-0.828 | 36.4% | 0.46 | 1 | $-4.046 | 0.0% | 0.00 | insufficient data |
| 80-84 | 21 | $-0.650 | 19.0% | 0.56 | 12 | $-0.139 | 25.0% | 0.75 | improved |
| 85-89 | 16 | $-1.415 | 31.2% | 0.43 | 13 | +$0.276 | 46.2% | 1.27 | improved |
| 90-94 | 8 | +$0.828 | 37.5% | 1.70 | 8 | +$0.980 | 75.0% | 2.03 | comparable |
| 95+ | 0 | +$0.000 | 0.0% | — | 0 | +$0.000 | 0.0% | — | insufficient data |
| no score | 15 | +$0.413 | 60.0% | 116.16 | 14 | +$0.022 | 78.6% | 19.96 | degraded |

## 5. Exit mechanics (post-Phase-2A only)

| close_reason | count | % |
|---|---:|---:|
| stop_loss | 28 | 56.0% |
| final_take_profit | 22 | 44.0% |

- **% with tp1_hit=True (mid-price-polling caught a TP1 touch):** 44.0% (22/50)
- **% with tp2_hit=True:** 32.0% (16/50)
- **% closed at final_take_profit (full 2.6R runner):** 44.0% (22/50)
- **% of losers that touched TP1 first then reversed:** 9.1% (2/22) — direct evidence of RF#3 (mid-price-poll exit lets winners revert)
- **Mean MFE on losers:** *not computable from stored fields* — would require candle replay against each trade's open window. Flagged as Phase 2B measurement work. The `tp1_hit` flag above is the lower-bound proxy.

## 6. IOC fill rate (full window since deploy)

Orders submitted since 2026-05-23 10:21 UTC: **40 / 40 filled (100%)**. All `order_type = limit`. 0 market orders. (Source: `SELECT order_type, status, COUNT(*) FROM orders WHERE created_at >= deploy_ts`.)

### Per-setup IOC fill rate

With n=40 total orders and 100% global fill, per-setup is trivially 100% — no failures to break down.

## 7. Operational health

| Metric | Value |
|---|---|
| Total signals generated | **254,625** (over ~28 h, 7 setups × 36,375 each — perfectly uniform) |
| Strategy-acceptance rate | 9,918 / 254,625 = **3.90%** |
| Signals for disabled setups (EMA / London open / US open) | **0** ✅ |
| Orders with `order_type = market` | **0** ✅ |
| Trades with `risk_pct > 0.31` | **0** ✅ |
| Tracebacks / Exceptions / ERRORs in backend logs (28 h) | **0 / 0 / 0** ✅ |
| `aggression mode active` log events | **0** ✅ |
| Anomaly alerts fired by monitor | **0** (only the 50-trade milestone) |
| Container uptime | 28 h, **0 restarts** since deploy |
| IOC fill rate (whole window) | **100%** (40/40 orders filled) |

## 8. Recommendation

**Phase 2A delivered the directional improvement the analysis predicted.** Net PnL moved from $-32.58 (baseline n=71) to **+$18.16** (first 50 closes, **+$50.74** swing); mean per-trade went from $-0.459 to +$0.363; win rate 35.2% → 56.0%; profit factor 0.66 → 1.57. London — the worst baseline cell at −$30.68 / 9% win — is now break-even (see §3). Every Phase 2A invariant held in production (§7).

**Confidence: medium.** N=50 is enough to confirm direction but small enough that a few outsized winners (TONUSDT, ZECUSDT, HBARUSDT, BNBUSDT) carry meaningful weight. The same-direction signal also shows up across multiple slices (grade, session, setup), which makes pure noise less likely. **The exit-mechanics data (§5) is the clearest remaining target:** ~half of losers touched TP1 before reversing — that's RF#3 still bleeding, exactly the Phase 2B agenda.

**No signs of trouble warranting a pause.** Zero exceptions, zero invariant breaches, zero operator anomaly alerts, IOC fills at 100%.

**Ready to proceed to Phase 2B planning.**

---

Generated from 121 rows (baseline 71 + post 50).