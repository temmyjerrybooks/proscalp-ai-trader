import { useApi } from "../hooks";
import type { Performance as PerformanceType } from "../types";
import { MetricCard, Panel } from "../components/UI";

const fallback: PerformanceType = {
  win_rate: 0,
  profit_factor: 0,
  average_win: 0,
  average_loss: 0,
  max_drawdown: 0,
  best_strategy: "pending",
  worst_strategy: "pending",
  pnl_chart: [10000, 10000]
};

export function Performance() {
  const { data } = useApi<PerformanceType>("/api/performance", fallback, { pollIntervalMs: 10000 });
  const min = Math.min(...data.pnl_chart);
  const max = Math.max(...data.pnl_chart);
  const points = data.pnl_chart
    .map((value, index) => {
      const x = (index / Math.max(1, data.pnl_chart.length - 1)) * 100;
      const y = 90 - ((value - min) / Math.max(1, max - min)) * 70;
      return `${x},${y}`;
    })
    .join(" ");

  return (
    <div className="space-y-4">
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-5">
        <MetricCard label="Win Rate" value={`${data.win_rate}%`} />
        <MetricCard label="Profit Factor" value={data.profit_factor} />
        <MetricCard label="Average Win" value={data.average_win} />
        <MetricCard label="Average Loss" value={data.average_loss} accent="text-danger" />
        <MetricCard label="Max Drawdown" value={`${data.max_drawdown}%`} accent="text-amber-300" />
        <MetricCard label="Closed Trades" value={data.total_trades ?? 0} />
      </div>
      <Panel>
        <div className="mb-3 flex justify-between text-sm">
          <span>Equity Curve</span>
          <span className="text-slate-400">{data.best_strategy} / {data.worst_strategy}</span>
        </div>
        <svg viewBox="0 0 100 100" className="h-56 w-full rounded bg-panel2">
          <polyline fill="none" stroke="#2dd4bf" strokeWidth="2" points={points} />
        </svg>
      </Panel>
    </div>
  );
}
