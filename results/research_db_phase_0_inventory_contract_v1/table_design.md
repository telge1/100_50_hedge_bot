# Table Design Notes — BTC/DOGE Research DB

**Phase:** 0 (Design only)
**Database:** `btc_doge_research`
**DDL:** `proposed_schema.sql`

---

## Design Principles

1. **Separate database** — no mutation of `orderbook_analysis` or `signal_generator`
2. **Idempotent inserts** — ReplacingMergeTree + version column; views for query-time dedup
3. **No FINAL dependency** — `research_*_v` views with `argMax` for hot paths
4. **Provenance on every row** — source, contract_version, processor_version
5. **Neutral vs derived split** — market facts vs `research_features`

---

## Per-Table Summary

### research_public_trades

| Aspect | Choice |
|--------|--------|
| Engine | ReplacingMergeTree(ingested_at) |
| PARTITION BY | (symbol, toYYYYMM(event_time)) |
| ORDER BY | (symbol, event_time, trade_id) |
| Decimal | price Decimal(18,8); size Decimal(24,12) |
| Nullable | none on core fields |
| Rows/day | BTC ~2-4M; DOGE ~200-400K |
| Storage/day | ESTIMATED 200-500 MB compressed BTC |
| Query patterns | time range by symbol; point lookup by trade_id |
| Pruning | symbol + month partition |
| Risks | ReplacingMerge lag; mitigated by view |

### research_liquidation_events

| Aspect | Choice |
|--------|--------|
| Engine | ReplacingMergeTree(ingested_at) |
| PARTITION BY | (symbol, toYYYYMM(event_time)) |
| ORDER BY | (symbol, event_time, event_key) |
| Rows/day | sparse (~10-500 BTC) |
| Storage | negligible |
| Risks | low volume; event_key collision unlikely |

### research_orderbook_1s

| Aspect | Choice |
|--------|--------|
| Engine | ReplacingMergeTree(ingested_at) |
| PARTITION BY | (symbol, toYYYYMMDD(bucket_time)) |
| ORDER BY | (symbol, bucket_time) |
| Rows/day | 86400/symbol |
| Storage/day | ESTIMATED 50-100 MB/symbol compressed |
| Codec | ZSTD(3) on Float64 metrics optional Phase 1 |
| Risks | rebuild CPU-heavy from raw; batch by hour |

### research_market_1s

| Aspect | Choice |
|--------|--------|
| Engine | ReplacingMergeTree(ingested_at) |
| Wide table | yes — bounded neutral fields only |
| NOT stored | raw events, level detail, strategy flags |
| finalization_status | open → finalized after watermark |
| source_coverage_mask | bitfield per source presence |
| Rows/day | 86400/symbol |

**Fields read from normalized tables at query time (alternative design):**
If wide table too heavy, Phase 1 may use `JOIN` across `research_public_trades` aggregate MV + `research_orderbook_1s` + liq/OI rollups instead. Current design favors single-table backtest speed.

### research_market_1m

| Aspect | Choice |
|--------|--------|
| Engine | ReplacingMergeTree(ingested_at) |
| PARTITION BY | (symbol, toYYYYMM(bucket_time)) |
| Aggregation | causal from 1s only; no future candle data |
| genuine/carried_forward_seconds | explicit counts per minute |

### research_orderbook_levels (OPTIONAL)

| Aspect | Choice |
|--------|--------|
| Defer | until pool/wall benchmark proves need |
| Rows/day | ~34M/symbol at 200 depth — high |
| Alternative | Parquet cold + top-N summary in CH |
| OB1000 | 5× storage; on-demand replay preferred initially |

### research_coverage

| Aspect | Choice |
|--------|--------|
| Engine | MergeTree (append audit snapshots) |
| Purpose | gap visibility, quality gates |

### research_pipeline_state

| Aspect | Choice |
|--------|--------|
| Engine | ReplacingMergeTree(last_successful_run) |
| Purpose | checkpoint, watermark, error isolation per symbol |

### research_features

| Aspect | Choice |
|--------|--------|
| Engine | ReplacingMergeTree(computed_at) |
| value_json | flexible; typed columns added per frozen feature |
| HINDSIGHT | usable_for_live_signal=0 always |

---

## Storage Estimates (both symbols)

| Horizon | ESTIMATED |
|---------|-----------|
| 1 day | 1-2 GB all core tables |
| 1 month | 30-60 GB |
| 1 year | NOT_PROVEN (depends on retention policy) |

Orderbook levels **excluded** from estimate.

---

## Float vs Decimal

- **Decimal** for price, size, notional (no NaN; JSON-safe)
- **Float64** for bps, imbalance ratios (document tolerance; clamp NaN on export)

---

## TTL

Not recommended Phase 1 — research history valuable. Revisit after 12 months operational data.

---

## Late-Arrival Strategy

1. Processor reads overlap window `[watermark - overlap, now)`
2. Re-insert affected buckets with newer `ingested_at`
3. `finalization_status` flipped only after `watermark` passes
4. `late_rows_corrected` counter in pipeline_state

---

## History/Live Unified Contract

Same column semantics for archive-imported and live-streamed rows.
`source` column preserves lineage; quality_flags mark seam anomalies.
