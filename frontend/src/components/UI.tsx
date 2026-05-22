import type { ReactNode } from "react";

export function Panel({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <section className={`rounded border border-line bg-panel p-4 shadow-xl shadow-black/10 ${className}`}>{children}</section>;
}

export function MetricCard({ label, value, accent = "text-mint" }: { label: string; value: ReactNode; accent?: string }) {
  return (
    <Panel>
      <div className="text-xs uppercase tracking-wide text-slate-400">{label}</div>
      <div className={`mt-2 text-2xl font-semibold ${accent}`}>{value}</div>
    </Panel>
  );
}

export function StatusBadge({ value }: { value: string }) {
  const clean = value.toLowerCase();
  const tone = clean.includes("danger") || clean.includes("loss") || clean.includes("locked") || clean.includes("bad") || clean.includes("negative") || clean.includes("lose")
    ? "bg-red-500/15 text-red-300"
    : clean.includes("hot") || clean.includes("approved") || clean.includes("running") || clean.includes("good") || clean.includes("positive") || clean.includes("win")
      ? "bg-emerald-500/15 text-emerald-300"
      : clean.includes("paper") || clean.includes("good")
        ? "bg-mint/15 text-mint"
        : "bg-amber-500/15 text-amber-300";
  return <span className={`inline-flex rounded px-2 py-1 text-xs font-medium ${tone}`}>{value}</span>;
}

export function ProgressBar({ value, danger = false }: { value: number; danger?: boolean }) {
  const width = Math.max(0, Math.min(100, value));
  return (
    <div className="h-2 overflow-hidden rounded bg-slate-800">
      <div className={`h-full ${danger ? "bg-danger" : "bg-mint"}`} style={{ width: `${width}%` }} />
    </div>
  );
}

export function DataTable<T extends Record<string, unknown>>({
  rows,
  columns
}: {
  rows: T[];
  columns: { key: keyof T; label: string; render?: (row: T) => ReactNode }[];
}) {
  return (
    <div className="overflow-x-auto rounded border border-line">
      <table className="min-w-full divide-y divide-line text-sm">
        <thead className="bg-panel2 text-xs uppercase tracking-wide text-slate-400">
          <tr>
            {columns.map((column) => (
              <th key={String(column.key)} className="px-3 py-3 text-left font-medium">
                {column.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-line">
          {rows.map((row, index) => (
            <tr key={index} className="hover:bg-panel2/70">
              {columns.map((column) => (
                <td key={String(column.key)} className="whitespace-nowrap px-3 py-3 text-slate-200">
                  {column.render ? column.render(row) : String(row[column.key] ?? "")}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
