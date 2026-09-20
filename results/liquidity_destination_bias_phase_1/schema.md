# Phase-1 episode schema

Contract: `liquidity_destination_episode_contract_v1`

## `episodes.csv`

One row is one eligible, non-overlapping factual episode. Targets are immutable
values selected at `t0_utc`; future path values only determine `outcome` and
touch timestamps.

| Field | Meaning |
|---|---|
| `episode_id` | SHA-256 of contract, symbol, T0, target IDs and horizon |
| `t0_utc` / `knowledge_cutoff_utc` | Causal decision timestamp; identical in V1 |
| `horizon_end_utc` | Exclusive path end |
| `price_t0` | Last event-time public-trade price from a bucket strictly before T0 |
| `upper_target_*` | Frozen nearest active ASK LLD pool |
| `upper_touch_price` | Frozen upper pool near edge (`lower_price`) |
| `upper_target_available_at` | Causal availability timestamp, no later than T0 |
| `lower_target_*` | Frozen nearest active BID LLD pool |
| `lower_touch_price` | Frozen lower pool near edge (`upper_price`) |
| `lower_target_available_at` | Causal availability timestamp, no later than T0 |
| `distance_*_bps` | T0 distance to the frozen near edge |
| `target_source` | Always `LLD_POOL` |
| `target_contract_version` | Canonical LLD provider contract |
| `outcome` | One of the five frozen outcomes |
| `first_touch_utc` | First observed touch bucket; empty for NEITHER |
| `upper_touch_utc` / `lower_touch_utc` | Independently observed first touch buckets |
| `eligibility` | `ELIGIBLE` for episode rows |
| `exclusion_reason` | Empty for eligible rows |
| `source_coverage_*` | Loaded bounded trade-source boundaries |
| `canonical_snapshot_sha256` | Provenance hash from the canonical LLD snapshot |
| `builder_version` / `contract_version` | Reproduction keys |

## `excluded_candidates.csv`

Every rejected 60-second grid candidate is retained with machine-readable
`exclusion_reason`. An overlapping candidate is rejected until the prior
episode reaches first touch or its horizon ends.

## Determinism

`episodes.csv`, `excluded_candidates.csv`, `summary.json`, and `contract.json`
are core artifacts. `run_manifest.json` records individual SHA-256 values and
one combined fingerprint. `generated_at_utc` is excluded from all core files.

## Explicit semantics

- LLD pools are historically derived locations, not resting Full-OB walls.
- OB200 and Full OB are not used to invent target depth.
- Empty trade seconds are valid no-trade intervals; source quality comes from
  event `coverage_status` and bounded start/end coverage.
- No acceptance, bias, prediction, profitability, or order fields exist.
