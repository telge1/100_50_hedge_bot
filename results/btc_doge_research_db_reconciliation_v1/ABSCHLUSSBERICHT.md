# BTC/DOGE Research DB — Reconciliation Abschlussbericht

**Verdict:** `BTC_DOGE_RESEARCH_DB_READY_WITH_DOCUMENTED_SOURCE_GAPS`  
**Zeitpunkt:** 2026-09-03T07:12:00Z  
**Contract:** `btc_doge_research_reconciliation_v1`  
**Output:** `results/btc_doge_research_db_reconciliation_v1/`

---

## 1. Verdict

**`BTC_DOGE_RESEARCH_DB_READY_WITH_DOCUMENTED_SOURCE_GAPS`**

Alle 662 importierbaren Plansegmente sind terminal (**625 READY + 37 PARTIAL**).  
RUNNING/FAILED/MISSING = 0. Keine kanonischen OB200-Duplikate mehr.  
PARTIAL ausschließlich wegen belegter Source-Gaps. Golden-Hour-Parität BTC+DOGE = EXACT.

Fight-CLI-Umschaltung: **noch nicht** — Eligibility-Gates für PARTIAL/SOURCE_NOT_AVAILABLE müssen vor Cutover implementiert werden. Die Research-DB selbst ist für COMPLETE-Fenster einsatzbereit.

---

## 2. Root Causes

### A. Doppelte OB200-Snapshots

`_ready_exists()` übersprang nur `status='READY'`. PARTIAL-Segmente und Reimports mit neuem `build_id` (Golden Hour: anderer Source-Pfad/Fingerprint im Build-Hash) schrieben identische Snapshots erneut (Audit-/Smoke-Reimport).

Betroffene Stunden (vorher):

| Fenster | Ursache | Rows vorher | Unique | Extra |
|---------|---------|------------:|-------:|------:|
| BTC 2026-08-27 06:00–07:00 | gleicher `build_id`, 2× PARTIAL-Insert | 7198 | 3599 | 3599 |
| BTC 2026-08-24 22:47:54–23:00 | gleicher `build_id`, PARTIAL dann COMPLETE | 1452 | 726 | 726 |
| BTC 2026-08-31 18:00–19:00 (Golden) | zwei `build_id`s, Marktinhalt identisch | 7200 | 3600 | 3600 |

Marktpayloads waren inhaltlich identisch (Level-Hashes/mids/update_ids). Stop-Gate bestanden.

### B. RUNNING-Orphan

Append-only `research_batch_runs`: RUNNING und PARTIAL mit **identischem `started_at`**.  
`argMax(status, started_at)` war bei Gleichstand nicht deterministisch und zeigte RUNNING, obwohl PARTIAL (3567 Rows) existierte.

### C. PARTIAL-Zählwiderspruch (36 vs 24+18)

Kein Datenfehler — **unterschiedliche Nenner**:

| Zahl | Bedeutung |
|------|-----------|
| 36 + 1 RUNNING | naive `argMax(status, started_at)` inkl. Orphan-Tie |
| **37 PARTIAL** | kanonisch terminal-bevorzugend (Orphan als PARTIAL) |
| 24 | Boundary-Audit `PARTIAL_TRUE_GAP` über **alle Quellstunden** |
| **19** | importierte OB200-PARTIAL-**Plansegmente** (8 BTC + 11 DOGE) |
| **18** | OI-PARTIAL-Tage (9 BTC + 9 DOGE) |

**Disjunkt:** 19 OB200 + 18 OI = **37 PARTIAL**.

---

## 3. Repository / Branch / HEAD / Dirty

| Feld | Wert |
|------|------|
| Branch | `feature/btc-doge-research-db` |
| HEAD | `2af0477dc4654d47603a2845a0ea1463b0bcfa12` |
| Dirty | ~67 Dateien (inkl. Idempotenz-Fix uncommitted) |
| ClickHouse | 26.7.1.1315 |
| Disk | 370G frei / 58% used |
| Heartbeat | COMPLETED, 662/662, remaining=0, failed=0 |
| Runner | PID 1248597 beendet (stale OK) |

---

## 4. Tabellenengine und kanonische Schlüssel

| Tabelle | Engine | ORDER BY | Kanonischer Schlüssel |
|---------|--------|----------|------------------------|
| `research_ob200_snapshots_1s` | MergeTree (kein Replacing) | `(symbol, snapshot_ts, producer_id, contract_version, build_id)` | logisch: `(symbol, snapshot_ts)` |
| `research_batch_runs` | MergeTree append-only | `(batch_id, status, phase)` | Status: READY/PARTIAL/FAILED > RUNNING, dann `started_at`, dann `completed_at` |

Keine Annahme über ReplacingMergeTree. Reparatur über exakte DELETE-Prädikate bzw. Append.

---

## 5–7. Duplikate und Orphan — vorher/nachher

| Mutation | Vorher | Nachher |
|----------|--------|---------|
| M1 Aug27 | 7198 / 3599 unique | **3599 / 3599**, Gap 06:42:23 bleibt, PARTIAL |
| M2 Aug24 | 1452 / 726 | **726 / 726** |
| M3 Golden | 7200 / 3600 (2 builds) | **3600 / 3600** (Plan-`build_id` `ecd48675…`) |
| M4 Orphan | naive RUNNING / effektiv PARTIAL | **naive + kanonisch PARTIAL**, 3567s |

Globale Duplikate `(symbol, snapshot_ts)`: **0**.

---

## 8. Endgültige disjunkte PARTIAL-Zählung

**37 PARTIAL** = 19 OB200 + 18 OPEN_INTEREST.  
READY 625. Summe 662.

---

## 9. Exakte echte Source-Gaps (Auszug)

- BTC 2026-08-27 06:00: **fehlt 06:42:23Z**, 3599s, 06:00:00 vorhanden, PARTIAL  
- DOGE 2026-08-31 23:00: 3567/3600, 33 fehlende Sekunden, PARTIAL  
- Weitere 17 OB200 Mid-Hour-PARTIAL-Segmente und 18 OI-Partial-Tage: siehe `partial_reconciliation_after.csv`  
- Keine Imputation, keine PARTIAL→READY-Promotion

---

## 10. Ausgeführte Mutationen

1. `ALTER TABLE … DELETE` Aug27 späterer `computed_at`-Kopie (3599 Rows)  
2. `ALTER TABLE … DELETE` Aug24 `coverage_status='PARTIAL'`-Kopie (726 Rows)  
3. `ALTER TABLE … DELETE` Golden non-plan `build_id` (3600 Rows)  
4. `INSERT` append-only terminal PARTIAL `phase=RECOVERY` für DOGE-Orphan  

Details: `mutation_audit.json`, `repair_plan.json`.

---

## 11. Backup- und Recovery-Nachweis

JSONL-Backups unter `backups/` mit SHA256 in `backup_manifest.json`  
(Loser-Rows vollständig, Winner-Keys, Batch-Metadaten). Rollback = Re-INSERT aus Backup.

---

## 12. Idempotenz-Fix

In `full_history_runner.py` / `segment_loader.py`:

- Terminal-Skip für **READY und PARTIAL**  
- Existing-Rows → Recovery-Terminal ohne Re-Insert  
- Foreign-`build_id` am gleichen `batch_id` → **CONFLICT**  
- Batch-Claim mit `claim:`-Token; genau ein Writer  
- Loader-Guard: OB200-Insert übersprungen wenn `build_id`-Rows existieren  
- PARTIAL/COMPLETE-Semantik unverändert; CANDLES weiter blockiert  

---

## 13. Tests

`tests/research/test_ob200_idempotency.py` + bestehende Suites:  
**42 passed** (`test_results.txt`).

Abgedeckt: Reimport/Audit/Smoke/PARTIAL-Skip, Fingerprint-Konflikt, paralleler Claim, Crash-Recovery, Source-Gaps bleiben PARTIAL, COMPLETE bleibt COMPLETE, CANDLES blockiert.

---

## 14. Post-Repair-Invarianten

Alle erfüllt (`post_repair_invariants.json`):

- 662/662 terminal, RUNNING=0, FAILED=0, MISSING=0  
- READY+PARTIAL=662  
- 0 OB-Duplikatstunden  
- Aug27: 3599 / Gap 06:42:23 / PARTIAL  
- DOGE 23:00: 3567 / PARTIAL  
- Golden BTC+DOGE: EXACT  

---

## 15. Parität

| Fenster | Verdict |
|---------|---------|
| Golden BTC 18:00 | EXACT (3600, 200×200, 1 build) |
| Golden DOGE 18:00 | EXACT |
| Aug27 BTC Gap | SOURCE_GAP (korrekt) |

---

## 16. Performance (Warm p50)

| Query | p50 | Ziel |
|-------|----:|------|
| OB200 BTC 60m | ~4.2 ms | < 5 s |
| OB200 DOGE 60m | ~3.8 ms | < 5 s |
| Fight-Input Join 60m | ~14.7 ms | < 10 s |

---

## 17. Collector-/Live-Sicherheitsbestätigung

- Nur `btc_doge_research` mutiert  
- Kein DROP/TRUNCATE, kein Full-History-Rerun  
- Collector-PIDs unverändert (u.a. 147111 OI/Liq, 3946369 OB raw-archive)  
- Keine Dashboard-/Fight-Engine-Änderung  

---

## 18. Commit / Push

**Kein Commit, kein Push.**

---

## 19. Fight-CLI-Umschaltung

**Noch nicht freigeben.**  

Die Research-DB ist für timestamp-basierte Analysen und Fight-Inputs auf **COMPLETE**-Fenstern bereit. Vor CLI-Cutover fehlen:

1. Eligibility-Gate: PARTIAL-/SOURCE_NOT_AVAILABLE-Fenster ablehnen oder begrenzt zulassen  
2. Kanonische Batch-Statusabfrage (terminal-bevorzugend, nicht naives `argMax` allein)  
3. Optional: Smoke, dass Idempotenz-Fix in CI grün bleibt  

Roharchiv-Replay für abgedeckte COMPLETE-Stunden kann durch die Research-DB ersetzt werden.
