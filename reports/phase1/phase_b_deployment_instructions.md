# Phase B — Proposed Raspberry Pi Deployment Guide (DO NOT DEPLOY WITHOUT AUTHORIZATION)

> **Status:** Proposed configuration for future review only. Nothing has been installed, enabled, copied, or started on the Raspberry Pi.

## 1. Operational Separation Principles

`deploy/solana-replay-simulator.service` is designed as a **manual, one-shot (`Type=oneshot`)** unit that:
- Has **zero** unit dependencies (`Requires=`, `Wants=`, `PartOf=`, `BindsTo=`, `Conflicts=`) on `solana-trading-agent.service`.
- Enforces **`PrivateNetwork=true`** at the Linux kernel namespace level so outbound calls to Jupiter, Gemini, Helius, DexScreener, or GeckoTerminal are impossible even if attempted.
- Enforces **`ReadOnlyPaths=/home/tmalone1250/solana-trading-agent`** with write permission restricted solely to `reports/` and an isolated `PrivateTmp=true` `/tmp` mount.
- Caps resource consumption on the Raspberry Pi (`MemoryMax=512M`, `CPUQuota=50%`, `Nice=10`, `TimeoutStartSec=180`) so running an offline analysis cannot starve the live trading service.
- Logs under a dedicated journal identifier (`SyslogIdentifier=solana-replay-simulator`).

---

## 2. Safe Snapshot Staging Prerequisite

Because DuckDB holds an exclusive OS write lock while `solana-trading-agent.service` is active, `simulator/data_loader.py` refuses to blindly copy a write-locked database by default (`LIVE_WRITER_LOCK_HELD`).

Always point `solana-replay-simulator.service` at a **separate offline snapshot** under `/home/tmalone1250/solana-trading-agent/reports/snapshots/`:
- Either copy an archived post-soak `.duckdb` (+ `.duckdb.wal` if present) when the live writer is not holding the lock, **or**
- Stage a quiescent-verified snapshot copy explicitly into `reports/snapshots/` and verify its read-only load before running full reports.

Never open or point any writable tool at `/home/tmalone1250/solana-trading-agent/solana_meme_agent.duckdb`.

---

## 3. Manual Installation & Execution Commands (For Future Authorized Use Only)

```bash
# 1. Copy unit file (do NOT run enable --now; service is manual one-shot only)
sudo cp /home/tmalone1250/solana-trading-agent/deploy/solana-replay-simulator.service /etc/systemd/system/
sudo systemctl daemon-reload

# 2. Verify solana-trading-agent.service is untouched
systemctl status solana-trading-agent.service --no-pager

# 3. Trigger a single one-shot replay run against the separate snapshot
sudo systemctl start solana-replay-simulator.service

# 4. Inspect dedicated simulator logs (separate from trading agent logs)
journalctl -u solana-replay-simulator.service -n 100 --no-pager
```
