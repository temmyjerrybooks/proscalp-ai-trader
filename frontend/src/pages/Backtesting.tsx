import { Play } from "lucide-react";
import { useState } from "react";
import { apiJsonPost } from "../api";
import { MetricCard, Panel } from "../components/UI";

export function Backtesting() {
  const [result, setResult] = useState<Record<string, unknown> | null>(null);
  const [symbol, setSymbol] = useState("BTCUSDT");
  const [strategy, setStrategy] = useState("EMA pullback scalp");
  const [timeframe, setTimeframe] = useState("5m");
  const [sessionName, setSessionName] = useState("london");
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");

  async function run() {
    const report = await apiJsonPost<Record<string, unknown>>("/api/backtesting/run", {
      symbol,
      strategy,
      timeframe,
      session_name: sessionName,
      start_date: startDate || null,
      end_date: endDate || null
    });
    setResult(report);
  }

  return (
    <div className="space-y-4">
      <Panel>
        <div className="grid gap-3 md:grid-cols-[1fr_1.4fr_.8fr_.9fr_.95fr_.95fr_auto]">
          <select value={symbol} onChange={(event) => setSymbol(event.target.value)} className="h-10 rounded border border-line bg-panel2 px-3 text-sm"><option>BTCUSDT</option><option>ETHUSDT</option><option>SOLUSDT</option></select>
          <select value={strategy} onChange={(event) => setStrategy(event.target.value)} className="h-10 rounded border border-line bg-panel2 px-3 text-sm"><option>EMA pullback scalp</option><option>Momentum continuation scalp</option><option>VWAP reclaim scalp</option></select>
          <select value={timeframe} onChange={(event) => setTimeframe(event.target.value)} className="h-10 rounded border border-line bg-panel2 px-3 text-sm"><option>1m</option><option>3m</option><option>5m</option><option>15m</option><option>1h</option></select>
          <select value={sessionName} onChange={(event) => setSessionName(event.target.value)} className="h-10 rounded border border-line bg-panel2 px-3 text-sm"><option value="asia">Asia</option><option value="london">London</option><option value="new_york">New York</option><option value="off_session">Off-session</option></select>
          <input value={startDate} onChange={(event) => setStartDate(event.target.value)} className="h-10 rounded border border-line bg-panel2 px-3 text-sm" type="date" />
          <input value={endDate} onChange={(event) => setEndDate(event.target.value)} className="h-10 rounded border border-line bg-panel2 px-3 text-sm" type="date" />
          <button title="Run backtest" onClick={run} className="grid h-10 w-10 place-items-center rounded bg-mint text-base">
            <Play size={18} />
          </button>
        </div>
      </Panel>
      {result && (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
          <MetricCard label="Total Trades" value={String(result.total_trades ?? 0)} />
          <MetricCard label="Win Rate" value={`${String(result.win_rate ?? 0)}%`} />
          <MetricCard label="Profit Factor" value={String(result.profit_factor ?? 0)} />
          <MetricCard label="Expectancy" value={String(result.expectancy ?? 0)} />
          <MetricCard label="Candles" value={String(result.candles ?? 0)} />
        </div>
      )}
    </div>
  );
}
