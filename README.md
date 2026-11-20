# solana-meme-agent

Standalone, autonomous Solana memecoin trading agent built with **LangGraph**, **DuckDB**, **FastAPI**, **Gemini Decisions API (`gemini-decision-1`, `v1beta`)**, and **Jupiter Swap API V2**.

Trades one focused token at a time using a **3-State Focus Machine** (`SCANNING` -> `IN_POSITION` -> `WATCHING_REENTRY`) with `EXECUTION_MODE=paper|live` (default: `paper`).

## Quick Start

```bash
cp .env.example .env
# Fill in GEMINI_API_KEY and JUPITER_API_KEY (and SOLANA_PRIVATE_KEY_BS58 for live mode)

./venv/bin/pytest -v
./venv/bin/python main.py
```

Telemetry API runs at `http://localhost:8001/api/telemetry`.
