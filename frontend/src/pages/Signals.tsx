import { ChevronLeft, ChevronRight, Download, RotateCcw, Search } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { apiUrl, number2, signedMoney, timeLabel } from "../api";
import { useApi } from "../hooks";
import type { SignalPage, SignalRow, TradeRow } from "../types";
import { Panel, StatusBadge } from "../components/UI";

const ROW_LIMIT_OPTIONS = [200, 300, 400, 500, 600, 700, 800, 900, 1000, 2000];
const DEFAULT_ROW_LIMIT = 200;
const signalFallback: SignalPage = {
  items: [],
  total: 0,
  limit: DEFAULT_ROW_LIMIT,
  offset: 0,
  has_next: false,
  source_window: 0,
  filters: {}
};

export function Signals() {
  const [decision, setDecision] = useState("all");
  const [currentCycle, setCurrentCycle] = useState(true);
  const [side, setSide] = useState("all");
  const [executionStatus, setExecutionStatus] = useState("all");
  const [followUpStatus, setFollowUpStatus] = useState("all");
  const [verdict, setVerdict] = useState("all");
  const [settledOnly, setSettledOnly] = useState(true);
  const [csvDownloading, setCsvDownloading] = useState(false);
  const [csvError, setCsvError] = useState("");
  const [symbolInput, setSymbolInput] = useState("");
  const [setupInput, setSetupInput] = useState("");
  const [symbolFilter, setSymbolFilter] = useState("");
  const [setupFilter, setSetupFilter] = useState("");
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [minScore, setMinScore] = useState("");
  const [maxScore, setMaxScore] = useState("");
  const [rowLimit, setRowLimit] = useState(DEFAULT_ROW_LIMIT);
  const [offset, setOffset] = useState(0);
  const signalPath = useMemo(() => {
    const params = buildSignalParams({
      decision,
      currentCycle,
      symbol: symbolFilter,
      setup: setupFilter,
      side,
      executionStatus,
      followUpStatus,
      verdict,
      settledOnly,
      startDate,
      endDate,
      minScore,
      maxScore,
      limit: String(rowLimit),
      offset: String(offset)
    });
    return `/api/signals/search?${params.toString()}`;
  }, [
    currentCycle,
    decision,
    endDate,
    executionStatus,
    followUpStatus,
    maxScore,
    minScore,
    offset,
    rowLimit,
    setupFilter,
    settledOnly,
    side,
    startDate,
    symbolFilter,
    verdict
  ]);
  const csvPath = useMemo(() => {
    const params = buildSignalParams({
      decision,
      currentCycle,
      symbol: symbolFilter,
      setup: setupFilter,
      side,
      executionStatus,
      followUpStatus,
      verdict,
      settledOnly,
      startDate,
      endDate,
      minScore,
      maxScore
    });
    return `/api/signals/report.csv?${params.toString()}`;
  }, [currentCycle, decision, endDate, executionStatus, followUpStatus, maxScore, minScore, setupFilter, settledOnly, side, startDate, symbolFilter, verdict]);
  const { data, lastUpdated } = useApi<SignalPage>(signalPath, signalFallback, { pollIntervalMs: 5000 });
  const { data: openTrades } = useApi<TradeRow[]>("/api/trades/open", [], { pollIntervalMs: 3000 });
  const rows = data.items ?? [];
  const pageStart = data.total ? Math.min(data.offset + 1, data.total) : 0;
  const pageEnd = Math.min(data.offset + data.limit, data.total);
  const dateRangeActive = Boolean(startDate || endDate);
  const effectiveCurrentCycle = currentCycle && !dateRangeActive;

  useEffect(() => {
    if (data.total > 0 && offset >= data.total) {
      setOffset(0);
    }
  }, [data.total, offset]);

  function applyFilters() {
    setOffset(0);
    setSymbolFilter(symbolInput);
    setSetupFilter(setupInput);
  }

  function resetFilters() {
    setDecision("all");
    setCurrentCycle(true);
    setSide("all");
    setExecutionStatus("all");
    setFollowUpStatus("all");
    setVerdict("all");
    setSettledOnly(true);
    setCsvError("");
    setSymbolInput("");
    setSetupInput("");
    setSymbolFilter("");
    setSetupFilter("");
    setStartDate("");
    setEndDate("");
    setMinScore("");
    setMaxScore("");
    setRowLimit(DEFAULT_ROW_LIMIT);
    setOffset(0);
  }

  async function downloadCsv() {
    setCsvError("");
    setCsvDownloading(true);
    try {
      const response = await fetch(apiUrl(csvPath));
      if (!response.ok) {
        throw new Error(`${response.status} ${response.statusText}`);
      }
      const blob = await response.blob();
      const disposition = response.headers.get("Content-Disposition") ?? "";
      const match = disposition.match(/filename="?([^"]+)"?/i);
      const filename = match?.[1] ?? `proscalp-signals-${new Date().toISOString().slice(0, 10)}.csv`;
      const href = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = href;
      anchor.download = filename;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(href);
    } catch (error) {
      setCsvError(error instanceof Error ? error.message : "CSV download failed");
    } finally {
      setCsvDownloading(false);
    }
  }

  return (
    <div className="space-y-4">
      <Panel>
        <div className="mb-4 flex items-center justify-between gap-3">
          <div className="text-sm font-semibold">Running Trade Movement</div>
          <StatusBadge value="live" />
        </div>
        <div className="overflow-x-auto rounded border border-line">
          <div className="grid grid-cols-[7rem_5rem_5rem_5rem_5rem_6rem_6rem_7rem_7rem_7rem_1fr] gap-3 bg-panel2 px-3 py-3 text-xs uppercase tracking-wide text-slate-400">
            <div>Coin</div>
            <div>Side</div>
            <div>Score</div>
            <div>Grade</div>
            <div>Risk</div>
            <div>Entry</div>
            <div>Mark</div>
            <div>Move</div>
            <div>Unrealized</div>
            <div>Status</div>
            <div>Setup</div>
          </div>
          <div className="divide-y divide-line">
            {openTrades.map((trade) => (
              <div key={trade.id} className="grid grid-cols-[7rem_5rem_5rem_5rem_5rem_6rem_6rem_7rem_7rem_7rem_1fr] gap-3 px-3 py-3 text-sm">
                <div className="font-medium text-slate-100">{trade.symbol}</div>
                <div><StatusBadge value={trade.side} /></div>
                <div>{trade.setup_score ?? "-"}</div>
                <div><StatusBadge value={trade.setup_grade ?? "-"} /></div>
                <div>{trade.risk_pct === null || trade.risk_pct === undefined ? "-" : `${number2(trade.risk_pct)}%`}</div>
                <div>{number2(trade.entry)}</div>
                <div>{number2(trade.mark_price)}</div>
                <div className={movementPct(trade) >= 0 ? "text-emerald-300" : "text-danger"}>{number2(movementPct(trade))}%</div>
                <div className={Number(trade.unrealized_pnl ?? 0) >= 0 ? "text-emerald-300" : "text-danger"}>{signedMoney(trade.unrealized_pnl)}</div>
                <div><StatusBadge value={trade.status ?? "open"} /></div>
                <div className="min-w-0 whitespace-normal text-slate-300">{trade.setup}</div>
              </div>
            ))}
            {openTrades.length === 0 && <div className="px-3 py-8 text-center text-sm text-slate-400">No active trade movement to display</div>}
          </div>
        </div>
      </Panel>

      <Panel>
        <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
          <div>
            <div className="text-sm font-semibold">Generated Signals</div>
            <div className="text-xs text-slate-500">
              {lastUpdated ? `${pageStart}-${pageEnd} of ${data.total} / Updated ${timeLabel(lastUpdated)}` : "Syncing"}
            </div>
          </div>
          <div className="flex items-center gap-3">
            <label className="flex h-9 items-center gap-2 rounded border border-line bg-panel2 px-3 text-xs text-slate-300">
              <span>Rows</span>
              <select
                value={rowLimit}
                onChange={(event) => {
                  setRowLimit(Number(event.target.value));
                  setOffset(0);
                }}
                className="bg-transparent text-sm text-slate-100 outline-none"
              >
                {ROW_LIMIT_OPTIONS.map((value) => (
                  <option key={value} value={value}>{value}</option>
                ))}
              </select>
            </label>
            <div className="text-xs text-slate-500">Window {data.source_window}</div>
          </div>
        </div>
        <div className="mb-3 grid gap-3 xl:grid-cols-[8.5rem_8rem_8rem_10rem_10rem_10rem_10rem]">
          <select
            value={decision}
            onChange={(event) => {
              setDecision(event.target.value);
              setOffset(0);
            }}
            className="h-10 rounded border border-line bg-panel2 px-3 text-sm text-slate-100 outline-none"
          >
            <option value="all">All</option>
            <option value="accepted">Bot accepted</option>
            <option value="rejected">Bot rejected/not taken</option>
          </select>
          <select
            value={side}
            onChange={(event) => {
              setSide(event.target.value);
              setOffset(0);
            }}
            className="h-10 rounded border border-line bg-panel2 px-3 text-sm text-slate-100 outline-none"
          >
            <option value="all">All sides</option>
            <option value="long">Long</option>
            <option value="short">Short</option>
          </select>
          <label className="flex h-10 items-center gap-2 rounded border border-line bg-panel2 px-3 text-sm text-slate-200">
            <input
              type="checkbox"
              checked={effectiveCurrentCycle}
              disabled={dateRangeActive}
              onChange={(event) => {
                setCurrentCycle(event.target.checked);
                setOffset(0);
              }}
            />
            Current
          </label>
          <label className="flex h-10 items-center gap-2 rounded border border-line bg-panel2 px-3 text-sm text-slate-200">
            <input
              type="checkbox"
              checked={settledOnly}
              onChange={(event) => {
                setSettledOnly(event.target.checked);
                setOffset(0);
              }}
            />
            Closed outcomes
          </label>
          <select
            value={executionStatus}
            onChange={(event) => {
              setExecutionStatus(event.target.value);
              setOffset(0);
            }}
            className="h-10 rounded border border-line bg-panel2 px-3 text-sm text-slate-100 outline-none"
          >
            <option value="all">All trade states</option>
            <option value="not_selected">Not selected</option>
            <option value="blocked">Blocked</option>
            <option value="strategy_rejected">Strategy rejected</option>
            <option value="score_rejected">Score rejected</option>
            <option value="open">Open</option>
            <option value="pending">Pending</option>
            <option value="closed">Closed</option>
          </select>
          <select
            value={followUpStatus}
            onChange={(event) => {
              setFollowUpStatus(event.target.value);
              setOffset(0);
            }}
            className="h-10 rounded border border-line bg-panel2 px-3 text-sm text-slate-100 outline-none"
          >
            <option value="all">All follow-ups</option>
            <option value="waiting">Waiting</option>
            <option value="still_running">Still running</option>
            <option value="would_win">Would win</option>
            <option value="would_lose">Would lose</option>
            <option value="would_be_positive">Would be positive</option>
            <option value="would_be_negative">Would be negative</option>
            <option value="neutral">Neutral</option>
            <option value="actual_positive">Actual positive</option>
            <option value="actual_negative">Actual negative</option>
            <option value="actual_neutral">Actual neutral</option>
            <option value="actual_open_positive">Actual open positive</option>
            <option value="actual_open_negative">Actual open negative</option>
            <option value="actual_open_neutral">Actual open neutral</option>
            <option value="market_data_unavailable">Data unavailable</option>
            <option value="not_trackable">Not trackable</option>
          </select>
          <select
            value={verdict}
            onChange={(event) => {
              setVerdict(event.target.value);
              setOffset(0);
            }}
            className="h-10 rounded border border-line bg-panel2 px-3 text-sm text-slate-100 outline-none"
          >
            <option value="all">All decisions</option>
            <option value="good_acceptance">Good acceptance</option>
            <option value="bad_acceptance">Bad acceptance</option>
            <option value="good_rejection">Good rejection</option>
            <option value="bad_rejection">Bad rejection</option>
            <option value="pending">Pending</option>
            <option value="neutral">Neutral</option>
            <option value="unknown">Unknown</option>
          </select>
        </div>
        <div className="mb-4 grid gap-3 xl:grid-cols-[9.5rem_9.5rem_7rem_7rem_1fr_1fr_auto_auto_auto]">
          <input
            value={startDate}
            onChange={(event) => {
              setStartDate(event.target.value);
              setCurrentCycle(false);
              setOffset(0);
            }}
            type="date"
            className="h-10 rounded border border-line bg-panel2 px-3 text-sm text-slate-100 outline-none"
          />
          <input
            value={endDate}
            onChange={(event) => {
              setEndDate(event.target.value);
              setCurrentCycle(false);
              setOffset(0);
            }}
            type="date"
            className="h-10 rounded border border-line bg-panel2 px-3 text-sm text-slate-100 outline-none"
          />
          <input
            value={minScore}
            onChange={(event) => {
              setMinScore(event.target.value);
              setOffset(0);
            }}
            placeholder="Min score"
            inputMode="numeric"
            className="h-10 rounded border border-line bg-panel2 px-3 text-sm text-slate-100 outline-none placeholder:text-slate-500"
          />
          <input
            value={maxScore}
            onChange={(event) => {
              setMaxScore(event.target.value);
              setOffset(0);
            }}
            placeholder="Max score"
            inputMode="numeric"
            className="h-10 rounded border border-line bg-panel2 px-3 text-sm text-slate-100 outline-none placeholder:text-slate-500"
          />
          <input
            value={symbolInput}
            onChange={(event) => setSymbolInput(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") applyFilters();
            }}
            placeholder="Coin"
            className="h-10 rounded border border-line bg-panel2 px-3 text-sm text-slate-100 outline-none placeholder:text-slate-500"
          />
          <input
            value={setupInput}
            onChange={(event) => setSetupInput(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") applyFilters();
            }}
            placeholder="Setup"
            className="h-10 rounded border border-line bg-panel2 px-3 text-sm text-slate-100 outline-none placeholder:text-slate-500"
          />
          <button title="Apply filters" onClick={applyFilters} className="grid h-10 w-10 place-items-center rounded bg-mint text-base">
            <Search size={18} />
          </button>
          <button title="Reset filters" onClick={resetFilters} className="grid h-10 w-10 place-items-center rounded border border-line bg-panel2 text-slate-200">
            <RotateCcw size={18} />
          </button>
          <button
            title="Download CSV report"
            onClick={downloadCsv}
            disabled={csvDownloading}
            className="grid h-10 w-10 place-items-center rounded border border-line bg-panel2 text-slate-200 disabled:cursor-not-allowed disabled:opacity-50"
          >
            <Download size={18} />
          </button>
        </div>
        {csvError && <div className="mb-3 text-xs text-danger">CSV download failed: {csvError}</div>}
        <div className="overflow-x-auto rounded border border-line">
          <div className="grid min-w-[104rem] grid-cols-[8rem_7rem_1.2fr_5rem_6rem_7rem_5rem_7rem_7rem_7rem_10rem_9rem_1.7fr] gap-3 bg-panel2 px-3 py-3 text-xs uppercase tracking-wide text-slate-400">
            <div>Time</div>
            <div>Coin</div>
            <div>Setup</div>
            <div>Side</div>
            <div>Session</div>
            <div>Regime</div>
            <div>Score</div>
            <div>Setup Signal</div>
            <div>Bot Decision</div>
            <div>Trade</div>
            <div>Follow-Up</div>
            <div>Decision Quality</div>
            <div>Reason</div>
          </div>
          <div className="divide-y divide-line">
            {rows.map((row, index) => {
              const decision = row.decision ?? (row.accepted ? "accepted" : "rejected");
              const strategyStatus = row.strategy_status ?? (row.accepted ? "accepted" : "rejected");
              const executionStatus = row.execution_status ?? decision;
              const reasons = row.reason_summary || row.decision_reasons?.join("; ") || fallbackReason(row);
              return (
                <div key={row.id ?? `${row.symbol}-${row.setup_type}-${index}`} className="grid min-w-[104rem] grid-cols-[8rem_7rem_1.2fr_5rem_6rem_7rem_5rem_7rem_7rem_7rem_10rem_9rem_1.7fr] gap-3 px-3 py-3 text-sm">
                  <div className="text-slate-300">{timeLabel(row.created_at)}</div>
                  <div className="font-medium text-slate-100">{row.symbol}</div>
                  <div className="min-w-0 text-slate-200">{row.setup_type}</div>
                  <div><StatusBadge value={row.direction} /></div>
                  <div><StatusBadge value={contextLabel(row.signal_session)} /></div>
                  <div><StatusBadge value={contextLabel(row.signal_regime)} /></div>
                  <div>{row.setup_score ?? Math.round(row.confidence_score)}</div>
                  <div><StatusBadge value={strategyStatus} /></div>
                  <div><StatusBadge value={decision} /></div>
                  <div><StatusBadge value={executionStatus} /></div>
                  <div className="space-y-1" title={row.follow_up?.summary ?? ""}>
                    <StatusBadge value={followUpStatusLabel(row)} />
                    <div className={followUpTone(row.follow_up?.pnl_pct)}>
                      {signedPct(row.follow_up?.pnl_pct)}
                    </div>
                  </div>
                  <div title={row.follow_up?.summary ?? ""}><StatusBadge value={verdictLabel(row)} /></div>
                  <div className="min-w-0 whitespace-normal leading-5 text-slate-300">{reasons}</div>
                </div>
              );
            })}
            {rows.length === 0 && <div className="px-3 py-8 text-center text-sm text-slate-400">Waiting for generated signals</div>}
          </div>
        </div>
        <div className="mt-4 flex items-center justify-end gap-2">
          <button
            title="Previous page"
            disabled={data.offset <= 0}
            onClick={() => setOffset(Math.max(0, offset - rowLimit))}
            className="grid h-9 w-9 place-items-center rounded border border-line bg-panel2 text-slate-200 disabled:cursor-not-allowed disabled:opacity-40"
          >
            <ChevronLeft size={18} />
          </button>
          <button
            title="Next page"
            disabled={!data.has_next}
            onClick={() => setOffset(offset + rowLimit)}
            className="grid h-9 w-9 place-items-center rounded border border-line bg-panel2 text-slate-200 disabled:cursor-not-allowed disabled:opacity-40"
          >
            <ChevronRight size={18} />
          </button>
        </div>
      </Panel>
    </div>
  );
}

function movementPct(trade: TradeRow): number {
  const mark = Number(trade.mark_price ?? trade.entry);
  const entry = Number(trade.entry);
  if (!entry || !mark) return 0;
  const raw = ((mark - entry) / entry) * 100;
  return trade.side === "short" ? -raw : raw;
}

function fallbackReason(row: SignalRow): string {
  if (!row.accepted) {
    return row.rejection_reasons?.join("; ") || `${row.setup_type} conditions were not confirmed`;
  }
  return row.reason_for_entry?.join("; ") || "strategy conditions accepted";
}

function followUpStatusLabel(row: SignalRow): string {
  const followUp = row.follow_up;
  if (!followUp) return "tracking off";
  return statusLabel(followUp.status);
}

function verdictLabel(row: SignalRow): string {
  const verdict = row.follow_up?.verdict;
  if (!verdict) return "pending";
  return statusLabel(verdict);
}

function statusLabel(value: string): string {
  return value.replace(/_/g, " ");
}

function signedPct(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  const amount = Number(value);
  const sign = amount > 0 ? "+" : "";
  return `${sign}${number2(amount)}%`;
}

function followUpTone(value: number | null | undefined): string {
  const amount = Number(value ?? 0);
  if (amount > 0) return "text-xs text-emerald-300";
  if (amount < 0) return "text-xs text-danger";
  return "text-xs text-slate-500";
}

function buildSignalParams(options: {
  decision: string;
  currentCycle: boolean;
  symbol: string;
  setup: string;
  side: string;
  executionStatus: string;
  followUpStatus: string;
  verdict: string;
  settledOnly: boolean;
  startDate: string;
  endDate: string;
  minScore: string;
  maxScore: string;
  limit?: string;
  offset?: string;
}): URLSearchParams {
  const params = new URLSearchParams({
    decision: options.decision,
    side: options.side,
    execution_status: options.executionStatus,
    follow_up_status: options.followUpStatus,
    verdict: options.verdict
  });
  if (options.limit) params.set("limit", options.limit);
  if (options.settledOnly) params.set("settled_only", "true");
  if (options.offset) params.set("offset", options.offset);
  if (options.currentCycle && !options.startDate && !options.endDate) params.set("current_cycle", "true");
  if (options.symbol.trim()) params.set("symbol", options.symbol.trim());
  if (options.setup.trim()) params.set("setup", options.setup.trim());
  if (options.startDate) params.set("start_date", options.startDate);
  if (options.endDate) params.set("end_date", options.endDate);
  if (options.minScore.trim()) params.set("min_score", options.minScore.trim());
  if (options.maxScore.trim()) params.set("max_score", options.maxScore.trim());
  return params;
}

function contextLabel(value: unknown): string {
  const label = String(value ?? "").trim();
  return label && label !== "unknown" ? label : "-";
}
