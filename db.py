"""DuckDB persistence layer for state, trades, cooldowns, and Decision 1 logs."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import duckdb

from state import FocusState


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
                    last_evaluated_at VARCHAR,
                    latest_scores_json VARCHAR,
                    candidate_json VARCHAR NOT NULL
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
    ) -> None:
        ts = added_at or utc_now_iso()
        p_addr = str(pair_address if pair_address is not None else (candidate.get("pair_address") or ""))
        mc = float(candidate.get("market_cap_usd") or 0.0)
        liq = float(entry_liquidity_usd if entry_liquidity_usd is not None else (candidate.get("liquidity_usd") or 0.0))
        p_mc = float(peak_mc if peak_mc is not None else mc)
        l_mc = float(low_mc if low_mc is not None else mc)
        bd_json = json.dumps(leg2_breakdown) if leg2_breakdown is not None else None
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO active_watchlist (
                    mint, symbol, pair_address, added_at, safety_passed, entry_liquidity_usd,
                    peak_mc, low_mc, leg2_score, leg2_breakdown_json,
                    last_evaluated_at, latest_scores_json, candidate_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
                ON CONFLICT (mint) DO UPDATE SET
                    symbol = excluded.symbol,
                    pair_address = CASE
                        WHEN excluded.pair_address <> '' THEN excluded.pair_address
                        ELSE active_watchlist.pair_address
                    END,
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
                [mint, symbol, p_addr, ts, bool(safety_passed), liq, p_mc, l_mc, float(leg2_score), bd_json, json.dumps(candidate)],
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
