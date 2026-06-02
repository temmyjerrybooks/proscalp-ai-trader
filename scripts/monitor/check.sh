#!/bin/bash
# Phase 2A passive monitor — runs every 15 min via cron on Oracle.
# Alerts go to Telegram (creds read from /opt/proscalp-ai-trader/.env).
# Never prints secret values.
set -e
ENV_FILE=/opt/proscalp-ai-trader/.env
START_TS_FILE=/opt/proscalp-snapshots/phase2a-bot-start-ts.txt
STATE_DIR=/opt/proscalp-monitor/state
ALERTS_FILE=/opt/proscalp-monitor/alerts.log
CHECK_LOG=/opt/proscalp-monitor/check.log
LAST_CLOSED_FILE="$STATE_DIR/last_closed_count"
DEDUP_FILE="$STATE_DIR/alert_dedup"
# Phase 2B/2C ladder-aware additions:
DELIVERY_LOG=/opt/proscalp-monitor/delivery.log          # verified Telegram delivery log
LADDER_CLOCK_FILE=/opt/proscalp-snapshots/ladder-clock-start-ts.txt  # operator sets when the 120-clock starts
LAST_LADDER_CLOSED_FILE="$STATE_DIR/last_ladder_closed_count"

mkdir -p "$STATE_DIR"
touch "$DEDUP_FILE"

# Load Telegram creds without echoing.
if [ -f "$ENV_FILE" ]; then
  set -a; . "$ENV_FILE"; set +a
fi

START_TS=$(cat "$START_TS_FILE" 2>/dev/null || echo "")
NOW=$(date -u +%FT%TZ)

# Dedup: don't re-send the same alert within the same hour.
send_alert() {
  local msg="$1"
  local key="$2"
  local sig="${key}|$(date -u +%Y%m%d%H)"
  if grep -qF "$sig" "$DEDUP_FILE" 2>/dev/null; then
    return
  fi
  tail -100 "$DEDUP_FILE" > "${DEDUP_FILE}.tmp" 2>/dev/null || true
  mv "${DEDUP_FILE}.tmp" "$DEDUP_FILE" 2>/dev/null || true
  echo "$sig" >> "$DEDUP_FILE"
  echo "[$NOW] $msg" >> "$ALERTS_FILE"
  if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
    # Verify delivery (was fire-and-forget -o /dev/null): capture body + HTTP code,
    # require Telegram "ok":true AND http 200, log success/failure. Never logs the
    # token (the URL is not echoed; only the response body, which has no secret).
    local resp http
    resp=$(curl -sS -m 15 -w $'\n%{http_code}' -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
      --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
      --data-urlencode "text=ProScalp: ${msg}" 2>/dev/null || printf '\n000')
    http=$(printf '%s' "$resp" | tail -1)
    if printf '%s' "$resp" | grep -q '"ok":true' && [ "$http" = "200" ]; then
      echo "[$NOW] DELIVERED ($key): $msg" >> "$DELIVERY_LOG"
    else
      echo "[$NOW] DELIVERY_FAILED http=${http} ($key): $msg" >> "$DELIVERY_LOG"
    fi
  else
    echo "[$NOW] NO_TELEGRAM_CREDS ($key): $msg" >> "$DELIVERY_LOG"
  fi
}

# 1) Log anomalies in the last 16 minutes (slight overlap with 15-min cron).
LOG_TMP=$(mktemp)
docker logs --since 16m proscalp-ai-trader-backend-1 > "$LOG_TMP" 2>&1
while IFS=':' read -r pat desc; do
  count=$(grep -cE "$pat" "$LOG_TMP" 2>/dev/null || echo 0)
  count=${count:-0}
  if [ "$count" -gt 0 ] 2>/dev/null; then
    send_alert "${desc} (pattern '${pat}' x${count} in last 15m)" "log_${pat// /_}"
  fi
done <<'PATTERNS'
Traceback:bot exception
Exception:bot exception
EMA pullback scalp:disabled setup fired
London open breakout:disabled setup fired
US open breakout:disabled setup fired
session aggression mode active:aggression mode fired
PATTERNS
rm -f "$LOG_TMP"

# 2) Bot loop health.
STATUS_JSON=$(curl -sS --max-time 10 http://localhost/api/bot/status 2>/dev/null || echo "{}")
LOOP_ACTIVE=$(echo "$STATUS_JSON" | python3 -c "import json,sys
try:
    print(json.load(sys.stdin).get('loop_task_active'))
except Exception:
    print('?')" 2>/dev/null)
BOT_STATUS=$(echo "$STATUS_JSON" | python3 -c "import json,sys
try:
    print(json.load(sys.stdin).get('status'))
except Exception:
    print('?')" 2>/dev/null)
if [ "$LOOP_ACTIVE" != "True" ] || [ "$BOT_STATUS" != "running" ]; then
  send_alert "bot status=${BOT_STATUS} loop_active=${LOOP_ACTIVE}" "bot_stopped"
  # Ladder-specific escalation: if the ladder is ARMED in .env but the loop is
  # not running, an unattended halt must page (not sit silent). FIVE_TIER_LADDER_
  # ENABLED is sourced from .env above.
  if [ "${FIVE_TIER_LADDER_ENABLED:-false}" = "true" ]; then
    send_alert "🔴 LADDER ARMED but loop STOPPED (status=${BOT_STATUS} loop_active=${LOOP_ACTIVE}) — unattended halt during a ladder run" "ladder_armed_loop_stopped"
  fi
fi

# 3) DB invariant checks + counters (single query for efficiency).
DB=$(docker exec proscalp-ai-trader-postgres-1 psql -U proscalp -d proscalp -At -F'|' -c "
WITH s AS (SELECT '${START_TS}'::timestamp AT TIME ZONE 'UTC' AS start_ts)
SELECT
  (SELECT COUNT(*) FROM trades, s WHERE opened_at >= s.start_ts AND (metadata->>'risk_pct')::numeric > 0.31),
  (SELECT COUNT(*) FROM orders, s WHERE created_at >= s.start_ts AND order_type = 'market'),
  (SELECT COUNT(*) FROM trades, s WHERE status='closed' AND closed_at::date = (now() AT TIME ZONE 'UTC')::date AND opened_at >= s.start_ts AND realized_pnl < -10),
  (SELECT COUNT(*) FROM trades, s WHERE status='closed' AND opened_at >= s.start_ts),
  COALESCE((SELECT ROUND(100.0 * SUM(CASE WHEN status='filled' THEN 1 ELSE 0 END) / NULLIF(COUNT(*),0), 1)
            FROM orders WHERE created_at >= (now() - interval '4 hours')), 100),
  (SELECT COUNT(*) FROM orders WHERE created_at >= (now() - interval '4 hours')),
  COALESCE((SELECT CASE WHEN COUNT(*)=3 AND BOOL_AND(realized_pnl < -1) THEN 1 ELSE 0 END
            FROM (SELECT realized_pnl FROM trades, s WHERE status='closed' AND opened_at >= s.start_ts ORDER BY closed_at DESC LIMIT 3) recent), 0)
")
IFS='|' read OVER_RISK MARKET_ORD BAD_DAY CLOSED FILL_RATE ORDERS_4H LOSS_3 <<< "$DB"

[ "${OVER_RISK:-0}" -gt 0 ] 2>/dev/null && send_alert "trades with risk_pct > 0.31: ${OVER_RISK}" "over_risk"
[ "${MARKET_ORD:-0}" -gt 0 ] 2>/dev/null && send_alert "MARKET-type orders detected: ${MARKET_ORD}" "market_orders"
[ "${BAD_DAY:-0}" -gt 0 ] 2>/dev/null && send_alert "single-trade loss > \$10 today (n=${BAD_DAY})" "bad_loss_day_$(date -u +%Y%m%d)"
[ "${LOSS_3:-0}" = "1" ] && send_alert "3 consecutive closed trades each lost > \$1" "loss_streak_3"

# IOC fill rate (only alert with at least 5 orders sampled).
if [ -n "${FILL_RATE:-}" ] && [ "${ORDERS_4H:-0}" -ge 5 ] 2>/dev/null; then
  if python3 -c "import sys; sys.exit(0 if float('${FILL_RATE}') < 30 else 1)" 2>/dev/null; then
    send_alert "IOC fill rate ${FILL_RATE}% over last 4h (n=${ORDERS_4H})" "low_fill_rate"
  fi
fi

# 4) LADDER failure signals (Phase 2B/2C) — from the same risk_events the bot emits.
#    Each is a paging condition for an unattended ladder run. 16-min window (cron overlap).
LADDER=$(docker exec proscalp-ai-trader-postgres-1 psql -U proscalp -d proscalp -At -F'|' -c "
SELECT
  (SELECT COUNT(*) FROM risk_events WHERE event_type='ladder_sync_anomaly' AND created_at >= now() - interval '16 minutes'),
  (SELECT COUNT(*) FROM risk_events WHERE event_type='protective_orders_circuit_breaker' AND created_at >= now() - interval '16 minutes'),
  (SELECT COUNT(*) FROM risk_events WHERE event_type='ladder_circuit_breaker' AND created_at >= now() - interval '16 minutes'),
  (SELECT COUNT(*) FROM risk_events WHERE event_type='ladder_partial_attach' AND created_at >= now() - interval '16 minutes'),
  (SELECT COUNT(*) FROM risk_events WHERE event_type='protective_orders_failed' AND created_at >= now() - interval '16 minutes')
")
IFS='|' read SYNC_ANOM C1_TRIP C2_TRIP PARTIAL PROT_FAIL <<< "$LADDER"
[ "${SYNC_ANOM:-0}" -gt 0 ] 2>/dev/null && send_alert "🔴 ladder_sync_anomaly x${SYNC_ANOM} in last 15m — UNATTRIBUTED RESIDUAL (the reconciler bug class; halt+investigate)" "ladder_sync_anomaly"
[ "${C1_TRIP:-0}" -gt 0 ] 2>/dev/null && send_alert "🔴 C1 breaker tripped (protective_orders_circuit_breaker) x${C1_TRIP} — resting path disabled" "c1_breaker"
[ "${C2_TRIP:-0}" -gt 0 ] 2>/dev/null && send_alert "🔴 C2 breaker tripped (ladder partial-attach rate) x${C2_TRIP} — ladder auto-disabled" "c2_breaker"
[ "${PARTIAL:-0}" -gt 0 ] 2>/dev/null && send_alert "⚠️ ladder_partial_attach (degraded/dropped tiers) x${PARTIAL} in last 15m" "ladder_partial_attach"
[ "${PROT_FAIL:-0}" -gt 0 ] 2>/dev/null && send_alert "🔴 protective_orders_failed x${PROT_FAIL} in last 15m — attach failed" "protective_orders_failed"

# 5) LADDER milestones — 60 / 120 closed ladder trades since the operator-set clock start.
#    Clock start is written to LADDER_CLOCK_FILE when the 120-trade verdict run begins;
#    absent => milestone tracking idle (no false milestone off historical closes).
LADDER_CLOCK_START=$(cat "$LADDER_CLOCK_FILE" 2>/dev/null || echo "")
LCLOSED="na"
if [ -n "$LADDER_CLOCK_START" ]; then
  LCLOSED=$(docker exec proscalp-ai-trader-postgres-1 psql -U proscalp -d proscalp -At -c "
    SELECT COUNT(*) FROM risk_events WHERE event_type='ladder_trade_closed' AND created_at >= '${LADDER_CLOCK_START}'::timestamptz")
  LCLOSED=${LCLOSED:-0}
  LAST_LCLOSED=$(cat "$LAST_LADDER_CLOSED_FILE" 2>/dev/null || echo 0)
  echo "$LCLOSED" > "$LAST_LADDER_CLOSED_FILE"
  for M in 60 120; do
    if [ "${LCLOSED:-0}" -ge "$M" ] 2>/dev/null && [ "${LAST_LCLOSED:-0}" -lt "$M" ] 2>/dev/null; then
      send_alert "🏁 MILESTONE: ${M} closed ladder trades since clock start ${LADDER_CLOCK_START} (120-trade verdict run)" "ladder_milestone_${M}"
    fi
  done
fi

# 50-trade milestone (one-shot).
LAST_COUNT=$(cat "$LAST_CLOSED_FILE" 2>/dev/null || echo 0)
echo "${CLOSED:-0}" > "$LAST_CLOSED_FILE"
if [ "${CLOSED:-0}" -ge 50 ] 2>/dev/null && [ "${LAST_COUNT:-0}" -lt 50 ] 2>/dev/null; then
  send_alert "MILESTONE: 50 closed trades reached since deploy. Time for Phase 2A evaluation report." "milestone_50"
fi

echo "[$NOW] check OK closed=${CLOSED:-0} fill_rate_4h=${FILL_RATE:-?}% orders_4h=${ORDERS_4H:-0} ladder[anom=${SYNC_ANOM:-0} c1=${C1_TRIP:-0} c2=${C2_TRIP:-0} partial=${PARTIAL:-0} protfail=${PROT_FAIL:-0} closed=${LCLOSED:-na}]" >> "$CHECK_LOG"
