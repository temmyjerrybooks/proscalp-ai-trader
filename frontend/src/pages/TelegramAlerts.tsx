import { Send } from "lucide-react";
import { apiGet, timeLabel } from "../api";
import { useApi } from "../hooks";
import type { TelegramAlertRow } from "../types";
import { DataTable, Panel, StatusBadge } from "../components/UI";

const alertTypes = [
  "bot started",
  "new signal detected",
  "trade opened",
  "trade partially closed",
  "stop loss hit",
  "daily hard loss shutdown",
  "session aggression mode activated",
  "backtest completed"
];

export function TelegramAlerts() {
  const { data, refresh, lastUpdated } = useApi<TelegramAlertRow[]>("/api/telegram/alerts", [], { pollIntervalMs: 5000 });

  async function test() {
    await apiGet("/api/telegram/test");
    await refresh();
  }

  return (
    <div className="space-y-4">
      <Panel>
        <div className="mb-4 flex items-center justify-between">
          <div>
            <div className="text-sm font-semibold">Telegram Alerts</div>
            <div className="text-xs text-slate-500">{lastUpdated ? `Updated ${timeLabel(lastUpdated)}` : "Syncing"}</div>
          </div>
          <button title="Test alert" onClick={test} className="grid h-10 w-10 place-items-center rounded bg-mint text-base">
            <Send size={18} />
          </button>
        </div>
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
          {alertTypes.map((type) => (
            <div key={type} className="flex items-center justify-between rounded border border-line bg-panel2 p-3 text-sm">
              <span>{type}</span>
              <StatusBadge value="on" />
            </div>
          ))}
        </div>
      </Panel>
      <Panel>
        <div className="mb-4 text-sm font-semibold">Recent Deliveries</div>
        <DataTable
          rows={data as unknown as Record<string, unknown>[]}
          columns={[
            { key: "created_at", label: "Time", render: (row) => timeLabel(row.created_at as string | undefined) },
            { key: "alert_type", label: "Type" },
            { key: "delivered", label: "Status", render: (row) => <StatusBadge value={row.delivered ? "delivered" : "failed"} /> },
            { key: "message", label: "Message" },
            { key: "error", label: "Error" }
          ]}
        />
      </Panel>
    </div>
  );
}
