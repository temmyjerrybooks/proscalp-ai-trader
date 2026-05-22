import { Download, RotateCcw, XCircle } from "lucide-react";
import { useMemo, useState } from "react";
import { apiPost, apiUrl, number2, signedMoney, timeLabel } from "../api";
import { useApi } from "../hooks";
import type { DashboardData, TradeRow } from "../types";
import { DataTable, Panel, StatusBadge } from "../components/UI";

const dashboardFallback: DashboardData = {
  account_equity: 0,
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

export function TradeMonitor() {
  const [historyStart, setHistoryStart] = useState("");
  const [historyEnd, setHistoryEnd] = useState("");
  const [historyOutcome, setHistoryOutcome] = useState("all");
  const historyQuery = useMemo(
    () => buildHistoryQuery(historyOutcome, historyStart, historyEnd, "300"),
    [historyEnd, historyOutcome, historyStart]
  );
  const csvHistoryQuery = useMemo(
    () => buildHistoryQuery(historyOutcome, historyStart, historyEnd, "10000"),
    [historyEnd, historyOutcome, historyStart]
  );
  const historyPath = `/api/trades/history?${historyQuery}`;
  const { data, refresh, lastUpdated } = useApi<TradeRow[]>("/api/trades/open", [], { pollIntervalMs: 3000 });
  const { data: history } = useApi<TradeRow[]>(historyPath, [], { pollIntervalMs: 10000 });
  const { data: dashboard } = useApi<DashboardData>("/api/dashboard", dashboardFallback, { pollIntervalMs: 5000 });

  async function closeAll() {
    if (data.length === 0) return;
    if (!confirm("Close all open positions?")) return;
    await apiPost("/api/trades/close-all");
    await refresh();
  }

  function resetHistoryFilters() {
    setHistoryStart("");
    setHistoryEnd("");
    setHistoryOutcome("all");
  }

  function downloadHistoryCsv() {
    window.location.assign(apiUrl(`/api/trades/history.csv?${csvHistoryQuery}`));
  }

  return (
    <div className="space-y-4">
      <Panel>
        <div className="mb-4 flex items-center justify-between gap-3">
          <div>
            <div className="text-sm font-semibold">Open Positions</div>
            <div className="text-xs text-slate-500">{lastUpdated ? `Updated ${timeLabel(lastUpdated)}` : "Syncing"}</div>
          </div>
          <button title="Close all" disabled={data.length === 0} onClick={closeAll} className="grid h-10 w-10 place-items-center rounded bg-danger text-white disabled:cursor-not-allowed disabled:opacity-40">
            <XCircle size={18} />
          </button>
        </div>
        <DataTable
          rows={data as unknown as Record<string, unknown>[]}
          columns={[
            { key: "symbol", label: "Coin" },
            { key: "side", label: "Side", render: (row) => <StatusBadge value={String(row.side)} /> },
            { key: "status", label: "Status", render: (row) => <StatusBadge value={String(row.status ?? "open")} /> },
            { key: "entry_session", label: "Session", render: (row) => <StatusBadge value={contextLabel(row.entry_session)} /> },
            { key: "entry_regime", label: "Regime", render: (row) => <StatusBadge value={contextLabel(row.entry_regime)} /> },
            { key: "setup_score", label: "Score", render: (row) => scoreLabel(row) },
            { key: "setup_grade", label: "Grade", render: (row) => <StatusBadge value={String(row.setup_grade ?? "-")} /> },
            { key: "risk_pct", label: "Risk", render: (row) => riskLabel(row) },
            { key: "entry", label: "Entry", render: (row) => number2(Number(row.entry)) },
            { key: "mark_price", label: "Mark", render: (row) => number2(row.mark_price as number | undefined) },
            { key: "stop_loss", label: "Stop", render: (row) => number2(Number(row.stop_loss)) },
            { key: "quantity", label: "Qty", render: (row) => number2(Number(row.quantity)) },
            { key: "unrealized_pnl", label: "Unrealized", render: (row) => <span className={Number(row.unrealized_pnl ?? 0) >= 0 ? "text-emerald-300" : "text-danger"}>{signedMoney(Number(row.unrealized_pnl ?? 0))}</span> },
            { key: "setup", label: "Setup" },
            { key: "source", label: "Source", render: (row) => <StatusBadge value={String(row.source ?? "database")} /> }
          ]}
        />
        {data.length === 0 && <div className="py-8 text-center text-sm text-slate-400">No open {dashboard.active_mode} positions</div>}
      </Panel>

      <Panel>
        <div className="mb-4 text-sm font-semibold">Exit Plan</div>
        <div className="overflow-hidden rounded border border-line">
          <div className="grid grid-cols-[7rem_6rem_7rem_7rem_7rem_7rem_7rem_1fr] gap-3 bg-panel2 px-3 py-3 text-xs uppercase tracking-wide text-slate-400">
            <div>Coin</div>
            <div>Side</div>
            <div>Loss Exit</div>
            <div>Next Target</div>
            <div>TP1</div>
            <div>TP2</div>
            <div>Final TP</div>
            <div>Distance</div>
          </div>
          <div className="divide-y divide-line">
            {data.map((trade) => {
              const levels = takeProfitLevels(trade);
              const next = nextTarget(trade, levels);
              const mark = Number(trade.mark_price ?? trade.entry);
              return (
                <div key={`exit-${trade.id}`} className="grid grid-cols-[7rem_6rem_7rem_7rem_7rem_7rem_7rem_1fr] gap-3 px-3 py-3 text-sm">
                  <div className="font-medium text-slate-100">{trade.symbol}</div>
                  <div><StatusBadge value={trade.side} /></div>
                  <div className="text-danger">{number2(trade.stop_loss)}</div>
                  <div className="text-emerald-300">{number2(next)}</div>
                  <div>{number2(levels[0])}</div>
                  <div>{number2(levels[1])}</div>
                  <div>{number2(levels[2] ?? levels[levels.length - 1])}</div>
                  <div className="whitespace-normal text-slate-300">
                    Stop {number2(absDistancePct(mark, trade.stop_loss))}% away / Target {number2(next ? absDistancePct(mark, next) : 0)}% away
                  </div>
                </div>
              );
            })}
            {data.length === 0 && <div className="px-3 py-8 text-center text-sm text-slate-400">No exit plan while no trades are open</div>}
          </div>
        </div>
        <div className="mt-3 text-xs leading-5 text-slate-400">
          Loss exit triggers at stop. Profit model: TP1 closes 40%, TP2 closes 30%, final TP closes the remaining position. Stop moves to break-even after about 0.8R in profit.
        </div>
      </Panel>

      <Panel>
        <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
          <div>
            <div className="text-sm font-semibold">Trade History</div>
            <div className="text-xs text-slate-500">{history.length} rows</div>
          </div>
          <div className="grid gap-2 sm:grid-cols-[9.5rem_9.5rem_8.5rem_auto_auto]">
            <input
              type="date"
              value={historyStart}
              onChange={(event) => setHistoryStart(event.target.value)}
              className="h-10 rounded border border-line bg-panel2 px-3 text-sm text-slate-100 outline-none"
            />
            <input
              type="date"
              value={historyEnd}
              onChange={(event) => setHistoryEnd(event.target.value)}
              className="h-10 rounded border border-line bg-panel2 px-3 text-sm text-slate-100 outline-none"
            />
            <select
              value={historyOutcome}
              onChange={(event) => setHistoryOutcome(event.target.value)}
              className="h-10 rounded border border-line bg-panel2 px-3 text-sm text-slate-100 outline-none"
            >
              <option value="all">All</option>
              <option value="positive">Positive</option>
              <option value="negative">Negative</option>
              <option value="open">Open</option>
              <option value="closed">Closed</option>
              <option value="breakeven">Breakeven</option>
            </select>
            <button title="Reset filters" onClick={resetHistoryFilters} className="grid h-10 w-10 place-items-center rounded border border-line bg-panel2 text-slate-200">
              <RotateCcw size={18} />
            </button>
            <button title="Download CSV" onClick={downloadHistoryCsv} className="grid h-10 w-10 place-items-center rounded bg-mint text-base">
              <Download size={18} />
            </button>
          </div>
        </div>
        <DataTable
          rows={history as unknown as Record<string, unknown>[]}
          columns={[
            { key: "opened_at", label: "Opened", render: (row) => timeLabel(row.opened_at as string | undefined) },
            { key: "closed_at", label: "Closed", render: (row) => timeLabel(row.closed_at as string | undefined) },
            { key: "symbol", label: "Coin" },
            { key: "side", label: "Side", render: (row) => <StatusBadge value={String(row.side)} /> },
            { key: "status", label: "Status", render: (row) => <StatusBadge value={String(row.status ?? "open")} /> },
            { key: "entry_session", label: "Session", render: (row) => <StatusBadge value={contextLabel(row.entry_session)} /> },
            { key: "entry_regime", label: "Regime", render: (row) => <StatusBadge value={contextLabel(row.entry_regime)} /> },
            { key: "setup_score", label: "Score", render: (row) => scoreLabel(row) },
            { key: "setup_grade", label: "Grade", render: (row) => <StatusBadge value={String(row.setup_grade ?? "-")} /> },
            { key: "risk_pct", label: "Risk", render: (row) => riskLabel(row) },
            { key: "realized_pnl", label: "Realized", render: (row) => <span className={Number(row.realized_pnl ?? 0) >= 0 ? "text-emerald-300" : "text-danger"}>{signedMoney(Number(row.realized_pnl ?? 0))}</span> },
            { key: "close_reason", label: "Exit" },
            { key: "setup", label: "Setup" }
          ]}
        />
      </Panel>
    </div>
  );
}

function takeProfitLevels(trade: TradeRow): number[] {
  const value = trade.take_profit;
  if (Array.isArray(value)) return value.map(Number).filter(Number.isFinite);
  const levels = (value as { levels?: unknown }).levels;
  return Array.isArray(levels) ? levels.map(Number).filter(Number.isFinite) : [];
}

function nextTarget(trade: TradeRow, levels: number[]): number | null {
  const mark = Number(trade.mark_price ?? trade.entry);
  if (!mark || levels.length === 0) return levels[0] ?? null;
  if (trade.side === "short") {
    return levels.find((level) => mark > level) ?? levels[levels.length - 1] ?? null;
  }
  return levels.find((level) => mark < level) ?? levels[levels.length - 1] ?? null;
}

function absDistancePct(mark: number, target: number | null): number {
  if (!mark || !target) return 0;
  return Math.abs(((target - mark) / mark) * 100);
}

function scoreLabel(row: Record<string, unknown>): string {
  return row.setup_score === null || row.setup_score === undefined ? "-" : number2(Number(row.setup_score));
}

function riskLabel(row: Record<string, unknown>): string {
  return row.risk_pct === null || row.risk_pct === undefined ? "-" : `${number2(Number(row.risk_pct))}%`;
}

function contextLabel(value: unknown): string {
  const label = String(value ?? "").trim();
  return label && label !== "unknown" ? label : "-";
}

function buildHistoryQuery(outcome: string, startDate: string, endDate: string, limit: string): string {
  const params = new URLSearchParams({ limit, outcome });
  if (startDate) params.set("start_date", startDate);
  if (endDate) params.set("end_date", endDate);
  return params.toString();
}
