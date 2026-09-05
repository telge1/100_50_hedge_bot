# Historical Break Pull vs Consumption Deep Dive

**Primary Decision:** `PULL_DOMINATES_ACCEPTED_BREAKS`

Mode: Historical OB + Public Trades on the same 15 deep-dive structure breaks.
No AUC / ML / live gate. Matching tolerance ±750ms (cross-feed).

## 1. Mechanism determination

- Events with a non-`NO_CLEAR_MECHANISM` class: **12/15**
- Confidence: {'MEDIUM': 11, 'LOW': 4}

## 2. Mechanism counts

- {'PULL_DOMINANT': 11, 'NO_CLEAR_MECHANISM': 3, 'REFILL_ABSORPTION': 1}

## 3. Accepted breaks (typical)

- n=10 · {'PULL_DOMINANT': 6, 'NO_CLEAR_MECHANISM': 3, 'REFILL_ABSORPTION': 1}
- **0× CONSUMPTION_DOMINANT** among accepted events under ±750ms trade matching.
- Typical: wall-zone qty collapses in the last ~60s with matched aggressor qty ≪ removal (ratios often ~0.01–0.15).
  - `DOGEUSDT_PROTECTED_LOW_BREAK_bearish_20260228_0p09133_4h`: PULL_DOMINANT ratio≈0.015 pull_s≈58s
  - `DOGEUSDT_PROTECTED_LOW_BREAK_bearish_20260228_0p09106_1h`: PULL_DOMINANT ratio≈0.025 pull_s≈58s
  - `DOGEUSDT_PROTECTED_LOW_BREAK_bearish_20260106_0p14909_1h`: PULL_DOMINANT (prior OB-only said CONSUMED; trades say pull)
  - `DOGEUSDT_PROTECTED_LOW_BREAK_bearish_20260220_0p09777_1h`: PULL_DOMINANT ratio≈0.085
  - `DOGEUSDT_PROTECTED_HIGH_BREAK_bullish_20260220_0p09882_1h`: REFILL_ABSORPTION (counterexample vs pull narrative)

## 4. Reclaim / Hold (typical)

- n=5 · {'PULL_DOMINANT': 5}
- Reclaims also show **pull-dominant** zone depletion — pull alone does **not** separate accept vs reclaim here.
- Difference vs accepted is not mechanism class in this sample; refill/absorption only appeared once (and on an accepted label).
  - `APTUSDT ... 20260512`: PULL_DOMINANT with large refill after dips
  - `APTUSDT ... 20260523`: PULL_DOMINANT
  - `DOGEUSDT ... 20260228` high: PULL_DOMINANT

## 5–6. Timing before first_break

- Events with pull_start: 14; median seconds before break: **~54s**
- Events with consumption_start: 6; median seconds before break: **~3s** (late, near break, small matched qty)

## 7. Refill / Absorption

- REFILL_ABSORPTION count: 1 (accepted-labeled DOGE 2026-02-20 high break) — rare in this set; not yet a clean reclaim separator.

## 8. Clearest events

- DOGEUSDT Feb-28 low (1h+4h) → PULL_DOMINANT
- DOGEUSDT Jan-06 low → PULL_DOMINANT (trades overturn OB-only “consumed”)
- DOGEUSDT Feb-28 high → PULL_DOMINANT
- DOGEUSDT Feb-20 low → PULL_DOMINANT

## 9. Counterexamples

- Accepted but REFILL_ABSORPTION: DOGEUSDT 2026-02-20 high break
- Reclaim but PULL_DOMINANT (all 5 reclaims): pull ≠ accept signal by itself
- Prior OB-only WALL_CONSUMED_OR_REMOVED_BREAK (DOGE Jan-06) → **PULL_DOMINANT** once trades matched

## 10. Robust enough for a later statistical test (not run)

- Pre-break pull_start_seconds (~30–60s) is common — need distance-conditioned test vs controls
- consumption_ratio near break (usually ≪0.3 here) vs outcome
- Not yet: pull class alone as accept/reclaim discriminator (fails on this sample)

Caveat: OB deltas and public trades are separate feeds; ±750ms match is sync tolerance, not µs causality.

Quality: {'DATA_VALID': 14, 'DATA_WARNING': 1} (DOGE 2026-01-15 RESET_SEEN)
Artifacts: `results/historical_break_pull_consumption_deep_dive_20260808/`
Tests: 10 passed (`test_historical_break_pull_consumption.py`)

