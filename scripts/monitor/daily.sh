#!/bin/bash
# Phase 2A daily summary at 23:55 UTC. Writes file + sends Telegram digest.
set -e
ENV_FILE=/opt/proscalp-ai-trader/.env
[ -f "$ENV_FILE" ] && { set -a; . "$ENV_FILE"; set +a; }
START_TS=$(cat /opt/proscalp-snapshots/phase2a-bot-start-ts.txt 2>/dev/null || echo "")
DAY=$(date -u +%Y-%m-%d)
OUT=/opt/proscalp-snapshots/daily/${DAY}.txt
mkdir -p /opt/proscalp-snapshots/daily

{
  echo "=== ProScalp Phase 2A daily summary ${DAY} ==="
  echo "Generated: $(date -u +%FT%TZ)  | Bot deployed: ${START_TS}"
  echo
  echo "--- Today's closed trades ---"
  docker exec proscalp-ai-trader-postgres-1 psql -U proscalp -d proscalp -c "
SELECT setup_name, symbol, side, metadata->>'normal_grade' AS grade,
       metadata->>'setup_score' AS score,
       metadata->>'close_reason' AS reason,
       ROUND(realized_pnl::numeric,3) AS pnl,
       (closed_at - opened_at) AS dur
FROM trades WHERE status='closed' AND closed_at::date = '${DAY}'::date
ORDER BY closed_at;
"
  echo "--- Today's totals ---"
  docker exec proscalp-ai-trader-postgres-1 psql -U proscalp -d proscalp -c "
SELECT COUNT(*) AS closed,
       ROUND(SUM(realized_pnl)::numeric,2) AS net_pnl,
       ROUND(AVG(realized_pnl)::numeric,3) AS avg_pnl,
       ROUND((100.0*COUNT(*) FILTER(WHERE realized_pnl>0)/NULLIF(COUNT(*),0))::numeric,1) AS win_pct
FROM trades WHERE status='closed' AND closed_at::date = '${DAY}'::date;
"
  echo "--- Since Phase 2A deploy (${START_TS} UTC) ---"
  docker exec proscalp-ai-trader-postgres-1 psql -U proscalp -d proscalp -c "
SELECT COUNT(*) AS closed,
       ROUND(SUM(realized_pnl)::numeric,2) AS net_pnl,
       ROUND(AVG(realized_pnl)::numeric,3) AS avg_pnl,
       ROUND((100.0*COUNT(*) FILTER(WHERE realized_pnl>0)/NULLIF(COUNT(*),0))::numeric,1) AS win_pct
FROM trades WHERE status='closed' AND opened_at >= '${START_TS}'::timestamp AT TIME ZONE 'UTC';
"
  echo "--- IOC fill rate since deploy ---"
  docker exec proscalp-ai-trader-postgres-1 psql -U proscalp -d proscalp -c "
SELECT order_type, status, COUNT(*) FROM orders
WHERE created_at >= '${START_TS}'::timestamp AT TIME ZONE 'UTC' GROUP BY 1,2 ORDER BY 1,2;
"
  echo "--- Baseline comparison (pre-Phase-2A, May 16-19, n=71) ---"
  echo "Baseline: net=-\$32.58, avg=-\$0.46/trade, win=35.2%"
  echo
  echo "--- 24h anomaly counts ---"
  LOG_TMP=$(mktemp)
  docker logs --since 24h proscalp-ai-trader-backend-1 > "$LOG_TMP" 2>&1
  for pat in "Traceback" "Exception" "ERROR" "EMA pullback scalp" "London open breakout" "US open breakout" "session aggression mode active"; do
    count=$(grep -cE "$pat" "$LOG_TMP" 2>/dev/null || echo 0)
    printf "  %-40s %s\n" "$pat" "${count:-0}"
  done
  rm -f "$LOG_TMP"
} > "$OUT" 2>&1

# Push first ~3500 chars to Telegram.
if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
  TG_TEXT=$(head -c 3500 "$OUT")
  curl -sS -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
    --data-urlencode "text=${TG_TEXT}" -o /dev/null || true
fi
