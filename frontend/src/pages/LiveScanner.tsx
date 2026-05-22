import { RefreshCcw } from "lucide-react";
import { apiPost, compactNumber } from "../api";
import { useApi } from "../hooks";
import type { ScannerRow } from "../types";
import { DataTable, Panel, StatusBadge } from "../components/UI";

export function LiveScanner() {
  const { data, loading, refresh } = useApi<ScannerRow[]>("/api/scanner/top50", [], { pollIntervalMs: 15000 });

  async function runScan() {
    await apiPost("/api/scanner/run");
    await refresh();
  }

  return (
    <Panel>
      <div className="mb-4 flex items-center justify-between gap-3">
        <div>
          <div className="text-sm font-semibold">Top 50 Watchlist</div>
          <div className="text-xs text-slate-400">{loading ? "Loading" : `${data.length} symbols`}</div>
        </div>
        <button title="Run scan" onClick={runScan} className="grid h-10 w-10 place-items-center rounded bg-mint text-base">
          <RefreshCcw size={18} />
        </button>
      </div>
      <DataTable
        rows={data as unknown as Record<string, unknown>[]}
        columns={[
          { key: "rank", label: "#" },
          { key: "symbol", label: "Coin" },
          { key: "score", label: "Score" },
          { key: "liquidity_rating", label: "Liquidity" },
          { key: "spread_bps", label: "Spread bps" },
          { key: "volume", label: "Volume", render: (row) => compactNumber(Number(row.volume)) },
          { key: "volatility_pct", label: "Volatility %" },
          { key: "trade_permission", label: "Permission", render: (row) => <StatusBadge value={String(row.trade_permission)} /> }
        ]}
      />
    </Panel>
  );
}
