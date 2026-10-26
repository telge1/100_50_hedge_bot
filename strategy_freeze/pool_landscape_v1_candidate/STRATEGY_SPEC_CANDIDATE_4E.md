# STRATEGY_SPEC_CANDIDATE_4E — Pending limit order V1

| Field | Value |
|-------|--------|
| parent | `STRATEGY_SPEC_CANDIDATE_4D.md` |
| approval | **NOT** `FINAL_APPROVED` |
| source | `Signal_definierung.md` § STRATEGY RULE – PENDING LIMIT ORDER V1 |

## USER_CONFIRMED rules

| rule_id | Side | Rule |
|---------|------|------|
| EXEC-USER-PENDING-LIMIT-V1 | SHORT | Sell-limit at **upper pool lower edge** |
| EXEC-USER-PENDING-LIMIT-V1 | LONG | Buy-limit at **lower pool upper edge** |
| EXEC-PENDING-TTL-01 | BOTH | **No** fixed time expiry while entry pool remains valid depth-2 ACTIVE zone |
| EXEC-POOL-DEACTIVATED-01 | BOTH | Cancel pending if original entry pool loses valid depth-2 zone before fill (causal 3A lifecycle) |
| EXEC-POOL-ORIGIN-01 | BOTH | Single origin loss does **not** auto-cancel if same entry zone continues (origin overlap + band continuity) |
| EXEC-POOL-IDENTITY-01 | BOTH | If identity unclear → `POOL_IDENTITY_AMBIGUOUS` (no invented ACTIVE/DEAD) |
| EXEC-AMBIG-01 | BOTH | Same-bar limit touch + `POOL_DISAPPEARED` without continuation → `AMBIGUOUS_POOL_FILL_SEQUENCE` |
| EXEC-POSITION-01 | BOTH | Filled positions unaffected by pending cancel logic |

## Implementation

- `tools/phase4d_pool_landscape_backtester_v2/pending_limit_simulator.py`
- `pool_zone_tracker.py` (read-only Phase 3A `*_pool_lifecycle.csv`)
- Config: `entry_execution_variant=USER_PENDING_LIMIT_V1`

## Open (unchanged from 4D)

- TP pool “geeignet” quality beyond depth-2 + distance
- Market structure gates (disabled)
- Slippage / funding
