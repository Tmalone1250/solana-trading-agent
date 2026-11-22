"""Configuration for solana-meme-agent."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL = 1_000_000_000

DEFAULT_WHALE_WALLET_LABELS: dict[str, str] = {
    "862TYS3qj1vRGWo2": "Wugi",
    "J9TYAsWWidbrcZybmLSfrLzryANf4CgJBLdvwdGuC8MB": "Johnson",
    "4BdKaxN8G6ka4GYtQQWk4G4dZRUTX2vQH9GcXdBREFUk": "Jijo",
    "BQVz7fQ1WsQmSTMY3umdPEPPTm1sdcBcX9sP7o6kPRmB": "Limfork.eth",
    "387FRwow6MKDhSwuMULhKjqeXuxoBY4BbbaSntdk9LaH": "Shawty",
    "ardinRsN1mNYVeoJWTBsWeYeXvuR9UUDGMsCDKpb6AT": "trunoest",
    "4vw54B78b2h7rw9Ud9": "decu",
}


class AgentConfig(BaseSettings):
    """Runtime configuration loaded from environment variables / .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Core modes
    EXECUTION_MODE: Literal["paper", "live"] = "paper"
    HUNTING_LANE: Literal["momentum_runners", "graduations"] = "momentum_runners"

    # API Keys
    GEMINI_API_KEY: str = ""
    JUPITER_API_KEY: str = ""
    SOLANA_PRIVATE_KEY_BS58: str = ""

    # Narrowed Risk Parameters & Dual-Lane 3+3 Concurrency
    PAPER_STARTING_SOL: float = Field(default=0.7, gt=0.0)
    PAPER_TX_FEE_SOL: float = Field(default=0.001, ge=0.0)
    TRADE_SIZE_SOL: float = Field(default=0.5, gt=0.0)
    MAX_CHART_POSITIONS: int = Field(default=3, ge=1)
    MAX_WHALE_POSITIONS: int = Field(default=3, ge=1)
    MAX_OPEN_POSITIONS: int = Field(default=6, ge=1)
    HARD_STOP_LOSS_PCT: float = Field(default=0.10, gt=0.0, lt=1.0)
    BREAKEVEN_ARM_PCT: float = Field(default=0.08, gt=0.0)
    BREAKEVEN_FLOOR_PCT: float = Field(default=0.02, ge=0.0)
    TRAIL_ARM_PCT: float = Field(default=0.12, gt=0.0)
    TRAIL_PULLBACK_PCT: float = Field(default=0.06, gt=0.0, lt=1.0)
    PARTIAL_TP_TRIGGER_PCT: float = Field(default=15.0, gt=0.0)
    PARTIAL_TP_SELL_FRACTION: float = Field(default=0.50, gt=0.0, lt=1.0)
    RUNNER_TRAIL_PULLBACK_PCT: float = Field(default=10.0, gt=0.0, lt=100.0)
    REENTRY_CURL_PCT: float = Field(default=0.05, gt=0.0)
    MAX_REENTRY_CHASE_PCT: float = Field(default=0.08, gt=0.0, lt=1.0)
    MAX_REENTRIES_PER_MINT: int = Field(default=2, ge=0)
    EXODUS_SELL_RATIO: float = Field(default=0.65, gt=0.0, le=1.0)
    EXODUS_LIQ_DROP_PCT: float = Field(default=0.10, gt=0.0, le=1.0)
    WATCH_TIMEOUT_MINUTES: int = Field(default=20, gt=0)
    DAILY_MAX_LOSS_PCT: float = Field(default=0.20, gt=0.0, le=1.0)

    # Scanner, Rotating Watchlist & Safety Thresholds
    MAX_WATCH_BATCH: int = Field(default=5, gt=0)
    MIN_WATCHLIST_SOAK_MINUTES: float = Field(default=3.0, ge=0.0)
    MIN_MC_USD: float = Field(default=100_000.0, ge=0.0)
    MAX_MC_USD: float = Field(default=3_000_000.0, ge=0.0)
    MIN_AGE_MINUTES: float = Field(default=30.0, ge=0.0)
    MIN_VOL_1H_USD: float = Field(default=30_000.0, ge=0.0)
    MIN_LIQUIDITY_USD: float = Field(default=25_000.0, ge=0.0)
    MIN_VOLUME_1H_USD: float = Field(default=30_000.0, ge=0.0)
    MAX_TOP10_HOLDERS_PCT: float = Field(default=20.0, gt=0.0, le=100.0)
    MIN_LP_LOCKED_PCT: float = Field(default=95.0, ge=0.0, le=100.0)
    COOLDOWN_HOURS: float = Field(default=2.0, gt=0.0)
    SAFETY_REJECT_COOLDOWN_MINUTES: int = Field(default=60, gt=0)
    VAMP_DROP_COOLDOWN_MINUTES: int = Field(default=20, gt=0)
    LOW_SCORE_COOLDOWN_MINUTES: int = Field(default=15, gt=0)
    STALE_WATCH_TIMEOUT_MINUTES: int = Field(default=15, gt=0)
    STALE_WATCH_COOLDOWN_MINUTES: int = Field(default=10, gt=0)
    VAMP_EVICT_THRESHOLD: float = Field(default=0.85, ge=0.0, le=1.0)
    VAMP_WARN_THRESHOLD: float = Field(default=0.75, ge=0.0, le=1.0)
    VAMP_CONSECUTIVE_STRIKES: int = Field(default=2, ge=1)
    LOW_SCORE_EVICT_THRESHOLD: float = Field(default=2.0, ge=0.0, le=5.0)

    # Decision 1 & Leg 2 Entry Thresholds
    DECISION_MODEL: str = "gemini-decision-1"
    MIN_BUY_CONFIDENCE: float = Field(default=0.60, ge=0.0, le=1.0)
    MIN_NARRATIVE_SCORE: float = Field(default=2.5, ge=0.0, le=5.0)
    MIN_MOMENTUM_SCORE: float = Field(default=3.0, ge=0.0, le=5.0)
    MAX_VAMP_ENTRY: float = Field(default=0.55, ge=0.0, le=1.0)
    LEG2_FAST_TRACK_MIN_SCORE: float = Field(default=70.0, ge=0.0, le=100.0)
    LEG2_FAST_TRACK_WATCH_SCORE: float = Field(default=80.0, ge=0.0, le=100.0)
    LEG2_FAST_TRACK_MIN_MOM: float = Field(default=2.8, ge=0.0, le=5.0)
    LEG2_FAST_TRACK_MIN_CONF: float = Field(default=0.55, ge=0.0, le=1.0)
    REENTRY_MIN_BUY_RATIO: float = Field(default=0.58, ge=0.0, le=1.0)
    REENTRY_MIN_LEG2_SCORE: float = Field(default=3.5, ge=0.0, le=5.0)
    REENTRY_MIN_LEG2_PREDICTOR_SCORE: float = Field(default=65.0, ge=0.0, le=100.0)
    REENTRY_MAX_DEAD_CAT: float = Field(default=0.25, ge=0.0, le=1.0)
    WATCH_MAX_BLEED_PCT: float = Field(default=0.25, gt=0.0, lt=1.0)

    # Scalper Expert Entry Engine, BREAKOUT_IGNITION & OHLCV Cache
    OHLCV_CACHE_SECONDS: float = Field(default=90.0, gt=0.0)
    OHLCV_PACING_DELAY_SECONDS: float = Field(default=6.5, ge=0.0)
    ANTI_CHASE_HIGH_BUFFER_PCT: float = Field(default=0.05, ge=0.0, lt=1.0)
    SCALPER_MAX_RSI_5M: float = Field(default=70.0, gt=0.0, le=100.0)
    SCALPER_MIN_BUY_RATIO_5M: float = Field(default=0.58, ge=0.0, le=1.0)
    SCALPER_MIN_TXNS_5M: int = Field(default=15, ge=1)
    BREAKOUT_MIN_VOL_RATIO: float = Field(default=1.5, gt=0.0)
    BREAKOUT_MAX_COIL_RANGE_PCT: float = Field(default=10.0, gt=0.0)
    BREAKOUT_MIN_SCORE: int = Field(default=75, ge=0, le=100)

    # Jupiter Token API v2 Discovery & Organic Enrichment
    ENABLE_JUPITER_DISCOVERY: bool = True
    JUPITER_TOKENS_V2_BASE_URL: str = "https://api.jup.ag/tokens/v2"
    JUPITER_POLL_CACHE_SECONDS: float = Field(default=60.0, gt=0.0)
    JUPITER_MIN_ORGANIC_SCORE: float = Field(default=0.0, ge=0.0, le=100.0)
    JUPITER_REQUEST_DELAY_SECONDS: float = Field(default=0.25, ge=0.0)

    # X (TwitterAPI.io) Discovery & Sentiment
    ENABLE_X_DISCOVERY: bool = False
    TWITTERAPI_IO_KEY: str = ""
    TWITTERAPI_IO_BASE_URL: str = "https://api.twitterapi.io"
    X_LIST_IDS: str = ""
    X_SEARCH_QUERY: str = '"pump" (solana OR dexscreener) min_faves:5 -filter:retweets'
    X_POLL_INTERVAL_MINUTES: int = Field(default=30, gt=0)
    X_DAILY_TWEET_CAP: int = Field(default=1000, gt=0)
    X_SIGNAL_TTL_MINUTES: int = Field(default=60, gt=0)

    # Helius Enhanced Webhook Whale Tracking
    ENABLE_HELIUS_WHALE_WEBHOOK: bool = False
    HELIUS_API_KEY: str = ""
    HELIUS_WEBHOOK_ID: str = ""
    HELIUS_WEBHOOK_URL: str = ""
    HELIUS_API_BASE_URL: str = "https://api.helius.xyz/v0"
    HELIUS_ROSTER_CACHE_SECONDS: float = Field(default=300.0, gt=0.0)
    HELIUS_AUTH_HEADER: str = ""
    HELIUS_TRACKED_WALLETS: str = ""
    WHALE_WALLET_LABELS: dict[str, str] = Field(
        default_factory=lambda: dict(DEFAULT_WHALE_WALLET_LABELS)
    )
    WHALE_SIGNAL_TTL_MINUTES: int = Field(default=30, gt=0)
    WHALE_MIN_SOL_SPENT: float = Field(default=0.5, ge=0.0)

    # WHALE_SCALP Fast-Track Strategy (Slot 2: Multi-Whale Confluence up to $50M MCAP)
    WHALE_STRATEGY_ENABLED: bool = True
    WHALE_POSITION_SOL: float = Field(default=0.25, gt=0.0)
    WHALE_MIN_SOLO_BUY_SOL: float = Field(default=1.5, gt=0.0)
    WHALE_MIN_UNIQUE_WALLETS: int = Field(default=2, ge=1)
    WHALE_MIN_CONFLUENCE_WALLETS: int = Field(default=2, ge=1)
    WHALE_ENTRY_MAX_AGE_SECONDS: int = Field(default=60, gt=0)
    WHALE_MIN_MCAP_USD: float = Field(default=15_000.0, ge=0.0)
    WHALE_MAX_MCAP_USD: float = Field(default=50_000_000.0, gt=0.0)
    WHALE_MIN_LIQ_USD: float | None = Field(default=None, ge=0.0)
    WHALE_MIN_LIQUIDITY_USD: float = Field(default=40_000.0, ge=0.0)
    WHALE_TP_PCT: float = Field(default=15.0, gt=0.0)
    WHALE_PARTIAL_TP_TRIGGER_PCT: float | None = Field(default=None, gt=0.0)
    WHALE_PARTIAL_TP_SELL_FRACTION: float | None = Field(default=None, gt=0.0, lt=1.0)
    WHALE_RUNNER_TRAIL_PULLBACK_PCT: float | None = Field(default=None, gt=0.0, lt=100.0)
    WHALE_BREAKEVEN_ARM_PCT: float = Field(default=5.0, gt=0.0)
    WHALE_BREAKEVEN_FLOOR_PCT: float = Field(default=1.0, ge=0.0)
    WHALE_TRAIL_ARM_PCT: float = Field(default=8.0, gt=0.0)
    WHALE_TRAIL_DROP_PCT: float = Field(default=3.5, gt=0.0, lt=100.0)
    WHALE_TIGHT_TRAIL_ARM_PCT: float = Field(default=12.0, gt=0.0)
    WHALE_TIGHT_TRAIL_DROP_PCT: float = Field(default=2.5, gt=0.0, lt=100.0)
    WHALE_HARD_STOP_PCT: float = Field(default=-10.0, lt=0.0, gt=-100.0)
    WHALE_TIME_STOP_SECONDS: int = Field(default=300, gt=0)
    WHALE_TIME_STOP_MIN_PNL_PCT: float = Field(default=4.0)
    WHALE_MONITOR_POLL_SECONDS: float = Field(default=2.0, gt=0.0)
    WHALE_MIN_BREAKOUT_SCORE: int = Field(default=50, ge=0, le=100)
    WHALE_BLOCK_EXTENDED: bool = True

    # Endpoints
    DEXSCREENER_BASE_URL: str = "https://api.dexscreener.com"
    GECKOTERMINAL_BASE_URL: str = "https://api.geckoterminal.com/api/v2"
    GECKOTERMINAL_CACHE_SECONDS: float = Field(default=60.0, gt=0.0)
    RUGCHECK_BASE_URL: str = "https://api.rugcheck.xyz"
    JUPITER_BASE_URL: str = "https://api.jup.ag"

    # Loop, Persistence & Telemetry
    MONITOR_POLL_SECONDS: float = Field(default=5.0, gt=0.0)
    SCAN_POLL_SECONDS: float = Field(default=15.0, gt=0.0)
    DUCKDB_PATH: str = "solana_meme_agent.duckdb"
    TELEMETRY_HOST: str = "0.0.0.0"
    TELEMETRY_PORT: int = 8001
    AGENT_LOCAL_URL: str = "http://127.0.0.1:8002"

    @model_validator(mode="after")
    def validate_live_keys(self) -> "AgentConfig":
        if self.EXECUTION_MODE == "live" and not self.SOLANA_PRIVATE_KEY_BS58.strip():
            raise ValueError(
                "SOLANA_PRIVATE_KEY_BS58 is required when EXECUTION_MODE='live'."
            )
        if self.WHALE_PARTIAL_TP_TRIGGER_PCT is None:
            self.WHALE_PARTIAL_TP_TRIGGER_PCT = float(self.PARTIAL_TP_TRIGGER_PCT)
        if self.WHALE_PARTIAL_TP_SELL_FRACTION is None:
            self.WHALE_PARTIAL_TP_SELL_FRACTION = float(self.PARTIAL_TP_SELL_FRACTION)
        if self.WHALE_RUNNER_TRAIL_PULLBACK_PCT is None:
            self.WHALE_RUNNER_TRAIL_PULLBACK_PCT = float(self.RUNNER_TRAIL_PULLBACK_PCT)
        return self

    def lane_bounds(self) -> tuple[float, float, float]:
        """Return (min_mc_usd, max_mc_usd, min_age_seconds) for HUNTING_LANE."""
        if self.HUNTING_LANE == "momentum_runners":
            return (self.MIN_MC_USD, self.MAX_MC_USD, self.MIN_AGE_MINUTES * 60.0)
        return (50_000.0, 150_000.0, 0.0)

    def x_list_id_list(self) -> list[str]:
        """Return parsed comma-separated X_LIST_IDS."""
        return [item.strip() for item in self.X_LIST_IDS.split(",") if item.strip()]

    def helius_tracked_wallet_set(self) -> set[str]:
        """Return parsed comma-separated HELIUS_TRACKED_WALLETS."""
        return {item.strip() for item in self.HELIUS_TRACKED_WALLETS.split(",") if item.strip()}

    def whale_min_unique_wallets(self) -> int:
        """Return required unique tracked wallets for WHALE_SCALP confluence."""
        return max(int(self.WHALE_MIN_UNIQUE_WALLETS), int(self.WHALE_MIN_CONFLUENCE_WALLETS))

    def whale_min_liquidity_usd(self) -> float:
        """Return WHALE_SCALP liquidity floor (WHALE_MIN_LIQ_USD takes precedence if set)."""
        if self.WHALE_MIN_LIQ_USD is not None:
            return float(self.WHALE_MIN_LIQ_USD)
        return float(self.WHALE_MIN_LIQUIDITY_USD)

    def resolve_wallet_label(self, wallet: str) -> str:
        """Resolve a human-readable Kolscan/custom label or fall back to truncated AbCd...wXyZ."""
        addr = (wallet or "").strip()
        if not addr:
            return "unknown"
        if addr in self.WHALE_WALLET_LABELS:
            return self.WHALE_WALLET_LABELS[addr]
        for key, label in self.WHALE_WALLET_LABELS.items():
            if key and addr.startswith(key):
                return label
        if len(addr) > 10:
            return f"{addr[:4]}...{addr[-4:]}"
        return addr


def load_config(**overrides) -> AgentConfig:
    """Instantiate AgentConfig with optional keyword overrides."""
    return AgentConfig(**overrides)
