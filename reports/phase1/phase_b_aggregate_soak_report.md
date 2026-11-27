# Phase 1 BREAKOUT_IGNITION — Aggregate Historical Replay & Outcome Report

## 1. Provenance & Configuration Envelope

| Attribute | Value |
| :--- | :--- |
| **Strategy Scope** | `BREAKOUT_IGNITION` ONLY |
| **Runtime Code Provenance** | `HISTORICAL_CODE_VERSION_UNVERIFIED` |
| **Configuration Profile** | `supplied_phase1_pi_snapshot` (`SUPPLIED_PI_ENV_SNAPSHOT_NOT_VERIFIED_AS_PID_8315_EFFECTIVE_RUNTIME`) |
| **Active Soak Cutoff (`--since`)** | `2026-11-26T03:15:45+00:00` |
| **Snapshot Method** | `OFFLINE_LOCK_FREE_SHA256_VERIFIED_SNAPSHOT` (`wal_included=True`) |
| **Source DB SHA-256** | `fd5fe1ef0517eda7a23fe8d0240d8bbec058b5584f3ce4d4a505f1a3e8dc3abd` |
| **Source WAL SHA-256** | `771c2ce29a60edc121b42427317aeab48b665cedcab99ad98fd108eaf7e5252b` |

---

## 2. Episode-Grouped Decision Replay Summary

| Metric | Count / Value |
| :--- | ---: |
| Total `DECISION_1` Observations Replayed | `18332` |
| Distinct Mint Watchlist Episodes (`DecisionEpisode`) | `854` |
| Unique Mints Evaluated | `101` |
| Recorded `APPROVED` / `BLOCKED` / `WATCH` / `REJECT` | `17` / `22` / `17621` / `672` |
| Replay Verdict: `DETERMINISTICALLY_REJECTED_OR_BLOCKED` | `18315` |
| Replay Verdict: `NOT_REPLAYABLE_FOR_FULL_APPROVAL` (0 scalar fails; candle/txn gates `NOT_REPLAYABLE`) | `17` |
| Consistent With Recorded Outcome (`supplied_phase1_pi_snapshot`) | `18332 / 18332` (`mismatches=0`) |

---

## 3. Historical Execution & Fee Accounting Analysis (`trades` + `action_log`)

| Execution Metric | Value |
| :--- | ---: |
| `BREAKOUT_IGNITION` Entries (`BUY`) | `17` |
| Closed Lifecycles / Open at Snapshot | `17` / `0` |
| `PARTIAL_SELL` Legs / Final `SELL` Legs | `0` / `17` |
| Configured Paper Fee Per Leg (`PAPER_TX_FEE_SOL`) | `0.0010 SOL` (`EXPECTED_FROM_CODE_DEFAULT_OMITTED_IN_SUPPLIED_SNAPSHOT`) |
| All Trade Fee Deductions Verified Exact | `True` (`VERIFIED=17`, `FAILED=0`, `UNKNOWN=0`, coverage=`100.00%`, `0.0340 SOL` expected fees) |
| Gross PnL (Before Fees) | `-0.277308 SOL` |
| Net Realized PnL (After Fees) | `-0.311308 SOL` |
| Win Rate (Closed Lifecycles) | `23.53%` (`4W / 13L`) |
| Instant `EXODUS_SELL_FLOW` Exits (`<= 45s` hold) | `4` (5s monitor trajectory: `UNAVAILABLE`) |

### Trigger-to-Settlement Slippage by Exit Category

| Exit Category | Count | Trigger Evidence | Settlement Evidence | Min Slippage % | Median Slippage % | Mean Slippage % | Max Slippage % |
| :--- | ---: | :--- | :--- | ---: | ---: | ---: | ---: |
| `BREAKEVEN_SHIELD` | 2 | `OBSERVED_QUOTE` | `OBSERVED_QUOTE` | `+0.0000%` | `+0.0001%` | `+0.0001%` | `+0.0001%` |
| `EXODUS_SELL_FLOW` | 11 | `PRICE_ONLY` | `OBSERVED_QUOTE` | `-2.4241%` | `-0.4519%` | `-0.9584%` | `+0.1038%` |
| `HARD_STOP_LOSS` | 3 | `OBSERVED_QUOTE` | `OBSERVED_QUOTE` | `-0.0001%` | `+0.0001%` | `+0.0004%` | `+0.0013%` |
| `TRAILING_TP` | 1 | `OBSERVED_QUOTE` | `OBSERVED_QUOTE` | `-0.0000%` | `-0.0000%` | `+0.0000%` | `-0.0000%` |

### Reconstructed Historical Trade Lifecycles

| Buy ID | Symbol | Entry UTC | Hold (s) | Exit Category | Trigger Ev. | Monitor SOL | Settled SOL | Slippage % | Net PnL SOL | MFE % | MAE % |
| ---: | :--- | :--- | ---: | :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: |
| 355 | **NORFEATH** | `2026-11-26T04:59:02.248617+00:00` | `701.4` | `HARD_STOP_LOSS` | `OBSERVED_QUOTE` | `0.4349` | `0.4349` | `+0.001%` | `-0.0671` | `-3.14%` | `-13.02%` |
| 356 | **FIONA** | `2026-11-26T05:03:38.514414+00:00` | `694.8` | `EXODUS_SELL_FLOW` | `PRICE_ONLY` | `0.5015` | `0.4992` | `-0.452%` | `-0.0028` | `+0.91%` | `-2.49%` |
| 359 | **PUMPCHAN** | `2026-11-26T06:39:03.068334+00:00` | `2125.8` | `TRAILING_TP` | `OBSERVED_QUOTE` | `0.5183` | `0.5183` | `-0.000%` | `+0.0163` | `+14.03%` | `-0.60%` |
| 361 | **Pompom** | `2026-11-26T07:34:00.837828+00:00` | `981.0` | `EXODUS_SELL_FLOW` | `PRICE_ONLY` | `0.4967` | `0.4949` | `-0.360%` | `-0.0071` | `+3.68%` | `-7.74%` |
| 363 | **Zero** | `2026-11-26T08:12:24.188317+00:00` | `905.0` | `HARD_STOP_LOSS` | `OBSERVED_QUOTE` | `0.4394` | `0.4394` | `+0.000%` | `-0.0626` | `+0.63%` | `-12.11%` |
| 364 | **JIMMY** | `2026-11-26T08:25:41.624987+00:00` | `5.6` | `EXODUS_SELL_FLOW` | `PRICE_ONLY` | `0.4991` | `0.4907` | `-1.679%` | `-0.0113` | `-0.18%` | `-0.18%` |
| 367 | **UHI** | `2026-11-26T10:10:00.673262+00:00` | `1311.5` | `EXODUS_SELL_FLOW` | `PRICE_ONLY` | `0.4835` | `0.4779` | `-1.164%` | `-0.0241` | `+6.16%` | `-4.91%` |
| 368 | **NORFEATH** | `2026-11-26T10:13:23.195377+00:00` | `168.4` | `HARD_STOP_LOSS` | `OBSERVED_QUOTE` | `0.4394` | `0.4394` | `-0.000%` | `-0.0626` | `-1.92%` | `-12.12%` |
| 371 | **JIMMY** | `2026-11-26T11:43:49.653198+00:00` | `1114.0` | `BREAKEVEN_SHIELD` | `OBSERVED_QUOTE` | `0.5093` | `0.5093` | `+0.000%` | `+0.0073` | `+9.69%` | `-2.57%` |
| 373 | **JIMMY** | `2026-11-26T13:41:54.097772+00:00` | `559.8` | `BREAKEVEN_SHIELD` | `OBSERVED_QUOTE` | `0.5099` | `0.5099` | `+0.000%` | `+0.0079` | `+8.82%` | `-1.55%` |
| 375 | **UHI** | `2026-11-26T14:23:57.530429+00:00` | `38.3` | `EXODUS_SELL_FLOW` | `PRICE_ONLY` | `0.4849` | `0.4776` | `-1.522%` | `-0.0244` | `-1.92%` | `-5.09%` |
| 377 | **BASKET** | `2026-11-26T14:40:48.853453+00:00` | `24.9` | `EXODUS_SELL_FLOW` | `PRICE_ONLY` | `0.4942` | `0.4933` | `-0.193%` | `-0.0087` | `-0.69%` | `-1.15%` |
| 379 | **JIMMY** | `2026-11-26T17:08:32.028104+00:00` | `1386.3` | `EXODUS_SELL_FLOW` | `PRICE_ONLY` | `0.4768` | `0.4666` | `-2.146%` | `-0.0354` | `-1.56%` | `-9.56%` |
| 380 | **Zero** | `2026-11-26T17:14:27.283510+00:00` | `5.6` | `EXODUS_SELL_FLOW` | `PRICE_ONLY` | `0.4984` | `0.4863` | `-2.424%` | `-0.0157` | `-0.32%` | `-0.32%` |
| 383 | **FIONA** | `2026-11-26T18:09:08.597959+00:00` | `230.0` | `EXODUS_SELL_FLOW` | `PRICE_ONLY` | `0.4988` | `0.4994` | `+0.104%` | `-0.0026` | `+1.08%` | `-0.82%` |
| 385 | **UHI** | `2026-11-26T19:05:53.254961+00:00` | `605.2` | `EXODUS_SELL_FLOW` | `PRICE_ONLY` | `0.4820` | `0.4799` | `-0.447%` | `-0.0221` | `-0.22%` | `-7.25%` |
| 387 | **FIONA** | `2026-11-26T21:21:20.581518+00:00` | `1402.8` | `EXODUS_SELL_FLOW` | `PRICE_ONLY` | `0.5072` | `0.5059` | `-0.261%` | `+0.0039` | `+3.78%` | `-2.49%` |

---

## 4. Episode-Grouped Outcome & Repeated-Observation Analysis (`PRICE_ONLY`)

> **Important:** `candidate_outcomes` forward metrics reflect 60-second DexScreener spot polling (`PRICE_ONLY`), NOT executable Jupiter quotes.

| Outcome / Deduplication Metric | Value |
| :--- | ---: |
| Total `candidate_outcomes` Rows in Window | `861` (`838` finalized) |
| Unique Mints vs Repeated Outcome Rows | `101` unique / `760` repeats |
| `DECISION_1` Ticks Suppressed by 15m Dedup | `17471` |
| Approved Episodes With `ENTERED` Outcome Suppressed by Prior `WATCH`/`REJECT` | `17 / 17` |
| Episodes With Recorded `MFE >= +50%` (`PRICE_ONLY`) | `28` episodes across `19` unique mints |
| Median Episode Best MFE % / Worst MAE % | `+4.22%` / `-6.04%` |

### Top 10 Most Repeated Mints (Grouped by Episode)

| Symbol | Mint | Watchlist Episodes | `DECISION_1` Rows | `candidate_outcomes` Rows |
| :--- | :--- | ---: | ---: | ---: |
| **FIONA** | `B34nXKzCgsJb9ChLuSHPRpXLE3WFSWyeT8Cqkzzmpump` | 34 | 2291 | 46 |
| **JIMMY** | `38M2z4UCfZEkUHFzD48vhKQhFhNyRGto8pZbLjNipump` | 35 | 2054 | 36 |
| **UHI** | `Bnw5JRPek5JvJhzUiB9yrs2cmzt4bLeFqSRzDzSppump` | 33 | 2033 | 41 |
| **Zero** | `DTSkmo3sCCKLCh91dJLiWCpwcKtK3yDBZMqZGSV1pump` | 48 | 1459 | 49 |
| **NORFEATH** | `3gmFG4sFDJErDV8pacFrL2uUPXeGJTYLjfRy4hFypump` | 37 | 1150 | 40 |
| **BASKET** | `3Tg6YnNrfPdFfsY14MaNsDMWLseAyrUDS3myJtbaJN6f` | 53 | 955 | 55 |
| **Pompom** | `5EmvWDizkpjAXbcfCcVq69w1id8GaLzi4LLwtuSgpump` | 16 | 639 | 14 |
| **Investor** | `J9eBkCb4UtquAMQqB6duwBmR99fFSMoTnDcaMB9Wpump` | 22 | 415 | 20 |
| **PUMPCHAN** | `Fo2HAwr7ZAUM2bSZ7MWSi16e3aWGpbw5MsUDYCttpump` | 8 | 388 | 9 |
| **PAIR** | `32rBxcFjCFnyqZ41CEPM5AL67X1dcw8w83vZt6Kxpump` | 20 | 362 | 20 |
