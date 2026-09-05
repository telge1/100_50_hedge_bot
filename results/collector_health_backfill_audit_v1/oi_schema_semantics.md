# OI Schema & Semantics Proof (Phase A, read-only)

Evidence UTC window: 2026-09-04 ~17:16–17:18Z. No production writes.

## Dual pipelines (must not be conflated)

| Pipeline | Engine | Table | Writer | Grain | OI value semantics |
|----------|--------|-------|--------|-------|--------------------|
| **Live CH** | ClickHouse `orderbook_analysis` | `open_interest_events`, `open_interest_5s`, `open_interest_5m_history` | `orderbook_analyse.oi_liquidation_collector` | events / 5s snapshot / REST 5m | contracts `open_interest` (+ optional value/single*) |
| **Research MySQL** | InnoDB `regime_scanner_research` | `research_open_interest_5m` | `research.regime_scanner.derivatives` importer | 5m buckets from **1m** `liquidation_data` | **last** minute snapshot in bucket (not mean) |

These are **different SoTs** with different provenance. REST `/v5/market/open-interest?intervalTime=5min` maps directly to CH `open_interest_5m_history`, **not** to the research importer path without semantic conversion.

---

## 1–3. Live CH tables (proven via DESCRIBE + DDL `sql/003_oi_liquidation_schema.sql`)

### `open_interest_5m_history`

- ORDER BY `(symbol, bucket_time, source, collector_instance_id)` (MergeTree; logical dedupe by query, not ReplacingMergeTree).
- `bucket_time`: REST `timestamp` ms → UTC datetime (used as 5m bucket label).
- Columns: `open_interest`, `open_interest_value`, `source`, `inserted_at`, …
- Live counts (2026-09-04): **8640 rows, 1 symbol (BTCUSDT)**, min `2026-07-19 15:10`, max `2026-08-18 15:05`, `source=BYBIT_REST_5M_HISTORY`.
- Internal span gaps for BTC: **0** (`have=8640`, `span_expected=8640`).
- DOGEUSDT: **0 rows**.

### `open_interest_5s` / `open_interest_events`

- Live WS `tickers.{symbol}` → events on change; 5s wall-clock **current state snapshot** (not average).
- Requires `openInterest` + `openInterestValue`; `singleOpenInterest*` optional.
- Max timestamps frozen at **2026-09-01 16:46** despite PID 147111 running → **STALE**.

### Dedup

- CH MergeTree: duplicates possible; consumers should `argMax`/latest by `inserted_at`/`received_at` (DDL comment).
- REST backfill (`backfill.py`) writes `openInterest` → `open_interest`; skips rows without `openInterest`.

---

## 4–7. Research MySQL (code+DDL proven; live counts **not** queried)

DDL: `research/regime_scanner/derivatives/schema.py` (`SCHEMA_VERSION = derivatives_5m_v1`).

```
UNIQUE KEY uq_research_oi_5m (symbol, bucket_start, import_version)
```

- `bucket_start` = floor(minute_ts, 5m); `bucket_end` = start+5m.
- Join contract (docs/features): `market_candles.open_time = bucket_start`.
- Live COUNT/MIN/MAX/dup/gap: **BLOCKED** — user `liq_collector` → `Access denied` to `regime_scanner_research`. Earlier claimed ~169542 rows **not re-verified** this session.

---

## 8–11. Aggregation / units

| Question | Answer | Proof |
|----------|--------|-------|
| Live writes raw/1m/5m? | Live: event + **5s snapshot**. 5m only via REST backfill module | collector + `backfill.py` |
| 5m value = last/avg/first? | **CH REST:** exchange 5m point (stock). **Research:** **last** 1m snapshot in bucket | `aggregate_5m.py` docstring L261–265; `oi = last.open_interest` |
| openInterest vs singleOpenInterest? | Stored primary: **openInterest**. `singleOpenInterest` optional on events/5s; REST sample returns both; CH 5m hist does **not** store single* | schema + REST sample |
| BTC/DOGE units | Linear contracts level in `open_interest` (BTC sample ~60k contracts). Not USD unless `open_interest_value`/`open_interest_usd` | REST/CH parity equal on `openInterest` |

---

## 12–13. `oi_change_*`

- **Not stored** on `research_open_interest_5m` or CH OI tables.
- Computed in feature layers (`oi_price_delta_pattern`, orderflow features) with **sequence/gap guards** (`sequence_id`, `gap_before_seconds`, `SEQUENCE_GAP_SECONDS` in derivatives aggregate; `oi_valid` in features).
- Crossing gaps: sequence_id increments when minute gap ≥ threshold; features treat invalid OI windows as non-computable (NaN / `oi_valid=False`) — **not** a silent cross-gap ratio on the OI table itself.

---

## 14–15. 51-coin OI/Liq pipeline vs research

- **New live 51-coin path:** CH `orderbook_analysis` tables above via `oi_liquidation_collector` (universe JSON 51).
- **Research path:** MySQL curated 5m from `liquidation_research.liquidation_data` 1m → last-in-bucket.
- **Semantic difference:** REST 5m stock series ≠ 1m-last aggregation; different DB engines; different keys (`bucket_time`+`source` vs `bucket_start`+`import_version`).

---

## Canonical keys for backfill design

| Target | Canonical key | Preferred for Phase B UI/backfill? |
|--------|---------------|-------------------------------------|
| CH `open_interest_5m_history` | `(symbol, bucket_time)` + `source=BYBIT_REST_5M_HISTORY` | **YES** — REST parity proven |
| MySQL `research_open_interest_5m` | `(symbol, bucket_start, import_version=derivatives_5m_v1)` | Only after ACL + explicit conversion design |

**Freshness bounds (derived):**

- Public trades continuous: lag ≪ 5s healthy; warn >30s; stale >120s (from live lag ~0.09s and trade frequency).
- OI 5s: expect write every ~5s per symbol when WS healthy; stale if `max(bucket_time)` or health `event_ts` age > 60s.
- OI 5m hist: not continuous; gap = missing closed 5m buckets vs REST.
- Liquidations: event-sparse; require WS/health heartbeat <60s, not new rows.
