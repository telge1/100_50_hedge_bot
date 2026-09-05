# ABSCHLUSSBERICHT — OI 5m BTC/DOGE Production Backfill

## VERDICT

`OI_5M_BTC_DOGE_BACKFILL_COMPLETE_EXACT_PARITY`

## Zeitfenster / Cutoff

| | |
|--|--|
| Start | `2026-08-18T15:10:00Z` |
| Cutoff (frozen) | `2026-09-04T17:38:05Z` |
| Last closed bucket written | `2026-09-04T17:35:00Z` |
| Granularity | **5m** (`intervalTime=5min`) |
| Source mark | `BYBIT_REST_5M_HISTORY` |
| Target | `orderbook_analysis.open_interest_5m_history` |

Daten **nach** dem Cutoff wurden bewusst nicht geschrieben (später Live/Gap-Fill).

## Importierte Buckets

| Symbol | erwartet | inserted | missing after |
|--------|----------|----------|---------------|
| BTCUSDT | 4926 | **4926** | **0** |
| DOGEUSDT | 4926 | **4926** | **0** |
| **Total** | 9852 | **9852** | **0** |

Table logical rows: 8640 → **18492** (2 symbols; max `2026-09-04 17:35:00`).

## Gates

```text
BTC_SOURCE_DB_PARITY=true
DOGE_SOURCE_DB_PARITY=true
MISSING_BUCKETS_AFTER_IMPORT=0
LOGICAL_DUPLICATES=0
TIMESTAMP_SHIFT=0
PARSE_REJECTS=0
BTC_MISSING=0
DOGE_MISSING=0
WOULD_INSERT=0   # idempotence dry-run
```

## Pilot (isoliert)

- Table: `open_interest_5m_history_pilot_v1` (**nicht** gedroppt)
- 12 BTC buckets; insert 12 / re-insert 0; exact OI+timestamp parity; `parse_rejects=0`

## Chunks

- 36 chunks (18×BTC + 18×DOGE), 288 buckets/day then final 30
- Manifest: `chunk_manifest.csv`
- Retries: 0

## Research-Kompatibilität

- Historische Analysen können `open_interest_5m_history` mit `source=BYBIT_REST_5M_HISTORY` und `granularity=5m` joinen über `bucket_time` (UTC).
- **Keine** Ausgabe als 5s; Live-`open_interest_5s` / `open_interest_events` unverändert und getrennt.
- Coverage im Importfenster endet am Cutoff (`≤ 2026-09-04T17:35:00Z`); Post-Cutoff bleibt fehlend/noch nicht erfasst.
- MySQL `research_open_interest_5m` wurde **nicht** beschrieben.

## PIDs (unverändert)

| Role | PID |
|------|-----|
| Dashboard | **1780509** |
| OI/Liq | **147111** |
| Public Trades | **1661773** |
| Full OB | **STOPPED** (absent) |

```text
DESTRUCTIVE_ACTIONS_EXECUTED=false
COLLECTOR_RESTART_EXECUTED=false
COMMIT_EXECUTED=false
PUSH_EXECUTED=false
OPTIMIZE_EXECUTED=false
TABLE_DROP_EXECUTED=false
```

## Artefakte

`results/oi_5m_btc_doge_production_backfill_v1/`

- PREFLIGHT.md, isolated_pilot_parity.json, chunk_manifest.csv
- btc_source_db_parity.json, doge_source_db_parity.json
- coverage_before_after.csv, remaining_gaps.csv
- idempotence_dry_run.json, gates.json, run.log, ABSCHLUSSBERICHT.md

**STOP** — kein Collector-Neustart.
