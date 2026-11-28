# CHIBI Research State
Last Updated: 2026-11-28T14:31Z

## 1. Historical Deployment & Provenance Snapshot
- **Repository:** `Tmalone1250/solana-trading-agent` (`dev_2` at `fcacc8a7f9a8621f0b71993e5d565e13b8c3d48f`, `origin/dev_1` at `d88e0f374cb7af4c157557ff1e11247de54d5687` — `VERIFIED`).
- **Raspberry Pi (`tmalone1250@192.168.1.67`, `/home/tmalone1250/solana-trading-agent`):**
  - Service: `solana-trading-agent.service` (documented mixed provenance under tx `e4f_deploy_20261128T013054Z_735838`: `553fd59` base + `executor_node.py` from `fcacc8a`, SHA-256 `4a997c40b4d4970afb6068ecaa486f27f478e1cb2f3945b69b30c35498d6efc8`; 16 non-runtime test files retained at `553fd59`).
  - Frozen paper config: `EXECUTION_MODE=paper`, `CHART_ENABLED_STRATEGIES=BREAKOUT_IGNITION`, `WHALE_STRATEGY_ENABLED=false`, `ENABLE_HELIUS_WHALE_WEBHOOK=false`, `MAX_REENTRIES_PER_MINT=0`, `MIN_WATCHLIST_SOAK_MINUTES=1.0`, `SAFETY_CACHE_TTL_SECONDS=300.0`, `PRE_EXECUTION_SAFETY_TTL_SECONDS=120.0`, `REQUIRE_PINNED_POOL_ROUTE_IN_PAPER=false`, E4A optional thresholds `None`.
- **Vercel Dashboard (`https://chibi-bot.vercel.app`):** Serving promoted `dev_2` build polling `https://solana-api.projectkami.net/api/telemetry`; exact Vercel commit SHA is `UNKNOWN`.
- **Live Capital Constraint (Operator Reference):** `~1.5 SOL` potential future live capital (no live authorization; feasibility against `0.50 SOL` trade size + reserves/buffers unresolved).

## 2. Current Research Direction
- Discovery, candidate scheduling, entry intelligence, and exit intelligence are open to fundamental redesign or removal.
- `BREAKOUT_IGNITION` has not demonstrated a repeatable trading edge. No replacement strategy has been selected.
- Earlier entry detection, broader opportunity coverage, and more responsive position management are competing `HYPOTHESIS` directions, not predetermined solutions.
- Antigravity's independent strategic assessment is pending (scheduled next after charter installation and integration).
- The current post-E4F paper soak is an ongoing **diagnostic observation window** (no prospectively finalized Experiment Brief under v1.1).

## 3. Post-E4F Diagnostic Observation Window (Since `2026-11-28T01:31:54Z`)
- **Baseline:** Starting cash `6.895216 SOL`, raw trade ID `413`, `0` open positions. (Pre-E4F window `00:56:34Z`–`01:30Z` archived separately as diagnostic-only.)
- **Operator-Reported Morning Observations (`OBSERVED` / operator-supplied):**
  - Dashboard screenshot showed `10` known completed losing lifecycles, `0` known winners, `~6.5186 SOL` available cash, `0` open positions.
  - `Vicewire` entered near `07:24 CST`, showed favorable movement, then closed at `~-13.03%` net loss.
  - Repeated losing `Fireball` entries; repeated evaluation of `FIONA`, `JIMMY`, `WLFI`, `MoonBoy`; morning journal showed spare watchlist capacity despite concentrated candidate evaluation.
- **Independently Verified Pi Telemetry (`VERIFIED` at `2026-11-28T14:30:43Z`):**
  - `execution_mode=paper`, `focus_state=SCANNING`, `sol_balance=6.542683 SOL`, `daily_pnl_sol=-0.355093 SOL`, `0` open positions, active watchlist `1/5` (`['WLFI']`, confirming spare capacity).
  - `11` completed post-E4F lifecycles (`1W / 10L`, `9.1%` WR, `net_pnl_sol=-0.352533 SOL` across just `4` symbols: `FIONA` x4 including `+4.83%` win at `14:30:43Z`, `JIMMY` x4, `Fireball` x2 at `-22.83%` & `-12.86%`, `Vicewire` x1 at `13:24:11Z`–`13:29:31Z` `-13.03%` `HARD_STOP_LOSS`). Prior to `FIONA`'s `14:30:43Z` win, the 10 completed lifecycles were `0W / 10L` (`6.518557 SOL` cash).
  - Note: *Favorable intra-trade excursion on `Vicewire` and causal attribution of losses remain `UNVERIFIED` / `UNKNOWN` pending dedicated offline analysis.*

## 4. Unresolved Work & Open Blockers
- **Unfinished Local Branch (`impl/dashboard-e4f-compatibility`):** Untouched dirty working tree (`8` modified files + `5` untracked files/dirs, started from `fcacc8a`). Contains financial aggregation and control-gating changes -> requires **Class C** review; `tsc`/`lint`/`build`/`node --test` final verification interrupted by server restart (`UNKNOWN`).
- **Unresolved Backend Regression Failures (`VERIFIED` present at `fcacc8a`):**
  - `tests/test_insider_scanner.py::test_unconditional_hard_stop_loss_on_leg2_reentry_chart_and_whale_positions_at_minus_11_75_pct`
  - `tests/test_outcome_tracker.py::test_paper_tx_fee_sol_deducted_on_buy_partial_sell_and_sell`
  - `tests/test_soak_readiness.py::test_signal_and_telemetry_completeness_mc_usd_exact_quote_and_live_momentum_cache`
  - Actual root causes and any impact on the current observation window remain `UNKNOWN` / unresolved.

## 5. Disagreement Log
- *(None formally open.)*

## 6. Next Pending Operator Decision
- Review and authorize integration of `docs/agents-v1.1` into `dev_2` and activation of `AGENTS.md` v1.1 in the primary workspace, then proceed to collaborative discussion of the operator's trading-agent vision and Antigravity's independent strategic assessment.
