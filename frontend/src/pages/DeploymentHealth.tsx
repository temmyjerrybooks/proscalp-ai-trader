import { useApi } from "../hooks";
import { MetricCard, Panel, StatusBadge } from "../components/UI";

export function DeploymentHealth() {
  const { data } = useApi<Record<string, unknown>>("/health", {}, { pollIntervalMs: 10000 });
  const details = (data.details ?? {}) as Record<string, unknown>;
  return (
    <div className="space-y-4">
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <MetricCard label="API Status" value={<StatusBadge value={data.ok ? "ok" : "error"} />} />
        <MetricCard label="Database" value={<StatusBadge value={data.database ? "ok" : "error"} />} />
        <MetricCard label="Exchange" value={<StatusBadge value={data.exchange === false ? "error" : "not checked"} />} />
        <MetricCard label="Latency" value={`${String(data.latency_ms ?? 0)}ms`} />
      </div>
      <Panel>
        <div className="mb-3 text-sm font-semibold">Health Details</div>
        <pre className="overflow-auto rounded bg-panel2 p-3 text-xs text-slate-300">{JSON.stringify(details, null, 2)}</pre>
      </Panel>
    </div>
  );
}
