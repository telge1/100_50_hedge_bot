# STRATEGY_SPEC_CANDIDATE_4D — User price rules amendment

| Field | Value |
|-------|--------|
| parent_candidate | `pool_landscape_v1_candidate` |
| amendment | `4D_strict_price_rules` |
| approval | **NOT** `FINAL_APPROVED` |
| supersedes | Dense-cluster TP wording in older candidate text for **backtest math** only where this doc is explicit |

## Newly confirmed (Signal_definierung.md § SHORT – ENTRY / SL / TP V1, user task 4D)

| rule_id | Side | Status | Formula |
|---------|------|--------|---------|
| SHORT-ENTRY-LEVEL-01 | SHORT | USER_CONFIRMED | `entry_level = upper_pool_lower_edge` |
| SHORT-SL-01 | SHORT | USER_CONFIRMED | `sl = upper_pool_upper_edge * 1.002` |
| SHORT-TP-01 | SHORT | USER_CONFIRMED | Nearest **depth-2** lower pool with `top` at least `min_tp_distance_pct` below **actual fill**; `tp = lower_pool_upper_edge` |
| LONG-ENTRY-LEVEL-01 | LONG | USER_CONFIRMED | `entry_level = lower_pool_upper_edge` |
| LONG-SL-01 | LONG | USER_CONFIRMED | `sl = lower_pool_lower_edge * 0.998` |
| LONG-TP-01 | LONG | USER_CONFIRMED | Nearest **depth-2** upper pool with `bottom` at least `min_tp_distance_pct` above **actual fill**; `tp = upper_pool_lower_edge` |
| POS-01 | BOTH | USER_CONFIRMED | `max_open_positions = 1` |

## Open / experimental

| rule_id | Status | Note |
|---------|--------|------|
| EXEC-ENTRY-01 | OPEN | Limit touch diagnostic only in 4D; `entry_execution_user_approved=false` |
| TP-QUALITY-01 | OPEN | “Geeigneter” pool beyond distance → `TP_POOL_ELIGIBILITY_UNRESOLVED` if quality gate enabled |
| MS-GATE-01 | EXPERIMENTAL | 3B WATCH — **disabled** in 4D (`market_structure_gate_enabled=false`) |
| FILTER-AGE-01 | RESEARCH | `pool_age_hard_gate=false` |

## Implementation reference

- Backtester: `tools/phase4d_pool_landscape_backtester_v2/`
- Config hash recorded in `results/phase4d_pool_landscape_backtester_v2/backtest_summary.md`

## Tests

- `tools/phase4d_pool_landscape_backtester_v2/test_phase4d_strict.py`
