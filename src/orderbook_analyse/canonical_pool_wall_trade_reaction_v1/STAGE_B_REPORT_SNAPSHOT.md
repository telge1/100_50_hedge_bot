# Stage B — CLEAR_POOL_SELECTION_RULE_V1

Generated: `2026-08-31T18:48:22Z`
Rule: `CLEAR_POOL_SELECTION_RULE_V1`

> Große HTF-Zonen, die beim Anlauf wirklich voll mit Book-Liquidität sind — der Rest ist nachrangig. Entscheidung = Verhalten dieser Zonen-Liquidität.

## Setup

- Candidates: **Stage A A7 pass** (`stage_a_candidates.csv`, raw zone depth)
- Not input: old 1s `wall_in_pool` Strong shortlist
- Tracker: reused `audit_cluster_case` (Raw OB200 + `public_trades_canonical`)
- Wall anchor = strongest level inside A7-confirmed zone at touch
- No entry / PnL

## Counts

- Candidates: **126**
- Zone wall found at touch: **126**

### zone_label

- `ZONE_HELD`: **73**
- `ZONE_PULLED`: **38**
- `ZONE_UNKNOWN`: **15**

### evidence_class (six-case)

- `POOL_REJECTION_MIXED_WALL_REACTION`: **72** → `ZONE_HELD`
- `WALL_CANCEL_OR_MOVE_DOMINANT`: **38** → `ZONE_PULLED`
- `INSUFFICIENT_DATA`: **15** → `ZONE_UNKNOWN`
- `POOL_REJECTION_WITH_ABSORPTION_EVIDENCE`: **1** → `ZONE_HELD`

## Files

- `stage_b_summary.csv`
- `stage_b_timelines.csv`
- `summary.json`
