"""DuckDB persistence layer for state, trades, cooldowns, and Decision 1 logs."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import duckdb

from state import FocusState

ACTION_LOG_COLUMNS = [
    "timestamp_utc",
    "bot",
    "mode",
    "action",
    "symbol",
    "asset_id",
    "qty",
    "notional",
    "unit",
    "price_or_value",
    "pnl_amount",
    "pnl_pct",
    "strategy",
    "reason",
]


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
        self._init_schema()
        self._ensure_initial_state()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _init_schema(self) -> None:
        with self._lock:
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
                    rationale VARCHAR
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
            self._migrate_watchlist_columns()

    def _migrate_watchlist_columns(self) -> None:
        """Ensure upgraded columns exist on pre-existing DuckDB files."""
        existing = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info('active_watchlist')").fetchall()
        }
        migrations = [
            ("pair_address", "VARCHAR DEFAULT ''"),
            ("safety_passed", "BOOLEAN DEFAULT FALSE"),
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
                       daily_reset_date, active_position_json, last_decision_rationale,
                       updated_at
                FROM agent_state WHERE id = 1
                """
            ).fetchone()
            assert row is not None
            today = utc_now().date().isoformat()
            focus_state, sol_bal, daily_start, daily_pnl, reset_date, pos_json, rationale, updated_at = row

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

            return {
                "focus_state": focus_state,
                "sol_balance": float(sol_bal),
                "daily_start_sol": float(daily_start),
                "daily_pnl_sol": float(daily_pnl),
                "daily_reset_date": reset_date,
                "active_position": json.loads(pos_json) if pos_json else None,
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
            if last_decision_rationale is None:
                self._conn.execute(
                    """
                    UPDATE agent_state
                    SET focus_state = ?, sol_balance = ?, daily_pnl_sol = ?,
                        active_position_json = ?, updated_at = ?
                    WHERE id = 1
                    """,
                    [focus_state, sol_balance, daily_pnl_sol, pos_json, utc_now_iso()],
                )
            else:
                self._conn.execute(
                    """
                    UPDATE agent_state
                    SET focus_state = ?, sol_balance = ?, daily_pnl_sol = ?,
                        active_position_json = ?, last_decision_rationale = ?, updated_at = ?
                    WHERE id = 1
                    """,
                    [
                        focus_state,
                        sol_balance,
                        daily_pnl_sol,
                        pos_json,
                        last_decision_rationale,
                        utc_now_iso(),
                    ],
                )

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
                SELECT mint, symbol, pair_address, added_at, safety_passed, entry_liquidity_usd,
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
            added_at,
            safety_passed,
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
            result.append(
                {
                    "mint": mint,
                    "symbol": symbol,
                    "pair_address": pair_address or "",
                    "added_at": added_at,
                    "safety_passed": bool(safety_passed),
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
                    "candidate": json.loads(cand_json) if cand_json else {},
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
        p_addr = str(pair_address if pair_address is not None else (candidate.get("pair_address") or ""))
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
                    mint, symbol, pair_address, added_at, safety_passed, entry_liquidity_usd,
                    peak_mc, low_mc, leg2_score, leg2_breakdown_json, discovery_source,
                    jup_organic_score, jup_meta_json,
                    last_evaluated_at, latest_scores_json, candidate_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
                ON CONFLICT (mint) DO UPDATE SET
                    symbol = excluded.symbol,
                    pair_address = CASE
                        WHEN excluded.pair_address <> '' THEN excluded.pair_address
                        ELSE active_watchlist.pair_address
                    END,
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

    def mark_watchlist_safety_passed(self, mint: str, passed: bool = True) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE active_watchlist SET safety_passed = ? WHERE mint = ?",
                [bool(passed), mint],
            )

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
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO trades (
                    timestamp, mode, side, mint, symbol, in_amount, out_amount,
                    sol_value, pnl_sol, pnl_pct, reason, tx_signature, rationale
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                ],
            )

    def get_recent_trades(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, timestamp, mode, side, mint, symbol, in_amount, out_amount,
                       sol_value, pnl_sol, pnl_pct, reason, tx_signature, rationale
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
        ]
        return [dict(zip(keys, r)) for r in rows]

    def get_recent_closed_trades(self, limit: int = 15) -> list[dict[str, Any]]:
        """Return the last N closed trades (SELL fills) with entry/exit SOL and PnL."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT symbol, mint, sol_value, pnl_sol, pnl_pct, reason, timestamp
                FROM trades
                WHERE UPPER(side) = 'SELL'
                ORDER BY id DESC
                LIMIT ?
                """,
                [limit],
            ).fetchall()
        closed: list[dict[str, Any]] = []
        for symbol, mint, exit_sol, pnl_sol, pnl_pct, reason, ts in rows:
            exit_val = float(exit_sol or 0.0)
            pnl_val = float(pnl_sol or 0.0)
            entry_val = max(0.0, exit_val - pnl_val)
            pct_val = float(pnl_pct or 0.0) * 100.0
            closed.append(
                {
                    "symbol": symbol,
                    "mint": mint,
                    "entry_sol": round(entry_val, 6),
                    "exit_sol": round(exit_val, 6),
                    "pnl_sol": round(pnl_val, 6),
                    "pnl_pct": round(pct_val, 2),
                    "exit_reason": reason,
                    "closed_at": ts,
                }
            )
        return closed

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
    ) -> None:
        """Append a real action row to DuckDB action_log (never routine scan ticks)."""
        ts = timestamp_utc or utc_now_iso()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO action_log (
                    timestamp_utc, bot, mode, action, symbol, asset_id,
                    qty, notional, unit, price_or_value, pnl_amount, pnl_pct,
                    strategy, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    ts,
                    bot,
                    mode,
                    action.upper(),
                    symbol,
                    asset_id,
                    qty,
                    notional,
                    unit,
                    price_or_value,
                    pnl_amount,
                    pnl_pct,
                    strategy,
                    reason,
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
