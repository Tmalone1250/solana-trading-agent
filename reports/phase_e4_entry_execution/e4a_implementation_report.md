# Phase E4A Implementation Report — BREAKOUT_IGNITION Pre-Broadcast Approval & Quote Integrity

- **Branch:** `impl/e4a-approval-quote-integrity`
- **Source Baseline:** `f7c04c27279524fe12f442236df3be205a65718b` (approved Phase E4D implementation)
- **Design Reference:** `reports/phase_e4_entry_execution/e4_implementation_design.md` (§4A, Rev 4 `a27273b1289ea1868e4b314365def8e72f567a45`)
- **Scope:** E4A only (`state.py`, `config.py`, `alpha_node.py`, `executor_node.py`, and tests). `db.py`, `state_ledger.py`, `tx_reconciler.py`, `main.py`, `.env`, and runtime databases were not modified.

## Summary of Implemented Changes

### 1. `state.py` — `PendingOrder` E4A Provenance Fields
Added 9 optional E4A fields to `PendingOrder` (all defaulting to `None`):
- `approved_at`
- `approval_price_observed_at`
- `approval_pair_address`
- `approval_price_native`
- `approval_price_source`
- `approval_candle_close_usd`
- `approval_ohlcv_fetched_at`
- `approval_candle_ts`
- `token_decimals`

### 2. `config.py` — E4A Threshold Settings (No Invented Defaults)
Added 10 optional `Field(default=None, ...)` settings to `AgentConfig`:
- `MAX_BUY_APPROVAL_AGE_SECONDS`
- `MAX_APPROVAL_PRICE_AGE_SECONDS`
- `MAX_ENTRY_PRICE_DRIFT_PCT`
- `MAX_NEGATIVE_ENTRY_DRIFT_PCT`
- `MAX_WORST_CASE_ENTRY_DRIFT_PCT`
- `MAX_QUOTE_AGE_SECONDS`
- `MAX_ACCEPTED_SLIPPAGE_BPS`
- `MAX_ENTRY_PRICE_IMPACT_PCT`
- `MIN_BLOCK_HEIGHT_HEADROOM`
- `MIN_RFQ_EXPIRY_HEADROOM_SECONDS`

### 3. `alpha_node.py` — Approval Provenance & Pinned-Pool Price
- Retained OHLCV fetch provenance (`prov_by_mint`) for audit-only metadata (`approval_ohlcv_fetched_at`, `approval_candle_ts`, `approval_candle_close_usd`).
- Recorded `approved_at` at Decision 1 approval.
- Fetched DexScreener `/tokens/v1/solana/{mint}` and matched strictly against `chainId == "solana"` and `pairAddress == winner_pair` to record `approval_price_native`, `approval_price_observed_at`, `approval_pair_address`, and `approval_price_source = "dexscreener_pinned_pool_native"`.
- Resolved `token_decimals` explicitly from candidate metadata or Jupiter `/price/v3` (`decimals`), without defaulting to `6`.

### 4. `executor_node.py` — Pre-Broadcast Approval & Quote Integrity Gates
- Added `E4AValidationError` and pre-broadcast validation helpers (`_validate_e4a_approval_provenance`, `_parse_e4a_price_impact_pct`, `_validate_e4a_quote_timing_and_expiry`, `_validate_e4a_quote_receipt`).
- In live mode for `BREAKOUT_IGNITION` `BUY`, fails closed (`E4A_LIVE_THRESHOLDS_UNSET`) if any required E4A threshold is unset (`None`).
- Validates `approved_at`, `approval_price_observed_at`, `approval_pair_address == pending_order.pair_address`, `approval_price_source`, finite positive `approval_price_native`, and explicit `token_decimals` (`0..18`) before reserving SOL or calling Jupiter.
- Validates quote receipt across three checkpoints (`RECEIPT`, `PRE_SIGN`, `PRE_SUBMIT`), including:
  - `out_amount > 0` in both paper and live modes.
  - Quote age (`MAX_QUOTE_AGE_SECONDS`).
  - Router-specific expiry headroom (`lastValidBlockHeight` via Solana RPC `getBlockHeight` with `MIN_BLOCK_HEIGHT_HEADROOM`; RFQ `expireAt` with `MIN_RFQ_EXPIRY_HEADROOM_SECONDS`).
  - RTSE preservation (no `slippageBps` request parameter sent) and response `slippageBps <= MAX_ACCEPTED_SLIPPAGE_BPS`.
  - Price impact validation (`MAX_ENTRY_PRICE_IMPACT_PCT`).
  - Entry price drift (`MAX_ENTRY_PRICE_DRIFT_PCT`, `MAX_NEGATIVE_ENTRY_DRIFT_PCT`) and `otherAmountThreshold` worst-case entry drift (`MAX_WORST_CASE_ENTRY_DRIFT_PCT`).
- Any `E4AValidationError` before broadcast releases the active `reservation_id`, transitions any `PREPARED` intent to `FAILED` (`ABORTED_BEFORE_BROADCAST`), logs to `action_log`, and returns `{"pending_order": None}`.

## Artifacts
- Full diff: [e4a_full_diff.patch](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/reports/phase_e4_entry_execution/e4a_full_diff.patch)
- Test results: [e4a_test_results.md](file:///home/tmalone1250/ToshibaDrive/Dev/Web3_Dev/solana-trading-agent/reports/phase_e4_entry_execution/e4a_test_results.md)

## E4A Corrective Pass (from `0688fb704aedfb75f3bac329f52064d7af9a9aae`)
- **Fail closed when `otherAmountThreshold` is missing or inconsistent:** In `executor_node.py` (`_validate_e4a_quote_receipt`), when E4A entry-drift validation is active, `otherAmountThreshold` is required and strictly parsed (`_parse_strict_int(..., min_value=1)`), and any `otherAmountThreshold > out_amount` is rejected (`INCONSISTENT_OTHER_AMOUNT_THRESHOLD`).
- **Strict atomic/integer parsing:** Added `_parse_strict_int` in `executor_node.py` (rejecting `None`, `bool`, `float`, fractional/scientific strings, empty/whitespace strings, and out-of-range values) and applied it to `outAmount` (outside the paper-SELL fallback `try/except`, rejecting malformed `outAmount` in both paper and live modes), `otherAmountThreshold`, `lastValidBlockHeight`, and `slippageBps`.
- **Explicit router allowlist & expiry handling:** In `_validate_e4a_quote_timing_and_expiry`, `metis`/`dflow`/`okx` route to RPC `getBlockHeight` + `lastValidBlockHeight` headroom, `jupiterz` routes to `expireAt` RFQ headroom, and missing, empty, malformed, or unsupported router identifiers fail closed in live mode (`UNSUPPORTED_OR_MISSING_ROUTER_<STAGE>`) without falling into the block-height branch.
- **Regression coverage:** Added `test_e4a_corrective_strict_parsing_missing_oat_and_router_allowlist` in `tests/test_e4a_approval_quote_integrity.py`. Only `executor_node.py`, `tests/test_e4a_approval_quote_integrity.py`, and E4A report artifacts were modified in this corrective pass.

## E4A Post-Broadcast Output Validation Correction (from `531272a637905f105d85a2f11ea1e973874b22af`)
- **Strict `outputAmountResult` validation:** Replaced permissive `int(exec_resp["outputAmountResult"])` in `executor_node.py` with `_parse_strict_int(..., "OUTPUT_AMOUNT_RESULT", min_value=1)`, rejecting booleans, floats, fractional/scientific strings, empty/malformed strings, zero, and negatives while preserving valid positive integers and canonical integer strings.
- **Unresolved post-broadcast lifecycle preserved:** Any missing (for active E4A), malformed, or non-positive `outputAmountResult` raises `RuntimeError` (never `E4AValidationError`) after `broadcast_attempted = True`, keeping the E4D intent `UNRESOLVED` (never `FAILED`), transitioning the reservation to `BROADCAST_UNRESOLVED`, skipping position/trade settlement, and leaving resolution to startup reconciliation.
- **Regression coverage:** Added `test_e4a_post_broadcast_output_amount_result_strict_and_unresolved_lifecycle` in `tests/test_e4a_approval_quote_integrity.py`.
