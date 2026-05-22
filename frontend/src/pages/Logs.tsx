import { timeLabel } from "../api";
import { useApi } from "../hooks";
import type { ActivityRow } from "../types";
import { Panel, StatusBadge } from "../components/UI";

export function Logs() {
  const { data, lastUpdated } = useApi<ActivityRow[]>("/api/activity", [], { pollIntervalMs: 3000 });
  return (
    <Panel>
      <div className="mb-4 flex items-center justify-between gap-3">
        <div className="text-sm font-semibold">System Logs</div>
        <div className="text-xs text-slate-500">{lastUpdated ? `Updated ${timeLabel(lastUpdated)}` : "Syncing"}</div>
      </div>
      <div className="divide-y divide-line rounded border border-line">
        {data.map((row) => (
          <div key={`${row.source}-${row.id}`} className="grid gap-3 px-3 py-3 text-sm md:grid-cols-[8rem_7rem_7rem_1fr]">
            <div className="text-slate-500">{timeLabel(row.time)}</div>
            <div><StatusBadge value={row.source} /></div>
            <div><StatusBadge value={row.symbol ?? row.type} /></div>
            <div className="text-slate-300">{row.message}</div>
          </div>
        ))}
        {data.length === 0 && <div className="px-3 py-8 text-center text-sm text-slate-400">No activity recorded yet</div>}
      </div>
    </Panel>
  );
}
