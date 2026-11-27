export type FocusState = "SCANNING" | "IN_POSITION" | "WATCHING_REENTRY";

export interface LiveJupiterPnl {
  slot?: "chart_slot" | "whale_slot" | string;
  mint?: string | null;
  symbol?: string | null;
  strategy?: string;
  triggering_wallets?: string[];
  entry_sol: number;
  initial_entry_sol?: number;
  remaining_cost_basis_sol?: number;
  partial_tp_taken?: boolean;
  realized_partial_pnl_sol?: number;
  current_sol_value: number;
  peak_sol_value: number;
  pnl_sol: number;
  pnl_pct: number;
  breakeven_armed: boolean;
  trail_armed: boolean;
  retrace_low_sol: number | null;
  run_peak_sol: number | null;
  watch_started_at: string | null;
  last_5m_buy_ratio: number | null;
  last_5m_sell_ratio: number | null;
  last_liquidity_usd: number | null;
}

export interface BreakoutTelemetry {
  score: number;
  state: "FORMING" | "COILING" | "IGNITION" | "EXTENDED" | string;
  timeframe: "5m" | "1m_fallback" | string;
  vol_ratio: number;
  ema_ext_pct: number;
  passed_gates: string[];
  failed_gates: string[];
}

export interface WatchlistScores {
  narrative_strength?: number;
  momentum_quality?: number;
  is_copycat_vamp?: number;
  action?: string;
  action_confidence?: number;
  leg2_score?: number;
  leg2_fast_track?: boolean;
  breakout_telemetry?: BreakoutTelemetry | null;
  approved?: boolean;
}

export interface WatchlistSlot {
  mint: string;
  symbol: string;
  pair_address?: string;
  added_at: string;
  safety_passed: boolean;
  market_cap_usd?: number;
  liquidity_usd?: number;
  peak_mc: number;
  low_mc: number;
  leg2_score: number;
  vamp_strikes?: number;
  rsi_5m: number | null;
  setup_type: string;
  scalper_eligible: boolean;
  dist_from_1h_high_pct: number | null;
  status?: "APPROVED" | "BLOCKED" | "SOAKING" | "READY" | string;
  time_in_slot_seconds: number;
  time_in_slot_minutes: number;
  last_evaluated_at: string | null;
  scores: WatchlistScores | null;
  breakout_telemetry?: BreakoutTelemetry | null;
}

export interface ClosedTrade {
  symbol: string;
  mint: string;
  entry_sol: number | null;
  exit_sol: number;
  pnl_sol: number | null;
  pnl_pct: number | null;
  gross_pnl_sol?: number | null;
  gross_pnl_incomplete?: boolean;
  net_pnl_known?: boolean;
  lifecycle_complete?: boolean;
  fee_accounting_status?: string;
  exit_reason: string;
  strategy?: string;
  closed_at: string;
}

export interface ClosedTradesSummary {
  total_trades: number;
  known_trades?: number;
  unknown_trades?: number;
  wins: number;
  losses: number;
  win_rate_pct: number | null;
  win_rate_basis?: string;
  net_pnl_sol: number | null;
}

export interface DecisionRationale {
  timestamp: string;
  mint: string;
  symbol: string;
  phase: string;
  action: string;
  confidence: number | null;
  rationale: string;
}

export interface WhaleSignal {
  mint: string;
  buy_count: number;
  unique_whales: number;
  total_sol_spent: number;
  sell_count: number;
  last_seen_utc: string;
  agent_status?: string | null;
  agent_reason?: string | null;
  breakout_telemetry?: BreakoutTelemetry | null;
}

export interface WhaleTradeWallet {
  wallet: string;
  label: string;
}

export interface WhaleTrade {
  status: "OPEN" | "CLOSED" | string;
  symbol: string;
  mint: string;
  entry_sol: number;
  exit_sol: number;
  pnl_sol: number;
  pnl_pct: number;
  partial_tp_taken?: boolean;
  realized_partial_pnl_sol?: number;
  exit_reason: string;
  opened_at: string | null;
  closed_at: string | null;
  hold_seconds: number | null;
  triggering_wallets: WhaleTradeWallet[];
}

export interface WhaleStats {
  total_trades: number;
  wins: number;
  losses: number;
  win_rate_pct: number;
  total_pnl_sol: number;
  avg_hold_seconds: number;
}

export interface WhaleEvent {
  signature: string;
  wallet: string;
  wallet_label: string;
  mint: string;
  side: "BUY" | "SELL" | string;
  sol_amount: number;
  token_amount: number;
  timestamp_utc: string;
}

export interface WhaleRosterEntry {
  wallet: string;
  wallet_label: string;
  buys_24h: number;
  sells_24h: number;
  total_sol_buys: number;
  total_sol_sells: number;
  total_sol_volume: number;
  last_seen_utc: string | null;
  last_mint: string | null;
  last_side: string | null;
}

export interface WhaleDiscoveryPayload {
  enabled: boolean;
  tracked_wallet_count: number;
  active_whale_mints: number;
  events_today: number;
  total_sol_volume_today: number;
  active_signals: WhaleSignal[];
  recent_events: WhaleEvent[];
  roster: WhaleRosterEntry[];
  whale_trades?: WhaleTrade[];
  whale_stats?: WhaleStats;
}

export interface TelemetryPayload {
  focus_state: FocusState;
  execution_mode: "paper" | "live";
  hunting_lane: string;
  active_mint: string | null;
  active_symbol: string | null;
  chart_positions?: LiveJupiterPnl[];
  whale_positions?: LiveJupiterPnl[];
  active_positions?: LiveJupiterPnl[];
  max_chart_positions?: number;
  max_whale_positions?: number;
  active_watchlist: WatchlistSlot[];
  whale_discovery?: WhaleDiscoveryPayload;
  live_jupiter_pnl: LiveJupiterPnl | null;
  sol_balance: number;
  daily_pnl_sol: number;
  daily_max_loss_hit: boolean;
  latest_decision_1_rationale: string;
  decision_1_rationales: DecisionRationale[];
  recent_trades: ClosedTrade[];
  closed_trades_summary?: ClosedTradesSummary;
  active_cooldowns: Array<{ mint: string; expires_at: string; reason: string }>;
  updated_at: string;
}

export function formatUsdCompact(value?: number | null): string {
  if (!value || value <= 0) return "—";
  if (value >= 1_000_000) return `$${(value / 1_000_000).toFixed(2)}M`;
  if (value >= 1_000) return `$${(value / 1_000).toFixed(1)}K`;
  return `$${value.toFixed(0)}`;
}

export function formatSol(value?: number | null, signed = false): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  const prefix = signed && value > 0 ? "+" : "";
  return `${prefix}${value.toFixed(4)} SOL`;
}

export function formatShortTime(iso?: string | null): string {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  } catch {
    return iso;
  }
}

export function formatRelativeTime(iso?: string | null): string {
  if (!iso) return "—";
  try {
    const diffSec = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000));
    if (diffSec < 60) return `${diffSec}s ago`;
    const diffMin = Math.floor(diffSec / 60);
    if (diffMin < 60) return `${diffMin}m ago`;
    const diffHr = Math.floor(diffMin / 60);
    if (diffHr < 24) return `${diffHr}h ago`;
    return `${Math.floor(diffHr / 24)}d ago`;
  } catch {
    return iso;
  }
}

export function truncateAddress(addr?: string | null): string {
  if (!addr) return "—";
  if (addr.length <= 10) return addr;
  return `${addr.slice(0, 4)}...${addr.slice(-4)}`;
}
