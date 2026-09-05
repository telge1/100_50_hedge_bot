# BTC/DOGE Research DB — Final Read-Only Abschlussaudit

**Verdict:** `BTC_DOGE_RESEARCH_DB_FINAL_PARTIAL`  
**Audit-Zeitpunkt:** 2026-09-03T06:55:00Z  
**Contract:** `btc_doge_research_full_history_v1`  
**Output:** `results/btc_doge_research_db_final_audit_v1/`

---

## 1. Finales Verdict

**`BTC_DOGE_RESEARCH_DB_FINAL_PARTIAL`**

Alle 662 importierbaren Plan-Segmente wurden verarbeitet. Die Research-DB ist für timestamp-basierte Analysen und Fight-Inputs **nutzbar**, sofern COMPLETE/PARTIAL-Gates pro Fenster angewendet werden. Vollständige lückenlose Full History über alle Modalitäten ist **nicht** gegeben — echte Source-Gaps und PARTIAL-Stunden sind dokumentiert und dürfen nicht verschleiert werden.

Nicht `FINAL_READY` wegen:
- 36 CH-PARTIAL-Segmente (OB200 Mid-Hour-Gaps, OI unvollständig)
- 1 verwaister `RUNNING`-Batch-Metadatensatz (effektiv PARTIAL mit Daten)
- 2 OB200-Stunden mit doppelten Snapshots (Re-Import während Audit-Smoke)

Nicht `FINAL_BLOCKED` — Defekte sind erklärbar, lokalisiert und gate-bar.

---

## 2. Repository / Branch / HEAD / Dirty

| Feld | Wert |
|------|------|
| Branch | `feature/btc-doge-research-db` |
| HEAD | `2af0477dc4654d47603a2845a0ea1463b0bcfa12` |
| Dirty | 67 untracked/modified Dateien (Backfill-Recovery-Code uncommitted) |
| Commit/Push | Keine (Audit read-only) |

---

## 3. Heartbeat / Progress-Invarianten

**`run/btc_doge_research_full_history/heartbeat.json`:**

| Invariante | Wert | OK |
|------------|------|-----|
| status | COMPLETED | ✓ |
| completed | 662 | ✓ |
| importable_segments | 662 | ✓ |
| ready_segments + skipped_segments | 472 + 190 = 662 | ✓ |
| remaining_segments | 0 | ✓ |
| failed_segments | 0 | ✓ |

- `progress.json`, `watermarks.json`: valides JSON ✓
- `runner_pid` 1248597: Prozess **beendet** (stale PID nach sauberem COMPLETED — kein Fehler)
- Collector unverändert (u.a. PID 147111 OI/Liq-Live)

---

## 4. ClickHouse Batch-Status (Source of Truth)

**Latest status pro `batch_id` (argMax started_at):**

| Status | Anzahl |
|--------|-------:|
| READY | 625 |
| PARTIAL | 36 |
| RUNNING | 1 |
| FAILED | 2 (nicht-importierbare CANDLES-Metadaten) |

**Plan-Mapping (662 importierbare Segmente):** 625 READY + 36 PARTIAL + 1 RUNNING = **662/662 abgedeckt**, 0 MISSING.

| Symbol:Modalität | READY | PARTIAL | RUNNING |
|------------------|------:|--------:|--------:|
| BTC:OB200 | 138 | 8 | 0 |
| DOGE:OB200 | 135 | 10 | 1 |
| BTC/DOGE:OPEN_INTEREST | 4 | 9 | 0 |
| BTC/DOGE:PUBLIC_TRADES, LIQ, TPO, VOL | je 43 | 0 | 0 |

**190 skipped** im Heartbeat = bereits vor dem Lauf in CH vorhandene READY/PARTIAL-Segmente (Idempotenz).

---

## 5–6. Coverage je Symbol und Modalität

### Zeiträume (Daten in `btc_doge_research`)

| Symbol | Modalität | available_from | available_to |
|--------|-----------|----------------|--------------|
| BTCUSDT | PUBLIC_TRADES | 2026-07-19 | 2026-08-31 |
| BTCUSDT | OB200 | 2026-08-24 22:47:54 | 2026-08-31 23:59:59 |
| BTCUSDT | LIQUIDATIONS | 2026-08-18 | 2026-08-31 |
| BTCUSDT | OPEN_INTEREST | 2026-08-18 | 2026-08-31 (PARTIAL) |
| DOGEUSDT | PUBLIC_TRADES | 2026-07-19 | 2026-08-31 |
| DOGEUSDT | OB200 | 2026-08-24 22:47:54 | 2026-08-31 23:59:59 |
| DOGEUSDT | LIQUIDATIONS | 2026-08-18 | 2026-08-31 |
| DOGEUSDT | OPEN_INTEREST | 2026-08-18 | 2026-08-31 (PARTIAL) |
| CANDLES | — | COVERAGE_ONLY | extern / nicht fh-importiert |

### Klassifikation (662 importierbare Segmente)

| Klasse | Anzahl |
|--------|-------:|
| COMPLETE (READY) | 625 |
| PARTIAL_WITH_DOCUMENTED_GAPS | 36 (+1 effektiv) |
| COVERAGE_ONLY (CANDLES) | 86 Plan-Zeilen, nicht importiert |
| SOURCE_NOT_AVAILABLE | 0 im Import-Plan |
| FAILED (importierbar) | 0 |

**Producer-Seams:** `LIVE_RAW_FROM = 2026-08-24T22:47:54Z`, `LIVE_TERMINAL = 2026-08-28T16:26:23Z` (queue_full — nur Live-Producer betroffen, Shadow-Archive weiter gültig).

Details: `coverage_by_symbol_modality.csv`, `coverage_intervals.csv`, `partial_segments.csv`

---

## 7. OB200 — Gaps und Boundary-Ergebnis

**Boundary-Audit** (`ob200_boundary_audit_full.json`):

| Klasse | Stunden |
|--------|--------:|
| COMPLETE_3600 | 402 |
| PARTIAL_TRUE_GAP (Mid-Hour) | 24 |
| ZERO_DURATION_AUXILIARY (Stubs) | 432 |

- Zero-Duration-Stubs: **nicht importiert**, als `BOUNDARY_STATE_AUXILIARY` verknüpft
- Kein Fall `MISSING_INITIAL_STATE` — Primärdateien liefern 06:00:00
- Boundary-Seed nicht benötigt für auditierte Stunden

### Referenzfall BTCUSDT 2026-08-27T06:00–07:00Z

| Prüfpunkt | Ergebnis |
|-----------|----------|
| 06:00:00 vorhanden | ✓ |
| Fehlende Sekunde | **06:42:23Z** (Mid-Hour-Gap) |
| Unique seconds | 3599 / 3600 |
| CH-Status | PARTIAL |
| Erfundene Snapshots | Nein |
| Boundary-Duplikat | Nein (Gap, kein Boundary-Problem) |

**Hinweis:** Diese Stunde enthält zudem **3599 doppelte Zeilen** (7198 total / 3599 unique) durch Re-Import während Audit-Smoke — siehe §13.

OB200-Integrität: `ob200_integrity_by_segment.csv`, Gaps: `ob200_missing_seconds.csv`, `source_gaps.csv`

---

## 8. Modalitäten-Integrität

| Modalität | BTC | DOGE | Befund |
|-----------|-----|------|--------|
| PUBLIC_TRADES | dedup_sum = source_sum | gleich | Buy/Sell-Volumina konsistent |
| LIQUIDATIONS | 33504 events, dedup OK | 3300 events, dedup OK | event_key unique, bankruptcy_price vollständig |
| OPEN_INTEREST | 364908 rows, 9 partial-Tage | 213053 rows, 9 partial-Tage | PARTIAL explizit markiert |
| TPO_PROFILE | tpo_count 1–18 | tpo_count 1–17 | Bracket-TPO, nicht Volume-at-Price |
| VOLUME_PROFILE | base_volume gewichtet | base_volume gewichtet | getrennt von TPO |
| CANDLES | COVERAGE_ONLY | COVERAGE_ONLY | absichtlich nicht importiert |

Details: `modality_integrity.json`

---

## 9. Parität (Stichproben)

| Fenster | Modalität | Verdict |
|---------|-----------|---------|
| 2026-07-20 BTC/DOGE | Trades | EXACT |
| 2026-08-27 BTC | Trades | EXACT |
| 2026-08-31 BTC | Trades | EXACT |
| 2026-08-31T18:00 BTC | OB200 3600s | EXACT |
| 2026-08-31T18:00 DOGE | OB200 3600s | EXACT |
| 2026-08-26 BTC | Trades | DEFECT (0 fh-rows — Pilot-Tag, Phase-2 separat) |

Details: `parity_samples.csv`

---

## 10. Performance (read-only Warm-Queries)

| Query | p50 | Ziel | Pass |
|-------|----:|------|:----:|
| OB200 BTC 60m | 2.6 ms | < 5 s | ✓ |
| OB200 DOGE 60m | 2.1 ms | < 5 s | ✓ |
| Trades 60m | 2.5 ms | < 10 s | ✓ |
| Liq+OI 60m | 3.8 ms | < 10 s | ✓ |
| Fight-Input Join 60m | 13.6 ms | < 10 s | ✓ |
| Profile Anchor | 1.7 ms | — | ✓ |

Timestamp-Analyse **ohne Roharchiv-Replay** ist für abgedeckte Fenster praktikabel.

Details: `query_benchmarks.csv`

---

## 11. Zulässige Analysezeiträume

**Fight-Analyse zulässig für:**
- COMPLETE-Tage/Stunden (625 Segmente) — alle Modalitäten des Segments READY
- Golden Window **2026-08-31T18:00–19:00Z** BTC+DOGE OB200: COMPLETE, EXACT parity
- Trades/Liq/Profile: ab 2026-07-19 (BTC/DOGE) bzw. 2026-08-18 (Liq/OI)

**Nur mit PARTIAL-Gate / eingeschränkt:**
- 36 OB200/OI PARTIAL-Segmente (fehlende Sekunden / unvollständige OI-Tage)
- DOGE OB200 2026-08-31T23:00Z (3567/3600, effektiv PARTIAL)
- 2 OB200-Stunden mit Duplikat-Snapshots (vor Fight deduplizieren oder rebuild_id nutzen)

**Nicht zulässig ohne externe Quelle:**
- CANDLES (COVERAGE_ONLY — `orderbook_analysis` / extern)
- Zeiten vor OB200-Live-Start ohne Shadow-Datei
- 2026-08-26 Pilot-Tag (separater Phase-2-Pfad, nicht fh-batch)

---

## 12. Antworten Phase 7

1. **662/662 importierbare Segmente abgearbeitet?** Ja (Heartbeat + Plan-Mapping).
2. **190 skipped = bereits READY/PARTIAL?** Ja, CH-Nachweis via Idempotenz-Skips.
3. **FAILED/RUNNING importierbar?** 0 FAILED importierbar; 1 RUNNING-Metadaten-Orphan (Daten PARTIAL vorhanden).
4. **BTC vollständig:** Trades/Liq/TPO/Vol: 43 Tage COMPLETE; OB200: 138/146 Stunden COMPLETE; OI: 4/13 Tage COMPLETE.
5. **DOGE vollständig:** Analog; OB200 135/146 COMPLETE.
6. **PARTIAL:** 36 CH + 1 effektiv (siehe `partial_segments.csv`).
7. **Fehlende Quellen:** Im Plan als EXCLUDED/SOURCE_NOT_AVAILABLE markiert, nicht importiert.
8. **Alle Quelldateien berücksichtigt?** OB200 aus realen Dateipfaden (post-fix); Boundary-Stubs auxiliär.
9. **Quelldateien im Plan fehlend?** Keine offenen Import-Lücken im 662-Plan.
10. **Plan ohne Quelle?** 0 MISSING_IN_CH.
11. **Fight-einsatzbereit?** Ja mit Gates (COMPLETE-only default, PARTIAL explizit).
12. **CLI bei PARTIAL:** Fenster als `PARTIAL_WITH_DOCUMENTED_GAPS` markieren, keine Vollständigkeit annehmen, ggf. abort oder degraded mode.
13. **Roharchiv-Pipeline ersetzbar?** Ja für abgedeckte COMPLETE-Fenster; PARTIAL/Gaps weiterhin aus Research-DB-Metadaten lesen.
14. **Restarbeiten vor Fight-CLI-Umschaltung:**
    - PARTIAL/SOURCE-Gates in CLI
    - 2 OB200-Duplikat-Stunden bereinigen (read-only audit empfiehlt dedup oder skip build_id)
    - RUNNING-Orphan-Metadaten bereinigen (optional, kein Datenverlust)
    - OI PARTIAL-Tage dokumentiert akzeptieren oder nachimportieren

---

## 13. Sind restlos alle vorhandenen Daten importiert?

**Ja, alle im Plan als import_eligible markierten Quellen sind verarbeitet.**  
**Nein, nicht jede Sekunde/Tag ist lückenlos** — 24 OB200 Mid-Hour-Gaps, 18 OI-Partial-Tage/Symbol, und dokumentierte Source-Limits bleiben bestehen. Nichts wurde imputiert oder erfunden.

---

## 14. Live-Sicherheit

| Prüfung | Status |
|---------|--------|
| Collector unverändert | ✓ |
| Dashboard unverändert | ✓ |
| Fight-Engine/CLI unverändert | ✓ |
| CH INSERT/ALTER/DROP | Keine ✓ |
| Backfill re-run | Keine ✓ |
| Bestehende Result-Ordner | Nicht überschrieben ✓ |

---

## 15. Artefakte

| Datei | Inhalt |
|-------|--------|
| `final_verdict.json` | Verdict + Blocker |
| `clickhouse_batch_status.json` | CH Batch-Lage |
| `coverage_by_symbol_modality.csv` | 662 Segment-Klassifikation |
| `partial_segments.csv` | 36 PARTIAL-Segmente |
| `ob200_integrity_by_segment.csv` | OB200 je Stunde |
| `parity_samples.csv` | Stichproben |
| `query_benchmarks.csv` | Performance |
| `readiness_matrix.csv` | Fight-Eligibility je Modalität |
| `safety_manifest.json` | Safety-Nachweis |
| `source_inventory_reconciliation.json` | Plan↔CH Abgleich |

**Kein Commit. Kein Push.**
