# Full-History Backfill v1 — BTCUSDT / DOGEUSDT

## 1. Verdict

`BTC_DOGE_RESEARCH_DB_FULL_HISTORY_PARTIAL`

Der resumierbare Full-History-Lauf ist abgeschlossen. Alle zweifelsfrei belegten
Coverage-Segmente sind in `btc_doge_research` vorhanden. Es existiert jedoch nur
**ein** vollständiger gemeinsamer UTC-Tag (`2026-08-26`), der bereits durch den
Phase-2-Pilot geladen wurde. Kein zusätzlicher Symboltag konnte importiert werden.

## 2. Branch / HEAD / Dirty

- Branch: `feature/btc-doge-research-db`
- HEAD: `e6897df014f1688a3fde019f613a5e9a131c6293`
- Tracked Worktree: sauber (Phase-2-Checkpoint committed)
- Uncommitted (Full-History-Arbeit, bewusst nicht committed):
  - `research/btc_doge_research/full_history_contracts.py`
  - `research/btc_doge_research/full_history_inventory.py`
  - `research/btc_doge_research/full_history_runner.py`
  - `research/btc_doge_research/full_history_validate.py`
  - `research/btc_doge_research/phase2_day_loader.py`
  - `results/btc_doge_research_db_full_history_v1/`
- Fachfremde untracked Dateien unverändert bewahrt

## 3. Phase-2-Checkpoint-Commit

- Commit: `e6897df` — `research: add BTC DOGE research DB phase 2 pilot`
- Enthalten: Phase-2-Code, Tests, Phase-2-Reports
- Nicht enthalten: Phase-1B-Artefakte, Full-History-Code
- `git diff --check`: sauber vor Commit
- Tests vor Commit: 118 passed, 1 skipped

## 4. Kein Push

Kein `git push` durchgeführt.

## 5. Coverage-Inventar

Inventar erzeugt unter `results/btc_doge_research_db_full_history_v1/`:

| Datei | Zweck |
|---|---|
| `full_history_coverage_inventory.csv` | Vollständiges Inventar je Symbol/Tag |
| `full_history_coverage_summary.json` | Aggregierte Eligibility |
| `excluded_days.csv` | Ausgeschlossene Symboltage mit Gründen |
| `producer_segments.csv` | Producer-Zuordnung je Symbol/Tag |
| `source_gaps.csv` | Belegte Source-Gaps |

Summary:

```json
{
  "eligible_symbol_days": 2,
  "excluded_symbol_days": 86,
  "eligible_days_btc": ["2026-08-26"],
  "eligible_days_doge": ["2026-08-26"],
  "pilot_day": "2026-08-26",
  "pilot_ready": 1
}
```

Inventar-Fenster: 2026-07-19 bis 2026-08-31 UTC (44 Tage × 2 Symbole = 88 Symboltage).

## 6. Einbezogene Tage je Symbol

| Symbol | READY-Tage | Quelle |
|---|---|---|
| BTCUSDT | 1 (`2026-08-26`) | Phase-2-Pilot-Batch |
| DOGEUSDT | 1 (`2026-08-26`) | Phase-2-Pilot-Batch |

Neu durch Full-History-Runner geladen: **0** Symboltage (Pilot-Tag wird bewusst
übersprungen; keine weiteren eligible Tage).

## 7. Ausgeschlossene Tage und Gründe

86 Symboltage ausgeschlossen. Häufigste Gründe (Mehrfachnennungen möglich):

| Grund | Vorkommen |
|---|---|
| `NO_OB200_RAW_FILES` | 72 |
| `INCOMPLETE_OI` | 78 |
| `NO_PRODUCER` | 10 |
| `ORDERING_AMBIGUOUS_PRESENT` | 3 |
| `AFTER_QUEUE_FULL` | 6 |
| `QUEUE_FULL_PARTIAL_DAY` | 2 |
| `INCOMPLETE_OB200` | 2 |

Wesentliche Einzelfälle im Live-Fenster:

- **2026-08-24**: Partial-Start-Tag (Live ab 22:47:54Z), unvollständige OI/OB200
- **2026-08-25**: OI 17.279/17.280, 4 ORDERING_AMBIGUOUS-Buckets
- **2026-08-27**: OI 17.268/17.280 (unvollständig)
- **2026-08-28**: `queue_full`-Partial-Tag (Terminal 16:26:23Z)
- **2026-08-29 … 2026-08-31**: `AFTER_QUEUE_FULL`
- **2026-07-19 … 2026-08-18**: Day-ZIP-Producer ohne FS-OB200-Rohdateien
- **2026-08-19 … 2026-08-23**: Kein Producer (`NO_PRODUCER`)

## 8. Producer-Lineage

Zwei getrennte Producer, nicht vermischt:

**A. Live Collector** — `BYBIT_OB200_LIVE_COLLECTOR_V3`
- Semantik: `raw_ob200_event_time_eos_v1` / `RECEIVE_TIME_ASOF`
- Kanonisch ab: `2026-08-24T22:47:54Z`
- Terminal: `queue_full` bei `2026-08-28T16:26:26Z`
- Kein Carried-Forward über `queue_full`

**B. Day-ZIP-Importer** — `BYBIT_OB200_DAY_ZIP_IMPORTER_V3`
- Eigener Producer, eigene Semantik (`EVENT_TIME_END_OF_SECOND`)
- Endet: `2026-08-18`
- Keine importierbaren OB200-Rohdateien im FS für Research-Contract

Details: `producer_segments.csv`

## 9. Producer-Übergänge

- **Day-ZIP → NONE** (2026-08-18 → 2026-08-19): 5-Tage-Lücke ohne Producer
- **NONE → Live Collector** (2026-08-23 → 2026-08-24): Mid-Day-Start, kein vollständiger Tag
- **Live → queue_full** (2026-08-27 → 2026-08-28): Terminal-Partial-Tag
- **queue_full → AFTER_QUEUE_FULL** (2026-08-28 → 2026-08-29+): Kein weiterer Import

Segmentgrenzen explizit in `producer_segments.csv`. Keine Last-Write-Wins-Regel.
Overlap-Dedup nur innerhalb desselben Producer-Segments.

## 10. queue_full-Behandlung

- `terminal_reason=queue_full` für 2026-08-28 (beide Symbole)
- `coverage_complete=false`, Tag nicht READY
- Kein Zustand nach der Lücke fortgeschrieben
- Tage ab 2026-08-29 als `AFTER_QUEUE_FULL` ausgeschlossen

## 11. READY / PARTIAL / FAILED-Batches

| Status | Anzahl | Bedeutung |
|---|---|---|
| READY | 1 | Phase-2-Pilot `phase2:20260826:99aebc6d53248407` |
| FAILED | 4 | Historische Phase-2-Recovery-Versuche |
| RUNNING / RECOVERING* | 5 | Historische Zwischenzustände |

Full-History-Runner: **0 neue Batches** (keine eligible Tage außerhalb Pilot).
Keine FAILED/PARTIAL-Batches durch Full-History-Lauf.

## 12. Tabellen-Counts (Research DB, aktiv)

Kern-Phase-2-Facts (Pilot-Build):

| Tabelle | Rows |
|---|---|
| `research_public_trade_buckets_100ms` | 214.956 |
| `research_public_trade_buckets_500ms` | 136.571 |
| `research_public_trade_buckets_1s` | 99.251 |
| `research_liquidation_events` | 997 (933 kanonisch im Pilot) |
| `research_liquidation_buckets_500ms` | 633 |
| `research_open_interest_observations` | 34.560 |
| `research_tpo_bracket_ranges_30m` | 42 |
| `research_tpo_profile_bins_session` | 241 |
| `research_volume_profile_bins_session` | 241 |
| `research_profile_levels_session` | 12 |
| `research_ob200_snapshots_1s` | 172.800 |

Gesamtdatenbank komprimiert (alle `research_*`-Tabellen inkl. Legacy): **134.381.357 Bytes**.

Phase-2-Pilot-Kern (~104,7 MB komprimiert, unverändert zum Pilot).

## 13. Parität

`parity_status: PASS` — alle 2 Symboltage (BTC/DOGE 2026-08-26):

| Prüfung | Ergebnis |
|---|---|
| Trade-Count-Erhaltung | PASS |
| OI 17.280/Symbol | PASS |
| OB 86.400/Symbol, 200×200 Levels | PASS |
| Liquidation-Dedup | PASS |
| NaN/Inf (Phase-2-validiert) | PASS |

Outputs: `parity_by_symbol_day.csv`, `parity_failures.csv` (leer),
`conservation_by_symbol_day.json`, `ob_quality_by_symbol_day.csv`

Seam (Phase-2, unverändert): 1.796/1.800 exakt, 4 `ORDERING_AMBIGUOUS` erhalten.

## 14. Idempotenz

Phase-2-Pilot erneut ausgeführt:

```json
{
  "status": "IDEMPOTENT_SKIP",
  "batch_id": "phase2:20260826:99aebc6d53248407"
}
```

Keine zusätzlichen Facts, keine veränderten Checksums, keine verdoppelten Volumina.

## 15. Resume-Test

Full-History-Runner dreimal ausgeführt:

1. `backfill_run_1.json` — 0 geladen, eligible=0
2. `backfill_resume.json` — 0 geladen, eligible=0 (Resume: keine Recompute früherer READY-Tage)
3. `backfill_final.json` — `status: READY`, `pilot_batch_ready: true`

READY-Tage werden korrekt übersprungen; kein erneutes Berechnen des Pilot-Tags.

## 16. Laufzeit

| Lauf | Wall | CPU | Geladene Symboltage |
|---|---|---|---|
| Backfill Run 1 | 59,2 s | 52,4 s | 0 |
| Backfill Resume | 58,9 s | 52,7 s | 0 |
| Backfill Final | 58,4 s | 52,3 s | 0 |
| Inventar (einmalig) | ~60 s | — | — |
| Validierung | ~64 s | — | — |

Hauptzeit: Coverage-Inventar-Scan über 88 Symboltage (kein OB-Replay).

## 17. Peak RAM

- Full-History-Runner: **~59 MiB** Peak RSS (`peak_rss_kib: 59504`)
- Kein unkontrolliertes Multiprocessing
- Höchstens ein OB-Tagesbatch gleichzeitig (nicht ausgelöst, da 0 Loads)

## 18. Speicherverbrauch

- Freier Speicher vor/nach Lauf: **~371 GiB**
- Storage-Gate: PASS (0 erwartete Bytes, Reserve 20 GiB)
- Pilot OB200 komprimiert: 82.661.769 Bytes (`research_ob200_snapshots_1s`)
- Keine Speicher- oder RAM-Gefahr während des Laufs

## 19. Query-Benchmarks

6× 60-Minuten-Fenster auf 2026-08-26 (3 BTC, 3 DOGE), je OB200 + Fight-Input:

| Metrik | Ziel | Ergebnis (warm) |
|---|---|---|
| OB200 60 min | < 5 s | **149–212 ms** (3.600 Rows) |
| Fight-Input | < 10 s | **7–14 ms** |

Alle Benchmarks: **PASS**. Details: `query_benchmarks.csv`

## 20. Tests

- Vor Backfill (Phase-2-Suite): 118 passed, 1 skipped
- Nach Full-History-Fixes: `tests/research/test_btc_doge_research_phase2.py` — **7 passed**
- Keine 20-Minuten-BTC-Fight-Integration ausgeführt
- `git diff --check`: sauber auf Full-History-Dateien

## 21. Collector- / Live-Sicherheit

Unveränderte Live-Prozesse (PIDs aktiv):

| PID | Prozess |
|---|---|
| 147111 | OI/Liquidation Collector (live) |
| 1661773 | Signal Generator Live Collector Service |
| 3946369 | OB200 raw-archive-only (BTCUSDT, DOGEUSDT) |

- Keine Collector-/Dashboard-/Bot-Änderungen
- Keine destruktiven ClickHouse-Operationen (kein DROP/TRUNCATE/DELETE)
- Alle Writes weiterhin auf `btc_doge_research` begrenzt

## 22. Bestätigung: kein Watcher, keine Fight-CLI-Umstellung

Nicht implementiert (wie gefordert):

- Kein systemd-Service / Cronjob / Research-Watcher
- Keine Änderung an `run_btc_ob_fight_case.py`
- Kein automatischer Raw-Fallback
- Keine Dashboard- oder Collector-Anpassungen

## 23. Kein weiterer Commit nach dem Backfill

Full-History-Code und Reports bleiben uncommitted. Nur der Phase-2-Checkpoint
(`e6897df`) ist committed.

## 24. Kein Push

Kein Push zum Remote durchgeführt.

---

### Zusammenfassung

Unter striktem Coverage-Gate existiert in der verfügbaren Quellhistorie genau
**ein** vollständiger gemeinsamer UTC-Tag für BTCUSDT und DOGEUSDT. Dieser Tag
wurde bereits im Phase-2-Pilot geladen und ist paritätsgeprüft sowie idempotent.
Der Full-History-Runner hat korrekt **0 zusätzliche Tage** importiert und alle
86 ausgeschlossenen Symboltage dokumentiert. Die Research-Datenbank ist für den
belegten Coverage-Umfang konsistent und abfragefähig; der historische Spanne
nach ist das Ergebnis **PARTIAL**, nicht weil der Lauf fehlgeschlagen ist, sondern
weil die Quellen keinen weiteren zweifelsfrei vollständigen Tag liefern.
