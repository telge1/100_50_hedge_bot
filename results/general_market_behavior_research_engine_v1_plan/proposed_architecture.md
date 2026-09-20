# Proposed Architecture — GENERAL_MARKET_BEHAVIOR_RESEARCH_ENGINE_V1

```
Raw Sources (FS Full-OB / OB200/1000 + CH trades/OI/liq/candles)
  → Coverage & Replay Gate (FULL|PARTIAL|GAP)
  → Canonical temporal states (Ebene A: 1s causal rows)
  → Microstructure features (mb_features_v1)
  → Regime layer
  → Behavior episodes (Ebene B)
  → Future outcomes (strict post-t)
  → Pattern discovery (descriptive → rules/trees → baselines)
  → Chronological validation
  → (Later) Live state classifier — out of V1 implement scope
```

## Two data planes
### Ebene A — Continuous State
One row / symbol / second with shape, flow summary, footprint, oi/liq, regime, quality flags.

### Ebene B — Behavior Episodes
Variable windows triggered by causal detectors (imbalance surge, vacuum, wall test, cancel wave, liq cluster, …). Store start_t, end_t, trigger features, **not** outcomes in the same write path.

## Module map (planned package)
`research/general_market_behavior_v1/`
- `sources/` adapters
- `replay/` wraps OA continuous replay + OB200 path
- `coverage/` gate
- `join/` temporal join
- `features/` builders
- `regime/`
- `episodes/`
- `outcomes/`
- `discovery/`
- `validation/`
- `reporting/`

CLI: `scripts/run_general_market_behavior_research_v1.py` with modes `--inventory --verify-coverage --build-states --build-episodes --build-outcomes --discover-patterns --validate-patterns --analyze-window --full-pipeline`.

## Dual book strategy (pragmatic)
- **Track F (Full-OB):** whenever COMPLETE segments exist — gold microstructure.
- **Track D200:** OB200 multi-day history for pattern prevalence / regimes until Full-OB history grows.
Always label which track produced a row; never mix depth claims.
