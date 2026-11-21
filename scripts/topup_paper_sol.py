#!/usr/bin/env python3
"""Safe CLI helper to top up the paper wallet SOL balance in DuckDB."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running directly as `python3 scripts/topup_paper_sol.py --amount 1.0`
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config import load_config
from db import AgentDatabase


def topup_paper_balance(amount: float, db_path: str | None = None) -> tuple[float, float]:
    """Add `amount` SOL to the paper wallet balance in DuckDB and return (before, after)."""
    if amount <= 0:
        raise ValueError("Top-up amount must be greater than 0.")
    cfg = load_config()
    target_db = db_path or cfg.DUCKDB_PATH
    db = AgentDatabase(db_path=target_db, starting_sol=cfg.PAPER_STARTING_SOL)
    try:
        before, after = db.topup_paper_sol(amount)
    finally:
        db.close()
    return before, after


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Safely top up paper wallet SOL balance in DuckDB."
    )
    parser.add_argument(
        "--amount",
        type=float,
        required=True,
        help="Amount of SOL to add to the paper wallet balance (e.g. 1.0).",
    )
    parser.add_argument(
        "--db",
        type=str,
        default=None,
        help="Optional path to DuckDB file (defaults to DUCKDB_PATH from config/.env).",
    )
    args = parser.parse_args(argv)
    before, after = topup_paper_balance(args.amount, db_path=args.db)
    print(f"Paper SOL balance topped up by +{args.amount:.4f} SOL: {before:.4f} SOL -> {after:.4f} SOL")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
