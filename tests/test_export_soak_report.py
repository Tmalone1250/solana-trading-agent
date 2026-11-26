"""Unit tests for scripts/export_soak_report.py (--csv-dir and --db modes)."""

from __future__ import annotations

import csv
import json
import tempfile
from pathlib import Path

from db import AgentDatabase
from outcome_tracker import record_outcome_snapshot
from scripts.export_soak_report import generate_soak_report, main


def test_export_soak_report_db_mode_since_split_stage3_and_breakout_cap(tmp_path: Path):
    """Verify --db mode snapshots DuckDB + WAL safely, splits by --since, and renders Stage 3 & breakout tables."""
    db_path = tmp_path / "live_soak_test.duckdb"
    wal_path = Path(f"{db_path}.wal")
    wal_path.write_bytes(b"")  # Simulate presence of a WAL file alongside the database

    db = AgentDatabase(db_path=str(db_path), starting_sol=2.0)
    pre_ts = "2026-11-25T19:00:00+00:00"
    soak_ts1 = "2026-11-25T21:10:00+00:00"
    soak_ts2 = "2026-11-25T21:15:00+00:00"
    soak_ts3 = "2026-11-25T21:20:00+00:00"
    cutoff = "2026-11-25T20:51:57Z"

    # Seed Pre-Soak trade + Soak BUY & SELL trades
    db._conn.execute(
        """
        INSERT INTO trades (timestamp, mode, side, mint, symbol, in_amount, out_amount, sol_value, pnl_sol, pnl_pct, reason, strategy, opened_at)
        VALUES
            (?, 'paper', 'SELL', 'MintPre111111111', 'PRE', 1000, 450000000, 0.45, -0.05, -0.10, 'HARD_STOP_LOSS', 'SWEEP_RECLAIM', ?),
            (?, 'paper', 'BUY', 'MintSoak11111111', 'SOAK1', 500000000, 1000000, 0.50, NULL, NULL, 'DECISION_1_ENTRY', 'BREAKOUT_IGNITION', ?),
            (?, 'paper', 'SELL', 'MintSoak11111111', 'SOAK1', 1000000, 620000000, 0.62, 0.119, 0.238, 'TRAILING_TP', 'BREAKOUT_IGNITION', ?)
        """,
        [pre_ts, pre_ts, soak_ts1, soak_ts1, soak_ts3, soak_ts1],
    )
    db.log_action(
        mode="paper", action="SELL", symbol="SOAK1", asset_id="MintSoak11111111",
        qty=1000000.0, notional=0.62, price_or_value=0.62, pnl_amount=0.119, pnl_pct=23.8,
        strategy="BREAKOUT_IGNITION", reason="TRAILING_TP", timestamp_utc=soak_ts3,
        position_mfe_pct=28.4, position_mae_pct=-2.1, price_source="jupiter_exact_quote",
    )
    db.log_action(
        mode="paper", action="EVICT", symbol="STALE1", asset_id="MintStale1111111",
        strategy="NONE", reason="STALE_WATCH (16.2m > 15m)", timestamp_utc=soak_ts2, soak_minutes=16.2,
    )

    # Seed Stage 3 decisions for MintSoak11111111 showing cached narrative (3.50) + distinct live momentum (2.10 -> 3.85 -> 4.40)
    for ts, mom, act, setup in [
        (pre_ts, 1.80, "WATCH", "NONE"),
        (soak_ts1, 2.10, "WATCH", "NONE"),
        (soak_ts2, 3.85, "WATCH", "SWEEP_RECLAIM"),
        (soak_ts3, 4.40, "BUY", "BREAKOUT_IGNITION"),
    ]:
        sc = {
            "narrative_strength": 3.50,
            "momentum_quality": mom,
            "is_copycat_vamp": 0.20,
            "action": act,
            "action_confidence": 0.82,
            "leg2_score": 74.0,
            "rsi_5m": 58.0,
            "setup_type": setup,
            "scalper_eligible": setup == "BREAKOUT_IGNITION",
            "narrative_cache_hit": ts != soak_ts1,
            "momentum_source": "live_metrics" if ts != soak_ts1 else "gemini_decision_1",
            "approved": act == "BUY",
            "breakout_telemetry": {"score": 92, "state": "IGNITION"},
        }
        db._conn.execute(
            "INSERT INTO decisions_log (timestamp, mint, symbol, phase, action, confidence, scores_json, rationale) VALUES (?, ?, ?, 'ENTRY', ?, 0.82, ?, 'test')",
            [ts, "MintSoak11111111", "SOAK1", act, json.dumps(sc)],
        )

    # Seed candidate_outcomes (1 high-MFE missed runner + 1 entered trade)
    record_outcome_snapshot(
        db, mint="MintMissedRunner1", symbol="MISSED", source="CHART_ALPHA", decision="WATCH",
        decision_reason="WATCH -> WATCH", strategy="NONE", entry_price_usd=0.001, entry_mc_usd=300000.0,
        entry_liq_usd=50000.0,
    )
    db._conn.execute(
        "UPDATE candidate_outcomes SET discovered_at = ?, mfe_pct = 84.5, mae_pct = -4.2, price_5m_pct = 35.0, price_15m_pct = 72.0, price_30m_pct = 60.0 WHERE symbol = 'MISSED'",
        [soak_ts2],
    )
    db.record_whale_event(
        signature="sig_soak_1", wallet="WhaleWalletSoak11111", mint="MintSoak11111111",
        side="BUY", sol_amount=1.45, token_amount=500000.0, timestamp_utc=soak_ts1,
    )
    db.upsert_whale_decision("MintSoak11111111", "ENTERED", "WHALE_BREAKOUT_CONFLUENCE", updated_at=soak_ts1)
    db.close()
    # Re-create empty WAL file after clean close to test WAL snapshot copy path
    wal_path.write_bytes(b"")

    before_snaps = set(Path(tempfile.gettempdir()).glob("soak_audit_snap_*"))
    out_md = tmp_path / "report_from_db.md"
    rc = main(["--db", str(db_path), "--since", cutoff, "--out", str(out_md)])
    assert rc == 0
    assert out_md.is_file()

    # Verify temporary snapshot directory was cleaned up in finally block
    after_snaps = set(Path(tempfile.gettempdir()).glob("soak_audit_snap_*"))
    assert after_snaps - before_snaps == set()

    text = out_md.read_text(encoding="utf-8")
    assert "## 1. Header & Executive Summary" in text
    assert "## 2. Table Inventory & Schema Overview" in text
    assert "## 3. Trades Performance (`trades`)" in text
    assert "## 4. Alpha Decisions & Stage 3 Verification (`decisions_log`)" in text
    assert "## 5. Candidate Outcomes & Missed-Opportunity Telemetry (`candidate_outcomes`)" in text
    assert "## 6. Pipeline Activity, Slot Churn & Phase 0 Baseline (`action_log`)" in text
    assert "## 7. Whale Pipeline (`whale_events`, `whale_signals`, `whale_decisions`)" in text
    assert "## 8. Live State & Social Signals" in text
    # Verify Stage 3 momentum variance row for SOAK1: 3 soak evals, 3 distinct momentum values (2.10..4.40), narrative 3.50..3.50
    assert "| **SOAK1** | `MintSo..1111` | 3 | 2 | 3 | `2.10..4.40` | `3.50..3.50` |" in text
    assert "MISSED_RUNNER" in text
    assert "+84.50%" in text


def test_export_soak_report_csv_dir_mode_missing_optional_tables_and_150_breakout_cap(tmp_path: Path):
    """Verify --csv-dir mode handles missing optional CSVs gracefully and caps breakout log at 150 rows."""
    csv_dir = tmp_path / "csv_reports"
    csv_dir.mkdir(parents=True)
    cutoff = "2026-11-25T20:51:57Z"

    # Write only trades.csv and decisions_log.csv (all other 11 tables missing/optional)
    with (csv_dir / "trades.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["id", "timestamp", "mode", "side", "mint", "symbol", "in_amount", "out_amount", "sol_value", "pnl_sol", "pnl_pct", "reason", "strategy", "opened_at"],
        )
        w.writeheader()
        w.writerow({
            "id": "1", "timestamp": "2026-11-25T18:00:00+00:00", "mode": "paper", "side": "SELL",
            "mint": "PreMint", "symbol": "PRE", "in_amount": "100", "out_amount": "400",
            "sol_value": "0.40", "pnl_sol": "-0.10", "pnl_pct": "-0.20", "reason": "HARD_STOP_LOSS",
            "strategy": "SWEEP_RECLAIM", "opened_at": "2026-11-25T17:50:00+00:00",
        })
        w.writerow({
            "id": "2", "timestamp": "2026-11-25T22:00:00+00:00", "mode": "paper", "side": "SELL",
            "mint": "SoakMint", "symbol": "WIN", "in_amount": "100", "out_amount": "600",
            "sol_value": "0.60", "pnl_sol": "0.10", "pnl_pct": "0.20", "reason": "TRAILING_TP",
            "strategy": "BREAKOUT_IGNITION", "opened_at": "2026-11-25T21:50:00+00:00",
        })

    # Write 160 non-NONE soak breakout rows in decisions_log.csv to verify 150-row cap note
    with (csv_dir / "decisions_log.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["id", "timestamp", "mint", "symbol", "phase", "action", "confidence", "scores_json", "rationale"],
        )
        w.writeheader()
        for i in range(160):
            sc = {
                "narrative_strength": 3.2,
                "momentum_quality": round(2.0 + (i % 10) * 0.25, 2),
                "is_copycat_vamp": 0.15,
                "action": "WATCH" if i % 2 == 0 else "BUY",
                "action_confidence": 0.75,
                "setup_type": "BREAKOUT_IGNITION",
                "scalper_eligible": True,
                "narrative_cache_hit": True,
                "momentum_source": "live_metrics",
                "approved": i % 2 == 1,
            }
            w.writerow({
                "id": str(i + 1),
                "timestamp": f"2026-11-25T21:{(i // 60):02d}:{(i % 60):02d}+00:00",
                "mint": "MintCapTest111111",
                "symbol": "CAPT",
                "phase": "ENTRY",
                "action": sc["action"],
                "confidence": "0.75",
                "scores_json": json.dumps(sc),
                "rationale": "test cap",
            })

    out_md = tmp_path / "report_from_csv.md"
    md = generate_soak_report(csv_dir=csv_dir, since_str=cutoff, out_path=out_md)
    assert out_md.is_file()
    assert "| `trades` | `YES` | 2 | 1 | 1 |" in md
    assert "| `agent_state` | `NO` | 0 | 0 | 0 |" in md
    assert "Showing first 150 of 160 non-NONE setup evaluations in the Active Soak window." in md
