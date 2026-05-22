export type DashboardData = {
  account_equity: number;
  account_balance?: number;
  account_equity_source?: string;
  daily_pnl_pct: number;
  daily_pnl: number;
  open_trades: number;
  session_status: string;
  market_regime: string;
  active_exchange: string;
  active_mode: string;
  daily_profit_target_progress: number;
  daily_loss_limit_progress: number;
  bot: Record<string, unknown>;
};

export type ScannerRow = {
  rank: number;
  symbol: string;
  score: number;
  liquidity_rating: number;
  spread_bps: number;
  volume: number;
  volatility_pct: number;
  trade_permission: string;
  reasons: string[];
};

export type TradeRow = {
  id: string;
  symbol: string;
  side: string;
  status?: string;
  entry: number;
  stop_loss: number;
  take_profit: { levels?: number[] } | number[] | Record<string, unknown>;
  quantity: number;
  unrealized_pnl: number;
  realized_pnl?: number;
  setup: string;
  source?: string;
  mark_price?: number;
  opened_at?: string;
  closed_at?: string | null;
  close_reason?: string | null;
  setup_score?: number | null;
  setup_grade?: string | null;
  risk_pct?: number | null;
  risk_amount?: number | null;
  score_permission?: string | null;
  score_session?: string | null;
  entry_session?: string | null;
  entry_regime?: string | null;
};

export type SignalRow = {
  id?: string;
  created_at?: string;
  symbol: string;
  setup_type: string;
  confidence_score: number;
  setup_score?: number;
  grade?: string;
  direction: string;
  reason_for_entry: string[];
  rejection_reasons: string[];
  decision_reasons?: string[];
  reason_summary?: string;
  decision?: string;
  strategy_status?: string;
  execution_status?: string;
  signal_session?: string | null;
  signal_regime?: string | null;
  trade_id?: string | null;
  trade_status?: string | null;
  follow_up?: SignalFollowUp | null;
  accepted: boolean;
};

export type SignalFollowUp = {
  status: string;
  verdict: string;
  basis: string;
  settled?: boolean;
  summary: string;
  pnl_pct?: number | null;
  max_favorable_pct?: number | null;
  max_adverse_pct?: number | null;
  exit_reason?: string | null;
  exit_price?: number | null;
  evaluated_at?: string;
  minutes_elapsed?: number;
  horizon_minutes?: number;
};

export type SignalPage = {
  items: SignalRow[];
  total: number;
  limit: number;
  offset: number;
  has_next: boolean;
  source_window: number;
  filters: Record<string, unknown>;
};

export type SessionRow = {
  name: string;
  active: boolean;
  tradable: boolean;
  aggression_mode: boolean;
  start_utc: string;
  end_utc: string;
  user_time: string;
  session_high: number | null;
  session_low: number | null;
  notes: string[];
};

export type RiskStatus = {
  daily_loss_limit_pct: number;
  risk_per_trade: Record<string, number[]>;
  off_session_risk_per_trade?: Record<string, number[]>;
  max_concurrent_trades: number;
  exposure_limits: Record<string, number>;
  trade_score_thresholds?: Record<string, number>;
  off_session_trading_enabled?: boolean;
  kill_switch: boolean;
  warnings: string[];
  live_trading_enabled: boolean;
  futures_confirmed: boolean;
};

export type Performance = {
  win_rate: number;
  profit_factor: number;
  average_win: number;
  average_loss: number;
  max_drawdown: number;
  best_strategy: string;
  worst_strategy: string;
  pnl_chart: number[];
  total_trades?: number;
  total_pnl?: number;
};

export type ActivityRow = {
  id: string;
  time: string;
  source: string;
  type: string;
  severity: string;
  symbol?: string | null;
  message: string;
};

export type TelegramAlertRow = {
  id: string;
  alert_type: string;
  message: string;
  delivered: boolean;
  error?: string | null;
  created_at: string;
};
