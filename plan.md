# Fix Solo Whale Fast-Track Trigger (`db.py`)

## Root Cause
- In `db.py` (`get_whale_fast_track_triggers`), line 1721 checked `elif min_confluence_wallets <= 1 and solo_wallets:`, which silently disabled `SOLO` whale triggers whenever `WHALE_MIN_UNIQUE_WALLETS=2` (contradicting the docstring and blocking solo buys `>= WHALE_MIN_SOLO_BUY_SOL` from reaching `whale_lane.py`).

## Fix
1. **`db.py`:** Change `elif min_confluence_wallets <= 1 and solo_wallets:` to `elif solo_wallets:` so a single whale buy `>= min_solo_sol` within `max_age_seconds` qualifies as a `SOLO` trigger even when `min_confluence_wallets == 2`.
2. **`tests/test_whale_scalp.py`:** Update the solo trigger test to assert that with `min_confluence_wallets=2` and `min_solo_sol=0.9`, a single `1.5 SOL` buy returns a `SOLO` trigger while a `0.5 SOL` buy does not.
3. Run `./venv/bin/pytest -v`.
