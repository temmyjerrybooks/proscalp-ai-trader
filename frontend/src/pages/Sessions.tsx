import { useApi } from "../hooks";
import type { SessionRow } from "../types";
import { Panel, StatusBadge } from "../components/UI";

export function Sessions() {
  const { data } = useApi<SessionRow[]>("/api/sessions", [], { pollIntervalMs: 10000 });
  return (
    <div className="grid gap-4 lg:grid-cols-3">
      {data.map((session) => (
        <Panel key={session.name}>
          <div className="mb-3 flex items-center justify-between">
            <div className="text-sm font-semibold capitalize">{session.name.replace("_", " ")}</div>
            <StatusBadge value={session.aggression_mode ? "aggression" : session.active ? "active" : "closed"} />
          </div>
          <div className="space-y-2 text-sm text-slate-300">
            <div className="flex justify-between"><span>Tradable</span><span>{session.tradable ? "Yes" : "No"}</span></div>
            <div className="flex justify-between"><span>High</span><span>{session.session_high ?? "-"}</span></div>
            <div className="flex justify-between"><span>Low</span><span>{session.session_low ?? "-"}</span></div>
            <div className="text-xs text-slate-400">{session.notes.join(" · ")}</div>
          </div>
        </Panel>
      ))}
    </div>
  );
}
