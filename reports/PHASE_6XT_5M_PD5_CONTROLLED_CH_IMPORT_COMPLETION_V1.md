# PHASE 6XT – 5M PD5 CONTROLLED CH IMPORT COMPLETION V1

**Date:** 2026-10-21  
**VERDICT:** **`5M_PD5_CH_IMPORT_COMPLETE_PASS`**

Restimport der vorbereiteten **2542** Closes auf Line `historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd5_v1` (nach **50** Pilot-Closes). Referenz: 5m-PD1-Completion (`phase6xt_5m_pd1_ch_import_completion_v1`).

**Artefakte:** `logs/historical_import/overnight_2026/phase6xt_5m_pd5_ch_import_completion_v1/`

| Datei | Inhalt |
|-------|--------|
| `run_completion_import.py` | Idempotenter Batch-Runner (50er Batches) |
| `final_validate.py` | Unabhängiger Full-Line-Readback |
| `run.log` | Konsolenprotokoll (inkl. Resume) |
| `batches.jsonl` | Pro-Batch validate/import/readback |
| `progress.json` | `published_now: 2592`, `todo_remaining: 0` |
| `baseline_start.json` / `baseline_end.json` | CH-Baselines |
| `final_validation.json` | Aggregierter Readback |
| `final_summary.json` | Abschluss-Snapshot |

---

## 1. Preflight (verbindlich)

| Check | Ergebnis |
|-------|----------|
| Plan-Stems | **2592** (`2026-09-09` … `2026-09-17` 23:55 UTC) |
| Disk `5m_PD5` | **2592/2592** |
| `validate_bundle` (voller Plan via Runner-Preflight) | **0** Fehler |
| Frozen-Input | **`fe549309…`** |
| PD5 Config-Hash | **`533e04c7…`** |
| Publish **vor** Completion (Pilot) | **50** |
| Fehlende Closes (CH) | **2542** |
| `…_5mpd1_v1` | **2592** |
| `…_15mpd1_v1` | **17568** |
| Write-Gates | gesetzt |

---

## 2. Ausführung

| Feld | Wert |
|------|------|
| **test_run_id (stable)** | `d5e6f7a8-9b0c-4d1e-8f2a-3b4c5d6e7f8a` |
| **import_batch_id pattern** | `phase6xt_5m_pd5_ch_import_completion_v1_{run_uuid}_batch{NN}` |
| **Combo** | `StrategyCombo("5m", 5)` → `5m_PD5` |
| **Batch-Größe** | **50** |
| **Completion-Batches** | **50** (49×50 + 1×42 neue Publishes) |
| **Pipeline pro Batch** | `validate_batch` → `execute_batch_import` → `run_batch_readback_gate(require_sidecar=True)` |

**Pilot (unverändert):** `phase6xt_5m_pd5_ch_import_pilot_v1_8f3fee07-…` — 50 Closes `20260909T000000Z`–`20260909T040500Z`.

**Erster Completion-Stem:** `20260909T041000Z` (direkt nach Pilot).

**Letzter Stem:** `20260917T235500Z`.

### Unterbrechungen / Resume

1. **VALIDATE_FAIL (batch 01):** Ungültige Platzhalter-`RUN_UUID` (`c4d5e6f7-…` — keine gültige UUID). **Kein CH-Write.** UUID auf `d5e6f7a8-…` korrigiert.
2. **ProductivePrewriteGateError** (`orphan members without catalog`) bei Batch **39** und **46:** CH-Sichtbarkeits-Lag nach Batch-Write; `audit_snapshot_layers` zeigte vollständige Member/Catalog. **Resume** mit gleichem Lock/Runner — Batches idempotent fortgesetzt, **keine** fremden Lines berührt.

`progress.json` dokumentiert Resume (`last_batch: 50`, `todo_remaining: 0`).

---

## 3. Import-Ergebnis (Gesamt)

| Segment | Closes |
|---------|--------:|
| Kontext (vor 2026-09-16 00:00Z) | **2016** |
| Sichtbar (2026-09-16 … 2026-09-17 23:55Z) | **576** |
| **Publish `…_5mpd5_v1`** | **2592** |

| Layer (CH, unabhängiger Readback) | Ist | Soll |
|-----------------------------------|----:|-----:|
| Publish / Meta / Sidecar (distinct) | **2592** | **2592** |
| Member / Catalog | **474841** | **474841** |
| Eindeutige `snapshot_id` | **2592** | **2592** |
| `run_pilot_readback` (2592 Stems) | **0** fails | **0** |

---

## 4. Regression

| Line | Nach Abschluss |
|------|----------------:|
| `…_15mpd1_v1` | **17568** |
| `…_5mpd1_v1` | **2592** |
| `…_5mpd5_v1` | **2592** |
| Ref 15m `3c6bacc2…` / FP `71cc7b3e…` | **unverändert** |

---

## 5. Tests

```text
pytest test_batch_import_phase4a.py test_historical_lmv2_pd_contract_v1.py test_phase4k_prewrite_and_multibatch.py
→ 27 passed
```

---

## 6. Geänderte Dateien (diese Phase)

| Pfad | Änderung |
|------|----------|
| `logs/.../phase6xt_5m_pd5_ch_import_completion_v1/*` | Runner, Validierung, Logs (neu) |
| ClickHouse | **+2542** Publish auf `…_5mpd5_v1` (Pilot +50 bereits vorher) |

**Nicht geändert:** Coverage-Matrix, 5m/15m-PD1-Daten, Viewer, PD10, Generator.

---

## 7. Offene Risiken / Nächster Schritt

- **Viewer:** `ALLOWED_VIEWER_SCOPES` / Contract-5m-PD5-Line noch nicht für Dashboard freigegeben — separater Schritt nach Readback-Freigabe.
- **Completion-Resume:** Bei erneutem `ProductivePrewriteGateError` zuerst CH-Layers prüfen, dann **Resume** (kein zweiter Parallel-Writer).
- **Nächster freigegebener Scope (nicht Teil dieser Phase):** ggf. weiterer isolierter Pilot/Completion für **5m PD10** oder CH-Readback-Gates für Viewer-Aktivierung.

**STOP** — Completion abgeschlossen und unabhängig validiert.
