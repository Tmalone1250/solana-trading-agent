export type FocusState = "SCANNING" | "IN_POSITION" | "WATCHING_REENTRY";

export interface LiveJupiterPnl {
  entry_sol: number;
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

export interface WatchlistScores {
  narrative_strength?: number;
  momentum_quality?: number;
  is_copycat_vamp?: number;
  action?: string;
  action_confidence?: number;
  leg2_score?: number;
  leg2_fast_track?: boolean;
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
}

export interface ClosedTrade {
  symbol: string;
  mint: string;
  entry_sol: number;
  exit_sol: number;
  pnl_sol: number;
  pnl_pct: number;
  exit_reason: string;
  closed_at: string;
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

export interface TelemetryPayload {
  focus_state: FocusState;
  execution_mode: "paper" | "live";
  hunting_lane: string;
  active_mint: string | null;
  active_symbol: string | null;
  active_watchlist: WatchlistSlot[];
  live_jupiter_pnl: LiveJupiterPnl | null;
  sol_balance: number;
  daily_pnl_sol: number;
  daily_max_loss_hit: boolean;
  latest_decision_1_rationale: string;
  decision_1_rationales: DecisionRationale[];
  recent_trades: ClosedTrade[];
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
