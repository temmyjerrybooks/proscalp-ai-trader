import { SlidersHorizontal } from "lucide-react";
import { useApi } from "../hooks";
import { DataTable, Panel, StatusBadge } from "../components/UI";

type StrategyRow = {
  name: string;
  enabled: boolean;
  score_threshold: number;
  session_rules: string[];
};

export function StrategyLab() {
  const { data } = useApi<StrategyRow[]>("/api/strategies", []);
  return (
    <Panel>
      <div className="mb-4 flex items-center justify-between">
        <div className="text-sm font-semibold">Strategy Controls</div>
        <button title="Tune thresholds" className="grid h-10 w-10 place-items-center rounded bg-panel2 text-mint">
          <SlidersHorizontal size={18} />
        </button>
      </div>
      <DataTable
        rows={data as unknown as Record<string, unknown>[]}
        columns={[
          { key: "name", label: "Strategy" },
          { key: "enabled", label: "State", render: (row) => <StatusBadge value={row.enabled ? "enabled" : "disabled"} /> },
          { key: "score_threshold", label: "Threshold" },
          { key: "session_rules", label: "Sessions", render: (row) => (row.session_rules as string[]).join(", ") }
        ]}
      />
    </Panel>
  );
}
