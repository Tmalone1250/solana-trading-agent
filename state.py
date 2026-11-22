"""Shared state models and LangGraph state schema for the 3-State Focus Machine."""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional, TypedDict

from pydantic import BaseModel, Field, model_validator


class FocusState(str, Enum):
    """3-State Focus Machine states."""

    SCANNING = "SCANNING"
    IN_POSITION = "IN_POSITION"
    WATCHING_REENTRY = "WATCHING_REENTRY"


class CandidateToken(BaseModel):
    """Normalized Solana token candidate from DexScreener."""

    mint: str
    symbol: str
    name: str = ""
    pair_address: str = ""
    dex_id: str = ""
    price_usd: float = 0.0
    market_cap_usd: float = 0.0
    liquidity_usd: float = 0.0
    volume_1h_usd: float = 0.0
    volume_5m_usd: float = 0.0
    buys_5m: int = 0
    sells_5m: int = 0
    buys_1h: int = 0
    sells_1h: int = 0
    price_change_5m_pct: float = 0.0
    price_change_1h_pct: float = 0.0
    pair_created_at_ms: Optional[int] = None
    age_seconds: float = 0.0
    boost_amount: float = 0.0
    description: str = ""
    socials: list[str] = Field(default_factory=list)


class SafetyReport(BaseModel):
    """Deterministic RugCheck + Jupiter Shield safety verdict."""

    mint: str
    passed: bool
    reasons: list[str] = Field(default_factory=list)
    rugcheck_score: Optional[int] = None
    top10_holders_pct: Optional[float] = None
    lp_locked_pct: Optional[float] = None


class AlphaDecision(BaseModel):
    """Pure Decision 1 evaluation result for a single token."""

    mint: str
    symbol: str
    narrative_strength: float
    momentum_quality: float
    is_copycat_vamp: float
    action: str  # BUY | WATCH | REJECT
    action_confidence: float
    approved: bool
    rationale: str


class ReentryDecision(BaseModel):
    """Pure Decision 1 evaluation result for Leg 2 re-entry."""

    mint: str
    symbol: str
    leg_2_momentum: float
    is_dead_cat_bounce: float
    approved: bool
    rationale: str


class PendingOrder(BaseModel):
    """Order instruction emitted by alpha_node or position_monitor_node."""

    side: str  # "BUY" or "SELL"
    mint: str
    symbol: str
    amount_atomic: int  # lamports for BUY, token atomic units for SELL
    reason: str
    target_state_after_fill: FocusState
    apply_cooldown: bool = False
    entry_liquidity_usd: float = 0.0
    entry_mc_usd: Optional[float] = None
    whale_sol_flow: Optional[float] = None
    run_peak_sol: Optional[float] = None
    strategy: str = "SCALPER"
    triggering_wallets: list[str] = Field(default_factory=list)
    breakout_telemetry: Optional[dict[str, Any]] = None
    rationale: str = ""


class ActivePosition(BaseModel):
    """Tracked state for a focused token slot (Slot 1 chart_slot or Slot 2 whale_slot)."""

    mint: str
    symbol: str
    entry_sol: float
    token_amount: int
    decimals: int = 6
    strategy: str = "SCALPER"
    triggering_wallets: list[str] = Field(default_factory=list)
    whale_shadow_exit_flag: bool = False
    opened_at: Optional[str] = None
    entry_liquidity_usd: float = 0.0
    entry_mc_usd: Optional[float] = None
    whale_sol_flow: Optional[float] = None
    breakout_telemetry: Optional[dict[str, Any]] = None
    current_sol_value: float = 0.0
    peak_sol_value: float = 0.0
    pnl_sol: float = 0.0
    pnl_pct: float = 0.0
    partial_tp_taken: bool = False
    initial_entry_sol: float = 0.0
    remaining_cost_basis_sol: float = 0.0
    realized_partial_pnl_sol: float = 0.0
    breakeven_armed: bool = False
    trail_armed: bool = False
    retrace_low_sol: Optional[float] = None
    run_peak_sol: Optional[float] = None
    watch_started_at: Optional[str] = None
    reference_token_amount: int = 0
    reentry_count: int = 0
    last_5m_buy_ratio: Optional[float] = None
    last_5m_sell_ratio: Optional[float] = None
    last_liquidity_usd: Optional[float] = None

    @model_validator(mode="after")
    def _ensure_cost_basis_fields(self) -> "ActivePosition":
        if self.initial_entry_sol <= 0.0 and self.entry_sol > 0.0:
            self.initial_entry_sol = self.entry_sol
        if self.remaining_cost_basis_sol <= 0.0 and self.entry_sol > 0.0:
            self.remaining_cost_basis_sol = self.entry_sol
        return self


class WatchlistEntry(BaseModel):
    """Persisted slot in the 5-slot rotating SCANNING watchlist."""

    mint: str
    symbol: str
    pair_address: str = ""
    added_at: str
    safety_passed: bool = False
    entry_liquidity_usd: float = 0.0
    peak_mc: float = 0.0
    low_mc: float = 0.0
    leg2_score: float = 0.0
    leg2_breakdown: Optional[dict[str, Any]] = None
    rsi_5m: Optional[float] = None
    setup_type: str = "NONE"
    scalper_eligible: bool = False
    dist_from_1h_high_pct: Optional[float] = None
    last_evaluated_at: Optional[str] = None
    latest_scores: Optional[dict[str, Any]] = None
    candidate: dict[str, Any] = Field(default_factory=dict)


class AgentGraphState(TypedDict, total=False):
    """LangGraph state dictionary passed across all nodes in a single tick."""

    focus_state: str
    sol_balance: float
    daily_start_sol: float
    daily_pnl_sol: float
    halted_daily_loss: bool

    active_position: Optional[dict[str, Any]]
    whale_position: Optional[dict[str, Any]]
    chart_positions: list[dict[str, Any]]
    whale_positions: list[dict[str, Any]]
    slot: Optional[str]
    active_watchlist: list[dict[str, Any]]
    whale_fast_track_candidates: list[dict[str, Any]]
    raw_candidates: list[dict[str, Any]]
    safe_candidates: list[dict[str, Any]]
    rejected_candidates: list[dict[str, Any]]
    alpha_decisions: list[dict[str, Any]]
    last_decision_rationale: str

    pending_order: Optional[dict[str, Any]]
    last_execution: Optional[dict[str, Any]]
    monitor_event: Optional[str]
    error: Optional[str]
