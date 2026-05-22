import {
  Activity,
  AlertTriangle,
  BarChart3,
  Bell,
  Bot,
  Gauge,
  LineChart,
  ListFilter,
  RadioTower,
  ServerCog,
  Settings,
  Shield,
  TestTube2,
  WalletCards
} from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { apiGet } from "../api";

export type PageKey =
  | "dashboard"
  | "scanner"
  | "trades"
  | "signals"
  | "sessions"
  | "risk"
  | "performance"
  | "strategy"
  | "backtesting"
  | "exchange"
  | "telegram"
  | "logs"
  | "health";

const navItems = [
  { key: "dashboard", label: "Dashboard", icon: Gauge },
  { key: "scanner", label: "Live Scanner", icon: ListFilter },
  { key: "trades", label: "Trade Monitor", icon: Activity },
  { key: "signals", label: "Signals", icon: RadioTower },
  { key: "sessions", label: "Sessions", icon: BarChart3 },
  { key: "risk", label: "Risk Center", icon: Shield },
  { key: "performance", label: "Performance", icon: LineChart },
  { key: "strategy", label: "Strategy Lab", icon: TestTube2 },
  { key: "backtesting", label: "Backtesting", icon: WalletCards },
  { key: "exchange", label: "Exchange Settings", icon: Settings },
  { key: "telegram", label: "Telegram Alerts", icon: Bell },
  { key: "logs", label: "Logs", icon: AlertTriangle },
  { key: "health", label: "Deployment/Health", icon: ServerCog }
] as const;

type Props = {
  active: PageKey;
  onNavigate: (page: PageKey) => void;
  children: ReactNode;
};

type RuntimeSettings = {
  trading_mode: string;
  live_trading_enabled: boolean;
  futures_trading_confirmed: boolean;
};

export function Shell({ active, onNavigate, children }: Props) {
  const [settings, setSettings] = useState<RuntimeSettings | null>(null);
  const mode = settings?.trading_mode ?? "loading";
  const liveStatus = settings?.live_trading_enabled ? "LIVE ENABLED" : "LIVE LOCKED";
  const modeTone =
    mode === "testnet"
      ? "bg-amber-500/15 text-amber-300"
      : mode.includes("live")
        ? "bg-red-500/15 text-red-300"
        : "bg-emerald-500/15 text-emerald-300";

  useEffect(() => {
    let mounted = true;
    apiGet<RuntimeSettings>("/api/settings")
      .then((value) => {
        if (mounted) setSettings(value);
      })
      .catch(() => {
        if (mounted) setSettings(null);
      });
    return () => {
      mounted = false;
    };
  }, []);

  return (
    <div className="min-h-screen bg-base text-slate-100">
      <aside className="fixed inset-y-0 left-0 z-20 hidden w-72 border-r border-line bg-[#0e1528] lg:block">
        <div className="flex h-16 items-center gap-3 border-b border-line px-5">
          <div className="grid h-10 w-10 place-items-center rounded bg-mint text-base">
            <Bot size={22} />
          </div>
          <div>
            <div className="text-sm font-semibold tracking-wide">ProScalp AI Trader</div>
            <div className="text-xs text-slate-400">{mode.replace("_", " ")} mode</div>
          </div>
        </div>
        <nav className="space-y-1 p-3">
          {navItems.map((item) => {
            const Icon = item.icon;
            const selected = item.key === active;
            return (
              <button
                key={item.key}
                title={item.label}
                onClick={() => onNavigate(item.key)}
                className={`flex h-10 w-full items-center gap-3 rounded px-3 text-left text-sm transition ${
                  selected ? "bg-mint text-base" : "text-slate-300 hover:bg-panel2 hover:text-white"
                }`}
              >
                <Icon size={18} />
                <span className="truncate">{item.label}</span>
              </button>
            );
          })}
        </nav>
      </aside>
      <div className="lg:pl-72">
        <header className="sticky top-0 z-10 border-b border-line bg-base/95 px-4 py-3 backdrop-blur lg:px-6">
          <div className="flex items-center justify-between gap-3">
            <select
              value={active}
              onChange={(event) => onNavigate(event.target.value as PageKey)}
              className="h-10 w-full rounded border border-line bg-panel px-3 text-sm text-slate-100 lg:hidden"
            >
              {navItems.map((item) => (
                <option key={item.key} value={item.key}>
                  {item.label}
                </option>
              ))}
            </select>
            <div className="hidden lg:block">
              <div className="text-lg font-semibold">{navItems.find((item) => item.key === active)?.label}</div>
            </div>
            <div className="flex items-center gap-2 text-xs">
              <span className={`rounded px-2 py-1 ${modeTone}`}>{mode.toUpperCase().replace("_", " ")}</span>
              <span className="rounded bg-amber-500/15 px-2 py-1 text-amber-300">{liveStatus}</span>
            </div>
          </div>
        </header>
        <main className="mx-auto max-w-7xl px-4 py-5 lg:px-6">{children}</main>
      </div>
    </div>
  );
}
