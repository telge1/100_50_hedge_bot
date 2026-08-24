# EDC M0 market-data IO (P2D1)

Thin wrapper from StrategySpec V2 warmup contracts to `EdcM0MarketDataV2`.

## Purpose

```text
StrategySpecV2 + CatalogBundleV2
  + ClickHouseQueryClient (caller-owned)
→ load_edc_m0_market_data_v2
→ EdcM0MarketDataV2
→ execute_edc_m0_strict_sync_v2  (P2B; not part of this module)
```

No detection, no outcome simulation, no new SQL, no global client.

## Public API

```python
load_edc_m0_market_data_v2(
    spec: StrategySpecV2,
    catalogs: CatalogBundleV2,
    *,
    client: ClickHouseQueryClient,
    symbol: str,
    start: datetime,
    end: datetime,
) -> EdcM0MarketDataV2
```

Errors: `StrategyMarketDataError`.

## Client protocol

`ClickHouseQueryClient` / `ClickHouseQueryResult` are small duck-typed Protocols
matching clickhouse-connect (`query(...).result_rows`). The caller creates and
owns the client (same path as P2B local parity: `get_clickhouse_client`).

## Legacy loader

Uses existing `load_strategy_market_data` (shared_strategy) via lazy `importlib`
with **fixed** module paths. Importing `strategy_lab.adapters` does not load
Legacy.

### Exactly five queries

1. candles_1m
2. trades_1m
3. orderbook_1m
4. open_interest_1m
5. liquidations

### Internal key mapping (dict never leaves the wrapper)

| Legacy key | `EdcM0MarketDataV2` |
|---|---|
| `candles_1m` | `candles_1m` |
| `trades` | `trades_1m` |
| `ob` | `orderbook_1m` |
| `oi` | `open_interest_1m` |
| `liq` | `liquidations` |
| `pads` | validated, discarded |

Missing keys are rejected. Empty frames are **not** invented for missing keys.

## Fixed loader pads

Legacy semantics (not Spec-invented, not wrapper defaults):

| Pad | Value |
|---|---|
| Candle warmup | 5 days (= 120 hours) |
| Source pad | ±2 hours |
| Outcome pad | +12 hours |

Spec `warmup.source_loading` / `outcome_evaluation` must match these **exactly**
before the first query. Loader-returned `pads` must match the same values.
Mismatch → `StrategyMarketDataError` (Spec reject ⇒ 0 queries).

## Local XRP end-to-end

```bash
STRATEGY_LAB_EDC_IO_PARITY=1 pytest tests/strategy_lab/test_adapter_edc_io_parity.py -rs
```

Without the env flag the test **skips** (visible with `-rs`), never silent-pass.

Window: `2026-07-24` → `2026-08-23` UTC exclusive, XRPUSDT. Expects 5 queries,
15 candidates / 15 trades, Spec cost 0.11 %.

## Deferred

- 51-coin runner / checkpoint / resume
- standardized exports
- enrichment / root-cause analysis
- Cluster Sweep
