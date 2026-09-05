# Source Recovery v1 — Modality-Scoped Full-History Backfill

## 1. Verdict

`BTC_DOGE_RESEARCH_DB_SOURCE_RECOVERY_BACKFILL_STARTED`

Source-Reconciliation bestanden, Pilot-Parität und Idempotenz bestanden, Disk-Gate bestanden.
Der resumierbare Full-History-Backfill läuft per `nohup`.

## 2. Branch / HEAD / Dirty

- Branch: `feature/btc-doge-research-db`
- HEAD: `2af0477` (Checkpoint: `research: add resumable BTC DOGE full history backfill`)
- Uncommitted: Source-Recovery-Erweiterungen (Modality-Coverage, Discovery, Runner-CLI, Tests)
- Fachfremde untracked Dateien unverändert

## 3. Checkpoint-Commit

Commit `2af0477` enthält den ursprünglichen Full-History-Code (Tag-Gate-Version) und Phase-2-Day-Loader.
Die Modalitäts-Umstellung ist in uncommitted Dateien (bewusst nach Checkpoint).

## 4. Bereits importierte Daten (vor Source Recovery)

| Tag / Segment | Symbol | Modalität | Status |
|---|---|---|---|
| 2026-08-26 UTC | BTCUSDT + DOGEUSDT | Alle Phase-2-Facts | READY (Pilot `phase2:20260826`) |
| 172.800 OB200-1s-Snapshots | beide | OB200 | Phase-2-Build |

Phase-2-Pilot unverändert, nicht dupliziert.

## 5. Neu entdeckte Quellen

Discovery-Outputs unter `results/btc_doge_research_db_source_recovery_v1/`:

| Quelle | Typ | Producer |
|---|---|---|
| `orderbook_analyse/.../ob200_v3/` | SHADOW_ARCHIVE FS | `BYBIT_OB200_SHADOW_ARCHIVE_V3` |
| Live-Collector-Stream (bis queue_full) | LIVE | `BYBIT_OB200_LIVE_COLLECTOR_V3` |
| `orderbook_analysis.public_trades_canonical` | ClickHouse | `CLICKHOUSE_CANONICAL` |
| `orderbook_analysis.open_interest_5s` | ClickHouse | `CLICKHOUSE_OI_5S` |
| `orderbook_analysis.all_liquidations` | ClickHouse | `CLICKHOUSE_LIQUIDATIONS` |
| `signal_generator.candles_1m` | ClickHouse (Coverage only) | `CLICKHOUSE_CANDLES_1M` |
| `orderbook_analysis.orderbook_features_1s_v2` | CH derived (Hinweis only) | nicht import-eligible |

OB200-Shadow-Tage BTC: 2026-08-24 … 2026-08-31 (stundenweise Dateien vorhanden).

## 6. Ursache der vorherigen NO_OB200_RAW_FILES / AFTER_QUEUE_FULL-Ausschlüsse

**AFTER_QUEUE_FULL (z. B. 2026-08-31):**
`_producer_for_day()` invalidierte ganze UTC-Tage nach `2026-08-28T16:26:23Z` für den
Live-Producer — **ohne** das Shadow-Archiv zu prüfen. Tatsächlich existieren dort 24×
hourly `ob200_v3.zst`-Dateien pro Symbol/Tag (raw-archive-only Collector PID 3946369).

**NO_OB200_RAW_FILES (Day-ZIP-Zeitraum):**
Das alte Inventar scannte FS-OB200 nur für Live-Producer-Tage und markierte frühere Tage pauschal.
Trades/OI/Liquidationen in ClickHouse waren dennoch vorhanden, wurden aber durch All-or-Nothing-Tages-Gate ausgeschlossen.

Details: `source_lineage_reconciliation.json`, `previously_missed_sources.csv`

## 7. Golden-31.08-Quellpfad

- Referenz: `results/btc_ob_fight_cases/20260831T190000Z/run_018`
- Fenster: BTCUSDT 2026-08-31T18:30:00Z – 19:30:00Z
- Quelldateien:
  - `BTCUSDT/2026/08/31/BTCUSDT_20260831T180000Z_20260831T190000Z_ob200_v3.zst`
  - `BTCUSDT/2026/08/31/BTCUSDT_20260831T190000Z_20260831T200000Z_ob200_v3.zst`
- Root: `/home/telgenbuescher/projects/orderbook_analyse/data/orderbook_raw_shadow/ob200_v3`

## 8. Modality-Scoped Coverage

Kein All-or-Nothing-Tages-Gate mehr. Segmentstatus je Modalität:

- `READY`, `PARTIAL`, `MISSING`, `ORDERING_AMBIGUOUS`, `SOURCE_GAP`, `AFTER_QUEUE_FULL`, `CONFLICTING_PRODUCERS`

Beispiel 2026-08-27: Trades READY, OI PARTIAL (17.268/17.280) → Trades importierbar, OI mit `coverage_status=PARTIAL`.

Gesamt: 2.580 Segmente inventarisiert, **662 eligible** (86 CANDLES nur Coverage-Tracking).

## 9. Producer-Reconciliation

- `queue_full` invalidiert nur `BYBIT_OB200_LIVE_COLLECTOR_V3` ab Terminal
- `BYBIT_OB200_SHADOW_ARCHIVE_V3` deckt post-queue_full-Stunden ab (separate Producer-ID)
- Kein Carried-Forward über queue_full
- Kein stilles Last-Write-Wins
- 4 bekannte Seam-Fälle bleiben `ORDERING_AMBIGUOUS`

## 10. Pilot-Parität

Kontrollsegment (bisher ausgeschlossen):

- BTCUSDT OB200 2026-08-31T18:00:00Z – 19:00:00Z
- Producer: `BYBIT_OB200_SHADOW_ARCHIVE_V3`
- Ergebnis: **READY**, 3.600 Snapshots, 200×200 Levels
- Build: `ed650f8048cea17b8ecc921b0108f7308696e581504a9c891dff824256fdc0d1`

## 11. Idempotenz

Zweiter Pilot-Lauf: **`IDEMPOTENT_SKIP`** — keine zusätzlichen Facts, gleiche Checksums.

## 12. Backfill-Plan

- `backfill_plan.json` / `backfill_plan.csv`
- 662 eligible Segmente
- Modalitäten: PUBLIC_TRADES, LIQUIDATIONS, OPEN_INTEREST, OB200, TPO_PROFILE, VOLUME_PROFILE
- CANDLES: COVERAGE_ONLY (nicht importiert; Profile lesen direkt aus `signal_generator`)

## 13. Erwartete Datenmenge

- Erwartete komprimierte Bytes (mit 1,35× Sicherheitsfaktor): ~2,24 GB
- OB200-Segmente: stundenweise (~2 MB/Segment geschätzt)

## 14. Disk-/RAM-Gate

- Freier Speicher: ~371 GiB
- Mindestreserve: 20 GiB
- Gate: **PASS**

## 15. nohup gestartet

**Ja**

## 16. PID

- Backfill-PID: siehe `run/btc_doge_research_full_history/backfill.pid`
- Start-PID (Beispiel): `1202258`

## 17. Log

`logs/btc_doge_research_full_history.log`

## 18. Fortschritt (Stand Start)

- Heartbeat: `run/btc_doge_research_full_history/heartbeat.json`
- Progress: `run/btc_doge_research_full_history/progress.json`
- Watermarks: `run/btc_doge_research_full_history/watermarks.json`
- Beispiel: 19/662 Segmente nach ~8 s

## 19. Monitoring-Befehle

```bash
# Prozessstatus
cat run/btc_doge_research_full_history/backfill.pid
ps -p $(cat run/btc_doge_research_full_history/backfill.pid) -o pid,etime,rss,cmd

# Live-Log
tail -f logs/btc_doge_research_full_history.log

# Heartbeat / Fortschritt
cat run/btc_doge_research_full_history/heartbeat.json
cat run/btc_doge_research_full_history/progress.json | python3 -m json.tool

# Runner-Status
.venv/bin/python -m research.btc_doge_research.full_history_runner --status

# READY/PARTIAL/FAILED-Batches
.venv/bin/python -c "
from research.btc_doge_research.clickhouse import connect, rows
c=connect()
for r in rows(c,'SELECT status,count() FROM btc_doge_research.research_batch_runs GROUP BY status ORDER BY status'):
    print(r)
c.close()"

# ClickHouse Rowcounts (OB200 gesamt)
.venv/bin/python -c "
from research.btc_doge_research.clickhouse import connect, rows
c=connect()
print(rows(c,\"SELECT count(), formatReadableSize(sum(data_compressed_bytes)) FROM system.parts WHERE active AND database='btc_doge_research' AND table='research_ob200_snapshots_1s'\"))
c.close()"

# Freier Speicher
df -h /

# Kontrolliertes Stoppen (READY-Daten bleiben erhalten)
kill -TERM $(cat run/btc_doge_research_full_history/backfill.pid)

# Späteres Resume
nohup env PYTHONUNBUFFERED=1 .venv/bin/python -m research.btc_doge_research.full_history_runner --run --resume >> logs/btc_doge_research_full_history.log 2>&1 &
```

## 20. Collector-/Live-Sicherheit

Unveränderte PIDs: 147111 (OI/Liq), 1661773 (Live Service), 3946369 (OB raw-archive-only).
Keine Collector-/Dashboard-/Quelltabellen-Änderungen.

## 21. Kein Watcher

Kein systemd, kein Cronjob, kein dauerhafter Research-Watcher.

## 22. Keine Fight-CLI-Umstellung

`run_btc_ob_fight_case.py` unverändert.

## 23. Kein Push

Kein `git push` durchgeführt.

## 24. Tests

- `tests/research/test_btc_doge_research_phase2.py`: 7 passed
- `tests/research/test_btc_doge_research_full_history.py`: 9 passed
- `git diff --check`: sauber
