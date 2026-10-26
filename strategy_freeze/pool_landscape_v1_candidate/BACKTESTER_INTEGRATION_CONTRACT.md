# BACKTESTER_INTEGRATION_CONTRACT (draft — pre-freeze)

**Status:** `CANDIDATE_ONLY` — no integration until user approves final strategy SSOT.

## Preconditions

1. Released artifact: `strategy_freeze/pool_landscape_v1_final/` (future) with `FINAL_APPROVED` and content hashes.
2. Every trading decision field maps to a `rule_id` in `STRATEGY_SPEC_CANDIDATE.md` with status `USER_CONFIRMED` or explicit `USER_PARAMETER`.
3. Any rule still `OPEN` / `EXPERIMENTAL` → runtime must emit `STRATEGY_RULE_UNRESOLVED` and **must not** invent a default that changes entries/exits.

## Required parity surfaces

| Surface | Must match freeze |
|---------|-------------------|
| Landscape builder | PD1 exact unique-origin density, min-depth 2, ACTIVE SSOT window |
| Short candidate generation | User-confirmed trigger + filters (once specified) |
| TP selection | Dense cluster algorithm **as approved** — not Phase 4A nearest-top |
| SL anchors | 0.2% beyond pool edge |
| Market structure gate | Only if user validates detector or manual rule spec |
| Opportunity identity | User-approved dedup / re-entry rules |
| Position manager | max_open_positions, suppression reasons logged |
| Costs | roundtrip_fees_pct; slippage/funding if added by user |

## Prohibited before freeze

- Changing `tools/phase4a_*` or production scanner to “match” backtest silently.
- Treating Phase 2A/3B/4A research outputs as strategy authority.
- Hard-enabling `min_pool_age_bars` without user sign-off.

## Version binding (future final freeze)

- `strategy_id`: e.g. `pool_landscape_pd1_d2_v1`
- `version`: semver or date stamp
- `sha256` manifest over spec + parameters JSON
- Scanner and backtester read **same** manifest path via env or config — no hidden defaults.

## Test obligations post-integration

- Causal bars only; no lookahead on pool birth.
- Manual cases C001–C004, C008, C010–C011, C013 reproduce **labels** (not necessarily PnL).
- Count units: raw signal vs rejected entry vs open position vs closed trade (Phase 4B lesson).
