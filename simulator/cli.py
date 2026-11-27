"""Standalone read-only CLI for the BREAKOUT_IGNITION Historical Replay Simulator."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from simulator import (
    PAIR_REFERENCE_MINT,
    PAIR_REFERENCE_TIMESTAMP,
    PHASE1_ACTIVE_SOAK_SIM_SINCE,
)
from simulator.config_profiles import (
    PROFILE_REPO_DEFAULTS,
    PROFILE_SUPPLIED_PHASE1_PI,
    get_config_profile,
)
from simulator.data_loader import load_historical_dataset
from simulator.execution_analyzer import (
    analyze_breakout_executions,
    analyze_mint_execution,
)
from simulator.outcome_analyzer import (
    analyze_aggregate_outcomes,
    analyze_observation_outcome,
)
from simulator.report_renderer import (
    build_aggregate_soak_payload,
    build_token_observation_payload,
    render_aggregate_soak_report,
    render_replayability_report,
    render_token_observation_report,
    summarize_dataset_replay,
)
from simulator.replay_engine import (
    run_deterministic_gate_replay,
    run_recorded_decision_audit,
)
from simulator.snapshot_builder import find_snapshot_for_observation


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m simulator",
        description="Isolated Read-Only BREAKOUT_IGNITION Historical Decision Replay Simulator",
    )
    parser.add_argument(
        "--db",
        required=True,
        help="Path to offline/snapshot DuckDB file (copied read-only to /tmp with .wal if present).",
    )
    parser.add_argument(
        "--profile",
        default=PROFILE_SUPPLIED_PHASE1_PI,
        choices=[PROFILE_SUPPLIED_PHASE1_PI, PROFILE_REPO_DEFAULTS],
        help="Explicit hermetic configuration profile (never reads .env or os.environ).",
    )
    parser.add_argument(
        "--since",
        default=PHASE1_ACTIVE_SOAK_SIM_SINCE,
        help="UTC ISO-8601 lower bound for episode & trade aggregation (pass '' for all rows).",
    )
    parser.add_argument(
        "--mode",
        default="all",
        choices=["pair", "token", "aggregate", "replayability", "all"],
        help="Report mode to execute.",
    )
    parser.add_argument(
        "--mint",
        default=PAIR_REFERENCE_MINT,
        help="Target mint address for --mode token (defaults to PAIR reference mint).",
    )
    parser.add_argument(
        "--timestamp",
        default=PAIR_REFERENCE_TIMESTAMP,
        help="Target decision_timestamp (UTC ISO-8601) for --mode token.",
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        help="Directory to write Markdown and JSON report files (prints to stdout if omitted).",
    )
    parser.add_argument(
        "--allow-quiescent-live-copy",
        action="store_true",
        default=False,
        help="Opt into bounded quiescent-window SHA-256 verified retry if an active writer lock exists.",
    )
    return parser


def run_cli(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    since_utc: Optional[str] = args.since.strip() if args.since and args.since.strip() else None
    profile = get_config_profile(args.profile)
    dataset = load_historical_dataset(
        args.db,
        since_utc=since_utc,
        allow_quiescent_live_copy=bool(args.allow_quiescent_live_copy),
    )

    out_dir = Path(args.out_dir).resolve() if args.out_dir else None
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)

    if args.mode in ("pair", "token", "all"):
        target_mint = PAIR_REFERENCE_MINT if args.mode in ("pair", "all") else args.mint
        target_ts = PAIR_REFERENCE_TIMESTAMP if args.mode in ("pair", "all") else args.timestamp

        snap = find_snapshot_for_observation(
            dataset,
            mint=target_mint,
            decision_timestamp=target_ts,
            vamp_warn_threshold=float(profile.config.VAMP_WARN_THRESHOLD),
            low_score_threshold=float(profile.config.LOW_SCORE_EVICT_THRESHOLD),
        )
        audit = run_recorded_decision_audit(snap, profile)
        gate_rep = run_deterministic_gate_replay(snap, profile)
        mint_exec = analyze_mint_execution(
            dataset,
            mint=snap.mint,
            symbol=snap.symbol,
            profile=profile,
            since_utc=since_utc,
        )
        obs_out = analyze_observation_outcome(
            dataset,
            mint=snap.mint,
            symbol=snap.symbol,
            decision_timestamp=snap.decision_timestamp,
            base_mc_usd=snap.mc_usd,
            base_liq_usd=snap.liq_usd,
            episode_id=snap.episode_id,
        )
        token_md = render_token_observation_report(
            dataset=dataset,
            profile=profile,
            snapshot=snap,
            audit=audit,
            gate_replay=gate_rep,
            execution=mint_exec,
            outcome=obs_out,
        )
        token_json = build_token_observation_payload(
            dataset=dataset,
            snapshot=snap,
            audit=audit,
            gate_replay=gate_rep,
            execution=mint_exec,
            outcome=obs_out,
        )
        if out_dir is not None:
            stem = "phase_b_pair_reference_report" if target_mint == PAIR_REFERENCE_MINT else f"token_{snap.symbol.lower()}_report"
            (out_dir / f"{stem}.md").write_text(token_md, encoding="utf-8")
            (out_dir / f"{stem}.json").write_text(
                json.dumps(token_json, indent=2, sort_keys=True, default=str) + "\n",
                encoding="utf-8",
            )
        else:
            sys.stdout.write(token_md + "\n")

    if args.mode in ("aggregate", "replayability", "all"):
        replay_summary = summarize_dataset_replay(dataset, profile)

        if args.mode in ("replayability", "all"):
            rep_md = render_replayability_report(
                dataset=dataset,
                profile=profile,
                replay_summary=replay_summary,
            )
            if out_dir is not None:
                (out_dir / "phase_b_replayability_report.md").write_text(rep_md, encoding="utf-8")
            elif args.mode == "replayability":
                sys.stdout.write(rep_md + "\n")

        if args.mode in ("aggregate", "all"):
            exec_rep = analyze_breakout_executions(dataset, profile, since_utc=since_utc)
            out_rep = analyze_aggregate_outcomes(dataset, since_utc=since_utc)
            agg_md = render_aggregate_soak_report(
                dataset=dataset,
                profile=profile,
                replay_summary=replay_summary,
                execution_report=exec_rep,
                outcome_report=out_rep,
            )
            agg_json = build_aggregate_soak_payload(
                dataset=dataset,
                profile=profile,
                replay_summary=replay_summary,
                execution_report=exec_rep,
                outcome_report=out_rep,
            )
            if out_dir is not None:
                (out_dir / "phase_b_aggregate_soak_report.md").write_text(agg_md, encoding="utf-8")
                (out_dir / "phase_b_aggregate_soak_report.json").write_text(
                    json.dumps(agg_json, indent=2, sort_keys=True, default=str) + "\n",
                    encoding="utf-8",
                )
            elif args.mode == "aggregate":
                sys.stdout.write(agg_md + "\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
