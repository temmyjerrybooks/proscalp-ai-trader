import { ReactElement, useState } from "react";
import { Shell, type PageKey } from "./components/Shell";
import { Backtesting } from "./pages/Backtesting";
import { Dashboard } from "./pages/Dashboard";
import { DeploymentHealth } from "./pages/DeploymentHealth";
import { ExchangeSettings } from "./pages/ExchangeSettings";
import { LiveScanner } from "./pages/LiveScanner";
import { Logs } from "./pages/Logs";
import { Performance } from "./pages/Performance";
import { RiskCenter } from "./pages/RiskCenter";
import { Sessions } from "./pages/Sessions";
import { Signals } from "./pages/Signals";
import { StrategyLab } from "./pages/StrategyLab";
import { TelegramAlerts } from "./pages/TelegramAlerts";
import { TradeMonitor } from "./pages/TradeMonitor";

const pages: Record<PageKey, ReactElement> = {
  dashboard: <Dashboard />,
  scanner: <LiveScanner />,
  trades: <TradeMonitor />,
  signals: <Signals />,
  sessions: <Sessions />,
  risk: <RiskCenter />,
  performance: <Performance />,
  strategy: <StrategyLab />,
  backtesting: <Backtesting />,
  exchange: <ExchangeSettings />,
  telegram: <TelegramAlerts />,
  logs: <Logs />,
  health: <DeploymentHealth />
};

export default function App() {
  const [active, setActive] = useState<PageKey>("dashboard");
  return (
    <Shell active={active} onNavigate={setActive}>
      {pages[active]}
    </Shell>
  );
}
