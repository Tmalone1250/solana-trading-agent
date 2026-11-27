"""Module entry point (`python -m simulator`)."""

from __future__ import annotations

from simulator.cli import run_cli

if __name__ == "__main__":
    raise SystemExit(run_cli())
