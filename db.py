"""DuckDB persistence layer for state, trades, cooldowns, and Decision 1 logs."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import duckdb

from action_export import ACTION_LOG_COLUMNS, ACTION_LOG_MIGRATIONS
from memory_guard import configure_duckdb_connection, prune_duckdb_tables
from outcome_tracker import ensure_outcomes_table
from state import FocusState
from state_ledger import (
    apply_trade_settlement,
    release_buy_reservation,
    reserve_buy_slot,
    rollover_daily_if_needed_locked,
    update_lane_positions_only,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat()


class AgentDatabase:
    """Thread-safe DuckDB wrapper for solana-meme-agent."""

    def __init__(self, db_path: str = "solana_meme_agent.duckdb", starting_sol: float = 0.7):
        self.db_path = db_path
        self.starting_sol = starting_sol
        self._lock = threading.RLock()
        self._conn = duckdb.connect(db_path)
        configure_duckdb_connection(self._conn)
        self._init_schema()
        self._ensure_initial_state()

    def prune_bounded_tables(self, now: Optional[datetime] = None) -> None:
        """Prune expired cooldowns and bound high-frequency tables."""
        prune_duckdb_tables(self._conn, self._lock, now_dt=now)

    def get_duckdb_setting(self, name: str) -> str:
        """Query current value of a DuckDB configuration setting."""
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM duckdb_settings() WHERE name = ?",
                [name],
            ).fetchone()
        return str(row[0]) if row else ""

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _init_schema(self) -> None:
        with self._lock:
            ensure_outcomes_table(self._conn)
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_state (
                    id INTEGER PRIMARY KEY,
                    focus_state VARCHAR NOT NULL,
                    sol_balance DOUBLE NOT NULL,
                    daily_start_sol DOUBLE NOT NULL,
                    daily_pnl_sol DOUBLE NOT NULL,
                    daily_reset_date VARCHAR NOT NULL,
                    active_position_json VARCHAR,
                    whale_position_json VARCHAR,
                    chart_positions_json VARCHAR DEFAULT '[]',
                    whale_positions_json VARCHAR DEFAULT '[]',
                    strategy VARCHAR DEFAULT 'SCALPER',
                    triggering_wallets_json VARCHAR DEFAULT '[]',
                    whale_shadow_exit_requested BOOLEAN DEFAULT FALSE,
                    last_decision_rationale VARCHAR,
                    updated_at VARCHAR NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE SEQUENCE IF NOT EXISTS trades_id_seq START 1
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER DEFAULT nextval('trades_id_seq') PRIMARY KEY,
                    timestamp VARCHAR NOT NULL,
                    mode VARCHAR NOT NULL,
                    side VARCHAR NOT NULL,
                    mint VARCHAR NOT NULL,
                    symbol VARCHAR NOT NULL,
                    in_amount BIGINT NOT NULL,
                    out_amount BIGINT NOT NULL,
                    sol_value DOUBLE NOT NULL,
                    pnl_sol DOUBLE,
                    pnl_pct DOUBLE,
                    reason VARCHAR NOT NULL,
                    tx_signature VARCHAR,
                    rationale VARCHAR,
                    strategy VARCHAR DEFAULT 'SCALPER',
                    triggering_wallets_json VARCHAR DEFAULT '[]',
                    opened_at VARCHAR,
                    jupiter_price_impact_raw VARCHAR,
                    pair_address VARCHAR,
                    dex_id VARCHAR,
                    tx_fee_sol DOUBLE,
                    fee_status VARCHAR
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cooldowns (
                    mint VARCHAR PRIMARY KEY,
                    expires_at VARCHAR NOT NULL,
                    reason VARCHAR NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE SEQUENCE IF NOT EXISTS decisions_id_seq START 1
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS decisions_log (
                    id INTEGER DEFAULT nextval('decisions_id_seq') PRIMARY KEY,
                    timestamp VARCHAR NOT NULL,
                    mint VARCHAR NOT NULL,
                    symbol VARCHAR NOT NULL,
                    phase VARCHAR NOT NULL,
                    action VARCHAR NOT NULL,
                    confidence DOUBLE,
                    scores_json VARCHAR NOT NULL,
                    rationale VARCHAR NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS active_watchlist (
                    mint VARCHAR PRIMARY KEY,
                    symbol VARCHAR NOT NULL,
                    pair_address VARCHAR DEFAULT '',
                    dex_id VARCHAR,
                    added_at VARCHAR NOT NULL,
                    safety_passed BOOLEAN DEFAULT FALSE,
                    entry_liquidity_usd DOUBLE DEFAULT 0.0,
                    peak_mc DOUBLE DEFAULT 0.0,
                    low_mc DOUBLE DEFAULT 0.0,
                    leg2_score DOUBLE DEFAULT 0.0,
                    leg2_breakdown_json VARCHAR,
                    vamp_strikes INTEGER DEFAULT 0,
                    rsi_5m DOUBLE,
                    setup_type VARCHAR DEFAULT 'NONE',
                    scalper_eligible BOOLEAN DEFAULT FALSE,
                    dist_from_1h_high_pct DOUBLE,
                    discovery_source VARCHAR DEFAULT 'DEXSCREENER',
                    jup_organic_score DOUBLE,
                    jup_meta_json VARCHAR,
                    last_evaluated_at VARCHAR,
                    latest_scores_json VARCHAR,
                    candidate_json VARCHAR NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE SEQUENCE IF NOT EXISTS action_log_id_seq START 1
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS action_log (
                    id INTEGER DEFAULT nextval('action_log_id_seq') PRIMARY KEY,
                    timestamp_utc VARCHAR NOT NULL,
                    bot VARCHAR NOT NULL DEFAULT 'solana_meme',
                    mode VARCHAR NOT NULL,
                    action VARCHAR NOT NULL,
                    symbol VARCHAR NOT NULL,
                    asset_id VARCHAR NOT NULL,
                    qty DOUBLE,
                    notional DOUBLE,
                    unit VARCHAR NOT NULL DEFAULT 'SOL',
                    price_or_value DOUBLE,
                    pnl_amount DOUBLE,
                    pnl_pct DOUBLE,
                    strategy VARCHAR,
                    reason VARCHAR
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS x_seen_tweets (
                    tweet_id VARCHAR PRIMARY KEY,
                    fetched_at_utc VARCHAR NOT NULL,
                    author_username VARCHAR,
                    author_followers INTEGER DEFAULT 0,
                    like_count INTEGER DEFAULT 0,
                    text VARCHAR,
                    extracted_mints VARCHAR DEFAULT '[]'
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS order_intents (
                    intent_id VARCHAR PRIMARY KEY,
                    reservation_id VARCHAR,
                    lane VARCHAR NOT NULL,
                    mint VARCHAR NOT NULL,
                    symbol VARCHAR NOT NULL,
                    pair_address VARCHAR,
                    dex_id VARCHAR,
                    side VARCHAR NOT NULL,
                    strategy VARCHAR NOT NULL,
                    router VARCHAR,
                    order_mode VARCHAR,
                    jupiter_request_id VARCHAR,
                    tx_signature VARCHAR,
                    last_valid_block_height BIGINT,
                    expire_at VARCHAR,
                    input_mint VARCHAR NOT NULL,
                    output_mint VARCHAR NOT NULL,
                    amount_atomic BIGINT NOT NULL,
                    expected_out_amount BIGINT,
                    other_amount_threshold BIGINT,
                    token_decimals INTEGER,
                    taker_pubkey VARCHAR,
                    reserved_sol DOUBLE NOT NULL,
                    status VARCHAR NOT NULL,
                    error_detail VARCHAR,
                    order_snapshot_json VARCHAR,
                    created_at VARCHAR NOT NULL,
                    updated_at VARCHAR NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS social_signals (
                    mint VARCHAR PRIMARY KEY,
                    mention_count INTEGER NOT NULL DEFAULT 0,
                    unique_authors INTEGER NOT NULL DEFAULT 0,
                    total_likes INTEGER NOT NULL DEFAULT 0,
                    top_snippets_json VARCHAR NOT NULL DEFAULT '[]',
                    last_seen_utc VARCHAR NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS x_worker_state (
                    id INTEGER PRIMARY KEY,
                    last_poll_utc VARCHAR
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS whale_events (
                    signature VARCHAR PRIMARY KEY,
                    wallet VARCHAR NOT NULL,
                    mint VARCHAR NOT NULL,
                    side VARCHAR NOT NULL,
                    sol_amount DOUBLE NOT NULL,
                    token_amount DOUBLE NOT NULL,
                    timestamp_utc VARCHAR NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS whale_signals (
                    mint VARCHAR PRIMARY KEY,
                    buy_count INTEGER NOT NULL DEFAULT 0,
                    unique_whales INTEGER NOT NULL DEFAULT 0,
                    total_sol_spent DOUBLE NOT NULL DEFAULT 0.0,
                    sell_count INTEGER NOT NULL DEFAULT 0,
                    last_seen_utc VARCHAR NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS whale_decisions (
                    mint VARCHAR PRIMARY KEY,
                    status VARCHAR NOT NULL,
                    reason VARCHAR NOT NULL,
                    updated_at VARCHAR NOT NULL
                )
                """
            )
            self._migrate_watchlist_columns()
            self._migrate_action_log_columns()

    def _migrate_action_log_columns(self) -> None:
        """Ensure enriched full-board telemetry columns exist on pre-existing action_log tables."""
        existing = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info('action_log')").fetchall()
        }
        for col, col_type in ACTION_LOG_MIGRATIONS:
            if col not in existing:
                self._conn.execute(f"ALTER TABLE action_log ADD COLUMN {col} {col_type}")

    def _migrate_watchlist_columns(self) -> None:
        """Ensure upgraded columns exist on pre-existing DuckDB files."""
        existing = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info('active_watchlist')").fetchall()
        }
        migrations = [
            ("pair_address", "VARCHAR DEFAULT ''"),
            ("dex_id", "VARCHAR"),
            ("safety_passed", "BOOLEAN DEFAULT FALSE"),
            ("safety_checked_at", "VARCHAR"),
            ("entry_liquidity_usd", "DOUBLE DEFAULT 0.0"),
            ("peak_mc", "DOUBLE DEFAULT 0.0"),
            ("low_mc", "DOUBLE DEFAULT 0.0"),
            ("leg2_score", "DOUBLE DEFAULT 0.0"),
            ("leg2_breakdown_json", "VARCHAR"),
            ("vamp_strikes", "INTEGER DEFAULT 0"),
            ("rsi_5m", "DOUBLE"),
            ("setup_type", "VARCHAR DEFAULT 'NONE'"),
            ("scalper_eligible", "BOOLEAN DEFAULT FALSE"),
            ("dist_from_1h_high_pct", "DOUBLE"),
            ("discovery_source", "VARCHAR DEFAULT 'DEXSCREENER'"),
            ("jup_organic_score", "DOUBLE"),
            ("jup_meta_json", "VARCHAR"),
        ]
        for col, col_type in migrations:
            if col not in existing:
                self._conn.execute(f"ALTER TABLE active_watchlist ADD COLUMN {col} {col_type}")

        # Migrate agent_state columns
        state_cols = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info('agent_state')").fetchall()
        }
        for col, col_type in [
            ("strategy", "VARCHAR DEFAULT 'SCALPER'"),
            ("triggering_wallets_json", "VARCHAR DEFAULT '[]'"),
            ("whale_shadow_exit_requested", "BOOLEAN DEFAULT FALSE"),
        ]:
            if col not in state_cols:
                self._conn.execute(f"ALTER TABLE agent_state ADD COLUMN {col} {col_type}")

        # Migrate trades columns
        trade_cols = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info('trades')").fetchall()
        }
        for col, col_type in [
            ("strategy", "VARCHAR DEFAULT 'SCALPER'"),
            ("triggering_wallets_json", "VARCHAR DEFAULT '[]'"),
            ("opened_at", "VARCHAR"),
            ("jupiter_price_impact_raw", "VARCHAR"),
            ("pair_address", "VARCHAR"),
            ("dex_id", "VARCHAR"),
            ("tx_fee_sol", "DOUBLE"),
            ("fee_status", "VARCHAR"),
        ]:
            if col not in trade_cols:
                self._conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {col_type}")
        state_cols = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info('agent_state')").fetchall()
        }
        for col, col_type in [
            ("whale_position_json", "VARCHAR"),
            ("chart_positions_json", "VARCHAR DEFAULT '[]'"),
            ("whale_positions_json", "VARCHAR DEFAULT '[]'"),
        ]:
            if col not in state_cols:
                self._conn.execute(f"ALTER TABLE agent_state ADD COLUMN {col} {col_type}")
        wd_cols = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info('whale_decisions')").fetchall()
        }
        if "breakout_telemetry_json" not in wd_cols:
            self._conn.execute("ALTER TABLE whale_decisions ADD COLUMN breakout_telemetry_json VARCHAR")

    def _ensure_initial_state(self) -> None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM agent_state WHERE id = 1"
            ).fetchone()
            if row is None:
                today = utc_now().date().isoformat()
                self._conn.execute(
                    """
                    INSERT INTO agent_state (
                        id, focus_state, sol_balance, daily_start_sol,
                        daily_pnl_sol, daily_reset_date, active_position_json,
                        last_decision_rationale, updated_at
                    ) VALUES (1, ?, ?, ?, 0.0, ?, NULL, '', ?)
                    """,
                    [
                        FocusState.SCANNING.value,
                        self.starting_sol,
                        self.starting_sol,
                        today,
                        utc_now_iso(),
                    ],
                )

    def get_state(self) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT focus_state, sol_balance, daily_start_sol, daily_pnl_sol,
                       daily_reset_date, active_position_json, whale_position_json,
                       chart_positions_json, whale_positions_json,
                       last_decision_rationale, updated_at, strategy,
                       triggering_wallets_json, whale_shadow_exit_requested
                FROM agent_state WHERE id = 1
                """
            ).fetchone()
            assert row is not None
            today = utc_now().date().isoformat()
            (
                focus_state,
                sol_bal,
                daily_start,
                daily_pnl,
                reset_date,
                pos_json,
                whale_pos_json,
                chart_list_json,
                whale_list_json,
                rationale,
                updated_at,
                strategy,
                trig_json,
                shadow_flag,
            ) = row

            # Roll over daily PnL counter on new UTC day
            if reset_date != today:
                daily_start = sol_bal
                daily_pnl = 0.0
                reset_date = today
                self._conn.execute(
                    """
                    UPDATE agent_state
                    SET daily_start_sol = ?, daily_pnl_sol = 0.0, daily_reset_date = ?, updated_at = ?
                    WHERE id = 1
                    """,
                    [daily_start, reset_date, utc_now_iso()],
                )

            active_pos = json.loads(pos_json) if pos_json else None
            whale_pos = json.loads(whale_pos_json) if whale_pos_json else None
            chart_positions: list[dict[str, Any]] = json.loads(chart_list_json) if chart_list_json else []
            whale_positions: list[dict[str, Any]] = json.loads(whale_list_json) if whale_list_json else []
            trig_wallets = json.loads(trig_json) if trig_json else []
            if isinstance(active_pos, dict):
                if strategy and not active_pos.get("strategy"):
                    active_pos["strategy"] = strategy
                if trig_wallets and not active_pos.get("triggering_wallets"):
                    active_pos["triggering_wallets"] = trig_wallets
                if shadow_flag and str(active_pos.get("strategy") or "").upper() == "WHALE_SCALP":
                    active_pos["whale_shadow_exit_flag"] = True
                if focus_state == FocusState.IN_POSITION.value and str(active_pos.get("strategy") or "").upper() != "WHALE_SCALP":
                    if not chart_positions:
                        chart_positions = [active_pos]
                    else:
                        for idx, cp in enumerate(chart_positions):
                            if isinstance(cp, dict) and cp.get("mint") == active_pos.get("mint"):
                                chart_positions[idx] = {**cp, **active_pos}
                                break
            if isinstance(whale_pos, dict):
                if shadow_flag:
                    whale_pos["whale_shadow_exit_flag"] = True
                if not whale_positions:
                    whale_positions = [whale_pos]

            return {
                "focus_state": focus_state,
                "sol_balance": float(sol_bal),
                "daily_start_sol": float(daily_start),
                "daily_pnl_sol": float(daily_pnl),
                "daily_reset_date": reset_date,
                "active_position": active_pos,
                "whale_position": whale_pos if whale_pos is not None else (whale_positions[0] if whale_positions else None),
                "chart_positions": chart_positions,
                "whale_positions": whale_positions,
                "strategy": strategy or "SCALPER",
                "triggering_wallets": trig_wallets,
                "whale_shadow_exit_requested": bool(shadow_flag),
                "last_decision_rationale": rationale or "",
                "updated_at": updated_at,
            }

    def save_state(
        self,
        *,
        focus_state: str,
        sol_balance: float,
        daily_pnl_sol: float,
        active_position: Optional[dict[str, Any]],
        last_decision_rationale: Optional[str] = None,
    ) -> None:
        with self._lock:
            pos_json = json.dumps(active_position) if active_position is not None else None
            strategy = (
                str(active_position.get("strategy") or "SCALPER")
                if isinstance(active_position, dict)
                else "SCALPER"
            )
            trig_wallets = (
                list(active_position.get("triggering_wallets") or [])
                if isinstance(active_position, dict)
                else []
            )
            shadow_flag = (
                bool(active_position.get("whale_shadow_exit_flag", False))
                if isinstance(active_position, dict)
                else False
            )
            trig_json = json.dumps(trig_wallets)
            curr_row = self._conn.execute(
                "SELECT chart_positions_json, whale_positions_json, whale_shadow_exit_requested FROM agent_state WHERE id = 1"
            ).fetchone()
            chart_list: list[dict[str, Any]] = json.loads(curr_row[0]) if (curr_row and curr_row[0]) else []
            whale_list: list[dict[str, Any]] = json.loads(curr_row[1]) if (curr_row and curr_row[1]) else []
            is_whale_active = isinstance(active_position, dict) and str(active_position.get("strategy") or "").upper() == "WHALE_SCALP"
            if isinstance(active_position, dict) and not is_whale_active:
                a_mint = active_position.get("mint")
                if focus_state == FocusState.IN_POSITION.value and int(active_position.get("token_amount") or 0) > 0:
                    replaced = False
                    for idx, cp in enumerate(chart_list):
                        if isinstance(cp, dict) and cp.get("mint") == a_mint:
                            chart_list[idx] = active_position
                            replaced = True
                            break
                    if not replaced:
                        chart_list.append(active_position)
                else:
                    chart_list = [cp for cp in chart_list if isinstance(cp, dict) and cp.get("mint") != a_mint]
            elif active_position is None:
                # Never wipe open chart positions (token_amount > 0) when WATCHING_REENTRY abandons to SCANNING
                chart_list = [cp for cp in chart_list if isinstance(cp, dict) and int(cp.get("token_amount") or 0) > 0]
            any_whale_shadow = any(bool(wp.get("whale_shadow_exit_flag", False)) for wp in whale_list if isinstance(wp, dict))
            shadow_flag = (shadow_flag or any_whale_shadow) if is_whale_active else any_whale_shadow
            chart_list_json = json.dumps(chart_list)
            if last_decision_rationale is None:
                self._conn.execute(
                    """
                    UPDATE agent_state
                    SET focus_state = ?, sol_balance = ?, daily_pnl_sol = ?,
                        active_position_json = ?, chart_positions_json = ?, strategy = ?, triggering_wallets_json = ?,
                        whale_shadow_exit_requested = ?, updated_at = ?
                    WHERE id = 1
                    """,
                    [
                        focus_state,
                        sol_balance,
                        daily_pnl_sol,
                        pos_json,
                        chart_list_json,
                        strategy,
                        trig_json,
                        shadow_flag,
                        utc_now_iso(),
                    ],
                )
            else:
                self._conn.execute(
                    """
                    UPDATE agent_state
                    SET focus_state = ?, sol_balance = ?, daily_pnl_sol = ?,
                        active_position_json = ?, chart_positions_json = ?, strategy = ?, triggering_wallets_json = ?,
                        whale_shadow_exit_requested = ?, last_decision_rationale = ?, updated_at = ?
                    WHERE id = 1
                    """,
                    [
                        focus_state,
                        sol_balance,
                        daily_pnl_sol,
                        pos_json,
                        chart_list_json,
                        strategy,
                        trig_json,
                        shadow_flag,
                        last_decision_rationale,
                        utc_now_iso(),
                    ],
                )

    def topup_paper_sol(self, amount: float) -> tuple[float, float]:
        """Safely add SOL to the paper wallet balance and return (before, after)."""
        if amount <= 0:
            raise ValueError("Top-up amount must be positive.")
        with self._lock:
            row = self._conn.execute(
                "SELECT sol_balance, daily_start_sol FROM agent_state WHERE id = 1"
            ).fetchone()
            assert row is not None
            before = float(row[0])
            daily_start = float(row[1])
            after = round(before + float(amount), 6)
            new_daily_start = round(daily_start + float(amount), 6)
            self._conn.execute(
                """
                UPDATE agent_state
                SET sol_balance = ?, daily_start_sol = ?, updated_at = ?
                WHERE id = 1
                """,
                [after, new_daily_start, utc_now_iso()],
            )
            return before, after

    def get_all_open_mints(self) -> set[str]:
        """Return all mints currently open across chart_positions, whale_positions, and WATCHING_REENTRY."""
        st = self.get_state()
        mints: set[str] = set()
        for p in st.get("chart_positions") or []:
            if isinstance(p, dict) and p.get("mint"):
                mints.add(str(p["mint"]))
        for p in st.get("whale_positions") or []:
            if isinstance(p, dict) and p.get("mint"):
                mints.add(str(p["mint"]))
        for key in ("active_position", "whale_position"):
            p = st.get(key)
            if isinstance(p, dict) and p.get("mint"):
                mints.add(str(p["mint"]))
        return mints

    def reserve_buy_slot(self, **kwargs: Any) -> tuple[Optional[str], Optional[str]]:
        return reserve_buy_slot(self, **kwargs)

    def create_order_intent(
        self,
        *,
        intent_id: str,
        reservation_id: Optional[str],
        lane: str,
        mint: str,
        symbol: str,
        pair_address: Optional[str],
        dex_id: Optional[str],
        side: str,
        strategy: str,
        router: Optional[str],
        order_mode: Optional[str],
        jupiter_request_id: Optional[str],
        tx_signature: Optional[str],
        last_valid_block_height: Optional[int],
        expire_at: Optional[str],
        input_mint: str,
        output_mint: str,
        amount_atomic: int,
        expected_out_amount: Optional[int],
        other_amount_threshold: Optional[int],
        token_decimals: Optional[int],
        taker_pubkey: Optional[str],
        reserved_sol: float,
        status: str = "PREPARED",
        error_detail: Optional[str] = None,
        order_snapshot: Optional[dict[str, Any]] = None,
    ) -> None:
        now_iso = utc_now_iso()
        with self._lock:
            self._conn.execute("BEGIN TRANSACTION")
            try:
                self._conn.execute(
                    """
                    INSERT INTO order_intents (
                        intent_id, reservation_id, lane, mint, symbol, pair_address, dex_id,
                        side, strategy, router, order_mode, jupiter_request_id, tx_signature,
                        last_valid_block_height, expire_at, input_mint, output_mint, amount_atomic,
                        expected_out_amount, other_amount_threshold, token_decimals, taker_pubkey,
                        reserved_sol, status, error_detail, order_snapshot_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        intent_id,
                        reservation_id,
                        lane,
                        mint,
                        symbol,
                        pair_address,
                        dex_id,
                        side,
                        strategy,
                        router,
                        order_mode,
                        jupiter_request_id,
                        tx_signature,
                        last_valid_block_height,
                        expire_at,
                        input_mint,
                        output_mint,
                        int(amount_atomic),
                        int(expected_out_amount) if expected_out_amount is not None else None,
                        int(other_amount_threshold) if other_amount_threshold is not None else None,
                        int(token_decimals) if token_decimals is not None else None,
                        taker_pubkey,
                        float(reserved_sol),
                        status,
                        error_detail,
                        json.dumps(order_snapshot or {}),
                        now_iso,
                        now_iso,
                    ],
                )
                self._conn.execute("COMMIT")
            except BaseException:
                try:
                    self._conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise

    def transition_order_intent(
        self,
        intent_id: str,
        *,
        new_status: str,
        tx_signature: Optional[str] = None,
        error_detail: Optional[str] = None,
        expected_statuses: Optional[tuple[str, ...]] = None,
    ) -> bool:
        """Atomically and idempotently transition an order_intent status under self._lock."""
        now_iso = utc_now_iso()
        with self._lock:
            self._conn.execute("BEGIN TRANSACTION")
            try:
                row = self._conn.execute(
                    "SELECT status, tx_signature, error_detail FROM order_intents WHERE intent_id = ?",
                    [intent_id],
                ).fetchone()
                if not row:
                    self._conn.execute("ROLLBACK")
                    return False
                curr_status, curr_sig, curr_err = str(row[0]), row[1], row[2]
                if (
                    curr_status == new_status
                    and (tx_signature is None or tx_signature == curr_sig)
                    and (error_detail is None or error_detail == curr_err)
                ):
                    self._conn.execute("COMMIT")
                    return True
                # Never allow a terminal intent (RECONCILED or FAILED) to transition backward
                if curr_status in ("RECONCILED", "FAILED") and new_status != curr_status:
                    self._conn.execute("ROLLBACK")
                    return False
                if expected_statuses is not None and curr_status not in expected_statuses:
                    self._conn.execute("ROLLBACK")
                    return False
                sets = ["status = ?", "updated_at = ?"]
                params: list[Any] = [new_status, now_iso]
                if tx_signature is not None:
                    sets.append("tx_signature = ?")
                    params.append(tx_signature)
                if error_detail is not None:
                    sets.append("error_detail = ?")
                    params.append(error_detail)
                params.append(intent_id)
                self._conn.execute(
                    f"UPDATE order_intents SET {', '.join(sets)} WHERE intent_id = ?",
                    params,
                )
                self._conn.execute("COMMIT")
                return True
            except BaseException:
                try:
                    self._conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise

    def update_order_intent_snapshot(self, intent_id: str, snapshot: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE order_intents SET order_snapshot_json = ?, updated_at = ? WHERE intent_id = ?",
                [json.dumps(snapshot), utc_now_iso(), intent_id],
            )

    def _row_to_order_intent(self, row: tuple[Any, ...]) -> dict[str, Any]:
        keys = [
            "intent_id", "reservation_id", "lane", "mint", "symbol", "pair_address", "dex_id",
            "side", "strategy", "router", "order_mode", "jupiter_request_id", "tx_signature",
            "last_valid_block_height", "expire_at", "input_mint", "output_mint", "amount_atomic",
            "expected_out_amount", "other_amount_threshold", "token_decimals", "taker_pubkey",
            "reserved_sol", "status", "error_detail", "order_snapshot_json", "created_at", "updated_at",
        ]
        return dict(zip(keys, row))

    def get_order_intent(self, intent_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT intent_id, reservation_id, lane, mint, symbol, pair_address, dex_id,
                       side, strategy, router, order_mode, jupiter_request_id, tx_signature,
                       last_valid_block_height, expire_at, input_mint, output_mint, amount_atomic,
                       expected_out_amount, other_amount_threshold, token_decimals, taker_pubkey,
                       reserved_sol, status, error_detail, order_snapshot_json, created_at, updated_at
                FROM order_intents WHERE intent_id = ?
                """,
                [intent_id],
            ).fetchone()
        return self._row_to_order_intent(row) if row else None

    def get_active_locking_intents(self, mint: Optional[str] = None) -> list[dict[str, Any]]:
        with self._lock:
            if mint:
                rows = self._conn.execute(
                    """
                    SELECT intent_id, reservation_id, lane, mint, symbol, pair_address, dex_id,
                           side, strategy, router, order_mode, jupiter_request_id, tx_signature,
                           last_valid_block_height, expire_at, input_mint, output_mint, amount_atomic,
                           expected_out_amount, other_amount_threshold, token_decimals, taker_pubkey,
                           reserved_sol, status, error_detail, order_snapshot_json, created_at, updated_at
                    FROM order_intents
                    WHERE status IN ('PREPARED', 'SUBMITTED', 'UNRESOLVED', 'SETTLEMENT_PENDING_PERSISTENCE')
                      AND mint = ?
                    ORDER BY created_at ASC
                    """,
                    [mint],
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT intent_id, reservation_id, lane, mint, symbol, pair_address, dex_id,
                           side, strategy, router, order_mode, jupiter_request_id, tx_signature,
                           last_valid_block_height, expire_at, input_mint, output_mint, amount_atomic,
                           expected_out_amount, other_amount_threshold, token_decimals, taker_pubkey,
                           reserved_sol, status, error_detail, order_snapshot_json, created_at, updated_at
                    FROM order_intents
                    WHERE status IN ('PREPARED', 'SUBMITTED', 'UNRESOLVED', 'SETTLEMENT_PENDING_PERSISTENCE')
                    ORDER BY created_at ASC
                    """
                ).fetchall()
        return [self._row_to_order_intent(r) for r in rows]

    def release_buy_reservation(self, reservation_id: Optional[str]) -> None:
        release_buy_reservation(self, reservation_id)

    def apply_trade_settlement(self, **kwargs: Any) -> dict[str, Any]:
        return apply_trade_settlement(self, **kwargs)

    def update_lane_positions_only(self, **kwargs: Any) -> list[dict[str, Any]]:
        return update_lane_positions_only(self, **kwargs)

    def save_lane_positions(
        self,
        *,
        lane: str,
        positions: list[dict[str, Any]],
        sol_balance: float,
        daily_pnl_sol: float,
        focus_state: Optional[str] = None,
        active_position: Any = ...,
        last_decision_rationale: Optional[str] = None,
    ) -> None:
        """Atomically persist a multi-slot lane list (chart_positions or whale_positions) + shared equity."""
        with self._lock:
            col = "whale_positions_json" if lane == "whale" else "chart_positions_json"
            legacy_col = "whale_position_json" if lane == "whale" else "active_position_json"
            first_pos = positions[0] if positions else None
            legacy_val = first_pos if active_position is ... else active_position
            sets = [f"{col} = ?", f"{legacy_col} = ?", "sol_balance = ?", "daily_pnl_sol = ?", "updated_at = ?"]
            params: list[Any] = [
                json.dumps(positions),
                json.dumps(legacy_val) if legacy_val is not None else None,
                sol_balance,
                daily_pnl_sol,
                utc_now_iso(),
            ]
            if focus_state is not None:
                sets.append("focus_state = ?")
                params.append(focus_state)
            if lane == "whale":
                any_shadow = any(bool(p.get("whale_shadow_exit_flag", False)) for p in positions if isinstance(p, dict))
                sets.append("whale_shadow_exit_requested = ?")
                params.append(any_shadow)
            if last_decision_rationale is not None:
                sets.append("last_decision_rationale = ?")
                params.append(last_decision_rationale)
            self._conn.execute(f"UPDATE agent_state SET {', '.join(sets)} WHERE id = 1", params)

    def save_whale_state(
        self,
        *,
        whale_position: Optional[dict[str, Any]],
        sol_balance: float,
        daily_pnl_sol: float,
        last_decision_rationale: Optional[str] = None,
    ) -> None:
        """Atomically persist Slot 2 (whale_position_json) + shared SOL balance & daily PnL."""
        with self._lock:
            whale_json = json.dumps(whale_position) if whale_position is not None else None
            shadow_flag = (
                bool(whale_position.get("whale_shadow_exit_flag", False))
                if isinstance(whale_position, dict)
                else False
            )
            if last_decision_rationale is None:
                self._conn.execute(
                    """
                    UPDATE agent_state
                    SET whale_position_json = ?, sol_balance = ?, daily_pnl_sol = ?,
                        whale_shadow_exit_requested = ?, updated_at = ?
                    WHERE id = 1
                    """,
                    [whale_json, sol_balance, daily_pnl_sol, shadow_flag, utc_now_iso()],
                )
            else:
                self._conn.execute(
                    """
                    UPDATE agent_state
                    SET whale_position_json = ?, sol_balance = ?, daily_pnl_sol = ?,
                        whale_shadow_exit_requested = ?, last_decision_rationale = ?, updated_at = ?
                    WHERE id = 1
                    """,
                    [
                        whale_json,
                        sol_balance,
                        daily_pnl_sol,
                        shadow_flag,
                        last_decision_rationale,
                        utc_now_iso(),
                    ],
                )

    def flag_whale_shadow_exit(self, mint: str, seller_wallet: str) -> bool:
        """If an open WHALE_SCALP position on `mint` (any Whale slot or legacy Slot 1) was triggered by `seller_wallet`, flag exit."""
        with self._lock:
            st = self.get_state()
            whale_positions = list(st.get("whale_positions") or [])
            matched_idx: Optional[int] = None
            for idx, wp in enumerate(whale_positions):
                if isinstance(wp, dict) and wp.get("mint") == mint:
                    trig = set(wp.get("triggering_wallets") or st.get("triggering_wallets") or [])
                    if seller_wallet in trig:
                        matched_idx = idx
                        break
            if matched_idx is not None:
                whale_positions[matched_idx]["whale_shadow_exit_flag"] = True
                legacy_whale = whale_positions[0] if whale_positions else None
                self._conn.execute(
                    """
                    UPDATE agent_state
                    SET whale_shadow_exit_requested = TRUE, whale_positions_json = ?, whale_position_json = ?, updated_at = ?
                    WHERE id = 1
                    """,
                    [json.dumps(whale_positions), json.dumps(legacy_whale) if legacy_whale else None, utc_now_iso()],
                )
                return True

            chart_pos = st.get("active_position")
            if (
                st["focus_state"] == FocusState.IN_POSITION.value
                and isinstance(chart_pos, dict)
                and chart_pos.get("mint") == mint
                and str(chart_pos.get("strategy") or "").upper() == "WHALE_SCALP"
            ):
                trig = set(chart_pos.get("triggering_wallets") or st.get("triggering_wallets") or [])
                if seller_wallet in trig:
                    chart_pos["whale_shadow_exit_flag"] = True
                    self._conn.execute(
                        """
                        UPDATE agent_state
                        SET whale_shadow_exit_requested = TRUE, active_position_json = ?, updated_at = ?
                        WHERE id = 1
                        """,
                        [json.dumps(chart_pos), utc_now_iso()],
                    )
                    return True
            return False

    def set_cooldown(
        self,
        mint: str,
        hours: float = 2.0,
        reason: str = "exit",
        now: Optional[datetime] = None,
    ) -> str:
        base_dt = now or utc_now()
        expires_at = (base_dt + timedelta(hours=hours)).isoformat()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO cooldowns (mint, expires_at, reason)
                VALUES (?, ?, ?)
                ON CONFLICT (mint) DO UPDATE SET
                    expires_at = excluded.expires_at,
                    reason = excluded.reason
                """,
                [mint, expires_at, reason],
            )
        return expires_at

    def set_cooldown_minutes(
        self,
        mint: str,
        minutes: float,
        reason: str = "evict",
        now: Optional[datetime] = None,
    ) -> str:
        return self.set_cooldown(mint, hours=minutes / 60.0, reason=reason, now=now)

    def get_watchlist(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT mint, symbol, pair_address, dex_id, added_at, safety_passed, safety_checked_at, entry_liquidity_usd,
                       peak_mc, low_mc, leg2_score, leg2_breakdown_json, vamp_strikes,
                       rsi_5m, setup_type, scalper_eligible, dist_from_1h_high_pct,
                       discovery_source, jup_organic_score, jup_meta_json,
                       last_evaluated_at, latest_scores_json, candidate_json
                FROM active_watchlist
                ORDER BY added_at ASC
                """
            ).fetchall()
        result: list[dict[str, Any]] = []
        for (
            mint,
            symbol,
            pair_address,
            dex_id,
            added_at,
            safety_passed,
            safety_checked_at,
            entry_liq,
            peak_mc,
            low_mc,
            leg2_score,
            leg2_breakdown_json,
            vamp_strikes,
            rsi_5m,
            setup_type,
            scalper_eligible,
            dist_1h_high,
            discovery_source,
            jup_organic_score,
            jup_meta_json,
            last_eval,
            scores_json,
            cand_json,
        ) in rows:
            cand_obj = json.loads(cand_json) if cand_json else {}
            resolved_dex = dex_id or (str(cand_obj.get("dex_id") or "").strip() or None)
            result.append(
                {
                    "mint": mint,
                    "symbol": symbol,
                    "pair_address": pair_address or "",
                    "dex_id": resolved_dex,
                    "added_at": added_at,
                    "safety_passed": bool(safety_passed),
                    "safety_checked_at": safety_checked_at,
                    "entry_liquidity_usd": float(entry_liq or 0.0),
                    "peak_mc": float(peak_mc or 0.0),
                    "low_mc": float(low_mc or 0.0),
                    "leg2_score": float(leg2_score or 0.0),
                    "leg2_breakdown": json.loads(leg2_breakdown_json) if leg2_breakdown_json else None,
                    "vamp_strikes": int(vamp_strikes or 0),
                    "rsi_5m": float(rsi_5m) if rsi_5m is not None else None,
                    "setup_type": setup_type or "NONE",
                    "scalper_eligible": bool(scalper_eligible),
                    "dist_from_1h_high_pct": float(dist_1h_high) if dist_1h_high is not None else None,
                    "discovery_source": discovery_source or "DEXSCREENER",
                    "jup_organic_score": float(jup_organic_score) if jup_organic_score is not None else None,
                    "jup_meta": json.loads(jup_meta_json) if jup_meta_json else None,
                    "last_evaluated_at": last_eval,
                    "latest_scores": json.loads(scores_json) if scores_json else None,
                    "candidate": cand_obj,
                }
            )
        return result

    def upsert_watchlist_entry(
        self,
        *,
        mint: str,
        symbol: str,
        candidate: dict[str, Any],
        pair_address: Optional[str] = None,
        dex_id: Optional[str] = None,
        added_at: Optional[str] = None,
        safety_passed: bool = False,
        entry_liquidity_usd: Optional[float] = None,
        peak_mc: Optional[float] = None,
        low_mc: Optional[float] = None,
        leg2_score: float = 0.0,
        leg2_breakdown: Optional[dict[str, Any]] = None,
        discovery_source: Optional[str] = None,
        jup_organic_score: Optional[float] = None,
        jup_meta: Optional[dict[str, Any]] = None,
    ) -> None:
        ts = added_at or utc_now_iso()
        p_addr = str(pair_address if pair_address is not None else (candidate.get("pair_address") or "")).strip()
        raw_dex = dex_id if dex_id is not None else candidate.get("dex_id")
        d_id = str(raw_dex).strip() if isinstance(raw_dex, str) and raw_dex.strip() else None
        src = str(discovery_source or candidate.get("discovery_source") or "DEXSCREENER").upper()
        mc = float(candidate.get("market_cap_usd") or 0.0)
        liq = float(entry_liquidity_usd if entry_liquidity_usd is not None else (candidate.get("liquidity_usd") or 0.0))
        p_mc = float(peak_mc if peak_mc is not None else mc)
        l_mc = float(low_mc if low_mc is not None else mc)
        bd_json = json.dumps(leg2_breakdown) if leg2_breakdown is not None else None
        j_score = float(jup_organic_score) if jup_organic_score is not None else None
        j_meta_json = json.dumps(jup_meta) if jup_meta is not None else None
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO active_watchlist (
                    mint, symbol, pair_address, dex_id, added_at, safety_passed, entry_liquidity_usd,
                    peak_mc, low_mc, leg2_score, leg2_breakdown_json, discovery_source,
                    jup_organic_score, jup_meta_json,
                    last_evaluated_at, latest_scores_json, candidate_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
                ON CONFLICT (mint) DO UPDATE SET
                    symbol = excluded.symbol,
                    pair_address = CASE
                        WHEN active_watchlist.pair_address IS NOT NULL AND active_watchlist.pair_address <> ''
                            THEN active_watchlist.pair_address
                        ELSE excluded.pair_address
                    END,
                    dex_id = COALESCE(active_watchlist.dex_id, excluded.dex_id),
                    discovery_source = CASE
                        WHEN excluded.discovery_source = 'WHALE' THEN 'WHALE'
                        WHEN active_watchlist.discovery_source = 'WHALE' THEN 'WHALE'
                        WHEN excluded.discovery_source = 'X' THEN 'X'
                        WHEN active_watchlist.discovery_source = 'X' THEN 'X'
                        ELSE excluded.discovery_source
                    END,
                    jup_organic_score = COALESCE(excluded.jup_organic_score, active_watchlist.jup_organic_score),
                    jup_meta_json = COALESCE(excluded.jup_meta_json, active_watchlist.jup_meta_json),
                    peak_mc = GREATEST(active_watchlist.peak_mc, excluded.peak_mc),
                    low_mc = CASE
                        WHEN active_watchlist.low_mc <= 0 THEN excluded.low_mc
                        WHEN excluded.low_mc <= 0 THEN active_watchlist.low_mc
                        ELSE LEAST(active_watchlist.low_mc, excluded.low_mc)
                    END,
                    leg2_score = excluded.leg2_score,
                    leg2_breakdown_json = excluded.leg2_breakdown_json,
                    candidate_json = excluded.candidate_json
                """,
                [
                    mint,
                    symbol,
                    p_addr,
                    d_id,
                    ts,
                    bool(safety_passed),
                    liq,
                    p_mc,
                    l_mc,
                    float(leg2_score),
                    bd_json,
                    src,
                    j_score,
                    j_meta_json,
                    json.dumps(candidate),
                ],
            )

    def mark_watchlist_safety_passed(
        self,
        mint: str,
        passed: bool = True,
        *,
        checked_at: Optional[str] = None,
    ) -> None:
        ts = (checked_at or utc_now_iso()) if passed else None
        with self._lock:
            self._conn.execute(
                "UPDATE active_watchlist SET safety_passed = ?, safety_checked_at = ? WHERE mint = ?",
                [bool(passed), ts, mint],
            )

    def get_watchlist_entry(self, mint: str) -> Optional[dict[str, Any]]:
        """Return a single active_watchlist row by mint, or None."""
        for row in self.get_watchlist():
            if row.get("mint") == mint:
                return row
        return None

    def update_watchlist_scores(
        self,
        *,
        mint: str,
        scores: dict[str, Any],
        last_evaluated_at: Optional[str] = None,
        vamp_strikes: Optional[int] = None,
        rsi_5m: Optional[float] = None,
        setup_type: Optional[str] = None,
        scalper_eligible: Optional[bool] = None,
        dist_from_1h_high_pct: Optional[float] = None,
    ) -> None:
        ts = last_evaluated_at or utc_now_iso()
        set_clauses = ["latest_scores_json = ?", "last_evaluated_at = ?"]
        params: list[Any] = [json.dumps(scores), ts]
        if vamp_strikes is not None:
            set_clauses.append("vamp_strikes = ?")
            params.append(int(vamp_strikes))
        if rsi_5m is not None:
            set_clauses.append("rsi_5m = ?")
            params.append(float(rsi_5m))
        if setup_type is not None:
            set_clauses.append("setup_type = ?")
            params.append(str(setup_type))
        if scalper_eligible is not None:
            set_clauses.append("scalper_eligible = ?")
            params.append(bool(scalper_eligible))
        if dist_from_1h_high_pct is not None:
            set_clauses.append("dist_from_1h_high_pct = ?")
            params.append(float(dist_from_1h_high_pct))
        params.append(mint)
        with self._lock:
            self._conn.execute(
                f"UPDATE active_watchlist SET {', '.join(set_clauses)} WHERE mint = ?",
                params,
            )

    def remove_from_watchlist(self, mint: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM active_watchlist WHERE mint = ?", [mint])

    def is_on_cooldown(self, mint: str, now: Optional[datetime] = None) -> bool:
        now_dt = now or utc_now()
        with self._lock:
            row = self._conn.execute(
                "SELECT expires_at FROM cooldowns WHERE mint = ?", [mint]
            ).fetchone()
            if not row:
                return False
            expires_dt = datetime.fromisoformat(row[0])
            if expires_dt.tzinfo is None:
                expires_dt = expires_dt.replace(tzinfo=timezone.utc)
            return expires_dt > now_dt

    def get_active_cooldowns(self, now: Optional[datetime] = None) -> list[dict[str, Any]]:
        now_dt = now or utc_now()
        with self._lock:
            rows = self._conn.execute(
                "SELECT mint, expires_at, reason FROM cooldowns ORDER BY expires_at DESC"
            ).fetchall()
        active: list[dict[str, Any]] = []
        for mint, expires_at, reason in rows:
            exp_dt = datetime.fromisoformat(expires_at)
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=timezone.utc)
            if exp_dt > now_dt:
                active.append({"mint": mint, "expires_at": expires_at, "reason": reason})
        return active

    def log_trade(
        self,
        *,
        mode: str,
        side: str,
        mint: str,
        symbol: str,
        in_amount: int,
        out_amount: int,
        sol_value: float,
        reason: str,
        pnl_sol: Optional[float] = None,
        pnl_pct: Optional[float] = None,
        tx_signature: Optional[str] = None,
        rationale: str = "",
        strategy: str = "SCALPER",
        triggering_wallets: Optional[list[str]] = None,
        opened_at: Optional[str] = None,
        jupiter_price_impact_raw: Optional[str] = None,
        pair_address: Optional[str] = None,
        dex_id: Optional[str] = None,
        tx_fee_sol: Optional[float] = None,
        fee_status: Optional[str] = None,
    ) -> None:
        clean_pair = pair_address.strip() if isinstance(pair_address, str) and pair_address.strip() else None
        clean_dex = dex_id.strip() if isinstance(dex_id, str) and dex_id.strip() else None
        clean_fee_status = fee_status.strip() if isinstance(fee_status, str) and fee_status.strip() else None
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO trades (
                    timestamp, mode, side, mint, symbol, in_amount, out_amount,
                    sol_value, pnl_sol, pnl_pct, reason, tx_signature, rationale,
                    strategy, triggering_wallets_json, opened_at, jupiter_price_impact_raw,
                    pair_address, dex_id, tx_fee_sol, fee_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    utc_now_iso(),
                    mode,
                    side,
                    mint,
                    symbol,
                    int(in_amount),
                    int(out_amount),
                    float(sol_value),
                    pnl_sol,
                    pnl_pct,
                    reason,
                    tx_signature,
                    rationale,
                    strategy or "SCALPER",
                    json.dumps(triggering_wallets or []),
                    opened_at,
                    jupiter_price_impact_raw,
                    clean_pair,
                    clean_dex,
                    tx_fee_sol,
                    clean_fee_status,
                ],
            )

    def get_recent_trades(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, timestamp, mode, side, mint, symbol, in_amount, out_amount,
                       sol_value, pnl_sol, pnl_pct, reason, tx_signature, rationale,
                       pair_address, dex_id, tx_fee_sol, fee_status
                FROM trades ORDER BY id DESC LIMIT ?
                """,
                [limit],
            ).fetchall()
        keys = [
            "id",
            "timestamp",
            "mode",
            "side",
            "mint",
            "symbol",
            "in_amount",
            "out_amount",
            "sol_value",
            "pnl_sol",
            "pnl_pct",
            "reason",
            "tx_signature",
            "rationale",
            "pair_address",
            "dex_id",
            "tx_fee_sol",
            "fee_status",
        ]
        return [dict(zip(keys, r)) for r in rows]

    def _build_closed_lifecycles(self) -> list[dict[str, Any]]:
        """Reconstruct lifecycle-level closed trade records from BUY, PARTIAL_SELL, and SELL rows."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, mode, side, mint, symbol, sol_value, pnl_sol, pnl_pct, reason,
                       timestamp, strategy, triggering_wallets_json, opened_at, tx_fee_sol, fee_status
                FROM trades
                ORDER BY id ASC
                """
            ).fetchall()

        open_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        unmatched_buys_by_mint: dict[str, list[dict[str, Any]]] = {}
        unmatched_partials_by_mint: dict[str, list[dict[str, Any]]] = {}
        closed_asc: list[dict[str, Any]] = []

        for r in rows:
            rec = {
                "id": r[0],
                "mode": r[1],
                "side": str(r[2] or "").upper(),
                "mint": str(r[3] or ""),
                "symbol": r[4],
                "sol_value": r[5],
                "pnl_sol": r[6],
                "pnl_pct": r[7],
                "reason": r[8],
                "timestamp": r[9],
                "strategy": r[10],
                "triggering_wallets_json": r[11],
                "opened_at": str(r[12]).strip() if r[12] else None,
                "tx_fee_sol": r[13],
                "fee_status": str(r[14]).strip() if r[14] else None,
            }
            mint = rec["mint"]
            opened_at = rec["opened_at"]
            side = rec["side"]

            if side == "BUY":
                if opened_at:
                    open_by_key[(mint, opened_at)] = {"buy": rec, "partials": []}
                else:
                    unmatched_buys_by_mint.setdefault(mint, []).append(rec)
            elif side == "PARTIAL_SELL":
                if opened_at:
                    bucket = open_by_key.setdefault((mint, opened_at), {"buy": None, "partials": []})
                    bucket["partials"].append(rec)
                else:
                    unmatched_partials_by_mint.setdefault(mint, []).append(rec)
            elif side == "SELL":
                if opened_at:
                    bucket = open_by_key.pop((mint, opened_at), None)
                    buy_row = bucket["buy"] if bucket else None
                    partial_rows = bucket["partials"] if bucket else []
                    if buy_row is None and unmatched_buys_by_mint.get(mint):
                        buy_row = unmatched_buys_by_mint[mint].pop(-1)
                    lifecycle_id = f"{mint}:{opened_at}"
                else:
                    buy_row = unmatched_buys_by_mint[mint].pop(-1) if unmatched_buys_by_mint.get(mint) else None
                    partial_rows = unmatched_partials_by_mint.pop(mint, [])
                    lifecycle_id = f"{mint}:id:{rec['id']}"

                partial_sell_count = len(partial_rows)
                partial_exit_sol = sum(float(p["sol_value"] or 0.0) for p in partial_rows)
                final_sell_sol = float(rec["sol_value"] or 0.0)
                total_exit_sol = partial_exit_sol + final_sell_sol

                partial_pnls_present = all(p["pnl_sol"] is not None for p in partial_rows)
                realized_partial_pnl: Optional[float] = (
                    sum(float(p["pnl_sol"]) for p in partial_rows) if partial_pnls_present else None
                )
                final_sell_pnl: Optional[float] = float(rec["pnl_sol"]) if rec["pnl_sol"] is not None else None

                if buy_row is not None:
                    entry_sol: Optional[float] = float(buy_row["sol_value"] or 0.0)
                else:
                    # C1-R2: Never infer historical entry cost when no matching BUY row exists
                    entry_sol = None

                all_legs_present = (buy_row is not None) and partial_pnls_present and (final_sell_pnl is not None)
                leg_rows = [buy_row, *partial_rows, rec] if buy_row is not None else [*partial_rows, rec]
                all_fees_known = all_legs_present and all(
                    (lr["fee_status"] in ("PAPER_ASSUMED", "OBSERVED")) and (lr["tx_fee_sol"] is not None)
                    for lr in leg_rows
                )

                if buy_row is not None and entry_sol is not None:
                    gross_pnl_sol: Optional[float] = round(total_exit_sol - entry_sol, 6)
                elif realized_partial_pnl is not None and final_sell_pnl is not None:
                    gross_pnl_sol = round(realized_partial_pnl + final_sell_pnl, 6)
                elif final_sell_pnl is not None:
                    gross_pnl_sol = round(final_sell_pnl, 6)
                else:
                    gross_pnl_sol = None

                if all_fees_known:
                    assert buy_row is not None and realized_partial_pnl is not None and final_sell_pnl is not None
                    entry_fee = float(buy_row["tx_fee_sol"])
                    total_fees_sol: Optional[float] = round(
                        entry_fee + sum(float(p["tx_fee_sol"]) for p in partial_rows) + float(rec["tx_fee_sol"]), 6
                    )
                    fee_accounting_status = (
                        "OBSERVED" if all(lr["fee_status"] == "OBSERVED" for lr in leg_rows) else "PAPER_ASSUMED"
                    )
                    lifecycle_pnl: Optional[float] = round(realized_partial_pnl + final_sell_pnl - entry_fee, 6)
                    lifecycle_pnl_pct: Optional[float] = (
                        round((lifecycle_pnl / entry_sol) * 100.0, 2) if (entry_sol is not None and entry_sol > 0.0) else 0.0
                    )
                    lifecycle_complete = True
                    net_pnl_known = True
                    gross_pnl_incomplete = False
                else:
                    entry_fee = (
                        float(buy_row["tx_fee_sol"])
                        if (
                            buy_row is not None
                            and buy_row["tx_fee_sol"] is not None
                            and buy_row["fee_status"] in ("PAPER_ASSUMED", "OBSERVED")
                        )
                        else None
                    )
                    total_fees_sol = None
                    fee_accounting_status = "UNKNOWN"
                    lifecycle_complete = False
                    net_pnl_known = False
                    # C1-R1: Unknown-fee or incomplete lifecycles must never expose gross PnL as net pnl_sol
                    lifecycle_pnl = None
                    lifecycle_pnl_pct = None
                    gross_pnl_incomplete = True

                closed_asc.append(
                    {
                        "symbol": rec["symbol"],
                        "mint": mint,
                        "opened_at": opened_at,
                        "lifecycle_id": lifecycle_id,
                        "entry_sol": round(entry_sol, 6) if entry_sol is not None else None,
                        "exit_sol": round(total_exit_sol, 6),
                        "final_sell_sol": round(final_sell_sol, 6),
                        "pnl_sol": round(lifecycle_pnl, 6) if lifecycle_pnl is not None else None,
                        "pnl_pct": round(lifecycle_pnl_pct, 2) if lifecycle_pnl_pct is not None else None,
                        "gross_pnl_sol": gross_pnl_sol,
                        "gross_pnl_incomplete": gross_pnl_incomplete,
                        "realized_partial_pnl_sol": round(realized_partial_pnl, 6) if realized_partial_pnl is not None else None,
                        "final_sell_pnl_sol": round(final_sell_pnl, 6) if final_sell_pnl is not None else None,
                        "partial_sell_count": partial_sell_count,
                        "entry_fee_sol": round(entry_fee, 6) if entry_fee is not None else None,
                        "total_fees_sol": total_fees_sol,
                        "fee_accounting_status": fee_accounting_status,
                        "net_pnl_known": net_pnl_known,
                        "lifecycle_complete": lifecycle_complete,
                        "exit_reason": rec["reason"],
                        "strategy": rec["strategy"] or "SCALPER",
                        "triggering_wallets": json.loads(rec["triggering_wallets_json"]) if rec["triggering_wallets_json"] else [],
                        "closed_at": rec["timestamp"],
                    }
                )

        closed_asc.reverse()
        return closed_asc

    def get_recent_closed_trades(self, limit: int = 15) -> list[dict[str, Any]]:
        """Return the last N closed trade lifecycles (newest first) with complete lifecycle PnL."""
        return self._build_closed_lifecycles()[: max(0, int(limit))]

    def get_closed_trades_summary(self) -> dict[str, Any]:
        """Aggregate win/loss record, win rate %, and net realized SOL over known-fee closed lifecycles only."""
        lifecycles = self._build_closed_lifecycles()
        total = len(lifecycles)
        known = [lc for lc in lifecycles if lc.get("net_pnl_known") is True and lc.get("pnl_sol") is not None]
        known_trades = len(known)
        unknown_trades = total - known_trades
        wins = sum(1 for lc in known if float(lc["pnl_sol"]) > 0.0)
        losses = sum(1 for lc in known if float(lc["pnl_sol"]) <= 0.0)
        if known_trades > 0:
            net_pnl: Optional[float] = round(sum(float(lc["pnl_sol"]) for lc in known), 6)
            win_rate: Optional[float] = round((wins / known_trades) * 100.0, 1)
        elif total == 0:
            net_pnl = 0.0
            win_rate = 0.0
        else:
            net_pnl = None
            win_rate = None
        return {
            "total_trades": total,
            "known_trades": known_trades,
            "unknown_trades": unknown_trades,
            "wins": wins,
            "losses": losses,
            "win_rate_pct": win_rate,
            "win_rate_basis": "known_lifecycles_only",
            "net_pnl_sol": net_pnl,
        }

    def get_whale_trades(self, limit: int = 25) -> list[dict[str, Any]]:
        """Return closed WHALE_SCALP trades ordered newest-first with hold_seconds & triggering_wallets."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT symbol, mint, sol_value, pnl_sol, pnl_pct, reason, timestamp,
                       opened_at, triggering_wallets_json
                FROM trades
                WHERE UPPER(side) = 'SELL' AND UPPER(strategy) = 'WHALE_SCALP'
                ORDER BY id DESC
                LIMIT ?
                """,
                [int(limit)],
            ).fetchall()

        trades: list[dict[str, Any]] = []
        for symbol, mint, exit_sol, pnl_sol, pnl_pct, reason, closed_at, opened_at, trig_json in rows:
            exit_val = float(exit_sol or 0.0)
            pnl_val = float(pnl_sol or 0.0)
            entry_val = max(0.0, exit_val - pnl_val)
            pct_val = float(pnl_pct or 0.0) * 100.0
            hold_sec: Optional[float] = None
            if opened_at and closed_at:
                try:
                    o_dt = datetime.fromisoformat(opened_at)
                    c_dt = datetime.fromisoformat(closed_at)
                    if o_dt.tzinfo is None:
                        o_dt = o_dt.replace(tzinfo=timezone.utc)
                    if c_dt.tzinfo is None:
                        c_dt = c_dt.replace(tzinfo=timezone.utc)
                    hold_sec = round(max(0.0, (c_dt - o_dt).total_seconds()), 1)
                except ValueError:
                    hold_sec = None
            trades.append(
                {
                    "status": "CLOSED",
                    "symbol": symbol,
                    "mint": mint,
                    "entry_sol": round(entry_val, 6),
                    "exit_sol": round(exit_val, 6),
                    "pnl_sol": round(pnl_val, 6),
                    "pnl_pct": round(pct_val, 2),
                    "exit_reason": reason,
                    "opened_at": opened_at,
                    "closed_at": closed_at,
                    "hold_seconds": hold_sec,
                    "triggering_wallets": json.loads(trig_json) if trig_json else [],
                }
            )
        return trades

    def upsert_whale_decision(
        self,
        mint: str,
        status: str,
        reason: str,
        updated_at: Optional[str] = None,
        breakout_telemetry: Optional[dict[str, Any]] = None,
    ) -> None:
        """Persist latest WHALE_SCALP evaluation decision and optional breakout_telemetry for a mint."""
        ts = updated_at or utc_now_iso()
        bt_json = json.dumps(breakout_telemetry) if breakout_telemetry is not None else None
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO whale_decisions (mint, status, reason, updated_at, breakout_telemetry_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (mint) DO UPDATE SET
                    status = excluded.status,
                    reason = excluded.reason,
                    updated_at = excluded.updated_at,
                    breakout_telemetry_json = COALESCE(excluded.breakout_telemetry_json, whale_decisions.breakout_telemetry_json)
                """,
                [mint, status.upper(), reason, ts, bt_json],
            )

    def get_whale_decisions(self) -> dict[str, dict[str, Any]]:
        """Return all persisted whale_decisions (including breakout_telemetry) keyed by mint."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT mint, status, reason, updated_at, breakout_telemetry_json FROM whale_decisions"
            ).fetchall()
        return {
            str(mint): {
                "status": str(status),
                "reason": str(reason),
                "updated_at": str(updated_at),
                "breakout_telemetry": json.loads(bt_json) if bt_json else None,
            }
            for mint, status, reason, updated_at, bt_json in rows
        }

    def get_latest_buy_strategy(self, mint: str) -> Optional[str]:
        """Return the most recent non-empty BUY strategy recorded for `mint`."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT strategy FROM trades
                WHERE mint = ? AND UPPER(side) = 'BUY'
                  AND strategy IS NOT NULL AND TRIM(strategy) != ''
                ORDER BY id DESC LIMIT 1
                """,
                [mint],
            ).fetchone()
            if row and row[0]:
                return str(row[0])
            row2 = self._conn.execute(
                """
                SELECT strategy FROM action_log
                WHERE asset_id = ? AND UPPER(action) IN ('BUY', 'REENTRY_BUY')
                  AND strategy IS NOT NULL AND TRIM(strategy) != ''
                ORDER BY id DESC LIMIT 1
                """,
                [mint],
            ).fetchone()
            return str(row2[0]) if row2 and row2[0] else None

    def log_action(
        self,
        *,
        mode: str,
        action: str,
        symbol: str,
        asset_id: str,
        qty: Optional[float] = None,
        notional: Optional[float] = None,
        unit: str = "SOL",
        price_or_value: Optional[float] = None,
        pnl_amount: Optional[float] = None,
        pnl_pct: Optional[float] = None,
        strategy: str = "",
        reason: str = "",
        bot: str = "solana_meme",
        timestamp_utc: Optional[str] = None,
        sol_equity: Optional[float] = None,
        daily_realized_sol: Optional[float] = None,
        circuit_breaker: Optional[bool] = None,
        mc_usd: Optional[float] = None,
        liq_usd: Optional[float] = None,
        soak_minutes: Optional[float] = None,
        rsi: Optional[float] = None,
        leg2_score: Optional[float] = None,
        narrative_score: Optional[float] = None,
        momentum_score: Optional[float] = None,
        vamp_score: Optional[float] = None,
        buy_confidence: Optional[float] = None,
        peak_pnl_pct: Optional[float] = None,
        hold_seconds: Optional[float] = None,
        triggering_whales: Optional[str] = None,
        whale_sol_flow: Optional[float] = None,
        breakout_score: Optional[int] = None,
        breakout_state: Optional[str] = None,
        breakout_timeframe: Optional[str] = None,
        breakout_vol_ratio: Optional[float] = None,
        breakout_ema_ext_pct: Optional[float] = None,
        breakout_failed_gates: Optional[str] = None,
        jupiter_price_impact_raw: Optional[str] = None,
        monitor_sol_value: Optional[float] = None,
        price_source: Optional[str] = None,
        position_mfe_pct: Optional[float] = None,
        position_mae_pct: Optional[float] = None,
        pair_address: Optional[str] = None,
        dex_id: Optional[str] = None,
        router: Optional[str] = None,
        route_pool_keys: Optional[str] = None,
        pool_route_status: Optional[str] = None,
    ) -> None:
        """Append a structured board action row to DuckDB action_log."""
        ts = timestamp_utc or utc_now_iso()
        clean_pair = pair_address.strip() if isinstance(pair_address, str) and pair_address.strip() else None
        clean_dex = dex_id.strip() if isinstance(dex_id, str) and dex_id.strip() else None
        with self._lock:
            if sol_equity is None or daily_realized_sol is None or circuit_breaker is None:
                st_row = self._conn.execute(
                    "SELECT sol_balance, daily_start_sol, daily_pnl_sol FROM agent_state WHERE id = 1"
                ).fetchone()
                if st_row:
                    st_bal, st_start, st_pnl = float(st_row[0]), float(st_row[1]), float(st_row[2])
                    if sol_equity is None:
                        sol_equity = round(st_bal, 6)
                    if daily_realized_sol is None:
                        daily_realized_sol = round(st_pnl, 6)
                    if circuit_breaker is None:
                        circuit_breaker = bool(
                            action.upper() == "CIRCUIT_BREAKER"
                            or (st_start > 0 and (-st_pnl / st_start) >= 0.20)
                        )
            cols_sql = ", ".join(ACTION_LOG_COLUMNS)
            placeholders = ", ".join("?" for _ in ACTION_LOG_COLUMNS)
            self._conn.execute(
                f"INSERT INTO action_log ({cols_sql}) VALUES ({placeholders})",
                [
                    ts, bot, mode, action.upper(), symbol, asset_id,
                    qty, notional, unit, price_or_value, pnl_amount, pnl_pct,
                    strategy, reason,
                    sol_equity, daily_realized_sol, circuit_breaker,
                    mc_usd, liq_usd, soak_minutes, rsi, leg2_score,
                    narrative_score, momentum_score, vamp_score, buy_confidence,
                    peak_pnl_pct, hold_seconds, triggering_whales, whale_sol_flow,
                    breakout_score, breakout_state, breakout_timeframe,
                    breakout_vol_ratio, breakout_ema_ext_pct, breakout_failed_gates,
                    jupiter_price_impact_raw, monitor_sol_value, price_source,
                    position_mfe_pct, position_mae_pct,
                    clean_pair, clean_dex,
                    router, route_pool_keys, pool_route_status,
                ],
            )

    def get_action_log(
        self,
        *,
        limit: int = 1000,
        actions: Optional[list[str]] = None,
    ) -> list[dict[str, Any]]:
        """Query action_log newest-first with optional action type filtering."""
        cols_sql = ", ".join(ACTION_LOG_COLUMNS)
        with self._lock:
            if actions:
                clean = [a.strip().upper() for a in actions if a.strip()]
                if clean:
                    placeholders = ", ".join("?" for _ in clean)
                    rows = self._conn.execute(
                        f"SELECT {cols_sql} FROM action_log WHERE UPPER(action) IN ({placeholders}) ORDER BY id DESC LIMIT ?",
                        [*clean, int(limit)],
                    ).fetchall()
                else:
                    rows = self._conn.execute(
                        f"SELECT {cols_sql} FROM action_log ORDER BY id DESC LIMIT ?",
                        [int(limit)],
                    ).fetchall()
            else:
                rows = self._conn.execute(
                    f"SELECT {cols_sql} FROM action_log ORDER BY id DESC LIMIT ?",
                    [int(limit)],
                ).fetchall()
        return [dict(zip(ACTION_LOG_COLUMNS, r)) for r in rows]

    def has_circuit_breaker_today(self, now: Optional[datetime] = None) -> bool:
        """Avoid duplicating CIRCUIT_BREAKER rows on repeated halted ticks in the same UTC day."""
        today = (now or utc_now()).date().isoformat()
        with self._lock:
            row = self._conn.execute(
                """
                SELECT 1 FROM action_log
                WHERE action = 'CIRCUIT_BREAKER' AND timestamp_utc LIKE ?
                LIMIT 1
                """,
                [f"{today}%"],
            ).fetchone()
        return row is not None

    def count_x_tweets_today(self, now: Optional[datetime] = None) -> int:
        today = (now or utc_now()).date().isoformat()
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM x_seen_tweets WHERE fetched_at_utc LIKE ?",
                [f"{today}%"],
            ).fetchone()
        return int(row[0]) if row else 0

    def record_x_tweet(
        self,
        *,
        tweet_id: str,
        fetched_at_utc: str,
        author_username: str,
        author_followers: int,
        like_count: int,
        text: str,
        extracted_mints: list[str],
    ) -> bool:
        """Insert a tweet into x_seen_tweets if unseen; returns True if newly inserted."""
        with self._lock:
            exists = self._conn.execute(
                "SELECT 1 FROM x_seen_tweets WHERE tweet_id = ?",
                [tweet_id],
            ).fetchone()
            if exists is not None:
                return False
            self._conn.execute(
                """
                INSERT INTO x_seen_tweets (
                    tweet_id, fetched_at_utc, author_username, author_followers,
                    like_count, text, extracted_mints
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    tweet_id,
                    fetched_at_utc,
                    author_username,
                    int(author_followers),
                    int(like_count),
                    text,
                    json.dumps(extracted_mints),
                ],
            )
        return True

    def upsert_social_signal(
        self,
        *,
        mint: str,
        author_username: str,
        author_followers: int,
        like_count: int,
        retweet_count: int,
        is_blue_verified: bool,
        text: str,
        seen_at_utc: str,
    ) -> None:
        """Aggregate X mention counts, unique authors, total likes, and top 5 snippets per mint."""
        snippet = {
            "author": author_username,
            "followers": int(author_followers),
            "likes": int(like_count),
            "retweets": int(retweet_count),
            "verified": bool(is_blue_verified),
            "engagement": int(like_count) + int(retweet_count),
            "text": text[:280],
            "seen_at": seen_at_utc,
        }
        with self._lock:
            row = self._conn.execute(
                """
                SELECT mention_count, unique_authors, total_likes, top_snippets_json
                FROM social_signals WHERE mint = ?
                """,
                [mint],
            ).fetchone()
            if row is None:
                self._conn.execute(
                    """
                    INSERT INTO social_signals (
                        mint, mention_count, unique_authors, total_likes,
                        top_snippets_json, last_seen_utc
                    ) VALUES (?, 1, 1, ?, ?, ?)
                    """,
                    [mint, int(like_count), json.dumps([snippet]), seen_at_utc],
                )
            else:
                prev_mentions, prev_unique, prev_likes, prev_snips_json = row
                prev_snips: list[dict[str, Any]] = json.loads(prev_snips_json) if prev_snips_json else []
                known_authors = {str(s.get("author") or "").lower() for s in prev_snips}
                new_unique = int(prev_unique) + (
                    0 if author_username.lower() in known_authors else 1
                )
                merged = prev_snips + [snippet]
                merged.sort(key=lambda s: int(s.get("engagement") or 0), reverse=True)
                top5 = merged[:5]
                self._conn.execute(
                    """
                    UPDATE social_signals
                    SET mention_count = ?, unique_authors = ?, total_likes = ?,
                        top_snippets_json = ?, last_seen_utc = ?
                    WHERE mint = ?
                    """,
                    [
                        int(prev_mentions) + 1,
                        new_unique,
                        int(prev_likes) + int(like_count),
                        json.dumps(top5),
                        seen_at_utc,
                        mint,
                    ],
                )

    def get_active_social_signals(
        self,
        *,
        ttl_minutes: int = 60,
        now: Optional[datetime] = None,
    ) -> dict[str, dict[str, Any]]:
        """Return social_signals rows seen within ttl_minutes keyed by mint."""
        now_dt = now or utc_now()
        cutoff = now_dt - timedelta(minutes=ttl_minutes)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT mint, mention_count, unique_authors, total_likes,
                       top_snippets_json, last_seen_utc
                FROM social_signals
                ORDER BY total_likes DESC, mention_count DESC
                """
            ).fetchall()
        active: dict[str, dict[str, Any]] = {}
        for mint, mentions, uniq, likes, snips_json, last_seen in rows:
            seen_dt = datetime.fromisoformat(last_seen)
            if seen_dt.tzinfo is None:
                seen_dt = seen_dt.replace(tzinfo=timezone.utc)
            if seen_dt >= cutoff:
                active[mint] = {
                    "mint": mint,
                    "mention_count": int(mentions),
                    "unique_authors": int(uniq),
                    "total_likes": int(likes),
                    "top_snippets": json.loads(snips_json) if snips_json else [],
                    "last_seen_utc": last_seen,
                }
        return active

    def set_x_last_poll_utc(self, ts_iso: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO x_worker_state (id, last_poll_utc)
                VALUES (1, ?)
                ON CONFLICT (id) DO UPDATE SET last_poll_utc = excluded.last_poll_utc
                """,
                [ts_iso],
            )

    def get_x_last_poll_utc(self) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT last_poll_utc FROM x_worker_state WHERE id = 1"
            ).fetchone()
        return str(row[0]) if row and row[0] else None

    def record_whale_event(
        self,
        *,
        signature: str,
        wallet: str,
        mint: str,
        side: str,
        sol_amount: float,
        token_amount: float,
        timestamp_utc: str,
    ) -> bool:
        """Idempotently record a Helius whale swap event by signature; returns True if new."""
        with self._lock:
            exists = self._conn.execute(
                "SELECT 1 FROM whale_events WHERE signature = ?",
                [signature],
            ).fetchone()
            if exists is not None:
                return False
            self._conn.execute(
                """
                INSERT INTO whale_events (
                    signature, wallet, mint, side, sol_amount, token_amount, timestamp_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    signature,
                    wallet,
                    mint,
                    side.upper(),
                    float(sol_amount),
                    float(token_amount),
                    timestamp_utc,
                ],
            )
        return True

    def recompute_whale_signal(
        self,
        mint: str,
        *,
        ttl_minutes: int = 30,
        now: Optional[datetime] = None,
    ) -> Optional[dict[str, Any]]:
        """Aggregate whale_events for mint over ttl_minutes and upsert whale_signals."""
        now_dt = now or utc_now()
        cutoff = now_dt - timedelta(minutes=ttl_minutes)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT wallet, side, sol_amount, timestamp_utc
                FROM whale_events
                WHERE mint = ?
                """,
                [mint],
            ).fetchall()

            buy_count = 0
            sell_count = 0
            total_sol_spent = 0.0
            buy_wallets: set[str] = set()
            latest_ts: Optional[str] = None
            latest_dt: Optional[datetime] = None

            for wallet, side, sol_amt, ts_str in rows:
                ev_dt = datetime.fromisoformat(ts_str)
                if ev_dt.tzinfo is None:
                    ev_dt = ev_dt.replace(tzinfo=timezone.utc)
                if ev_dt < cutoff:
                    continue
                if latest_dt is None or ev_dt > latest_dt:
                    latest_dt = ev_dt
                    latest_ts = ts_str
                if str(side).upper() == "BUY":
                    buy_count += 1
                    buy_wallets.add(str(wallet))
                    total_sol_spent += float(sol_amt or 0.0)
                elif str(side).upper() == "SELL":
                    sell_count += 1

            if latest_ts is None:
                return None

            self._conn.execute(
                """
                INSERT INTO whale_signals (
                    mint, buy_count, unique_whales, total_sol_spent, sell_count, last_seen_utc
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (mint) DO UPDATE SET
                    buy_count = excluded.buy_count,
                    unique_whales = excluded.unique_whales,
                    total_sol_spent = excluded.total_sol_spent,
                    sell_count = excluded.sell_count,
                    last_seen_utc = excluded.last_seen_utc
                """,
                [
                    mint,
                    buy_count,
                    len(buy_wallets),
                    round(total_sol_spent, 6),
                    sell_count,
                    latest_ts,
                ],
            )
            return {
                "mint": mint,
                "buy_count": buy_count,
                "unique_whales": len(buy_wallets),
                "total_sol_spent": round(total_sol_spent, 6),
                "sell_count": sell_count,
                "last_seen_utc": latest_ts,
            }

    def get_active_whale_signals(
        self,
        *,
        ttl_minutes: int = 30,
        min_buys: int = 0,
        now: Optional[datetime] = None,
    ) -> dict[str, dict[str, Any]]:
        """Return whale_signals rows seen within ttl_minutes with buy_count >= min_buys."""
        now_dt = now or utc_now()
        cutoff = now_dt - timedelta(minutes=ttl_minutes)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT mint, buy_count, unique_whales, total_sol_spent, sell_count, last_seen_utc
                FROM whale_signals
                ORDER BY total_sol_spent DESC, buy_count DESC
                """
            ).fetchall()
        active: dict[str, dict[str, Any]] = {}
        for mint, buy_cnt, uniq, sol_spent, sell_cnt, last_seen in rows:
            seen_dt = datetime.fromisoformat(last_seen)
            if seen_dt.tzinfo is None:
                seen_dt = seen_dt.replace(tzinfo=timezone.utc)
            if seen_dt >= cutoff and int(buy_cnt) >= min_buys:
                active[mint] = {
                    "mint": mint,
                    "buy_count": int(buy_cnt),
                    "unique_whales": int(uniq),
                    "total_sol_spent": round(float(sol_spent or 0.0), 6),
                    "sell_count": int(sell_cnt),
                    "last_seen_utc": last_seen,
                }
        return active

    def get_whale_fast_track_triggers(
        self,
        *,
        confluence_window_minutes: int = 5,
        max_age_seconds: int = 90,
        min_confluence_wallets: int = 2,
        min_solo_sol: float = 2.0,
        now: Optional[datetime] = None,
    ) -> list[dict[str, Any]]:
        """Return mints qualifying for WHALE_SCALP fast-track entry.

        Qualifies when either:
          - >= min_confluence_wallets unique wallets bought within confluence_window_minutes
            (with the most recent buy within max_age_seconds), OR
          - A single whale buy >= min_solo_sol occurred within max_age_seconds.
        """
        now_dt = now or utc_now()
        conf_cutoff = now_dt - timedelta(minutes=confluence_window_minutes)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT mint, wallet, sol_amount, timestamp_utc
                FROM whale_events
                WHERE UPPER(side) = 'BUY'
                ORDER BY timestamp_utc DESC
                """
            ).fetchall()

        by_mint: dict[str, list[tuple[str, float, datetime]]] = {}
        for mint, wallet, sol_amt, ts_str in rows:
            ev_dt = datetime.fromisoformat(ts_str)
            if ev_dt.tzinfo is None:
                ev_dt = ev_dt.replace(tzinfo=timezone.utc)
            if ev_dt < conf_cutoff:
                continue
            by_mint.setdefault(str(mint), []).append((str(wallet), float(sol_amt or 0.0), ev_dt))

        triggers: list[dict[str, Any]] = []
        for mint, events in by_mint.items():
            latest_dt = max(ev[2] for ev in events)
            age_sec = max(0.0, (now_dt - latest_dt).total_seconds())
            if age_sec > max_age_seconds:
                continue

            unique_wallets: list[str] = []
            seen_w: set[str] = set()
            total_sol = 0.0
            solo_wallets: list[str] = []

            for wallet, sol_amt, ev_dt in events:
                total_sol += sol_amt
                if wallet not in seen_w:
                    seen_w.add(wallet)
                    unique_wallets.append(wallet)
                ev_age = max(0.0, (now_dt - ev_dt).total_seconds())
                if sol_amt >= min_solo_sol and ev_age <= max_age_seconds:
                    if wallet not in solo_wallets:
                        solo_wallets.append(wallet)

            if len(unique_wallets) >= min_confluence_wallets:
                triggers.append(
                    {
                        "mint": mint,
                        "trigger_type": "CONFLUENCE",
                        "triggering_wallets": unique_wallets,
                        "total_sol": round(total_sol, 4),
                        "age_seconds": round(age_sec, 1),
                    }
                )
            elif solo_wallets:
                triggers.append(
                    {
                        "mint": mint,
                        "trigger_type": "SOLO",
                        "triggering_wallets": solo_wallets,
                        "total_sol": round(total_sol, 4),
                        "age_seconds": round(age_sec, 1),
                    }
                )

        triggers.sort(key=lambda t: (t["trigger_type"] == "CONFLUENCE", t["total_sol"]), reverse=True)
        return triggers

    def count_whale_events_today(self, now: Optional[datetime] = None) -> int:
        today = (now or utc_now()).date().isoformat()
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM whale_events WHERE timestamp_utc LIKE ?",
                [f"{today}%"],
            ).fetchone()
        return int(row[0]) if row else 0

    def get_whale_sol_volume_today(self, now: Optional[datetime] = None) -> float:
        today = (now or utc_now()).date().isoformat()
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(sol_amount), 0.0) FROM whale_events WHERE timestamp_utc LIKE ?",
                [f"{today}%"],
            ).fetchone()
        return round(float(row[0] or 0.0), 4) if row else 0.0

    def get_recent_whale_events(self, limit: int = 25) -> list[dict[str, Any]]:
        """Return the last N whale swap events ordered newest-first."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT signature, wallet, mint, side, sol_amount, token_amount, timestamp_utc
                FROM whale_events
                ORDER BY timestamp_utc DESC
                LIMIT ?
                """,
                [int(limit)],
            ).fetchall()
        return [
            {
                "signature": r[0],
                "wallet": r[1],
                "mint": r[2],
                "side": r[3],
                "sol_amount": round(float(r[4] or 0.0), 4),
                "token_amount": float(r[5] or 0.0),
                "timestamp_utc": r[6],
            }
            for r in rows
        ]

    def get_whale_roster_stats(
        self,
        tracked_wallets: list[str],
        *,
        window_hours: int = 24,
        now: Optional[datetime] = None,
    ) -> list[dict[str, Any]]:
        """Aggregate 24h swap stats per wallet and left-join against tracked_wallets."""
        now_dt = now or utc_now()
        cutoff = now_dt - timedelta(minutes=window_hours * 60)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT wallet, mint, side, sol_amount, timestamp_utc
                FROM whale_events
                ORDER BY timestamp_utc DESC
                """
            ).fetchall()

        stats_by_wallet: dict[str, dict[str, Any]] = {}
        for wallet, mint, side, sol_amt, ts_str in rows:
            ev_dt = datetime.fromisoformat(ts_str)
            if ev_dt.tzinfo is None:
                ev_dt = ev_dt.replace(tzinfo=timezone.utc)
            if ev_dt < cutoff:
                continue
            st = stats_by_wallet.setdefault(
                str(wallet),
                {
                    "buys_24h": 0,
                    "sells_24h": 0,
                    "total_sol_buys": 0.0,
                    "total_sol_sells": 0.0,
                    "last_seen_utc": None,
                    "last_mint": None,
                    "last_side": None,
                },
            )
            side_up = str(side).upper()
            amt = float(sol_amt or 0.0)
            if side_up == "BUY":
                st["buys_24h"] += 1
                st["total_sol_buys"] += amt
            elif side_up == "SELL":
                st["sells_24h"] += 1
                st["total_sol_sells"] += amt
            # Rows are ordered newest-first, so first seen is latest
            if st["last_seen_utc"] is None:
                st["last_seen_utc"] = ts_str
                st["last_mint"] = str(mint)
                st["last_side"] = side_up

        # Left-join against tracked_wallets (preserving order; include newly added 0-swap wallets)
        ordered_wallets: list[str] = []
        seen: set[str] = set()
        for w in tracked_wallets:
            cleaned = str(w or "").strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                ordered_wallets.append(cleaned)
        if not ordered_wallets:
            ordered_wallets = list(stats_by_wallet.keys())

        roster: list[dict[str, Any]] = []
        for w in ordered_wallets:
            st = stats_by_wallet.get(w)
            buys = int(st["buys_24h"]) if st else 0
            sells = int(st["sells_24h"]) if st else 0
            sol_b = round(float(st["total_sol_buys"]), 4) if st else 0.0
            sol_s = round(float(st["total_sol_sells"]), 4) if st else 0.0
            roster.append(
                {
                    "wallet": w,
                    "buys_24h": buys,
                    "sells_24h": sells,
                    "total_sol_buys": sol_b,
                    "total_sol_sells": sol_s,
                    "total_sol_volume": round(sol_b + sol_s, 4),
                    "last_seen_utc": st["last_seen_utc"] if st else None,
                    "last_mint": st["last_mint"] if st else None,
                    "last_side": st["last_side"] if st else None,
                }
            )
        return roster

    def log_decision(
        self,
        *,
        mint: str,
        symbol: str,
        phase: str,
        action: str,
        confidence: Optional[float],
        scores: dict[str, Any],
        rationale: str,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO decisions_log (
                    timestamp, mint, symbol, phase, action, confidence, scores_json, rationale
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    utc_now_iso(),
                    mint,
                    symbol,
                    phase,
                    action,
                    confidence,
                    json.dumps(scores),
                    rationale,
                ],
            )

    def get_recent_decisions(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, timestamp, mint, symbol, phase, action, confidence, scores_json, rationale
                FROM decisions_log ORDER BY id DESC LIMIT ?
                """,
                [limit],
            ).fetchall()
        result = []
        for r in rows:
            result.append(
                {
                    "id": r[0],
                    "timestamp": r[1],
                    "mint": r[2],
                    "symbol": r[3],
                    "phase": r[4],
                    "action": r[5],
                    "confidence": r[6],
                    "scores": json.loads(r[7]),
                    "rationale": r[8],
                }
            )
        return result
