# Strategy Lab — Neutral V2 Contracts (`models/contracts_v2/`)

P3.6 introduces closed, neutral contract types shared by strategy models, catalog/v2,
and the V2 JSON Schema generator. Validators (P4) will consume these types later.

## Import layering

| Layer | May import |
|-------|------------|
| `models/` | `models/` only |
| `catalogs/v2/` | `models/` |
| `schema/` | `models/` |
| `validation/` (future) | `models/` + `catalogs/` |

**Forbidden:** `models/` → `catalogs/`

Static enforcement: `tests/strategy_lab/test_import_layering_v2.py`.

## Type separation

- **ParameterValueType** — plugin/feature parameters (`BoolParam`, `IntParam`, …)
- **FeatureOutputValueType** — feature outputs (`decimal`, `cluster_snapshot`, …)
- **TemporalShape** — `series` (bar-evolving scalar) vs `instant` (point-in-time)
- **CollectionShape** — `single`, `sequence`, `set`

`param_value_to_parameter_type()` is the single mapping from `ParamValue` instances.

## CollectionShape: `lld_liquidity_clusters.snapshots`

Legacy `active_clusters_as_of()` (`cluster_adapter.py`) returns `list[ClusterSnapshot]`
built by iterating `filter_clusters()` output. Consumers iterate in order; order is not
sorted and is not a mathematical set. Duplicates are not structurally expected but order
is engine-defined.

**Decision:** `CollectionShape.SEQUENCE` (not `SET`).

## Data granularity union (`DataGranularityV2`)

Closed discriminated union:

| Variant | Use when |
|---------|----------|
| `TimeframeGranularityV2` | Candle or explicitly aggregated bar timeframe |
| `SelectedSignalTimeframeGranularityV2` | Granularity bound to strategy's chosen signal TF |
| `EventStreamGranularityV2` | Native event streams (liquidations) |
| `SnapshotGranularityV2` | Point-in-time snapshots with fixed aligned timeframe |

No free-string granularity.

### EDC (`edc_m0_strict_sync`)

| Requirement | Granularity | Rationale |
|-------------|-------------|-----------|
| `edc_candles_signal_tf` | Timeframe 5m | Signal-TF OHLCV |
| `edc_candles_execution_1m` | Timeframe 1m | Execution candles |
| `edc_public_trades_1m` | Timeframe 1m | Aggregated 1m trade features at signal bar close |
| `edc_orderbook_ob200_v3_1m` | Timeframe 1m | Aggregated 1m orderbook features |
| `edc_liquidity_locations` | Snapshot 5m | Signal-TF-aligned liquidity snapshot |
| `edc_open_interest_1m` | Timeframe 1m | Aggregated 1m OI at window edge |
| `edc_liquidations` | Event stream | Native liquidation events |

### Cluster sweep (`cluster_sweep`)

| Requirement | Granularity | Rationale |
|-------------|-------------|-----------|
| `cluster_candles_signal_tf` | Selected signal TF | Caller-chosen 5m/15m via `aggregate_timeframe` |
| `cluster_liquidity_locations` | Selected signal TF | LLD clusters follow signal-TF OHLCV |
| `cluster_candles_execution_1m` | Timeframe 1m | Execution feed |
| `cluster_public_trades_1m` | Timeframe 1m | `fetch_trades_1m` → `toStartOfMinute` |
| `cluster_orderbook_ob200_v3_1m` | Timeframe 1m | `fetch_ob_1m` → 1m aggregates |
| `cluster_open_interest_1m` | Timeframe 1m | `fetch_oi_1m` → 1m aggregates |
| `cluster_liquidations` | Event stream | `fetch_liquidations` per-event rows |

## Separate padding types

- **SourceLoadingPaddingV2** — `candle_history`, `auxiliary_source_history`
- **OutcomeEvaluationPaddingV2** — `post_window_duration`

Each field is `DurationValue` or explicit `PaddingNotApplicable`. No shared generic
padding type; source-loading and outcome-evaluation are not interchangeable.

EDC: 120h candle history (5 calendar days), 2h auxiliary, 12h outcome post-window.
Cluster sweep: all padding fields `not_applicable`.

## Signal-engine warmup

**Plugin minimum** (`SignalEngineWarmupRequirementV2`):

- `minimum_bars`
- `timeframe_basis`: `selected_signal_timeframe` | `fixed_timeframe`
- `fixed_timeframe` only when basis is `fixed_timeframe`

EDC and cluster sweep: `minimum_bars=79`, `timeframe_basis=selected_signal_timeframe`.

**Frozen strategy** (`SignalEngineWarmupV2`):

- `minimum_bars`
- `bar_timeframe` (concrete chosen signal TF)

P4 will verify strategy `bar_timeframe` matches chosen signal TF and meets plugin minimum.

## Entry causality

`EntrySpecV2` closed enums — no `minimum_causal_delay_bars`:

- `signal_decision_timing`
- `entry_reference_rule`
- `entry_timing_anchor`
- `entry_price_reference` = `next_signal_tf_open`
- `execution_timeframe` (1m for outcomes; does not shift entry anchor)

EDC: next signal-TF open after signal bar close.
Cluster: next signal-TF open after confirmation bar close.

## Operator signatures

`OperatorSignatureV2` explicit overloads in catalog/v2:

- Series vs series, series vs `DecimalParam`, series vs `IntParam`
- Cross operators: series vs series only, with `ObservationContractV2`
- No `and` / `or` / `not` in catalog/v2

## PluginRefV2

- `plugin_id: StableIdentifier`
- `contract_version: ContractVersion` (e.g. `catalog/v2`)
- `config: tuple[ConfigEntry, ...]`

Reserved config keys (typed `StableIdentifier`): `mode_id`, `confirmation_policy`,
`plugin_id`, `contract_version`.

## StrategySpecV2 (pre-release)

Uses `DataRequirementSpecV2`, `WarmupSpecV2`, `EntrySpecV2`, single `features` source,
and `SignalDefinition` variants. Schema version remains `strategy_spec/v2`.

No validator conventions are defined in P3.6.
