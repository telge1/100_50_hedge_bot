# Strategy Lab — Catalog V2 (`catalogs/v2/`)

Parallel to frozen `catalogs/v1/`. V1 catalogs, schema, and legacy code are unchanged.

## Contract version

`CATALOG_CONTRACT_VERSION = "catalog/v2"`

## Layout

```
catalogs/v2/
  __init__.py
  models.py      # FeatureDescriptorV2, OperatorDescriptorV2, PluginDescriptorV2
  features.py    # ema, atr_wilder, lld_liquidity_clusters
  operators.py   # gt, gte, lt, lte, eq, ne, crosses_above, crosses_below
  plugins.py     # edc_m0_strict_sync, cluster_sweep
  registry.py    # closed registries + integrity checks
```

## Feature catalog

| Feature | Outputs | CollectionShape |
|---------|---------|-------------------|
| `ema` | `value` (decimal series) | `single` |
| `atr_wilder` | `value` (decimal series) | `single` |
| `lld_liquidity_clusters` | `snapshots` (cluster snapshots) | **`sequence`** |

Each output has explicit temporal shape, availability, missing-value policy, and
warmup formula. Legacy provenance references are metadata-only (no callables).

## Operator catalog

Eight comparison/cross operators. Logical `and`/`or`/`not` from v1 are excluded.
Multiple `OperatorSignatureV2` overloads per operator; cross operators require
`ObservationContractV2`.

## Plugin catalog

### `edc_m0_strict_sync`

- `catalog/v2`, adapter `adapter_pending`
- Confirmation: `core_research_supportive`
- Warmup: 79 bars, `selected_signal_timeframe`
- Entry: `signal_tf_next_open_after_signal_bar`
- Source/outcome padding with explicit durations

### `cluster_sweep`

- `catalog/v2`, adapter `adapter_pending`
- Signal TF: allowed set `{5m, 15m}`, reference 15m
- Warmup: 79 bars, `selected_signal_timeframe`
- Entry: `next_bar_open_after_confirmation_bar`
- Padding: explicit `not_applicable` on all padding dimensions

## catalog/v1 vs catalog/v2

| Aspect | v1 | v2 |
|--------|----|----|
| Contract version | `catalog/v1` | `catalog/v2` |
| Feature outputs | implicit `output_type` | explicit `FeatureOutputDescriptorV2` |
| Operators | includes and/or/not | comparison + cross only |
| Plugin ref | string id/version | `PluginRefV2` + `ContractVersion` |
| Data requirements | `granularity_minutes: int` | `DataGranularityV2` union |
| Padding | shared v1 types with int defaults | separate V2 types, no defaults |
| Warmup | `PluginSignalWarmup` bar index | `SignalEngineWarmupRequirementV2` + timeframe basis |

## Registry

```python
from orderbook_analyse.strategy_lab.catalogs.v2 import (
    get_feature_v2,
    get_operator_v2,
    get_plugin_v2,
    assert_production_catalog_integrity_v2,
)
```

Integrity checks: unique output IDs, no logical operators, plugin contract version,
warmup timeframe consistency.
