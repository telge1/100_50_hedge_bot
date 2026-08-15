# APT Cobertura Bundle Handoff

**Decision: `APT_COBERTURA_BUNDLE_HANDOFF_PASS_WITH_WARNINGS`**

## Answers

1. Exactly one APT record loaded: **yes**
2. Bundle start-ready: **True**
3. Break/signal/market: level=`1.7639`, signal=`2026-01-19T00:00:00+00:00`, market=`1.7223`
4. Exact book imported: long=`296.365` @ `1.864531340748192`; short=`197.59699999999998` @ `1.864561269615919`
5. Regular initial entry created: **no**
6. TEM source orders removed: before=`4` after=`0`
7. TEM cycle inherited as Cobertura cycle: **no** (source_cycle=`4`)
8. Neutralization qty: `98.76800000000003` (short)
9. Fill price: `1.7223`
10. New short avg: `1.8171506068270433`
11. Qty-neutral after: long=`296.365` short=`296.365` net=`0.0`
12. Prior realized only separate: `-11.900133102067503` (include_in_spread_target=`False`)
13. Warnings: `['FEE_RECONSTRUCTION_UNRESOLVED']`
14. Tests: see pytest `test_cobertura_bundle_handoff.py` + full Cobertura suite.
15. Suitable for isolated Cobertura replay after handoff: **yes** (qty-neutral seeded engine; no recovery run in this step).

## Scenario

- scenario_id: `full_qty_neutralization_spread_only_v1`
- neutralization_mode: `MATCH_SMALLER_SIDE_TO_LARGER_SIDE`

## Invariants

- pass: `True`
- failures: `[]`

## Decision

`APT_COBERTURA_BUNDLE_HANDOFF_PASS_WITH_WARNINGS`

