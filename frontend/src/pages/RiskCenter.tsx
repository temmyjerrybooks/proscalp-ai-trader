import { Siren } from "lucide-react";
import { apiPost } from "../api";
import { useApi } from "../hooks";
import type { RiskStatus } from "../types";
import { MetricCard, Panel, StatusBadge } from "../components/UI";

const fallback: RiskStatus = {
  daily_loss_limit_pct: -4,
  risk_per_trade: { "A+": [0.4, 0.5], A: [0.3, 0.4], B: [0.18, 0.29], C: [0.08, 0.15] },
  off_session_risk_per_trade: { "A+": [0.35, 0.45], A: [0.25, 0.35], B: [0.15, 0.25], C: [0.05, 0.12] },
  max_concurrent_trades: 5,
  exposure_limits: { total_pct: 35, coin_pct: 10, session_pct: 22 },
  trade_score_thresholds: { session: 55, off_session: 60, C: 55, B: 65, A: 75, "A+": 85 },
  off_session_trading_enabled: true,
  kill_switch: false,
  warnings: [],
  live_trading_enabled: false,
  futures_confirmed: false
};

export function RiskCenter() {
  const { data } = useApi<RiskStatus>("/api/risk/status", fallback, { pollIntervalMs: 10000 });
  return (
    <div className="space-y-4">
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <MetricCard label="Daily Loss Limit" value={`${data.daily_loss_limit_pct}%`} accent="text-danger" />
        <MetricCard label="Max Concurrent" value={data.max_concurrent_trades} />
        <MetricCard label="Kill Switch" value={<StatusBadge value={data.kill_switch ? "enabled" : "clear"} />} />
        <MetricCard label="Live Trading" value={<StatusBadge value={data.live_trading_enabled ? "enabled" : "locked"} />} />
      </div>
      <Panel>
        <div className="mb-4 flex items-center justify-between">
          <div className="text-sm font-semibold">Capital Controls</div>
          <button title="Emergency shutdown" onClick={() => apiPost("/api/bot/emergency-stop")} className="grid h-10 w-10 place-items-center rounded bg-danger text-white">
            <Siren size={18} />
          </button>
        </div>
        <div className="mb-3 text-xs uppercase tracking-wide text-slate-400">Normal Session</div>
        <div className="grid gap-4 md:grid-cols-4">
          {Object.entries(data.risk_per_trade).map(([grade, range]) => (
            <div key={grade} className="rounded border border-line bg-panel2 p-3">
              <div className="text-xs text-slate-400">{grade} risk</div>
              <div className="mt-1 text-lg font-semibold">{range[0]}% - {range[1]}%</div>
            </div>
          ))}
        </div>
        <div className="mb-3 mt-5 text-xs uppercase tracking-wide text-slate-400">Off-Session</div>
        <div className="grid gap-4 md:grid-cols-4">
          {Object.entries(data.off_session_risk_per_trade ?? {}).map(([grade, range]) => (
            <div key={`off-${grade}`} className="rounded border border-line bg-panel2 p-3">
              <div className="text-xs text-slate-400">{grade} risk</div>
              <div className="mt-1 text-lg font-semibold">{range[0]}% - {range[1]}%</div>
            </div>
          ))}
        </div>
      </Panel>
      <Panel>
        <div className="mb-4 text-sm font-semibold">Trade Score Thresholds</div>
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
          <div className="rounded border border-line bg-panel2 p-3">
            <div className="text-xs text-slate-400">Session minimum</div>
            <div className="mt-1 text-lg font-semibold">{data.trade_score_thresholds?.session ?? 55}+</div>
          </div>
          <div className="rounded border border-line bg-panel2 p-3">
            <div className="text-xs text-slate-400">Off-session minimum</div>
            <div className="mt-1 text-lg font-semibold">{data.trade_score_thresholds?.off_session ?? 60}+</div>
          </div>
          <div className="rounded border border-line bg-panel2 p-3">
            <div className="text-xs text-slate-400">Off-session mode</div>
            <div className="mt-1"><StatusBadge value={data.off_session_trading_enabled ? "enabled" : "disabled"} /></div>
          </div>
          <div className="rounded border border-line bg-panel2 p-3">
            <div className="text-xs text-slate-400">Aggressive A+</div>
            <div className="mt-1 text-lg font-semibold">{data.trade_score_thresholds?.["A+"] ?? 85}+ / off {data.trade_score_thresholds?.["off_session_A+"] ?? 90}+</div>
          </div>
        </div>
      </Panel>
      <Panel>
        <div className="mb-4 text-sm font-semibold">Exposure Caps</div>
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
          <LimitTile label="Total" value={data.exposure_limits.total_pct} detail={`strong ${data.exposure_limits.total_strong_pct ?? "-"}% / hot ${data.exposure_limits.total_hot_pct ?? "-"}%`} />
          <LimitTile label="Per Coin" value={data.exposure_limits.coin_pct} detail="single-symbol cap" />
          <LimitTile label="Session" value={data.exposure_limits.session_normal_pct ?? data.exposure_limits.session_pct} detail={`off ${data.exposure_limits.session_off_session_pct ?? "-"}% / hot ${data.exposure_limits.session_hot_pct ?? "-"}%`} />
          <LimitTile label="Open Risk" value={data.exposure_limits.open_risk_pct} detail={`off ${data.exposure_limits.open_risk_off_session_pct ?? "-"}% / hot ${data.exposure_limits.open_risk_hot_pct ?? "-"}%`} />
        </div>
      </Panel>
    </div>
  );
}

function LimitTile({ label, value, detail }: { label: string; value?: number; detail: string }) {
  return (
    <div className="rounded border border-line bg-panel2 p-3">
      <div className="text-xs text-slate-400">{label}</div>
      <div className="mt-1 text-lg font-semibold">{value ?? "-"}%</div>
      <div className="mt-1 text-xs text-slate-500">{detail}</div>
    </div>
  );
}
