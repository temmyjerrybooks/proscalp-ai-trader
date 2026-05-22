import { Pause, Play, Siren } from "lucide-react";
import { apiPost, money, signedMoney, timeLabel } from "../api";
import { useApi } from "../hooks";
import type { ActivityRow, DashboardData } from "../types";
import { MetricCard, Panel, ProgressBar, StatusBadge } from "../components/UI";

const fallback: DashboardData = {
  account_equity: 10000,
  account_balance: 10000,
  account_equity_source: "paper_starting_equity",
  daily_pnl_pct: 0,
  daily_pnl: 0,
  open_trades: 0,
  session_status: "closed",
  market_regime: "good",
  active_exchange: "binance",
  active_mode: "paper",
  daily_profit_target_progress: 0,
  daily_loss_limit_progress: 0,
  bot: {}
};

export function Dashboard() {
  const { data, refresh, lastUpdated } = useApi<DashboardData>("/api/dashboard", fallback, { pollIntervalMs: 5000 });
  const { data: activity } = useApi<ActivityRow[]>("/api/activity", [], { pollIntervalMs: 3000 });
  const signalChecks = botNumber(data.bot, "last_signal_count");
  const rejectionCount = botNumber(data.bot, "last_rejection_count");
  const orderCount = botNumber(data.bot, "last_order_count");
  const cycleSymbolLimit = botNumber(data.bot, "cycle_symbol_limit");
  const watchlistCount = botNumber(data.bot, "last_scan_count");
  const strategyCount = botNumber(data.bot, "strategy_count");

  async function control(path: string) {
    await apiPost(path);
    await refresh();
  }

  return (
    <div className="space-y-5">
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <MetricCard label="Account Equity" value={money(data.account_equity)} />
        <MetricCard label="Daily PnL" value={`${data.daily_pnl_pct.toFixed(2)}%`} accent={data.daily_pnl_pct >= 0 ? "text-emerald-300" : "text-danger"} />
        <MetricCard label="Open Trades" value={data.open_trades} accent="text-amber-300" />
        <MetricCard label="Market Regime" value={<StatusBadge value={data.market_regime} />} />
      </div>
      <div className="grid gap-4 lg:grid-cols-[1.2fr_0.8fr]">
        <Panel>
          <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
            <div>
              <div className="text-sm font-semibold">Session Command</div>
              <div className="text-xs text-slate-400">{data.active_exchange.toUpperCase()} / {data.active_mode}</div>
            </div>
            <div className="flex gap-2">
              <button title="Start bot" onClick={() => control("/api/bot/start")} className="grid h-10 w-10 place-items-center rounded bg-mint text-base">
                <Play size={18} />
              </button>
              <button title="Stop bot" onClick={() => control("/api/bot/stop")} className="grid h-10 w-10 place-items-center rounded bg-amber-500 text-base">
                <Pause size={18} />
              </button>
              <button title="Emergency stop" onClick={() => control("/api/bot/emergency-stop")} className="grid h-10 w-10 place-items-center rounded bg-danger text-white">
                <Siren size={18} />
              </button>
            </div>
          </div>
          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <div className="mb-2 flex justify-between text-xs text-slate-400">
                <span>Profit Target</span>
                <span>{data.daily_profit_target_progress.toFixed(1)}%</span>
              </div>
              <ProgressBar value={data.daily_profit_target_progress} />
            </div>
            <div>
              <div className="mb-2 flex justify-between text-xs text-slate-400">
                <span>Loss Limit</span>
                <span>{data.daily_loss_limit_progress.toFixed(1)}%</span>
              </div>
              <ProgressBar value={data.daily_loss_limit_progress} danger />
            </div>
          </div>
        </Panel>
        <Panel>
          <div className="mb-3 flex items-center justify-between gap-3">
            <div className="text-sm font-semibold">Runtime</div>
            <div className="text-xs text-slate-500">{lastUpdated ? `Updated ${timeLabel(lastUpdated)}` : "Syncing"}</div>
          </div>
          <div className="space-y-2 text-sm text-slate-300">
            <div className="flex justify-between"><span>Status</span><StatusBadge value={String(data.bot.status ?? "stopped")} /></div>
            <div className="flex justify-between"><span>Session</span><span>{data.session_status}</span></div>
            <div className="flex justify-between"><span>Mode</span><StatusBadge value={data.active_mode} /></div>
            <div className="flex justify-between"><span>Exchange</span><span>{data.active_exchange}</span></div>
            <div className="flex justify-between"><span>Equity Basis</span><StatusBadge value={formatSource(data.account_equity_source)} /></div>
            <div className="flex justify-between"><span>Daily PnL</span><span className={data.daily_pnl >= 0 ? "text-emerald-300" : "text-danger"}>{signedMoney(data.daily_pnl)}</span></div>
          </div>
          <div className="mt-3 grid gap-2 border-t border-line pt-3 text-sm text-slate-300 sm:grid-cols-2">
            <div className="flex justify-between gap-3"><span>Scan Scope</span><span>{formatScope(cycleSymbolLimit, watchlistCount)}</span></div>
            <div className="flex justify-between gap-3"><span>Strategies</span><span>{strategyCount || "-"}</span></div>
            <div className="flex justify-between gap-3"><span>Signal Checks</span><span>{signalChecks}</span></div>
            <div className="flex justify-between gap-3"><span>Rejected</span><span>{rejectionCount}</span></div>
            <div className="flex justify-between gap-3"><span>Orders Cycle</span><span>{orderCount}</span></div>
          </div>
        </Panel>
      </div>
      <Panel>
        <div className="mb-3 flex items-center justify-between gap-3">
          <div className="text-sm font-semibold">Live Activity</div>
          <StatusBadge value="auto refresh" />
        </div>
        <div className="divide-y divide-line rounded border border-line">
          {activity.slice(0, 12).map((item) => (
            <div key={`${item.source}-${item.id}`} className="grid gap-3 px-3 py-3 text-sm md:grid-cols-[8rem_7rem_1fr]">
              <div className="text-slate-500">{timeLabel(item.time)}</div>
              <div><StatusBadge value={item.symbol ?? item.source} /></div>
              <div className="min-w-0 text-slate-200">
                <span className="text-slate-400">{item.type}</span>
                <span className="mx-2 text-slate-600">/</span>
                <span>{item.message}</span>
              </div>
            </div>
          ))}
          {activity.length === 0 && <div className="px-3 py-8 text-center text-sm text-slate-400">Waiting for bot activity</div>}
        </div>
      </Panel>
    </div>
  );
}

function botNumber(bot: Record<string, unknown>, key: string): number {
  const value = bot[key];
  return typeof value === "number" ? value : Number(value ?? 0);
}

function formatScope(limit: number, watchlist: number): string {
  if (!limit && !watchlist) return "-";
  return `${limit || 0}/${watchlist || 0}`;
}

function formatSource(source?: string): string {
  return source ? source.split("_").join(" ") : "unknown";
}
