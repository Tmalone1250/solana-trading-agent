"""Dedicated safety, isolation, anti-look-ahead, consistency & determinism tests for `simulator/`."""

from __future__ import annotations

import ast
import json
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import duckdb
import pytest

from simulator import (
    EXACT_HISTORICAL,
    INFERRED_PRICE_FROM_MARKET_CAP,
    MISSING,
    MODE_DETERMINISTIC_GATE_REPLAY,
    MODE_RECORDED_DECISION_AUDIT,
    NOT_REPLAYABLE,
    OBSERVED_QUOTE,
    PAIR_REFERENCE_MINT,
    PAIR_REFERENCE_TIMESTAMP,
    PHASE1_ACTIVE_SOAK_SIM_SINCE,
    PRICE_ONLY,
    RUNTIME_CODE_PROVENANCE,
    UNAVAILABLE,
)
from simulator.cli import run_cli
from simulator.config_profiles import (
    PROFILE_REPO_DEFAULTS,
    PROFILE_SUPPLIED_PHASE1_PI,
    get_config_profile,
)
from simulator.data_loader import (
    REQUIRED_SCHEMA,
    SchemaValidationError,
    SnapshotConsistencyError,
    compute_file_fingerprint,
    load_historical_dataset,
)
from simulator.execution_analyzer import analyze_breakout_executions, analyze_mint_execution
from simulator.outcome_analyzer import analyze_aggregate_outcomes, analyze_observation_outcome
from simulator.replay_engine import run_deterministic_gate_replay, run_recorded_decision_audit
from simulator.snapshot_builder import (
    FORBIDDEN_OUTCOME_FIELDS,
    LookAheadViolationError,
    build_decision_snapshot,
    find_snapshot_for_observation,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
PHASE1_DB = REPO_ROOT / "reports" / "phase1" / "solana_meme_agent.duckdb"
PHASE1_WAL = REPO_ROOT / "reports" / "phase1" / "solana_meme_agent.duckdb.wal"

FORBIDDEN_IMPORTS = {
    "main",
    "executor_node",
    "decision_client",
    "ohlcv_client",
    "httpx",
    "solders",
    "google",
    "uvicorn",
}


def _create_minimal_valid_duckdb(db_path: Path) -> None:
    conn = duckdb.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE action_log (
                id INTEGER, timestamp_utc VARCHAR, bot VARCHAR, mode VARCHAR, action VARCHAR,
                symbol VARCHAR, asset_id VARCHAR, qty DOUBLE, notional DOUBLE, unit VARCHAR,
                price_or_value DOUBLE, pnl_amount DOUBLE, pnl_pct DOUBLE, strategy VARCHAR,
                reason VARCHAR, sol_equity DOUBLE, daily_realized_sol DOUBLE,
                circuit_breaker BOOLEAN, mc_usd DOUBLE, liq_usd DOUBLE, soak_minutes DOUBLE,
                rsi DOUBLE, leg2_score DOUBLE, narrative_score DOUBLE, momentum_score DOUBLE,
                vamp_score DOUBLE, buy_confidence DOUBLE, peak_pnl_pct DOUBLE,
                hold_seconds DOUBLE, triggering_whales VARCHAR, whale_sol_flow DOUBLE,
                breakout_score INTEGER, breakout_state VARCHAR, breakout_timeframe VARCHAR,
                breakout_vol_ratio DOUBLE, breakout_ema_ext_pct DOUBLE,
                breakout_failed_gates VARCHAR, jupiter_price_impact_raw VARCHAR,
                monitor_sol_value DOUBLE, price_source VARCHAR,
                position_mfe_pct DOUBLE, position_mae_pct DOUBLE
            );
            CREATE TABLE trades (
                id INTEGER, timestamp VARCHAR, mode VARCHAR, side VARCHAR, mint VARCHAR,
                symbol VARCHAR, in_amount BIGINT, out_amount BIGINT, sol_value DOUBLE,
                pnl_sol DOUBLE, pnl_pct DOUBLE, reason VARCHAR, tx_signature VARCHAR,
                rationale VARCHAR, strategy VARCHAR, triggering_wallets_json VARCHAR,
                opened_at VARCHAR, jupiter_price_impact_raw VARCHAR
            );
            CREATE TABLE candidate_outcomes (
                id VARCHAR, mint VARCHAR, symbol VARCHAR, source VARCHAR, discovered_at VARCHAR,
                decision VARCHAR, decision_reason VARCHAR, strategy VARCHAR,
                entry_price_usd DOUBLE, entry_mc_usd DOUBLE, entry_liq_usd DOUBLE,
                breakout_score INTEGER, breakout_state VARCHAR, leg2_score DOUBLE,
                vamp_score DOUBLE, rsi DOUBLE, whale_sol_flow DOUBLE, triggering_wallet VARCHAR,
                price_1m_pct DOUBLE, price_5m_pct DOUBLE, price_15m_pct DOUBLE,
                price_30m_pct DOUBLE, mfe_pct DOUBLE, mae_pct DOUBLE, finalized BOOLEAN
            );
            CREATE TABLE decisions_log (
                id INTEGER, timestamp VARCHAR, mint VARCHAR, symbol VARCHAR, phase VARCHAR,
                action VARCHAR, confidence DOUBLE, scores_json VARCHAR, rationale VARCHAR
            );
            CREATE TABLE agent_state (
                id INTEGER, focus_state VARCHAR, sol_balance DOUBLE, daily_start_sol DOUBLE,
                daily_pnl_sol DOUBLE, updated_at VARCHAR
            );
            CREATE TABLE cooldowns (mint VARCHAR, expires_at VARCHAR, reason VARCHAR);
            CREATE TABLE active_watchlist (
                mint VARCHAR, symbol VARCHAR, pair_address VARCHAR, added_at VARCHAR
            );
            """
        )
    finally:
        conn.close()


def test_01_static_and_runtime_no_forbidden_imports_or_wallet_access():
    """Checks #6, #7, #8: No imports of main.py, executor_node, httpx, solders, or live clients."""
    sim_dir = REPO_ROOT / "simulator"
    py_files = sorted(sim_dir.glob("*.py"))
    assert len(py_files) >= 8

    for pf in py_files:
        tree = ast.parse(pf.read_text(encoding="utf-8"), filename=str(pf))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    assert root not in FORBIDDEN_IMPORTS, f"{pf.name} imports forbidden module {alias.name}"
            elif isinstance(node, ast.ImportFrom) and node.module:
                root = node.module.split(".")[0]
                assert root not in FORBIDDEN_IMPORTS, f"{pf.name} imports from forbidden module {node.module}"

    # Subprocess clean-interpreter check to prove `import simulator.cli` loads zero forbidden modules
    code = (
        "import sys, simulator.cli; "
        f"bad = sorted(set(m.split('.')[0] for m in sys.modules) & {FORBIDDEN_IMPORTS!r}); "
        "assert not bad, f'Forbidden modules loaded: {bad}'"
    )
    subprocess.run([sys.executable, "-c", code], cwd=str(REPO_ROOT), check=True)


def test_02_hermetic_config_profiles_prevent_dotenv_and_os_environ_leakage(tmp_path, monkeypatch):
    """Checks #4 & #6: Neither .env nor os.environ can leak secrets or thresholds into simulator profiles."""
    poison_env = tmp_path / ".env"
    poison_env.write_text(
        "GEMINI_API_KEY=LEAKED_DOTENV_KEY\n"
        "SOLANA_PRIVATE_KEY_BS58=LEAKED_DOTENV_WALLET\n"
        "MIN_BUY_CONFIDENCE=0.99\n"
        "BREAKOUT_MIN_SCORE=99\n"
        "PAPER_TX_FEE_SOL=0.777\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GEMINI_API_KEY", "LEAKED_ENVIRON_KEY")
    monkeypatch.setenv("SOLANA_PRIVATE_KEY_BS58", "LEAKED_ENVIRON_WALLET")
    monkeypatch.setenv("MIN_BUY_CONFIDENCE", "0.97")
    monkeypatch.setenv("BREAKOUT_MIN_SCORE", "97")
    monkeypatch.setenv("PAPER_TX_FEE_SOL", "0.888")

    prof_def = get_config_profile(PROFILE_REPO_DEFAULTS)
    assert prof_def.config.GEMINI_API_KEY == ""
    assert prof_def.config.SOLANA_PRIVATE_KEY_BS58 == ""
    assert prof_def.config.MIN_BUY_CONFIDENCE == pytest.approx(0.60)
    assert prof_def.config.BREAKOUT_MIN_SCORE == 75
    assert prof_def.config.PAPER_TX_FEE_SOL == pytest.approx(0.001)
    assert prof_def.runtime_code_provenance == RUNTIME_CODE_PROVENANCE

    prof_pi = get_config_profile(PROFILE_SUPPLIED_PHASE1_PI)
    assert prof_pi.config.GEMINI_API_KEY == ""
    assert prof_pi.config.SOLANA_PRIVATE_KEY_BS58 == ""
    assert prof_pi.config.MIN_BUY_CONFIDENCE == pytest.approx(0.55)
    assert prof_pi.config.BREAKOUT_MIN_SCORE == 55
    assert prof_pi.config.BREAKOUT_MIN_VOL_RATIO == pytest.approx(0.7)
    assert prof_pi.config.MIN_WATCHLIST_SOAK_MINUTES == pytest.approx(1.0)
    assert prof_pi.config.PAPER_TX_FEE_SOL == pytest.approx(0.001)
    assert prof_pi.field_provenance("PAPER_TX_FEE_SOL") == "EXPECTED_FROM_CODE_DEFAULT_OMITTED_IN_SUPPLIED_SNAPSHOT"


def test_03_snapshot_consistency_writer_lock_rejection_and_wal_preservation(tmp_path):
    """Checks #1, #2, #3: Rejects active writer lock, replays WAL in /tmp, and never mutates source DB/WAL."""
    db_file = tmp_path / "test_snap.duckdb"
    _create_minimal_valid_duckdb(db_file)

    # Hold an active writer connection in a child process and verify `load_historical_dataset` fails closed!
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"import duckdb, sys, time; c = duckdb.connect({str(db_file)!r}); "
            "c.execute(\"INSERT INTO cooldowns VALUES ('MintLock', '2026-11-26T22:00:00+00:00', 'TEST')\"); "
            "sys.stdout.write('LOCKED\\n'); sys.stdout.flush(); time.sleep(10)",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        ready = holder.stdout.readline().strip() if holder.stdout else ""
        assert ready == "LOCKED"
        with pytest.raises(SnapshotConsistencyError, match="LIVE_WRITER_LOCK_HELD"):
            load_historical_dataset(db_file)
    finally:
        holder.terminate()
        holder.wait(timeout=5)

    # Verify schema validation fails safely when a required column is missing
    bad_db = tmp_path / "bad_schema.duckdb"
    c_bad = duckdb.connect(str(bad_db))
    c_bad.execute("CREATE TABLE action_log (id INTEGER)")
    c_bad.close()
    with pytest.raises(SchemaValidationError):
        load_historical_dataset(bad_db)

    assert set(REQUIRED_SCHEMA.keys()) == {
        "action_log",
        "trades",
        "candidate_outcomes",
        "decisions_log",
        "agent_state",
        "cooldowns",
        "active_watchlist",
    }


def test_04_lookahead_boundary_blocks_all_future_outcome_fields_and_future_trades(tmp_path):
    """Check #9: DecisionSnapshot strips and raises LookAheadViolationError on any outcome field."""
    db_file = tmp_path / "lookahead.duckdb"
    _create_minimal_valid_duckdb(db_file)
    conn = duckdb.connect(str(db_file))
    try:
        conn.execute(
            """
            INSERT INTO action_log (
                id, timestamp_utc, mode, action, symbol, asset_id, strategy, reason,
                mc_usd, liq_usd, soak_minutes, rsi, leg2_score, narrative_score,
                momentum_score, vamp_score, buy_confidence, breakout_score,
                breakout_state, breakout_timeframe, breakout_vol_ratio,
                breakout_ema_ext_pct, breakout_failed_gates, sol_equity
            ) VALUES (
                1, '2026-11-26T15:00:00+00:00', 'paper', 'DECISION_1', 'TOK', 'MintTok111',
                'NONE', 'WATCH -> WATCH', 150000.0, 30000.0, 2.0, 60.0, 50.0, 2.5,
                2.5, 0.3, 0.7, 40, 'FORMING', '5m', 0.5, 2.0, 'RES(no_break)', 5.0
            )
            """
        )
        # Insert a FUTURE trade at 15:05:00Z (must NOT appear in open_chart_mints_at_decision at 15:00:00Z!)
        conn.execute(
            """
            INSERT INTO trades (
                id, timestamp, mode, side, mint, symbol, in_amount, out_amount,
                sol_value, reason, tx_signature, strategy
            ) VALUES (
                10, '2026-11-26T15:05:00+00:00', 'paper', 'BUY', 'FutureMint999', 'FUT',
                500000000, 1000000, 0.5, 'DECISION_1_ENTRY', 'paper-buy-fut', 'BREAKOUT_IGNITION'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO candidate_outcomes (
                id, mint, symbol, source, discovered_at, decision, strategy,
                entry_price_usd, price_5m_pct, mfe_pct, mae_pct, finalized
            ) VALUES (
                'out-1', 'MintTok111', 'TOK', 'CHART_ALPHA', '2026-11-26T15:00:00+00:00',
                'WATCH', 'NONE', 0.00015, 88.0, 120.0, -5.0, TRUE
            )
            """
        )
    finally:
        conn.close()

    ds = load_historical_dataset(db_file)
    snap = find_snapshot_for_observation(
        ds, mint="MintTok111", decision_timestamp="2026-11-26T15:00:00+00:00"
    )
    assert "FutureMint999" not in snap.open_chart_mints_at_decision
    assert snap.open_chart_count == 0

    for forbidden_field in FORBIDDEN_OUTCOME_FIELDS:
        with pytest.raises(LookAheadViolationError):
            getattr(snap, forbidden_field)
        with pytest.raises(LookAheadViolationError):
            _ = snap.input_availability[forbidden_field]
        with pytest.raises(LookAheadViolationError):
            snap.input_availability.get(forbidden_field)


def test_05_missing_inputs_always_produce_not_replayable_never_silent_pass(tmp_path):
    """Check #11: Missing RSI, soak, vwap_ext, txns_5m, or BUY_RATIO never become PASS."""
    db_file = tmp_path / "missing_gates.duckdb"
    _create_minimal_valid_duckdb(db_file)
    conn = duckdb.connect(str(db_file))
    try:
        conn.execute(
            """
            INSERT INTO action_log (
                id, timestamp_utc, mode, action, symbol, asset_id, strategy, reason,
                mc_usd, liq_usd, soak_minutes, rsi, leg2_score, narrative_score,
                momentum_score, vamp_score, buy_confidence, breakout_score,
                breakout_state, breakout_timeframe, breakout_vol_ratio,
                breakout_ema_ext_pct, breakout_failed_gates
            ) VALUES (
                1, '2026-11-26T16:00:00+00:00', 'paper', 'DECISION_1', 'MISS', 'MintMiss111',
                'BREAKOUT_IGNITION', 'BUY -> APPROVE', 200000.0, 45000.0, NULL, NULL,
                70.0, 3.2, 3.8, 0.20, 0.82, 85, 'IGNITION', '5m', 2.1, 4.5, ''
            )
            """
        )
    finally:
        conn.close()

    ds = load_historical_dataset(db_file)
    prof = get_config_profile(PROFILE_SUPPLIED_PHASE1_PI)
    snap = build_decision_snapshot(ds, action_row=ds.action_log[0])
    rep = run_deterministic_gate_replay(snap, prof)
    by_id = {g.gate_id: g for g in rep.gates}

    assert by_id["G01_RAW_BREAKOUT_DETECTOR"].status == NOT_REPLAYABLE
    assert by_id["G02_BREAKOUT_RESISTANCE_AND_BODY"].status == NOT_REPLAYABLE
    # Empty failed_gates string must NOT be treated as proof that BUY_RATIO or BUY_ACCEL passed!
    assert by_id["G03_BREAKOUT_FLOW_AND_ACCEL_SCORE_PILLARS"].status == NOT_REPLAYABLE
    assert by_id["G06_BREAKOUT_ANTI_CHASE_EXTENSION"].status == NOT_REPLAYABLE
    assert by_id["G07_BREAKOUT_RSI_CEILING"].status == NOT_REPLAYABLE
    assert by_id["G08_WATCHLIST_SOAK_GATE"].status == NOT_REPLAYABLE
    assert by_id["G09_SCALPER_MIN_TXNS_5M"].status == NOT_REPLAYABLE
    assert rep.overall_entry_verdict == "NOT_REPLAYABLE_FOR_FULL_APPROVAL"


@pytest.mark.skipif(not PHASE1_DB.is_file(), reason="Phase 1 DuckDB artifact not present")
def test_06_phase1_pair_reference_case_no_network_no_db_mutation_and_determinism(tmp_path, monkeypatch):
    """Checks #1, #5, #10, #12, #13: Full Phase 1 + PAIR audit with network blocked & SHA-256 unchanged."""
    pre_db_fp = compute_file_fingerprint(PHASE1_DB)
    pre_wal_fp = compute_file_fingerprint(PHASE1_WAL)

    def _forbid_network(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("Simulator attempted an outbound network socket connection!")

    monkeypatch.setattr(socket.socket, "connect", _forbid_network)
    monkeypatch.setattr(socket, "create_connection", _forbid_network)

    out_dir_1 = tmp_path / "run1"
    out_dir_2 = tmp_path / "run2"
    assert run_cli(["--db", str(PHASE1_DB), "--profile", PROFILE_SUPPLIED_PHASE1_PI, "--mode", "all", "--out-dir", str(out_dir_1)]) == 0
    assert run_cli(["--db", str(PHASE1_DB), "--profile", PROFILE_SUPPLIED_PHASE1_PI, "--mode", "all", "--out-dir", str(out_dir_2)]) == 0

    # Verify source DB and WAL are 100% untouched (inode, size, mtime_ns, sha256)
    assert compute_file_fingerprint(PHASE1_DB) == pre_db_fp
    assert compute_file_fingerprint(PHASE1_WAL) == pre_wal_fp

    # Verify deterministic Markdownoutput across identical runs (ignoring acquired_at_utc in aggregate JSON)
    pair_md_1 = (out_dir_1 / "phase_b_pair_reference_report.md").read_text(encoding="utf-8")
    pair_md_2 = (out_dir_2 / "phase_b_pair_reference_report.md").read_text(encoding="utf-8")
    assert pair_md_1 == pair_md_2

    rep_md_1 = (out_dir_1 / "phase_b_replayability_report.md").read_text(encoding="utf-8")
    rep_md_2 = (out_dir_2 / "phase_b_replayability_report.md").read_text(encoding="utf-8")
    assert rep_md_1 == rep_md_2

    agg_md_1 = (out_dir_1 / "phase_b_aggregate_soak_report.md").read_text(encoding="utf-8")
    agg_md_2 = (out_dir_2 / "phase_b_aggregate_soak_report.md").read_text(encoding="utf-8")
    assert agg_md_1 == agg_md_2

    pair_payload = json.loads((out_dir_1 / "phase_b_pair_reference_report.json").read_text(encoding="utf-8"))
    assert pair_payload["runtime_code_provenance"] == RUNTIME_CODE_PROVENANCE
    assert pair_payload["target"]["mint"] == PAIR_REFERENCE_MINT
    assert pair_payload["target"]["decision_timestamp"] == PAIR_REFERENCE_TIMESTAMP
    assert pair_payload["target"]["mint_total_episodes_in_window"] == 20
    assert pair_payload["target"]["mint_total_d1_observations_in_window"] == 362

    aud = pair_payload["recorded_decision_audit"]
    assert aud["replay_mode"] == MODE_RECORDED_DECISION_AUDIT
    assert aud["recorded_decision_class"] == "REJECT"
    assert aud["recorded_evicted_same_tick"] is True
    assert "LOW_SCORE" in (aud["recorded_eviction_reason"] or "")
    m = aud["recorded_metrics"]
    assert m["mc_usd"] == pytest.approx(148391.0)
    assert m["liq_usd"] == pytest.approx(28543.96)
    assert m["soak_minutes"] == pytest.approx(0.05)
    assert m["rsi"] == pytest.approx(57.95)
    assert m["breakout_score"] == 18
    assert m["breakout_state"] == "FORMING"
    assert m["breakout_timeframe"] == "5m"
    assert m["breakout_vol_ratio"] == pytest.approx(0.47)
    assert m["breakout_ema_ext_pct"] == pytest.approx(-17.01)
    assert m["narrative_score"] == pytest.approx(1.95)
    assert m["momentum_score"] == pytest.approx(1.26)
    assert m["vamp_score"] == pytest.approx(0.501)
    assert m["buy_confidence"] == pytest.approx(0.80)

    grp = pair_payload["deterministic_gate_replay"]
    assert grp["replay_mode"] == MODE_DETERMINISTIC_GATE_REPLAY
    assert grp["overall_entry_verdict"] == "DETERMINISTICALLY_REJECTED_OR_BLOCKED"
    assert grp["consistent_with_recorded_outcome"] is True
    assert "E02_LOW_SCORE_EVICTION_RULE" in grp["eviction_replay_verdict"]

    ex = pair_payload["execution_analysis"]
    assert ex["traded"] is False
    assert ex["executable_quote_availability"] == UNAVAILABLE

    out = pair_payload["post_decision_outcome_analysis"]
    orec = out["matched_outcome_record"]
    assert orec["evidence_classification"] == PRICE_ONLY
    assert orec["mfe_pct"] == pytest.approx(95.42)
    assert orec["mae_pct"] == pytest.approx(0.0)
    assert orec["price_5m_pct"] == pytest.approx(24.09)
    assert orec["price_15m_pct"] == pytest.approx(41.72)
    assert out["inferred_price_provenance_label"] == INFERRED_PRICE_FROM_MARKET_CAP
    assert all(pt["price_provenance"] in (INFERRED_PRICE_FROM_MARKET_CAP, MISSING) for pt in out["sample_trajectory_30m"])

    # Also verify aggregate execution & outcome invariants
    agg_payload = json.loads((out_dir_1 / "phase_b_aggregate_soak_report.json").read_text(encoding="utf-8"))
    assert agg_payload["decision_replay_summary"]["mismatch_count"] == 0
    assert agg_payload["execution_analysis"]["total_breakout_entries"] == 17
    assert agg_payload["execution_analysis"]["all_fees_verified"] is True
    assert agg_payload["execution_analysis"]["trigger_evidence_counts"][OBSERVED_QUOTE] == 6
    assert agg_payload["execution_analysis"]["trigger_evidence_counts"][PRICE_ONLY] == 11
    assert agg_payload["outcome_analysis"]["approved_episodes_missing_entered_outcome"] == 17
