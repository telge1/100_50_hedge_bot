# PHASE 6XT – 5M PD5 ISOLATED CLICKHOUSE IMPORT PILOT V1

**Date:** 2026-10-21  
**VERDICT:** **`5M_PD5_CH_PILOT_PASS`**

Ein Batch mit **50** chronologischen 5m-PD5-Closes → ClickHouse-Line `historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd5_v1`. Kein zweiter Batch, keine Viewer-/SSOT-Änderungen außer minimaler Importer-Allowlist.

**Artefakte:** `logs/historical_import/overnight_2026/phase6xt_5m_pd5_isolated_ch_import_pilot_v1/` (`run_pilot.py`, `preflight.json`, `pilot_run.json`)

---

## 1. Minimale Importer-Freigabe

**Dateien (pools + Spread-Dashboard, identischer Diff):**

`research_charts/live_micro_lld_v2_ch_test/historical_json_import/historical_batch_config.py`

```python
ISOLATED_IMPORT_COMBOS: tuple[StrategyCombo, ...] = (
    StrategyCombo("5m", 1),
    StrategyCombo("5m", 5),
)
```

`STRATEGY_COMBOS` und Coverage-Matrix **unverändert**. `combo_for_folder("5m_PD5")` → `StrategyCombo("5m", 5)`.

**Test:** `test_isolated_combo_for_folder_5m_pd5` in `test_batch_import_phase4a.py`.

---

## 2. Preflight (vor CH-Write)

| Check | Ergebnis |
|-------|----------|
| Stems / Raster | **50** (`20260909T000000Z` … `20260909T040500Z`) |
| Katalog-Pfade | `production/results/2026-09/5m_PD5/` — alle vorhanden |
| `validate_batch` | **0** Fehler |
| Frozen-Input | **`fe549309…`** (je Bundle) |
| Config-Hash PD5 | **`533e04c7…`** |
| `…_5mpd5_v1` Publish vor Write | **0** |
| `…_5mpd1_v1` Publish | **2592** |
| `…_15mpd1_v1` Publish | **17568** |
| Ref 15m Meta (`3c6bacc2…`) | Fingerprint **`71cc7b3e…`** |
| Aktiver Import-Writer | keiner |
| Env-Gates (Write) | `HISTORICAL_JSON_IMPORT_ALLOW_WRITE=1`, `HISTORICAL_JSON_IMPORT_ALLOW_SIDECAR_WRITE=1` |

### Geplante Batch-Zeilen (`summarize_plans`)

| Layer | Anzahl |
|-------|--------:|
| publish / meta / sidecar | **50** |
| member / catalog | **10355** |

---

## 3. Pilot-Batch (Write)

| Feld | Wert |
|------|------|
| **import_batch_id** | `phase6xt_5m_pd5_ch_import_pilot_v1_8f3fee07-50e7-4df7-bece-3837a7e091be` |
| **batch_uuid** | `8f3fee07-50e7-4df7-bece-3837a7e091be` |
| **pipeline_line_id** | `historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd5_v1` |
| **month / combo** | `2026-09` / `5m_PD5` |
| **Zeitraum** | `2026-09-09T00:00:00Z` … `2026-09-09T04:05:00Z` |
| **Pipeline** | `validate_batch` → `execute_batch_import` → `run_batch_readback_gate(require_sidecar=True)` |
| **Wall time** | ~**19 s** |

### Original-Fingerprints (Stichprobe)

| Stem | snapshot_id (prefix) | active_state_fingerprint (prefix) | active_pools |
|------|----------------------|-----------------------------------|-------------:|
| `20260909T000000Z` | `0ea013ca…` | `1db3c895889f2c27…` | **212** |
| `20260909T000500Z` | (siehe `pilot_run.json` → `import_sample[1]`) | PD5-spezifisch | — |

Erster Close entspricht Staging-JSON-Fingerprint `1db3c895889f2c27…` (PD5 ≠ PD1 auf gleichem Zeitstempel).

---

## 4. ClickHouse-Readback

| Prüfung | Soll | Ist |
|---------|-----:|----:|
| Publish auf `…_5mpd5_v1` | 50 | **50** |
| Parity `run_pilot_readback` | 50/50 ok | **50/50** (`parity_fail: 0`) |
| Sidecar readback | 50 | **50** |
| Scope layers (batched) | meta/publish/sidecar je 50 | **50** / **10355** members+catalog |

Informative Parity-Hinweise `provenance:relative_dest=…` wie bei 5m-PD1-Pilot — `ok=True`.

---

## 5. Regression

| Metrik | Vorher | Nachher |
|--------|--------:|--------:|
| `…_15mpd1_v1` publish | 17568 | **17568** |
| `…_5mpd1_v1` publish | 2592 | **2592** |
| `…_5mpd5_v1` publish | 0 | **50** |
| Ref 15m snapshot / fingerprint | `3c6bacc2…` / `71cc7b3e…` | **unverändert** |

---

## 6. Tests

```text
pytest test_batch_import_phase4a.py test_historical_lmv2_pd_contract_v1.py test_phase4d_ch_sidecar_store.py
→ 27 passed
```

Keine Assertions abgeschwächt.

---

## 7. Geänderte Dateien

| Datei | Änderung |
|-------|----------|
| `historical_batch_config.py` (pools + spread) | `ISOLATED_IMPORT_COMBOS` + `StrategyCombo("5m", 5)` |
| `tests/test_batch_import_phase4a.py` | `test_isolated_combo_for_folder_5m_pd5` |
| `logs/.../phase6xt_5m_pd5_isolated_ch_import_pilot_v1/*` | Runner + JSON-Artefakte |

**Nicht geändert:** Viewer, Dashboard, Coverage-Matrix, 5m/15m-PD1-JSON, PD10, Generator.

---

## 8. Offene Risiken / Nächste Schritte

- **2542** Closes auf `…_5mpd5_v1` verbleiben **ohne** Freigabe (nächster Schritt wäre separater Completion-Import analog 5m PD1, nicht Teil dieses Pilots).
- Wiederholung desselben `BatchSpec` → Publish-Skip (Idempotenz).
- Viewer/Contract `ALLOWED_VIEWER_SCOPES` für 5m PD5 noch **nicht** freigegeben — Chart zeigt PD5 weiterhin disabled bis separate Freigabe nach vollem Import/Readback.

**STOP** — keine weiteren Batches ohne Freigabe.
