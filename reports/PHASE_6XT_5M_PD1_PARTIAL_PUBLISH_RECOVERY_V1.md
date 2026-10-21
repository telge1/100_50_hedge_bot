# PHASE 6XT – 5M PD1 PARTIAL PUBLISH FORENSICS & RECOVERY PLAN V1

**VERDICT: `PUBLISH_INTEGRITY_BLOCKED`**

Read-only-Forensik und Planung. Keine ClickHouse-Writes, kein Publish, keine Löschungen, kein Dashboard-/Queue-Neustart, kein Git.

Der freigegebene Controlled-Pilot (`stage_catalog_sidecar_only.py`) hat korrekt **ohne** Ledger geschrieben. Danach lief ein **nicht freigegebener** Completion-Runner (`run_completion_import.py`), der den 15m-Standardpfad `execute_batch_import` → `import_historical_close` → `commit_publish_ledger` nutzt und den Ledger **während der Forensik weiter befüllt**. Der vom Nutzer bestätigte Stand „50 veröffentlicht“ ist überholt.

**Forensik-Snapshot (wächst):**

| Messpunkt | Wert |
|---|---|
| Reconcile-JSON geschrieben | 791 veröffentlichte Closes, 1801 fehlend (lückenloser Suffix) |
| Direktabfrage kurz danach | **945** veröffentlichte Closes (`dup_dc = 0`) |
| Aktiver Prozess | PID **1570799** `python3 …/run_completion_import.py` (gestartet 2026-10-21 10:18:14 Lokalzeit, Laufzeit während Prüfung >5 min) |

Bis der Prozess gestoppt und der Ledger erneut gezählt ist, ist **keine** sichere Missing-only-Freigabe möglich.

---

## Phase A – Forensik der Ledger-Einträge

### A1 – Mechanismus (bewiesen)

Publish-Zeilen entstehen ausschließlich über den vorhandenen Importer:

```148:154:spread_recovery_hedge_short_dev/dashboard/research_charts/live_micro_lld_v2_ch_test/historical_json_import/import_close.py
    pub = commit_publish_ledger(
        state,
        pipeline_line_id=pipeline_line_id,
        line=line,
        faults=faults,
    )
```

`execute_batch_import` ruft das pro Stem auf:

```152:160:spread_recovery_hedge_short_dev/dashboard/research_charts/live_micro_lld_v2_ch_test/historical_json_import/batch_write.py
        outcome = import_historical_close(
            state,
            bundle["catalog_rows"],
            pipeline_line_id=spec.pipeline_line_id,
            test_run_id=run_id,
            line=line,
            sidecar_store=store,
            sidecar_row=sidecar_row,
        )
```

Der freigegebene Staging-Lauf **ruft `commit_publish_ledger` nicht auf** (nur Staging, Katalog, Sidecar):

```122:152:logs/historical_import/overnight_2026/phase6xt_5m_pd1_controlled_release_pilot_v1/stage_catalog_sidecar_only.py
            staged = write_active_snapshot_staged(state, test_run_id=BATCH)
            cat = write_active_pool_catalog_from_json_rows(...)
            ...
            side = store.write_and_verify(row)
            store.require_before_publish(row)
```

`stage_summary.json` meldet am Ende `publish_count: 0` (2026-10-21 ~08:12:02 UTC). `stage_progress.jsonl` zeigt `publish_count: 0` bei `done: 2592`.

### A2 – Erste 50 Zeilen (2026-09-09 00:00–04:05 UTC)

| Feld | Beweis |
|---|---|
| `decision_close` | `20260909T000000Z` … `20260909T040500Z` (50 aufeinanderfolgende 5m-Slots) |
| `created_at` (Ledger) | `2026-10-21T08:13:04.751` … `2026-10-21T08:13:15.132` UTC |
| `publish_complete` | 1 |
| Sidecar `import_batch_id` | überall **`c18994e7-77f7-4e74-a688-8a2ec5bbc059`** (Pilot-Batch, nicht Completion-Präfix) |
| Fingerprints | Controlled-Report: 0 Mismatches Publish↔Meta für die ersten 50 |

**Nicht belegt:** welcher Shell-Befehl/Prozess exakt um 08:13 UTC die erste `execute_batch_import`-Batch (50 Closes) ausgelöst hat. Es gibt **kein** passendes Log-Artefakt in `phase6xt_5m_pd1_controlled_release_pilot_v1/`. Der Mechanismus ist `import_historical_close` → `commit_publish_ledger`; asynchroner CH-Publish existiert nicht.

### A3 – Warum Staging „0“ meldete, danach aber Ledger-Zeilen da waren

Der Staging-Lauf prüft `publish_count()` nur bei `done % 100 == 0` oder am Ende (`stage_catalog_sidecar_only.py` Zeilen 163–169). Nach Prozessende war der Zähler **0**. Die ersten 50 Ledger-Inserts liegen **nach** diesem Zeitpunkt (08:13 UTC). Das schließt einen verzögerten/asynchronen Publish im Staging-Skript aus; es belegt einen **zweiten Writer** nach Staging-Ende.

### A4 – Completion-Runner (bewiesen)

Datei: `logs/historical_import/overnight_2026/phase6xt_5m_pd1_ch_import_completion_v1/run_completion_import.py`

- Zeile **187**: `execute_batch_import(CAT, spec, test_run_id=RUN_UUID)`
- Batch-Größe: `DEFAULT_BATCH_SIZE_CLOSES` (= **50**, `historical_batch_config.py`)
- `RUN_UUID`: `a8f3c2e1-4b5d-4a6e-9f0c-1d2e3f4a5b6c` (nicht die Pilot-UUID)
- `baseline_start.json`: beim Start bereits **`line_5m: 50`**
- `batches.jsonl` Batch 1: Stems `20260909T041000Z`–`20260909T081500Z`, `readback_ok: true`, `at: 2026-10-21T08:18:37Z` (passt zu Ledger-`created_at` ab Stem 51 ab ~08:18:21 UTC)

`progress.json` (während Forensik): `published_now: 650`, `last_batch: 12`, `todo_remaining: 1942` – der Lauf war **nicht** abgeschlossen.

Dieser Runner ist **nicht** Gegenstand der Freigabe in `PHASE_6XT_5M_PD1_CONTROLLED_RELEASE_PILOT_V1.md` (dort: aggregierter Readback **vor** jedem Publish; `execute_batch_import` explizit vermeiden bis Gate PASS).

### A5 – Aktive Writer / Transaktionen

Zum Prüfzeitpunkt: **ein** relevanter Import-Prozess aktiv (`run_completion_import.py`, PID 1570799). Kein separater Generator. Dashboard `app.py` unverändert laufend (nicht neu gestartet).

`historical_import_write_lock` (`batch_write.py` Zeilen 100–108) serialisiert `execute_batch_import` pro Host; parallele Completion + Staging wären zeitlich durch den abgeschlossenen Staging-Lock getrennt, nicht parallel im selben kritischen Abschnitt.

### A6 – Zugehörigkeit zum freigegebenen Batch

- Staging/Catalog/Sidecar für alle **2592** Closes: Pilot-UUID `c18994e7-77f7-4e74-a688-8a2ec5bbc059`
- Veröffentlichte Closes (Stichprobe): Sidecar zeigt dieselbe UUID; Completion-`import_batch_id` in `BatchSpec` wird beim Re-Import **nicht** in CH überschrieben, wenn die Sidecar-Zeile schon existiert (`provenance_identity.py` Zeilen 28–29: `import_batch_id` ist Resume-Metadaten; kanonische Felder müssen matchen)

### A7 – Parität Publish / Catalog / Original

Controlled-Pilot-Readback (vor Block): Meta 2592, Catalog 440098, Sidecar 2592, JSON↔Catalog 440098/440098, keine Duplikate `(snapshot_id, pool_id)`. Für veröffentlichte Prefix-Closes gilt `run_batch_readback_gate` im Completion-Lauf pro Batch (`readback_ok: true` in `batches.jsonl`).

### A8 – Zusätzliche Einträge seit Controlled-Report

Ja. Report stand bei **50** Ledger-Zeilen; Forensik zeigt **791+** (live **945+**) mit lückenlosem Prefix bis `20260911T175000Z`, danach Suffix fehlend ab `20260911T175500Z`.

### A9 – 15m-Referenz Readback

15m nutzt nach jedem Write-Batch:

```112:128:spread_recovery_hedge_short_dev/dashboard/research_charts/live_micro_lld_v2_ch_test/historical_json_import/batch_orchestrator.py
        write_out = execute_batch_import(...)
        rb = run_batch_readback_gate(
            list(spec.stems),
            ...
            require_sidecar=True,
        )
```

`run_batch_readback_gate` → `run_pilot_readback(..., use_clickhouse=True)` (`batch_runner.py` Zeilen 315–322). **Kein** aggregierter „alle 17568 vor dem ersten Publish“-Gate in der 15m-Monatskette; der 5m-Pilot hatte zusätzlich eine **explizite** Freigabe-Vorgabe (aggregiert vor Publish), die der Completion-Runner umgeht.

---

## Phase B – Read-only Abgleich EXPECTED / PUBLISHED / MISSING / EXTRA / DUPLICATES

**EXPECTED:** 2592 Stems `20260909T000000Z` … `20260917T235500Z` (5m-Raster).

**Stand Reconcile-Datei** (`logs/.../phase6xt_5m_pd1_partial_forensics_v1/reconcile.json`, ~Forensikmitte):

| Menge | Anzahl | Detail |
|---|---:|---|
| EXPECTED | 2592 | |
| PUBLISHED | 791 | **live weiter steigend** |
| MISSING | 1801 | **lückenloser Suffix** ab `20260911T175500Z` |
| EXTRA | 0 | |
| DUPLICATE `decision_close` im Ledger | 0 | |

**Erster / letzter Publish (Reconcile):**

- Erster: `20260909T000000Z`, `created_at` `2026-10-21T08:13:04.751Z`
- Letzter im Prefix: `20260911T175000Z`, `created_at` `2026-10-21T08:22:26.964Z`

**Catalog / Sidecar / Meta (unverändert vollständig):**

| Layer | Ist |
|---|---|
| Meta 5m PD1 `write_complete=1` | 2592 |
| Catalog `…_5mpd1_v1` | 440098 Zeilen, 2592 `snapshot_id` |
| Provenance | 2592 Zeilen (Pilot-Batch-UUID in Sidecar) |

**Production-Dateien / Provenance-Lookup:** unverändert zu Controlled-Pilot (2592 Dateien, Coverage-SHA `17aedaa3…`).

Die frühere Angabe „2542 fehlend“ gilt nur, wenn exakt **50** publiziert sind. **Aktuell fehlen ~1647–1801+** (je nach Zählmoment); Missing ist ein **einzelner zusammenhängender Suffix**, keine Lücken im MISSING-Set (`missing_internal_gaps: 0`).

---

## Phase C – Root Cause (mit Pfaden)

| # | Ursache | Beleg |
|---|---|---|
| 1 | Freigegeben war Staging ohne Publish; ein separater Pfad rief `execute_batch_import` auf | `stage_catalog_sidecar_only.py` vs. `run_completion_import.py:187` |
| 2 | Jeder Stem publish sofort in `import_historical_close` | `import_close.py:148-154` |
| 3 | Batchgröße 50 erklärt Publish-Schritte und Completion-Batches | `DEFAULT_BATCH_SIZE_CLOSES`, `batches.jsonl` |
| 4 | Staging-Ende `publish_count=0` korrekt; Ledger danach durch anderen Writer | Zeiten 08:12 vs. 08:13 UTC |
| 5 | Completion-Resume überspringt bereits publizierte Stems (`published_stems`, `todo`) | `run_completion_import.py:149-150, 221-222` |
| 6 | Verstoß gegen Pilot-Freigabe: Publish vor aggregiertem Gate | Controlled-Report + User-Freigabetext |
| 7 | Aktiver unautorisierter Writer während Forensik | PID 1570799 |

**Nicht als Beweis:** exakte PID/Shell für die **ersten** 50 Zeilen um 08:13 UTC (kein Log).

**Kein** Hinweis auf Retry/Timeout innerhalb CH; Abbruch/Resume ist prozesseitig (`progress.json`, erneuter `run_completion_import`-Start um 10:18).

---

## Phase D – Minimaler Recovery-Plan (nur nach separater Freigabe)

### D0 – Voraussetzung (operativ, nicht in dieser Phase ausgeführt)

1. **Stoppen** von PID 1570799 (`run_completion_import.py`) und Verifikation: kein weiterer `execute_batch_import`/`commit_publish_ledger` auf `…_5mpd1_v1`.
2. **Neu zählen:** `PUBLISHED`, `MISSING`, `EXTRA`, `DUPLICATES` (identisches Skript wie Phase B).

### D1 – Was nicht tun

- Keine Überschreibung bestehender Ledger-Zeilen (`resolve_existing_publish` / `allow_existing_publish=True` in `publisher.py`)
- Keine erneute Dateikopie, kein Catalog-Rewrite, keine neuen Snapshots
- **`run_completion_import.py` nicht** ohne neue Freigabe weiterlaufen lassen (widerspricht aggregiertem Gate der Pilot-Freigabe)
- 15m-Line nicht anfassen

### D2 – Zulässiger 15m-Mechanismus für fehlende Closes only

**Variante A (empfohlen, minimal Write-Surface):** Neues einmaliges Skript (nur nach Freigabe), das **nur** für Stems in `MISSING`:

1. `build_import_bundle` / bestehenden `ActiveStateSnapshot` aus CH-Meta nutzen
2. `sidecar_store.require_before_publish` (Sidecar existiert)
3. `commit_publish_ledger` (`publisher.py`) – **kein** erneutes Catalog-Insert
4. `resolve_existing_publish` überspringt bereits veröffentlichte Prefix-Closes

Danach: **aggregierter** Readback aller 2592 + 15m-Baseline + 97-Preise (wie Controlled-Pilot).

**Variante B (15m-Monatsstil):** `BatchOrchestrator` / `execute_batch_import` nur auf `MISSING`-Stems in Batches à 50, jeweils `run_batch_readback_gate` **nach** dem Batch — aber nur, wenn die Freigabe ausdrücklich **pro-Batch-** statt **aggregiert-vor-erstem-Publish** erlaubt (Pilot-Freigabe war aggregiert).

### D3 – Minimale Prozess-Korrektur (keine neue Architektur)

- Ein **einziger** Writer; `historical_import_write_lock` beibehalten
- Kein paralleles `run_completion_import` neben manuell freigegebenen Schritten
- Dokumentierte Batch-UUID pro Freigabe-Lauf (neue UUID, nicht `a8f3c2e1…` Gate-Resume ohne Freigabe)

Wenn nach Stop + Recount Fingerprints oder Catalog-Lücken auftreten: **STOP** — dann `PUBLISH_INTEGRITY_BLOCKED` bis manuell geklärt (kein blindes Resume).

---

## Phase E – Window-API-Readiness (read-only)

| Komponente | 15m-PD1 heute | 5m-PD1 später |
|---|---|---|
| Publish-Line | `resolve_historical_pd_config(1)` → `…_15mpd1_v1` | `…_5mpd1_v1` existiert im Importer, **nicht** im PD-Contract |
| Contract | `historical_lmv2_pd_contract_v1.py` Zeilen 14–20: nur 15m in `_PD_REGISTRY` | 5m-Eintrag fehlt |
| Window-Builder | `_published_pd1()` → `resolve_historical_pd_config` (`historical_window_v1.py` 134–137) | liest immer 15m-Ledger |
| Membership-Raster | `_causal_15m_decision_close` (`historical_window_v1.py` 93–96) | für 5m-LMV2: 5-Minuten-Slot (300 s), nicht `minute // 15` |
| API Body | `api.py` 112–116: nur `from_utc`, `to_utc` | kein `timeframe` / `pool_detail` |
| Research-UI | `paneSupports`: `pane.tf === "15m"` (`pd1_exact_origin_density_research_v1.js` 147–151) | 5m wird abgewiesen |
| JS-Adapter | `causalDecisionCloseMs`: 15-Minuten-Floor (`pd1_exact_density_trp_adapter.js` 17–20) | bei 5m: Floor auf 5 Minuten |
| Density/Canvas | `computeExactBandsForSide`, `drawEodDensity`, Buckets, Farben, Min-Depth | **unverändert**; nur Datenquelle + Slot-Breite |

**Minimale Parametrisierung (Konzept, keine Implementierung hier):**

1. API/Capabilities: optional `timeframe` (Default **`15m`**) + `pool_detail` (Default **1**)
2. `resolve_historical_pd_config` oder paralleler Resolver: `(pd, timeframe) → pipeline_line_id + config_hash`
3. `build_landscape_window`: `_causal_*_decision_close` abhängig von `timeframe` (900 s vs. 300 s)
4. `paneSupports` + ggf. Capability-Endpoint: 5m nur wenn Ledger auf `…_5mpd1_v1` für das Fenster existiert

15m bleibt Default überall, wo Parameter fehlen.

---

## Phase F – Regression & Freigabegrenzen

| Prüfung | Ergebnis (Forensik) |
|---|---|
| 15m-PD1-Baseline | 17568, Hash `4aeb36fe…` |
| September-Regression 97 Preise | PASS, Line `…_15mpd1_v1`, `phase5s_batch_reader_active=true` |
| Production-Guard Dateien/Provenance | 2592 Dateien; Coverage-SHA unverändert |
| Staging/Catalog/Sidecar | 2592 / 440098 / 2592 vollständig |
| Queue 4h PD1 | `pilot_verdict.json`: **BLOCKED** (unverändert, kein Eingriff) |
| Unerwarteter Writer | **`run_completion_import.py` aktiv** → Integrität blockiert |

### Konkreter nächster kontrollierter Freigabeschritt (Vorschlag)

1. **STOP** Completion-Runner; Ledger-Zähler stabilisieren.
2. Read-only Recount + Bericht `MISSING`/`PUBLISHED`/`EXTRA`/`DUPLICATES`.
3. Stichproben-`run_pilot_readback` auf allen bisher publizierten Prefix-Stems + JSON-Parität.
4. **Neue explizite Freigabe** nur für **Publish der dann ermittelten MISSING-Menge** (Variante A: `commit_publish_ledger` only).
5. Aggregierter Gate (2592 + 15m + 97) **vor** weiterem Publish, falls noch MISSING>0 nach Stop.
6. Erst danach Window-API-Parametrisierung (separate Freigabe).

---

## Artefakte

| Pfad | Inhalt |
|---|---|
| `logs/.../phase6xt_5m_pd1_controlled_release_pilot_v1/stage_summary.json` | Staging Ende, `publish_count: 0` |
| `logs/.../phase6xt_5m_pd1_controlled_release_pilot_v1/pre_publish_readback.json` | Erster Gate-Fail bei 50 |
| `logs/.../phase6xt_5m_pd1_ch_import_completion_v1/run_completion_import.py` | Unauthorized Completion |
| `logs/.../phase6xt_5m_pd1_ch_import_completion_v1/batches.jsonl` | Batch-Protokoll |
| `logs/.../phase6xt_5m_pd1_ch_import_completion_v1/progress.json` | Resume-Status |
| `logs/.../phase6xt_5m_pd1_partial_forensics_v1/reconcile.json` | EXPECTED/PUBLISHED-Snapshot |

---

## Verdict-Begründung

- **`PUBLISH_INTEGRITY_BLOCKED`:** aktiver, nicht freigegebener Publish-Writer; Ledger-Zustand driftet (50 → 791 → 945+); Verstoß gegen „aggregierter Readback vor Publish“ der Pilot-Freigabe.
- Nicht `READY_FOR_SAFE_MISSING_ONLY_PUBLISH_APPROVAL`: MISSING-Menge ist nicht stabil und Writer läuft.
- Nicht `PUBLISH_ROOT_CAUSE_UNRESOLVED`: Hauptmechanismus und Completion-Runner sind belegt; nur der **allererste** 50er-Trigger um 08:13 UTC ohne Shell-Log offen.

**STOP** – keine weiteren Writes in dieser Phase.
