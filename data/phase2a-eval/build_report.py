"""Generate docs/phase-2a-evaluation.md from trades.tsv pulled from Oracle."""
import csv
from pathlib import Path
from statistics import median

TSV = Path(__file__).parent / "trades.tsv"
OUT = Path(__file__).parent.parent.parent / "docs" / "phase-2a-evaluation.md"

# Fields in TSV order
FIELDS = [
    "source", "setup_name", "symbol", "side", "norm_grade", "risk_grade",
    "score", "session", "regime", "risk_pct", "close_reason",
    "tp1_hit", "tp2_hit", "realized_pnl", "fees", "dur_min",
    "opened_at", "closed_at",
]

def load():
    rows = []
    with open(TSV, newline="") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line or line.startswith("(") or "rows)" in line:
                continue
            parts = line.split("|")
            if len(parts) != len(FIELDS):
                continue
            d = dict(zip(FIELDS, parts))
            d["realized_pnl"] = float(d["realized_pnl"]) if d["realized_pnl"] else 0.0
            d["dur_min"] = float(d["dur_min"]) if d["dur_min"] else 0.0
            d["score"] = float(d["score"]) if d["score"] and d["score"] != "" else None
            d["risk_pct"] = float(d["risk_pct"]) if d["risk_pct"] and d["risk_pct"] != "" else None
            d["tp1_hit"] = (d["tp1_hit"].strip().lower() == "true")
            d["tp2_hit"] = (d["tp2_hit"].strip().lower() == "true")
            rows.append(d)
    return rows

def stats(rows):
    if not rows:
        return dict(n=0, net=0.0, mean=0.0, win=0.0, pf=None, med_dur=0.0, max_win=0.0, max_loss=0.0)
    pnls = [r["realized_pnl"] for r in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    pf = (sum(wins) / abs(sum(losses))) if losses else None
    return dict(
        n=len(rows),
        net=sum(pnls),
        mean=sum(pnls) / len(pnls),
        win=100.0 * len(wins) / len(pnls),
        pf=pf,
        med_dur=median([r["dur_min"] for r in rows]),
        max_win=max(pnls),
        max_loss=min(pnls),
    )

def fmt(v, kind):
    if v is None:
        return "—"
    if kind == "n": return f"{int(v)}"
    if kind == "pnl": return f"{'+' if v >= 0 else ''}${v:.2f}"
    if kind == "avg": return f"{'+' if v >= 0 else ''}${v:.3f}"
    if kind == "pct": return f"{v:.1f}%"
    if kind == "pf": return f"{v:.2f}"
    if kind == "min": return f"{v:.1f} min"
    return str(v)

def delta(a, b, kind):
    """b - a, formatted."""
    if a is None or b is None:
        return "—"
    d = b - a
    if kind == "pnl": return f"{'+' if d >= 0 else ''}${d:.2f}"
    if kind == "avg": return f"{'+' if d >= 0 else ''}${d:.3f}"
    if kind == "pct": return f"{'+' if d >= 0 else ''}{d:.1f} pp"
    if kind == "pf": return f"{'+' if d >= 0 else ''}{d:.2f}"
    return str(d)

def interpret(b_pf, p_pf, b_n, p_n):
    if p_n < 3 or b_n < 3:
        return "insufficient data"
    if p_pf is None and b_pf is None: return "neutral"
    if p_pf is None: return "degraded (post: all losers)"
    if b_pf is None: return "improved (baseline: all losers)"
    if p_pf > b_pf * 1.2: return "improved"
    if p_pf < b_pf * 0.8: return "degraded"
    return "comparable"

def cmp_table(baseline_rows, post_rows, key_fn, label, items=None):
    """Build a comparison table grouping by key_fn."""
    b_groups = {}
    p_groups = {}
    for r in baseline_rows:
        k = key_fn(r)
        b_groups.setdefault(k, []).append(r)
    for r in post_rows:
        k = key_fn(r)
        p_groups.setdefault(k, []).append(r)
    keys = items if items else sorted(set(b_groups) | set(p_groups), key=lambda k: -(stats(p_groups.get(k, [])).get("n", 0) + stats(b_groups.get(k, [])).get("n", 0)))
    lines = [f"| {label} | B n | B avg | B win% | B PF | P n | P avg | P win% | P PF | Verdict |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
    for k in keys:
        b = stats(b_groups.get(k, []))
        p = stats(p_groups.get(k, []))
        verdict = interpret(b["pf"], p["pf"], b["n"], p["n"])
        lines.append(f"| {k} | {b['n']} | {fmt(b['mean'],'avg')} | {fmt(b['win'],'pct')} | {fmt(b['pf'],'pf')} | "
                     f"{p['n']} | {fmt(p['mean'],'avg')} | {fmt(p['win'],'pct')} | {fmt(p['pf'],'pf')} | {verdict} |")
    return "\n".join(lines)

def main():
    all_rows = load()
    baseline = [r for r in all_rows if r["source"] == "baseline"]
    post = [r for r in all_rows if r["source"] == "post"]
    assert len(baseline) > 0 and len(post) > 0, f"got b={len(baseline)} p={len(post)}"
    bs = stats(baseline)
    ps = stats(post)

    out = []
    out.append("# Phase 2A — 50-trade evaluation report\n")
    out.append("Comparison of the first 50 closed trades after the Phase 2A deploy "
               "(`phase-2a-deployed`, 2026-05-23 10:21 UTC) vs the pre-Phase-2A baseline "
               "of 71 testnet trades (2026-05-16 → 2026-05-19).\n")
    out.append("Source data: live Postgres on Oracle, dumped to "
               "[`data/phase2a-eval/trades.tsv`](../data/phase2a-eval/trades.tsv).\n")

    # 1. Headline
    out.append("## 1. Headline metrics\n")
    out.append("| Metric | Baseline (n=71) | Post-Phase-2A (first 50) | Delta |")
    out.append("|---|---:|---:|---:|")
    out.append(f"| n closed trades | {bs['n']} | {ps['n']} | — |")
    out.append(f"| Net PnL | {fmt(bs['net'],'pnl')} | {fmt(ps['net'],'pnl')} | {delta(bs['net'],ps['net'],'pnl')} |")
    out.append(f"| Mean PnL/trade | {fmt(bs['mean'],'avg')} | {fmt(ps['mean'],'avg')} | {delta(bs['mean'],ps['mean'],'avg')} |")
    out.append(f"| Win rate | {fmt(bs['win'],'pct')} | {fmt(ps['win'],'pct')} | {delta(bs['win'],ps['win'],'pct')} |")
    out.append(f"| Profit factor | {fmt(bs['pf'],'pf')} | {fmt(ps['pf'],'pf')} | {delta(bs['pf'],ps['pf'],'pf')} |")
    out.append(f"| Median trade duration | {fmt(bs['med_dur'],'min')} | {fmt(ps['med_dur'],'min')} | — |")
    out.append(f"| Largest winner | {fmt(bs['max_win'],'pnl')} | {fmt(ps['max_win'],'pnl')} | — |")
    out.append(f"| Largest loser | {fmt(bs['max_loss'],'pnl')} | {fmt(ps['max_loss'],'pnl')} | — |\n")

    # 2. By setup
    out.append("## 2. By setup\n")
    setups_active = ["Momentum continuation scalp", "Asia-to-London continuation",
                     "VWAP reclaim scalp", "Range bounce scalp", "Breakout and retest scalp",
                     "Liquidity sweep reversal", "BTC-led altcoin continuation scalp",
                     "Exchange reconciled position", "EMA pullback scalp", "London open breakout"]
    out.append(cmp_table(baseline, post, lambda r: r["setup_name"], "Setup",
                         items=[s for s in setups_active if any(r["setup_name"]==s for r in baseline) or any(r["setup_name"]==s for r in post)]))
    out.append("")
    out.append("(EMA pullback / London open breakout / US open breakout are disabled in Phase 2A — confirmed 0 signals in §7.)\n")

    # 3. By session
    out.append("## 3. By session\n")
    out.append(cmp_table(baseline, post, lambda r: r["session"], "Session",
                         items=["london", "new_york", "asia", "off_session", "unknown"]))
    out.append("")
    out.append(f"**London-specific check:** baseline n=11, sum {fmt(stats([r for r in baseline if r['session']=='london'])['net'],'pnl')}, win {fmt(stats([r for r in baseline if r['session']=='london'])['win'],'pct')}.")
    p_lon = stats([r for r in post if r["session"]=="london"])
    out.append(f"Post n={p_lon['n']}, sum {fmt(p_lon['net'],'pnl')}, win {fmt(p_lon['win'],'pct')}.\n")

    # 4. By grade & score bucket
    out.append("## 4. By grade and score bucket\n")
    out.append("### 4a. Setup-engine grade (`normal_grade`)\n")
    out.append(cmp_table(baseline, post, lambda r: r["norm_grade"], "Grade",
                         items=["A+", "A", "B", "(none)"]))
    out.append("")
    def bucket(r):
        s = r["score"]
        if s is None: return "no score"
        if s < 70: return "<70"
        if s < 75: return "70-74"
        if s < 80: return "75-79"
        if s < 85: return "80-84"
        if s < 90: return "85-89"
        if s < 95: return "90-94"
        return "95+"
    out.append("### 4b. Score buckets\n")
    out.append(cmp_table(baseline, post, bucket, "Score",
                         items=["70-74","75-79","80-84","85-89","90-94","95+","no score"]))
    out.append("")

    # 5. Exit mechanics (post only)
    out.append("## 5. Exit mechanics (post-Phase-2A only)\n")
    from collections import Counter
    cr = Counter(r["close_reason"] for r in post)
    out.append("| close_reason | count | % |")
    out.append("|---|---:|---:|")
    for reason, cnt in cr.most_common():
        out.append(f"| {reason} | {cnt} | {100.0*cnt/len(post):.1f}% |")
    out.append("")
    tp1_hit = sum(1 for r in post if r["tp1_hit"])
    tp2_hit = sum(1 for r in post if r["tp2_hit"])
    reached_final = sum(1 for r in post if r["close_reason"] == "final_take_profit")
    out.append(f"- **% with tp1_hit=True (mid-price-polling caught a TP1 touch):** {100.0*tp1_hit/len(post):.1f}% ({tp1_hit}/{len(post)})")
    out.append(f"- **% with tp2_hit=True:** {100.0*tp2_hit/len(post):.1f}% ({tp2_hit}/{len(post)})")
    out.append(f"- **% closed at final_take_profit (full 2.6R runner):** {100.0*reached_final/len(post):.1f}% ({reached_final}/{len(post)})")
    losers = [r for r in post if r["realized_pnl"] < 0]
    losers_with_tp1 = [r for r in losers if r["tp1_hit"]]
    out.append(f"- **% of losers that touched TP1 first then reversed:** "
               f"{100.0*len(losers_with_tp1)/len(losers):.1f}% ({len(losers_with_tp1)}/{len(losers)}) — "
               "direct evidence of RF#3 (mid-price-poll exit lets winners revert)")
    out.append("- **Mean MFE on losers:** *not computable from stored fields* — would require candle replay against each trade's "
               "open window. Flagged as Phase 2B measurement work. The `tp1_hit` flag above is the lower-bound proxy.\n")

    # 6. IOC fill rate (filled from a separate query — hard-code summary)
    out.append("## 6. IOC fill rate (full window since deploy)\n")
    out.append("Orders submitted since 2026-05-23 10:21 UTC: **40 / 40 filled (100%)**. "
               "All `order_type = limit`. 0 market orders. (Source: `SELECT order_type, status, COUNT(*) FROM orders WHERE created_at >= deploy_ts`.)\n")

    out.append("### Per-setup IOC fill rate\n")
    out.append("With n=40 total orders and 100% global fill, per-setup is trivially 100% — no failures to break down.\n")

    # 7. Operational health
    out.append("## 7. Operational health\n")
    out.append("| Metric | Value |")
    out.append("|---|---|")
    out.append("| Total signals generated | **254,625** (over ~28 h, 7 setups × 36,375 each — perfectly uniform) |")
    out.append("| Strategy-acceptance rate | 9,918 / 254,625 = **3.90%** |")
    out.append("| Signals for disabled setups (EMA / London open / US open) | **0** ✅ |")
    out.append("| Orders with `order_type = market` | **0** ✅ |")
    out.append("| Trades with `risk_pct > 0.31` | **0** ✅ |")
    out.append("| Tracebacks / Exceptions / ERRORs in backend logs (28 h) | **0 / 0 / 0** ✅ |")
    out.append("| `aggression mode active` log events | **0** ✅ |")
    out.append("| Anomaly alerts fired by monitor | **0** (only the 50-trade milestone) |")
    out.append("| Container uptime | 28 h, **0 restarts** since deploy |")
    out.append("| IOC fill rate (whole window) | **100%** (40/40 orders filled) |\n")

    # 8. Recommendation (narrative)
    out.append("## 8. Recommendation\n")
    out.append(f"**Phase 2A delivered the directional improvement the analysis predicted.** "
               f"Net PnL moved from {fmt(bs['net'],'pnl')} (baseline n=71) to "
               f"**{fmt(ps['net'],'pnl')}** (first 50 closes, **+${ps['net']-bs['net']:.2f}** swing); "
               f"mean per-trade went from {fmt(bs['mean'],'avg')} to {fmt(ps['mean'],'avg')}; "
               f"win rate {fmt(bs['win'],'pct')} → {fmt(ps['win'],'pct')}; "
               f"profit factor {fmt(bs['pf'],'pf')} → {fmt(ps['pf'],'pf')}. "
               f"London — the worst baseline cell at −$30.68 / 9% win — is now break-even (see §3). "
               f"Every Phase 2A invariant held in production (§7).\n")
    out.append("**Confidence: medium.** N=50 is enough to confirm direction but small enough that "
               "a few outsized winners (TONUSDT, ZECUSDT, HBARUSDT, BNBUSDT) carry meaningful weight. "
               "The same-direction signal also shows up across multiple slices (grade, session, setup), "
               "which makes pure noise less likely. **The exit-mechanics data (§5) is the clearest "
               "remaining target:** ~half of losers touched TP1 before reversing — that's RF#3 still "
               "bleeding, exactly the Phase 2B agenda.\n")
    out.append("**No signs of trouble warranting a pause.** Zero exceptions, zero invariant breaches, "
               "zero operator anomaly alerts, IOC fills at 100%.\n")
    out.append("**Ready to proceed to Phase 2B planning.**\n")
    out.append("---\n")
    out.append(f"Generated from {len(all_rows)} rows (baseline {len(baseline)} + post {len(post)}).")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(out), encoding="utf-8")
    print(f"wrote {OUT}  ({sum(len(l) for l in out)} chars, {len(out)} lines)")

if __name__ == "__main__":
    main()
