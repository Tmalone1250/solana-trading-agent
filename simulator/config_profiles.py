"""Explicit, hermetic configuration profiles for the BREAKOUT_IGNITION Replay Simulator.

Never reads `.env`, secret files, or `os.environ`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic_settings import SettingsConfigDict

from config import AgentConfig
from simulator import (
    EXACT_HISTORICAL,
    RUNTIME_CODE_PROVENANCE,
    UNAVAILABLE,
)

PROFILE_REPO_DEFAULTS = "repo_defaults"
PROFILE_SUPPLIED_PHASE1_PI = "supplied_phase1_pi_snapshot"

PROVENANCE_REPO_DEFAULTS = "CODE_DEFAULTS_ONLY_NOT_HISTORICAL_PI_CONFIG"
PROVENANCE_SUPPLIED_PI_SNAPSHOT = (
    "SUPPLIED_PI_ENV_SNAPSHOT_NOT_VERIFIED_AS_PID_8315_EFFECTIVE_RUNTIME"
)
PROVENANCE_OMITTED_DEFAULT = "EXPECTED_FROM_CODE_DEFAULT_OMITTED_IN_SUPPLIED_SNAPSHOT"


class _HermeticAgentConfig(AgentConfig):
    """Subclass of AgentConfig that disables os.environ, .env, and secret-file lookups."""

    model_config = SettingsConfigDict(
        env_file=None,
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[Any],
        init_settings: Any,
        env_settings: Any,
        dotenv_settings: Any,
        file_secret_settings: Any,
    ) -> tuple[Any, ...]:
        # Strictly restrict configuration inputs to explicit Python kwargs (`init_settings`).
        return (init_settings,)


# Exact non-secret key-value snapshot supplied for Phase 1 Raspberry Pi review.
# Note: Does NOT prove PID 8315's exact historical in-memory configuration.
SUPPLIED_PHASE1_PI_SNAPSHOT_OVERRIDES: dict[str, Any] = {
    "EXECUTION_MODE": "paper",
    "CHART_ENABLED_STRATEGIES": "BREAKOUT_IGNITION",
    "WHALE_STRATEGY_ENABLED": False,
    "ENABLE_HELIUS_WHALE_WEBHOOK": False,
    "MAX_REENTRIES_PER_MINT": 0,
    "PAPER_STARTING_SOL": 3.0,
    "TRADE_SIZE_SOL": 0.5,
    "HARD_STOP_LOSS_PCT": 0.12,
    "BREAKEVEN_ARM_PCT": 0.08,
    "BREAKEVEN_FLOOR_PCT": 0.02,
    "TRAIL_ARM_PCT": 0.12,
    "TRAIL_PULLBACK_PCT": 0.06,
    "REENTRY_CURL_PCT": 0.05,
    "EXODUS_SELL_RATIO": 0.65,
    "EXODUS_LIQ_DROP_PCT": 0.10,
    "WATCH_TIMEOUT_MINUTES": 10,
    "DAILY_MAX_LOSS_PCT": 0.20,
    "MAX_WATCH_BATCH": 5,
    "MIN_WATCHLIST_SOAK_MINUTES": 1.0,
    "OHLCV_CACHE_SECONDS": 180.0,
    "ANTI_CHASE_HIGH_BUFFER_PCT": 0.05,
    "BREAKOUT_MIN_VOL_RATIO": 0.7,
    "BREAKOUT_MAX_COIL_RANGE_PCT": 7.0,
    "BREAKOUT_MIN_SCORE": 55,
    "MAX_OPEN_POSITIONS": 6,
    "MAX_CHART_POSITIONS": 3,
    "MAX_WHALE_POSITIONS": 3,
    "WHALE_MIN_BREAKOUT_SCORE": 25,
    "WHALE_BLOCK_EXTENDED": True,
    "MIN_MC_USD": 100_000.0,
    "MAX_MC_USD": 3_000_000.0,
    "MIN_AGE_MINUTES": 3.0,
    "MIN_VOL_1H_USD": 10_000.0,
    "MIN_LIQUIDITY_USD": 15_000.0,
    "MIN_BUY_CONFIDENCE": 0.55,
    "MIN_NARRATIVE_SCORE": 2.0,
    "MIN_MOMENTUM_SCORE": 2.0,
    "MAX_VAMP_ENTRY": 0.55,
    "COOLDOWN_HOURS": 1.0,
    "SAFETY_REJECT_COOLDOWN_MINUTES": 20,
    "VAMP_DROP_COOLDOWN_MINUTES": 20,
    "LOW_SCORE_COOLDOWN_MINUTES": 15,
    "STALE_WATCH_TIMEOUT_MINUTES": 15,
    "STALE_WATCH_COOLDOWN_MINUTES": 10,
    "VAMP_EVICT_THRESHOLD": 0.85,
    "VAMP_WARN_THRESHOLD": 0.75,
    "GECKOTERMINAL_CACHE_SECONDS": 180.0,
    "MONITOR_POLL_SECONDS": 5.0,
    "DUCKDB_PATH": "solana_meme_agent.duckdb",
    "TELEMETRY_HOST": "0.0.0.0",
    "TELEMETRY_PORT": 8002,
    "AGENT_LOCAL_URL": "http://127.0.0.1:8002",
}


@dataclass(frozen=True)
class ConfigProfile:
    """Explicit simulator configuration profile with field-level provenance."""

    name: str
    config: AgentConfig
    profile_provenance: str
    runtime_code_provenance: str
    supplied_fields: frozenset[str]
    defaulted_fields: frozenset[str]
    notes: str

    def field_provenance(self, field_name: str) -> str:
        """Return explicit provenance tag for an AgentConfig field."""
        if field_name not in AgentConfig.model_fields:
            return UNAVAILABLE
        if self.name == PROFILE_REPO_DEFAULTS:
            return PROVENANCE_REPO_DEFAULTS
        if field_name in self.supplied_fields:
            return PROVENANCE_SUPPLIED_PI_SNAPSHOT
        return PROVENANCE_OMITTED_DEFAULT

    def field_availability_class(self, field_name: str) -> str:
        """Classify historical certainty of a configuration field for PID 8315."""
        if field_name not in AgentConfig.model_fields:
            return UNAVAILABLE
        # Neither repo defaults nor the supplied snapshot prove PID 8315's live process env.
        return "PARTIALLY_AVAILABLE" if field_name in self.supplied_fields else "INFERRED"

    def to_metadata_dict(self) -> dict[str, Any]:
        return {
            "profile_name": self.name,
            "profile_provenance": self.profile_provenance,
            "runtime_code_provenance": self.runtime_code_provenance,
            "supplied_field_count": len(self.supplied_fields),
            "defaulted_field_count": len(self.defaulted_fields),
            "notes": self.notes,
            "effective_breakout_settings": {
                "EXECUTION_MODE": self.config.EXECUTION_MODE,
                "CHART_ENABLED_STRATEGIES": self.config.CHART_ENABLED_STRATEGIES,
                "TRADE_SIZE_SOL": self.config.TRADE_SIZE_SOL,
                "PAPER_TX_FEE_SOL": self.config.PAPER_TX_FEE_SOL,
                "MIN_WATCHLIST_SOAK_MINUTES": self.config.MIN_WATCHLIST_SOAK_MINUTES,
                "BREAKOUT_MIN_SCORE": self.config.BREAKOUT_MIN_SCORE,
                "BREAKOUT_MIN_VOL_RATIO": self.config.BREAKOUT_MIN_VOL_RATIO,
                "BREAKOUT_MAX_COIL_RANGE_PCT": self.config.BREAKOUT_MAX_COIL_RANGE_PCT,
                "SCALPER_MIN_TXNS_5M": self.config.SCALPER_MIN_TXNS_5M,
                "SCALPER_MIN_BUY_RATIO_5M": self.config.SCALPER_MIN_BUY_RATIO_5M,
                "MIN_BUY_CONFIDENCE": self.config.MIN_BUY_CONFIDENCE,
                "MIN_NARRATIVE_SCORE": self.config.MIN_NARRATIVE_SCORE,
                "MIN_MOMENTUM_SCORE": self.config.MIN_MOMENTUM_SCORE,
                "MAX_VAMP_ENTRY": self.config.MAX_VAMP_ENTRY,
                "LOW_SCORE_EVICT_THRESHOLD": self.config.LOW_SCORE_EVICT_THRESHOLD,
                "VAMP_EVICT_THRESHOLD": self.config.VAMP_EVICT_THRESHOLD,
                "HARD_STOP_LOSS_PCT": self.config.HARD_STOP_LOSS_PCT,
                "EXODUS_SELL_RATIO": self.config.EXODUS_SELL_RATIO,
            },
        }


def build_hermetic_config(**overrides: Any) -> AgentConfig:
    """Instantiate AgentConfig with _env_file=None and os.environ completely ignored."""
    return _HermeticAgentConfig(_env_file=None, **overrides)


def get_config_profile(profile_name: str = PROFILE_SUPPLIED_PHASE1_PI) -> ConfigProfile:
    """Return one of the two authorized explicit simulator configuration profiles."""
    all_fields = frozenset(AgentConfig.model_fields.keys())
    clean = (profile_name or "").strip().lower()

    if clean == PROFILE_REPO_DEFAULTS:
        cfg = build_hermetic_config()
        return ConfigProfile(
            name=PROFILE_REPO_DEFAULTS,
            config=cfg,
            profile_provenance=PROVENANCE_REPO_DEFAULTS,
            runtime_code_provenance=RUNTIME_CODE_PROVENANCE,
            supplied_fields=frozenset(),
            defaulted_fields=all_fields,
            notes=(
                "Pure config.py defaults instantiated with _env_file=None and os.environ ignored. "
                "Does NOT match Phase 1 Pi .env overrides."
            ),
        )

    if clean == PROFILE_SUPPLIED_PHASE1_PI:
        cfg = build_hermetic_config(**SUPPLIED_PHASE1_PI_SNAPSHOT_OVERRIDES)
        supplied = frozenset(SUPPLIED_PHASE1_PI_SNAPSHOT_OVERRIDES.keys())
        defaulted = all_fields - supplied
        return ConfigProfile(
            name=PROFILE_SUPPLIED_PHASE1_PI,
            config=cfg,
            profile_provenance=PROVENANCE_SUPPLIED_PI_SNAPSHOT,
            runtime_code_provenance=RUNTIME_CODE_PROVENANCE,
            supplied_fields=supplied,
            defaulted_fields=defaulted,
            notes=(
                "User-supplied Phase 1 Raspberry Pi .env snapshot instantiated hermetically "
                "(_env_file=None, os.environ ignored). Omitted keys (e.g., PAPER_TX_FEE_SOL=0.001, "
                "SCALPER_MIN_BUY_RATIO_5M=0.58, SCALPER_MIN_TXNS_5M=15, LOW_SCORE_EVICT_THRESHOLD=2.0) "
                "fall back to config.py defaults and are labeled EXPECTED_FROM_CODE_DEFAULT. "
                "Does NOT prove PID 8315's exact historical process environment."
            ),
        )

    raise ValueError(
        f"Unknown simulator config profile '{profile_name}'. "
        f"Supported profiles: {PROFILE_REPO_DEFAULTS}, {PROFILE_SUPPLIED_PHASE1_PI}."
    )


__all__ = [
    "ConfigProfile",
    "PROFILE_REPO_DEFAULTS",
    "PROFILE_SUPPLIED_PHASE1_PI",
    "PROVENANCE_OMITTED_DEFAULT",
    "PROVENANCE_REPO_DEFAULTS",
    "PROVENANCE_SUPPLIED_PI_SNAPSHOT",
    "SUPPLIED_PHASE1_PI_SNAPSHOT_OVERRIDES",
    "build_hermetic_config",
    "get_config_profile",
    "EXACT_HISTORICAL",
]
