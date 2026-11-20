"""Configuration for solana-meme-agent."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL = 1_000_000_000


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

    # Narrowed Risk Parameters
    PAPER_STARTING_SOL: float = Field(default=0.7, gt=0.0)
    TRADE_SIZE_SOL: float = Field(default=0.5, gt=0.0)
    HARD_STOP_LOSS_PCT: float = Field(default=0.10, gt=0.0, lt=1.0)
    BREAKEVEN_ARM_PCT: float = Field(default=0.08, gt=0.0)
    BREAKEVEN_FLOOR_PCT: float = Field(default=0.02, ge=0.0)
    TRAIL_ARM_PCT: float = Field(default=0.12, gt=0.0)
    TRAIL_PULLBACK_PCT: float = Field(default=0.06, gt=0.0, lt=1.0)
    REENTRY_CURL_PCT: float = Field(default=0.05, gt=0.0)
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

    # Scalper Expert Entry Engine & OHLCV Cache
    OHLCV_CACHE_SECONDS: float = Field(default=90.0, gt=0.0)
    OHLCV_PACING_DELAY_SECONDS: float = Field(default=0.35, ge=0.0)
    ANTI_CHASE_HIGH_BUFFER_PCT: float = Field(default=0.05, ge=0.0, lt=1.0)
    SCALPER_MAX_RSI_5M: float = Field(default=70.0, gt=0.0, le=100.0)
    SCALPER_MIN_BUY_RATIO_5M: float = Field(default=0.58, ge=0.0, le=1.0)
    SCALPER_MIN_TXNS_5M: int = Field(default=15, ge=1)

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

    @model_validator(mode="after")
    def validate_live_keys(self) -> "AgentConfig":
        if self.EXECUTION_MODE == "live" and not self.SOLANA_PRIVATE_KEY_BS58.strip():
            raise ValueError(
                "SOLANA_PRIVATE_KEY_BS58 is required when EXECUTION_MODE='live'."
            )
        return self

    def lane_bounds(self) -> tuple[float, float, float]:
        """Return (min_mc_usd, max_mc_usd, min_age_seconds) for HUNTING_LANE."""
        if self.HUNTING_LANE == "momentum_runners":
            return (self.MIN_MC_USD, self.MAX_MC_USD, self.MIN_AGE_MINUTES * 60.0)
        return (50_000.0, 150_000.0, 0.0)


def load_config(**overrides) -> AgentConfig:
    """Instantiate AgentConfig with optional keyword overrides."""
    return AgentConfig(**overrides)
