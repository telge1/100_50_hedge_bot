# Inkrementeller Research-Processor — Design (Phase 0)

**Processor name:** `btc_doge_research_processor_v1`
**Position:** Downstream of existing collectors — **Collector unverändert**

---

## 1. Architektur

```text
orderbook_analysis.*  ──┐
signal_generator.*    ──┼──► Research Processor ──► btc_doge_research.*
FS ob200_v3 archives  ──┘         │
                                  ├── research_pipeline_state (checkpoint)
                                  └── research_coverage (audit)
```

---

## 2. Komponenten

| Component | Responsibility |
|-----------|----------------|
| `TradeIngestor` | Read `public_trades_canonical FINAL` from checkpoint |
| `LiqIngestor` | Read `all_liquidations` with event_key dedup |
| `OIIngestor` | Read `open_interest_5s`; forward-fill only with flag |
| `OBReplayIngestor` | Replay FS ob200_v3 for seconds not in historical aggregate |
| `SecondAggregator` | Build `research_market_1s` |
| `MinuteAggregator` | Build `research_market_1m` from finalized 1s |
| `CoverageWriter` | Update `research_coverage` per batch |
| `StateStore` | Atomic checkpoint in `research_pipeline_state` |

---

## 3. Checkpoint & Idempotenz

```text
checkpoint = (processor, source, symbol) -> last_read_ts, watermark_ts, overlap_seconds
```

- **Batch:** configurable, default **60 seconds** of event time per source per run
- **Overlap:** OPEN CONFIG — derive from ingest lag p99 (~6-15s trades) + margin → propose **30-120s** initial
- **Idempotenz:** Re-insert same logical rows with newer `ingested_at`; ReplacingMergeTree collapses
- **Restart:** Read `research_pipeline_state`; resume from `last_read_ts - overlap`
- **Reprocessing:** Manual `--from`/`--to` symbol-scoped; does not affect other symbols

---

## 4. Watermark & Finalization

```text
watermark_ts = min(now - max_source_lag - overlap, last_complete_source_bucket)
```

| Status | Meaning |
|--------|---------|
| `provisional` | Bucket within overlap of live edge |
| `finalized` | All sources past watermark for bucket |

**1m finalization:** only after all 60 underlying 1s buckets finalized (or marked explicit gap).

Late-arrival correction: if trade arrives for finalized minute, reopen minute + affected 1s rows in overlap only.

---

## 5. Lauf-Intervalle (OPEN — aus Latenz ableiten)

| Parameter | Proposed range | Derivation |
|-----------|----------------|------------|
| Processor tick | 10-30s | Trade ingest p50 ~600-800ms; p99 ~6-15s |
| Batch event window | 30-120s | Small bounded reads |
| OB replay batch | 1 hour wall | Matches FS segment boundaries |
| Overlap | 30-120s | > max ingest lag observed |

**Nicht raten:** Endwerte in Phase 1 Pilot messen.

---

## 6. Ressourcen-Limits

| Limit | Value |
|-------|-------|
| RAM | cap replay buffer ~512MB/symbol |
| CH query | max_execution_time=120s |
| max_threads | 2 per processor |
| Parallel symbols | sequential default; optional parallel with memory guard |
| Backpressure | skip tick if prior batch running; log `status=backpressure` |

---

## 7. Symbol-Isolation

- Separate checkpoint rows per (processor, source, symbol)
- Exception in BTC OB replay must not block DOGE trades ingest
- Error column in pipeline_state; auto-retry next tick with exponential backoff (max 5)

---

## 8. OB Replay Pfad

```text
if bucket_time <= 2026-08-28T16:26:23Z:
    import from orderbook_features_1s_v2 (bulk backfill)
else:
    replay FS ob200_v3 segment for hour
    compute 1s features with LiveSecondClock semantics
    mark genuine vs CF
```

On-demand OB1000: **not** in incremental processor Phase 1 — socket request path remains dashboard-only.

---

## 9. Health & Freshness Metriken

Expose (log + optional Prometheus later):

- `research_processor_lag_seconds{symbol,source}`
- `research_last_finalized_ts{symbol,data_type}`
- `research_batch_rows_written`
- `research_late_rows_corrected`
- `research_coverage_gap_count`

---

## 10. Collector-Safety

- SELECT-only on source tables
- No writes to orderbook_analysis / signal_generator
- No process start/stop/restart
- Processor crash does not affect collectors

---

## 11. Reprocessing API (Phase 1 CLI sketch)

```bash
research_processor run --symbol BTCUSDT --once
research_processor backfill-day --symbol DOGEUSDT --date 2026-08-29
research_processor reprocess --symbol BTCUSDT --from 2026-08-31T18:00:00Z --to 2026-08-31T20:00:00Z
```

---

## 12. Abhängigkeiten

- `clickhouse_connect` read+write to `btc_doge_research` only
- `orderbook_analyse` replay modules (read-only import)
- `research/btc_ob_fight/liquidation_flow_contract.py` frozen mapping
