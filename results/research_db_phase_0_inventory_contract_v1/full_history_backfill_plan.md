# Full-History-Backfill-Plan (Phase 0 Design)

**Scope:** BTCUSDT + DOGEUSDT — all available history
**Execution:** Phase 1+ only — **NOT in Phase 0**

---

## 1. Reihenfolge (Pflicht)

| Step | Action |
|------|--------|
| 1 | Freeze contracts + `source_priority.json` |
| 2 | Select pilot: **2026-08-31** BTC hour 18:30-19:30 UTC (run_018 golden) + DOGE **2026-08-29** 08:00-15:30 UTC |
| 3 | Compare raw vs existing research (run_018, aggressor audit) |
| 4 | Backfill `research_public_trades` + `research_liquidation_events` |
| 5 | Backfill `research_orderbook_1s` (CH import ≤2026-08-28; FS replay after) |
| 6 | Build `research_market_1s` |
| 7 | Build `research_market_1m` |
| 8 | Coverage + idempotency proof |
| 9 | Daily batches for full range |
| 10 | Validate history/live seams |
| 11 | Enable incremental processor |
| 12 | Parity + performance benchmarks |

---

## 2. Verfügbarer Zeitraum je Quelle

| Source | BTC | DOGE |
|--------|-----|------|
| Trades canonical | 2026-07-19 → live | 2026-07-19 → live |
| OB aggregate 1s | 2026-07-19 → 2026-08-28 | same |
| OB raw FS | 2026-08-24 → live | same |
| OI 5s | 2026-08-18 → 2026-09-01* | same |
| Liquidations | 2026-08-18 → live | same |
| Candles 1m | 2025-12-11 → live | same |

*OI collector stale at audit — gap must be closed before live incremental.

**Effective unified OB 1s history:** 2026-07-19 → live (hybrid CH+FS).

---

## 3. Batch-Strategie

| Parameter | Value |
|-----------|-------|
| Primary batch unit | **1 UTC calendar day** per symbol |
| Trade/liq sub-batch | 1 hour (memory bound) |
| OB replay sub-batch | 1 hour (matches FS segments) |
| BTC before DOGE | **BTC first** (golden run_018 parity); DOGE second |
| Parallelism | **sequential** symbol-days default (OOM safety) |
| Temp storage | `/tmp/research_backfill/` scratch; delete after day commit |
| RAM limit | 512 MB replay / 1 GB peak per worker |

---

## 4. Checkpoint-Strategie

```text
research_pipeline_state.backfill_mode = true
checkpoint per (symbol, day, stage)
stages: trades | liq | oi | ob_1s | market_1s | market_1m | coverage
```

After each day:
1. Write coverage row
2. Commit checkpoint
3. Optional checksum: `cityHash64` sample 1000 random seconds

---

## 5. Resume & Retry

| Scenario | Behavior |
|----------|----------|
| Crash mid-day | Restart same day from last completed stage |
| CH timeout | Retry 3× with halved batch; log and continue next day if persistent |
| Missing FS segment | Mark gap in coverage; continue |
| Duplicate run | Idempotent — row counts + checksum match |

---

## 6. History/Live Seams

### Trades
- **Seam date:** ~2026-08-20 archive end / live overlap start
- **Validation:** `uniqExact(trade_id)` on overlap window; conflict report for field mismatches

### Orderbook
- **Seam:** 2026-08-28T16:26:23Z aggregate end → FS raw
- **Validation:** Parity audit last 24h of aggregate vs replay where FS exists

---

## 7. Missing Days / Overlaps

| Case | Handling |
|------|----------|
| Missing FS hour | coverage.gap_count++; OB 1s = CF or empty |
| Overlapping archive+live trades | dedup trade_id; prefer live if conflict-free |
| Zero liquidation events | valid — not a gap |
| OI missing buckets | forward-fill forbidden without flag; mark gap |

---

## 8. Late Arrivals (historical)

Backfill uses `FINAL` snapshot at batch time.
Incremental processor handles late arrivals post-backfill via overlap window.

---

## 9. Determinismus & Rollback

- **Deterministic:** Same source snapshot + processor_version → identical output (proven by double-run)
- **Rollback:** `ALTER TABLE ... DELETE WHERE processor_version=X` or drop partition for day — **raw sources untouched**
- No mutation of source ClickHouse tables

---

## 10. Erwartete Laufzeit (ESTIMATED — NOT_PROVEN)

| Scope | Estimate |
|-------|----------|
| Pilot 2 windows | 1-2 hours |
| 1 symbol 45d trades+ob | 4-8 hours |
| Full BTC+DOGE to Sep 2026 | 1-3 days sequential |

Dominated by OB FS replay CPU — not CH insert.

---

## 11. Speicherbedarf (ESTIMATED)

| Component | Estimate |
|-----------|----------|
| CH research DB after full backfill | 50-150 GB both symbols |
| Temp replay | <10 GB peak |

---

## 12. Query Limits

```sql
SETTINGS max_execution_time=120, max_threads=2, max_memory_usage=2000000000
```

No unbounded full-table scans — always `WHERE symbol=? AND time range`.
