# PHASE 6XT – 5M PD10 CONTROLLED CH IMPORT COMPLETION V1

**Date:** 2026-10-21  
**VERDICT:** **`5M_PD10_CH_IMPORT_COMPLETE_PASS`**

Restimport der vorbereiteten **2542** Closes auf Line `historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd10_v1` (nach **50** Pilot-Closes). Referenz: 5m-PD5-Completion (`phase6xt_5m_pd5_ch_import_completion_v1`).

**Artefakte:** `logs/historical_import/overnight_2026/phase6xt_5m_pd10_ch_import_completion_v1/`

| Datei | Inhalt |
|-------|--------|
| `run_completion_import.py` | Idempotenter Batch-Runner (50er Batches) |
| `final_validate.py` | Unabhängiger Full-Line-Readback |
| `run.log` | Konsolenprotokoll |
| `batches.jsonl` | **51** Completion-Batches (validate/import/readback) |
| `progress.json` | `published_now: 2592`, `todo_remaining: 0` |
| `baseline_start.json` / `baseline_end.json` | CH-Baselines |
| `final_validation.json` | Aggregierter Readback |
| `final_summary.json` | Abschluss-Snapshot |

---

## 1. Preflight (verbindlich)

| Check | Ergebnis |
|-------|----------|
| Plan-Stems | **2592** (`2026-09-09` … `2026-09-17` 23:55 UTC) |
| Disk `5m_PD10` | **2592/2592** |
| `validate_bundle` (voller Plan via Runner-Preflight) | **0** Fehler |
| Frozen-Input | **`fe549309…`** |
| PD10 Config-Hash | **`bd9ac4f3…`** |
| Publish **vor** Completion (Pilot) | **50** |
| Fehlende Closes (CH, tatsächlich) | **2542** |
| `…_5mpd5_v1` / `…_5mpd1_v1` / `…_15mpd1_v1` | **2592** / **2592** / **17568** |
| Write-Gates | gesetzt |

---

## 2. Ausführung

| Feld | Wert |
|------|------|
| **test_run_id (stable)** | `e6f7a8b9-0c1d-4e2f-9a3b-4c5d6e7f8a9b` |
| **import_batch_id pattern** | `phase6xt_5m_pd10_ch_import_completion_v1_{run_uuid}_batch{NN}` |
| **Combo** | `StrategyCombo("5m", 10)` → `5m_PD10` |
| **Batch-Größe** | **50** (max.) |
| **Completion-Batches** | **51** (50×50 + 1×42 neue Publishes) |
| **Pipeline pro Batch** | `validate_batch` → `execute_batch_import` → `run_batch_readback_gate(require_sidecar=True)` |
| **Wall time (Completion)** | ~**20 min** (`batches.jsonl` 11:13:06Z … 11:33:28Z) |

**Pilot (unverändert):** `phase6xt_5m_pd10_ch_import_pilot_v1_f6f651d0-d811-4ee8-8feb-c8f65203d145` — 50 Closes `20260909T000000Z`–`20260909T040500Z`.

**Erster Completion-Stem:** `20260909T041000Z` (direkt nach Pilot).

**Letzter Stem:** `20260917T235500Z`.

### ClickHouse-Lag / Resume

In diesem Lauf **kein** `ProductivePrewriteGateError` und **kein** manuelles Resume nötig (ein durchgängiger Runner-Durchlauf). Resume-Loop im Shell-Wrapper vorhanden für Abbrüche analog PD5; nicht ausgelöst.

---

## 3. Import-Ergebnis (Gesamt)

| Segment | Closes |
|---------|--------:|
| Kontext (vor 2026-09-16 00:00Z) | **2016** |
| Sichtbar (2026-09-16 … 2026-09-17 23:55Z) | **576** |
| **Publish `…_5mpd10_v1`** | **2592** |

| Layer (CH, unabhängiger Readback) | Ist | Soll |
|-----------------------------------|----:|-----:|
| Publish / Meta / Sidecar (distinct) | **2592** | **2592** |
| Member / Catalog | **577352** | **577352** |
| Eindeutige `snapshot_id` | **2592** | **2592** |
| `run_pilot_readback` (2592 Stems) | **0** fails | **0** |

---

## 4. Regression

| Line | Nach Abschluss |
|------|----------------:|
| `…_15mpd1_v1` | **17568** |
| `…_5mpd1_v1` | **2592** |
| `…_5mpd5_v1` | **2592** |
| `…_5mpd10_v1` | **2592** |
| Ref 15m `3c6bacc2…` / FP `71cc7b3e…` | **unverändert** (`final_validation.json`) |

**September-Regression (15m Referenz):** 15m-Ref-Snapshot und Fingerprint unverändert; dedizierte `build_landscape_window`-97-Preis-Probe in dieser Phase nicht erneut ausgeführt (Viewer-Contract-Import lokal nicht geladen — unverändert zu PD5-Completion-Bericht, der ebenfalls auf CH-Ref + pytest setzte).

---

## 5. Tests

```text
pytest test_batch_import_phase4a.py test_historical_lmv2_pd_contract_v1.py test_phase4k_prewrite_and_multibatch.py
→ 28 passed
```

---

## 6. Geänderte Dateien (diese Phase)

| Pfad | Änderung |
|------|----------|
| `logs/.../phase6xt_5m_pd10_ch_import_completion_v1/*` | Runner, Validierung, Logs (neu) |
| ClickHouse | **+2542** Publish auf `…_5mpd10_v1` (Pilot +50 bereits vorher) |

**Nicht geändert:** Repo-Importer-Code (Allowlist `5m`/10 aus Pilot), Coverage-Matrix, 5m-PD1/PD5-Daten, 15m-Daten, Viewer, Generator, Git.

---

## 7. Nächster sicherer Freigabeschritt

- **Viewer / Contract:** `ALLOWED_VIEWER_SCOPES` und Dashboard-Checkboxen für **5m PD5/PD10** separat freigeben (CH-Bestand ist vollständig).
- **Kein** weiterer historischer Import ohne neue Freigabe.

**STOP** — Completion abgeschlossen und unabhängig validiert.
