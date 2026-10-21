# PHASE 6XT – 5M PD1 FINAL READBACK & 15M REFERENCE PARITY V1

**VERDICT: `5M_PD1_IMPORT_VALIDATED_VIEWER_READY_FOR_APPROVAL`**

Read-only. Keine Writes, Publishes, Imports, Viewer-Änderungen, Dashboard-Neustarts oder Git-Aktionen.

Nachweisbasis: direkte ClickHouse-Abfragen, `run_pilot_readback` über alle **2592** Stems (`use_clickhouse=True`), Stichproben-Fingerprints, `batches.jsonl` / `run.log`, `build_landscape_window` (September-Regression). `progress.json` nur als Korrelat, nicht als alleiniger PASS.

Artefakt: `logs/historical_import/overnight_2026/phase6xt_5m_pd1_final_readback_v1/validation.json`

---

## 1. Import-Abschluss (ClickHouse)

**Line:** `historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1`  
**Pilot-Staging-Batch (Sidecar):** `c18994e7-77f7-4e74-a688-8a2ec5bbc059`  
**Frozen:** `fe549309ef047ea092bd1fa3eba5c16fae647db7f3fad95dc567b47c66ebcb66`  
**Config-Hash:** `4aeb36fe22ef3ab24fc65c8e7bc33b6ca499dd51759d07936c96fee781c641c9`

| Prüfung | Soll | Ist |
|---|---:|---:|
| Publish `publish_complete=1` | 2592 | **2592** |
| Eindeutige `decision_close` | 2592 | **2592** |
| Eindeutige `snapshot_id` | 2592 | **2592** |
| Spanne | 2026-09-09 00:00 – 2026-09-17 23:55 UTC | **stimmt** |
| Abweichungen `config_hash` / TF / PD / Symbol | 0 | **0** |
| Doppelte `decision_close` im Ledger | 0 | **0** |
| MISSING / EXTRA vs. 2592-Stem-Plan | 0 / 0 | **0 / 0** |
| Meta 5m PD1 `write_complete=1` | 2592 | **2592** |
| Members | 440098 / 2592 Snapshots | **440098 / 2592** |
| Catalog auf 5m-Line | 440098 / 2592 | **440098 / 2592** |
| Member ↔ Catalog `(snapshot_id, pool_id)` | 0 / 0 Setdiff | **0 / 0** |
| Provenance-Zeilen | 2592 | **2592** |
| `frozen_input_id` ≠ fe549309… | 0 | **0** |
| Production-JSON-Dateien | 2592 | **2592** |

**`run_pilot_readback` (JSON ↔ published CH)** für alle 2592 Stems: **`parity_fail_count: 0`**.

**Fingerprints:** Stichprobe jedes 50. Stem + letzter Stem: `fingerprint_spot_bad: 0`.

### Batch-Runner (51 Batches)

| Quelle | Inhalt |
|---|---|
| `run.log` | `batch 51 ok published=2592 todo=0` |
| `progress.json` | `published_now: 2592`, `todo_remaining: 0`, `last_batch: 51` |
| `final_summary.json` / `baseline_end.json` | `line_5m: 2592`, `line_15m: 17568` |
| `batches.jsonl` | **51** Zeilen, alle `readback_ok: true`, **keine** `VALIDATE_FAIL` / `STOP` |

**Stem-Summe in `batches.jsonl`:** 2542 (= 50×50 + 1×42). Die ersten **50** Closes waren vor dem Completion-Resume bereits im Ledger (`published_before: 50` in `progress.json` / `baseline_start.json`). **50 + 2542 = 2592** — konsistent mit Gesamtzähler, kein fehlender Batch-Inhalt.

Letzter Batch: Stems `20260917T203000Z`–`20260917T235500Z`, **42** Closes.

### Prozessstatus

Zum Prüfzeitpunkt: **keine** aktiven Prozesse `run_completion_import` / `execute_batch_import`. Completion-Lauf in `run.log` bis Batch 51 abgeschlossen (kein separater Exit-Code in der Logdatei; Abbruch wäre in `run.log` / fehlender Batch 51 sichtbar).

---

## 2. 15m-Referenz (unverändert)

| Prüfung | Ergebnis |
|---|---|
| 15m-PD1 Publish | **17568** |
| `config_hash` 15m | **`4aeb36fe…`** (unverändert) |
| Stichprobe 2026-09-14 21:45 | Snapshot `3c6bacc268daa59b…`, Fingerprint `71cc7b3ee8f4f0ea…`, Prefix-Candle `4da21e346a9b4b01…` |
| September-Regression | **`build_landscape_window("2026-09-14T21:45:00Z","2026-09-15T05:45:00Z")` → 97 Preise**, `phase5s_batch_reader_active=true` |
| Regression-Line | **`historical_mixed_frozen_doge_apr_sep_2026_v1_15mpd1_v1`** (15m, nicht 5m) |

**Window-API-Pfad:** unverändert — Body nur `from_utc` / `to_utc` (`pd1_landscape_viewer_v1/api.py` 112–116), Auflösung über `resolve_historical_pd_config(1)` → 15m-Line (`historical_lmv2_pd_contract_v1.py` 14–20).

**Density / Canvas / Tiles / Slider:** in dieser Phase nicht geändert; dieselben Module wie in `PHASE_6XT_5M_STRICT_15M_REFERENCE_TRACE_V1.md` (`computeExactBandsForSide`, `drawEodDensity`, `planExactOriginFixedTiles`, `filterSegmentsByMinOrigins`, Paletten unverändert).

---

## 3. 5m-Chart-Readiness (nur Parametrisierung, keine Implementierung)

Verbindliche Kette: Original-JSON → Publish-Ledger `…_5mpd1_v1` → `PublishedActiveReader` → `POST /live-charts/viewer/api/window` → TRP-Adapter → Canvas. **Kein** Staging-Lesen.

Die **2592** publizierten 5m-Closes erfüllen die Daten-SSOT-Voraussetzung. Der Viewer wählt sie heute **nicht**, weil 15m fest verdrahtet ist.

### Minimale Parametrisierung (kleinster Viewer-Pilot)

**Ziel-Fenster (sichtbar):** `2026-09-16T00:00:00Z` – `2026-09-17T23:55:00Z` (**576** Closes, ein 48h-UTC-Tile).  
**Membership:** bestehendes **`WARMUP_HOURS = 168`** in `build_landscape_window` — Kontext ab **2026-09-09** (2016 Closes), bereits im Import enthalten.

| Schicht | Heute (15m Default) | Minimale Erweiterung für 5m PD1 |
|---|---|---|
| PD-Contract | `_PD_REGISTRY` nur 15m (`historical_lmv2_pd_contract_v1.py` 17–20) | Eintrag oder Resolver `(pool_detail=1, timeframe="5m")` → `…_5mpd1_v1`, gleicher `config_hash` |
| `build_landscape_window` | `_published_pd1()` → 15m-Line (`historical_window_v1.py` 134–137) | Parameter `timeframe` (Default **`"15m"`**) steuert Line + Membership-Raster |
| Membership-Raster | `_causal_15m_decision_close` (`historical_window_v1.py` 93–96, 267) | Bei `timeframe=="5m"`: Floor auf **5 Minuten** (300 s), nicht `minute // 15` |
| API | nur `from_utc`/`to_utc` | Optional `timeframe`, `pool_detail` im Body; Default 15m/1 |
| Capabilities | `landscape_capabilities()` hardcoded 15m-Line | Gleiche Felder, Line abhängig von TF |
| Research-UI | `paneSupports`: `pane.tf === "15m"` | 5m nur wenn explizit gewählt; **15m bleibt Default** |
| JS-Adapter | `causalDecisionCloseMs` ÷15 (`pd1_exact_density_trp_adapter.js` 17–20) | Bei 5m-Pane: ÷5 (oder TF aus Capabilities) |

**Unverändert lassen:** `computeExactBandsForSide`, Depth-Buckets, Farben, Opacity, `drawEodDensity`, `planExactOriginFixedTiles` (48h-Grid), Min-Depth-Slider, Preisachse (bereits 5m-Kerzen aus CH).

**Pilot-Testplan (nach separater Viewer-Freigabe):**

1. `POST /live-charts/viewer/api/window` mit `from_utc`/`to_utc` sichtbar + impliziter 168h-Warmup serverseitig.
2. **576** sichtbare Pool-Zustände, ältere ACTIVE Origins innerhalb 4-Monats-Fenster pro Close.
3. Origin-Sweep, 48h-Tile-Parity, JS/Python-Band-Parity wie bei 15m — gleiche Funktionen, nur 5m-Slot.

---

## 4. Abweichungen / Hinweise

- Der Completion-Runner (`run_completion_import.py`) nutzte `execute_batch_import` (15m-Monatsstil, Publish pro Batch). Das ist **abgeschlossen** und CH-seitig **vollständig validiert**; die ursprüngliche Pilot-Forderung „aggregierter Readback vor erstem Publish“ wurde durch den Resume-Pfad historisch nicht eingehalten — für **diesen** Abschlussbericht zählt der **aktuelle** Vollbestand.
- Sidecar zeigt **`uniqExact(import_batch_id)=1`** (Pilot-UUID); Completion-`import_batch_id`-Strings in `BatchSpec` werden bei idempotentem Sidecar nicht überschrieben (`provenance_identity.py` 28–29) — forensisch erwartbar, Canonical-Felder konsistent.

---

## 5. Verdict

**`5M_PD1_IMPORT_VALIDATED_VIEWER_READY_FOR_APPROVAL`**

- ClickHouse-Import: **2592/2592** Publish, Meta, Sidecar, Catalog/Member-Parität, **0** Readback-Fehler über alle Stems.
- 15m-Baseline und 97-Regression: **PASS**.
- Viewer: Daten auf der 5m-Line **bereit**; **minimale TF/Line-Parametrisierung** dokumentiert — Implementierung erfordert **separate Freigabe**.

**STOP.**
