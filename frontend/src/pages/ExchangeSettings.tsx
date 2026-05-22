import { PlugZap, ShieldCheck } from "lucide-react";
import { useState } from "react";
import { apiGet, apiJsonPost } from "../api";
import { useApi } from "../hooks";
import { Panel, StatusBadge } from "../components/UI";

export function ExchangeSettings() {
  const { data } = useApi<Record<string, unknown>>("/api/settings", {});
  const [result, setResult] = useState<Record<string, unknown> | null>(null);
  const [busy, setBusy] = useState(false);

  async function test() {
    setBusy(true);
    try {
      setResult(await apiGet<Record<string, unknown>>("/api/exchange/private-test"));
    } finally {
      setBusy(false);
    }
  }

  async function testOrder() {
    setBusy(true);
    try {
      setResult(
        await apiJsonPost<Record<string, unknown>>("/api/exchange/test-order", {
          symbol: "BTCUSDT",
          side: "buy",
          notional_usdt: 110,
          offset_bps: 1000
        })
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <Panel>
        <div className="mb-4 flex items-center justify-between">
          <div className="text-sm font-semibold">Exchange</div>
          <div className="flex gap-2">
            <button title="Test signed API connection" disabled={busy} onClick={test} className="grid h-10 w-10 place-items-center rounded bg-mint text-base disabled:opacity-40">
              <PlugZap size={18} />
            </button>
            <button title="Place and cancel testnet order" disabled={busy || data.trading_mode !== "testnet"} onClick={testOrder} className="grid h-10 w-10 place-items-center rounded bg-amber-500 text-base disabled:opacity-40">
              <ShieldCheck size={18} />
            </button>
          </div>
        </div>
        <div className="grid gap-4 md:grid-cols-2">
          <div className="rounded border border-line bg-panel2 p-3">
            <div className="text-xs text-slate-400">Exchange</div>
            <div className="mt-1 text-lg font-semibold">{String(data.exchange ?? "binance")}</div>
          </div>
          <div className="rounded border border-line bg-panel2 p-3">
            <div className="text-xs text-slate-400">Mode</div>
            <div className="mt-1"><StatusBadge value={String(data.trading_mode ?? "paper")} /></div>
          </div>
          <div className="rounded border border-line bg-panel2 p-3">
            <div className="text-xs text-slate-400">Market</div>
            <div className="mt-1 text-lg font-semibold">{String(data.market_type ?? "futures")}</div>
          </div>
          <div className="rounded border border-line bg-panel2 p-3">
            <div className="text-xs text-slate-400">Permissions</div>
            <div className="mt-1"><StatusBadge value={data.live_trading_enabled ? "live enabled" : "live locked"} /></div>
          </div>
        </div>
      </Panel>
      {result && (
        <Panel>
          <div className="mb-3 flex items-center justify-between">
            <div className="text-sm font-semibold">Last Exchange Test</div>
            <StatusBadge value={result.ok ? "ok" : "failed"} />
          </div>
          <pre className="overflow-auto rounded bg-panel2 p-3 text-xs text-slate-300">{JSON.stringify(result, null, 2)}</pre>
        </Panel>
      )}
    </div>
  );
}
