# PHASE 6XT – 5M PD10 ISOLATED CLICKHOUSE IMPORT PILOT V1

**Date:** 2026-10-21  
**VERDICT:** **`5M_PD10_CH_PILOT_PASS`**

Ein Batch mit **50** chronologischen 5m-PD10-Closes auf Line `historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd10_v1`. Kein Completion, keine Viewer-Änderungen.

**Artefakte:** `logs/historical_import/overnight_2026/phase6xt_5m_pd10_isolated_ch_import_pilot_v1/` (`run_pilot.py`, `pilot_run.json`)

---

## 1. Importer-Allowlist (minimal)

**Dateien (pools + Spread):** `historical_batch_config.py`

```python
ISOLATED_IMPORT_COMBOS: tuple[StrategyCombo, ...] = (
    StrategyCombo("5m", 1),
    StrategyCombo("5m", 5),
    StrategyCombo("5m", 10),
)
```

**Test:** `test_isolated_combo_for_folder_5m_pd10` in `test_batch_import_phase4a.py`.

---

## 2. Preflight

| Check | Ergebnis |
|-------|----------|
| Stems | **50** (`20260909T000000Z` … `20260909T040500Z`) |
| Katalog `5m_PD10` | vorhanden, `validate_batch` **0** Fehler |
| Frozen-Input | **`fe549309…`** |
| Config-Hash PD10 | **`bd9ac4f3…`** |
| `…_5mpd10_v1` vor Write | **0** |
| `…_5mpd5_v1` / `…_5mpd1_v1` / `…_15mpd1_v1` | **2592** / **2592** / **17568** |
| Write-Gates | gesetzt (`HISTORICAL_JSON_IMPORT_ALLOW_WRITE=1`, `HISTORICAL_JSON_IMPORT_ALLOW_SIDECAR_WRITE=1`) |

### Geplante Batch-Zeilen

| Layer | Anzahl |
|-------|--------:|
| publish / meta / sidecar | **50** |
| member / catalog | **12104** |

---

## 3. Import

| Feld | Wert |
|------|------|
| **import_batch_id** | `phase6xt_5m_pd10_ch_import_pilot_v1_f6f651d0-d811-4ee8-8feb-c8f65203d145` |
| **batch_uuid** | `f6f651d0-d811-4ee8-8feb-c8f65203d145` |
| **pipeline_line_id** | `historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd10_v1` |
| **Pipeline** | `validate_batch` → `execute_batch_import` → `run_batch_readback_gate(require_sidecar=True)` |
| **Wall time** | ~**19 s** |

### Original-Fingerprints (Stichprobe)

| Stem | Hinweis |
|------|---------|
| `20260909T000000Z` | `snapshot_id` `522bf03c…`, FP in `pilot_run.json` → `import_sample[0].publish.validation` |
| Erster Close | Staging-FP `bf502265863df1fa7cfa…` (PD10, ≠ PD1/PD5 auf gleichem Zeitstempel) |

---

## 4. ClickHouse-Readback

| Prüfung | Soll | Ist |
|---------|-----|----:|
| Publish `…_5mpd10_v1` | 50 | **50** |
| Parity `run_pilot_readback` | 50/50 | **50/50** (`parity_fail: 0`) |
| Sidecar | 50 | **50** |
| CH scope (Batch) | meta/publish/sidecar 50; members/catalog **12104** | **stimmt** |

Kein `ProductivePrewriteGateError` in diesem Pilotlauf (ein Durchlauf, kein Resume nötig).

---

## 5. Regression

| Line | Vorher | Nachher |
|------|--------:|--------:|
| `…_15mpd1_v1` | 17568 | **17568** |
| `…_5mpd1_v1` | 2592 | **2592** |
| `…_5mpd5_v1` | 2592 | **2592** |
| `…_5mpd10_v1` | 0 | **50** |
| Ref 15m `3c6bacc2…` / FP `71cc7b3e…` | — | **unverändert** |

---

## 6. Tests

```text
pytest test_batch_import_phase4a.py test_historical_lmv2_pd_contract_v1.py test_phase4d_ch_sidecar_store.py
→ 28 passed
```

---

## 7. Geänderte Dateien

| Pfad | Änderung |
|------|----------|
| `historical_batch_config.py` (pools + spread) | `ISOLATED_IMPORT_COMBOS` + `StrategyCombo("5m", 10)` |
| `test_batch_import_phase4a.py` | `test_isolated_combo_for_folder_5m_pd10` |
| `logs/.../phase6xt_5m_pd10_isolated_ch_import_pilot_v1/*` | Runner + `pilot_run.json` |

---

## 8. Nächster Freigabeschritt

**Kontrollierter CH-Completion-Restimport** für **2542** Closes auf `…_5mpd10_v1` (Stems ab `20260909T041000Z`), analog `phase6xt_5m_pd5_ch_import_completion_v1`:

- Stable `test_run_id` (gültige UUID v4)
- Batches à **50**, idempotentes Resume
- Bei `ProductivePrewriteGateError` (CH-Lag): read-only Re-Audit, dann **Resume** — keine Doppel-Writes
- Full-line `final_validate` + **2592** Publish vor Viewer-Freigabe

**STOP** — keine weiteren Closes ohne separate Freigabe.
