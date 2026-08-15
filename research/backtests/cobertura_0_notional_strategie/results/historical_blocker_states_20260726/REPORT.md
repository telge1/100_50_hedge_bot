# Historical Blocker State Extraction

**Decision: `BLOCKER_STATE_EXTRACTION_PASS_WITH_WARNINGS`**

APT reference: **APT_REFERENCE_WARNING**

- candidate cycle-4 end snapshot matches prior audit inventory, but cycle straddles signal_available_ts → not adopted as pre-signal state

## Answers

1. Fully resolvable (exact position + break + candle): **1 / 27**
2. Unique break event OK: **25 / 27**
3. Causal 5m candle OK: **25 / 27**
4. Exact position state before signal: **1 / 27**
5. Ready for Cobertura backtest: **1 / 27**
6. Extra short qty (resolved rows): n=1 min=534.6080000000001 max=534.6080000000001 sum=534.6080000000001
7. Extra short notional: min=327.9285472 max=327.9285472 sum=327.9285472
8. Short-average change: only computed when exact pre-signal state exists (see `blocker_neutralization_calculation.csv`).
9. New long/short avg spread: `post_neutralization_avg_spread_pct_from_long` in outputs.
10. Short already larger than long: **0**
11. Incomplete fee/economics reconstruction: **27** (fees never in cycle timelines; flagged `FEES_NOT_IN_SOURCE`).
12. Lookahead/invariant fails: **0**; ambiguous event matches: **0**.

## Key finding

Most blockers have a recovery cycle whose fill span **straddles** `signal_available_ts`. Cycle-end inventory therefore cannot be proven as the pre-signal book without a fill-level ledger. Extraction refuses those states (`POSITION_SEMANTICS_UNRESOLVED` / `CYCLE_ACTIVE_ACROSS_SIGNAL`) instead of estimating. Candidate cycle-end snapshots are retained for audit only.

## Decision

`BLOCKER_STATE_EXTRACTION_PASS_WITH_WARNINGS`
