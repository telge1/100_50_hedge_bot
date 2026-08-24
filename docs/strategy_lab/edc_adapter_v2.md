# EDC M0 Strict Sync adapter (P2B)

In-memory adapter from validated/compiled StrategySpec V2 to `StrategyRunResultV2`.

## Boundary

```text
StrategySpecV2 + CompiledStrategyV2 + CatalogBundleV2
  + EdcM0MarketDataV2 (public preloaded frames)
→ execute_edc_m0_strict_sync_v2
→ StrategyRunResultV2
```

No ClickHouse client, no CLI, no registry in this function.

`EdcM0MarketDataV2` is the **public** named market-data contract for one adapter
run (typed DataFrames; no `dict`/`Any`). It replaces the untyped dict returned by
`load_strategy_market_data` at the adapter boundary only.

## Legacy reuse (no trade-logic copy)

| Step | Function |
|---|---|
| Aggregate signal TF | `aggregate_timeframe` |
| EMA / ATR | `attach_emas`, `attach_atr` (periods from Spec) |
| M0 detect | `detect_strict_sync_baseline` |
| Entry + supportive labels | `evaluate_candidates_canonical` (uses `next_signal_tf_open`) |
| Outcome | `simulate_tpsl_trade` + `apply_costs` |

`simulate_canonical_trade` is **not** used (hardcodes 0.15% costs).
`prepare_tf_frames` is **not** used; aggregation + EMA + ATR call the same
underlying helpers with Spec periods 9/20/59/14.

Legacy modules load **lazily** on the first `execute_edc_m0_strict_sync_v2` call
via fixed `importlib` paths (not Strategy-File controllable). Importing the
adapters package does not load Legacy. Static `import` of Legacy under
`strategy_lab/` would break Phase-1 AST isolation.

Band/coverage gates on `EmaDualCrossConfig` that are not Spec fields are passed
explicitly as frozen M0 research constants (same values as historical detection).

## Costs / notional

- Roundtrip: `float(spec.costs.roundtrip_cost.value)` → `apply_costs`
- Notional: Spec `fixed_notional` must equal engine `NOTIONAL_USDT` (1000);
  `apply_costs` has no notional parameter (no Legacy change in P2B)

## Deferred

ClickHouse IO wrapper, plugin registry, multi-coin, Cluster, MFE/MAE, explainability.

## Local XRP parity

Gitignored refs under `results/edc_sync_tolerance/…` + ClickHouse.

```bash
STRATEGY_LAB_EDC_PARITY=1 pytest tests/strategy_lab/test_adapter_edc_parity.py -k local
```

Without the env flag, local parity tests **skip** (not pass). With the flag and
missing data/CH, they **fail**.

Historical net +27.50 USDT used **0.15%** costs; Spec baseline is **0.11%**.
Control net is derived as `gross − costs` from the adapter run when gross matches
the reference cell.
