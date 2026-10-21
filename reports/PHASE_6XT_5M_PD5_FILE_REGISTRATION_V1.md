# PHASE 6XT – 5M PD5 CONTROLLED FILE REGISTRATION V1

**Date:** 2026-10-21  
**VERDICT:** **`5M_PD5_FILE_REGISTRATION_PASS`**

Freigabe: Schritt A (Datei-SSOT + Provenance) gemäß [`PHASE_6XT_5M_PD5_PD10_STRICT_IMPORT_PREPARATION_V1.md`](PHASE_6XT_5M_PD5_PD10_STRICT_IMPORT_PREPARATION_V1.md).  
Keine ClickHouse-Writes, Publishes, PD10, Viewer- oder Dashboard-Änderungen.

**Artefakte:** `logs/historical_import/overnight_2026/phase6xt_5m_pd5_file_registration_v1/`

| Datei | Inhalt |
|-------|--------|
| `run_pd5_file_registration.py` | Preflight, `copy2`, Index-Append, Lookup-Rebuild |
| `preflight.json` | PASS, 2592 Stems |
| `import_result.json` | Ergebnis + Batch-UUID |
| `provenance_index.json.backup` | Index vor Append |

**Batch:** `phase6xt_5m_pd5_file_registration_v1_93a2a23e-1348-42e2-a3b2-024f15719dc3`

---

## 1. Scope (ausgeführt)

| Feld | Wert |
|------|------|
| Symbol / TF / PD | DOGEUSDT / 5m / **5** |
| Fenster | `2026-09-09T00:00:00Z` … `2026-09-17T23:55:00Z` |
| Closes | **2592** (2016 Kontext + 576 sichtbar) |
| Frozen-Input | `fe549309ef047ea092bd1fa3eba5c16fae647db7f3fad95dc567b47c66ebcb66` |
| Config-Hash | `533e04c7cd0e1c93540ed9466a2a5a2eb626f82b828b9a45d1da234703ebd03c` |

---

## 2. Source-Inventar

| Quelle | Pfad | Sep-9–17 |
|--------|------|----------|
| Staging (Original) | `doge_multi_month_2026_history/staging/phase6xt_5m_pd5_jan_sep_v1/results/2026-09/5m_PD5/` | **2592/2592**, 5m-Raster ohne Lücken |
| Manifest | `…/phase6xt_5m_pd5_jan_sep_v1/run/manifest.json` | `config_hash` = PD5-Hash, September `fe549309…` |
| Overnight-Progress | `…/run/progress.json` | Status **COMPLETE** |

Preflight: `_validate_snapshot_loose` je Stem, `frozen_input.snapshot_id` = `fe549309…`, keine Zukunftsdaten, keine abweichenden Zieldateien vor Copy.

---

## 3. Ziel-Inventar

| Ziel | Anzahl |
|------|--------:|
| `doge_apr_sep_2026_v1_history/production/results/2026-09/5m_PD5/*.json` | **2592** |
| Eindeutige Stems | **2592** |
| `5m_PD10` unter `2026-09` | **0** (ausgeschlossen) |

---

## 4. SHA256-Abgleich

| Prüfung | Ergebnis |
|---------|----------|
| Staging ↔ Ziel (alle 2592 Stems) | **0** Abweichungen |
| `source_sha256` = `dest_sha256` in Index | **2592/2592** |
| Lookup `verify_file` + frozen id (volle Schleife) | **2592/2592** OK |

Stichprobe Pilot-Start `20260916T000000Z`: Dest `a1f8ece82bf34036…` = Staging.

---

## 5. Provenance-Readback

| Layer | Vorher | Nachher |
|-------|--------:|--------:|
| `provenance_index.json` Einträge (gesamt) | 47246 | **49838** (+**2592** nur `…/5m_PD5/…`) |
| `copied_count` | 47246 | **49838** |
| `provenance_lookup.json` `entry_count` | 56394 | **58986** (+2592) |
| `coverage_matrix.json` SHA256 | `17aedaa34…` | **unverändert** `17aedaa34…` |

Index-Zeilenformat analog 5m PD1 (`source_catalog`, `source_path` → Staging, `import_batch_id`, `catalog_id` = `doge_apr_sep_2026_v1`).

---

## 6. Inventory-Tests

```text
pytest research_charts/tests/test_batch_import_phase4a.py::test_catalog_inventory_matches_manifest
pytest research_charts/tests/test_historical_lmv2_pd_contract_v1.py
→ 5 passed
```

Anpassung (PD1-Muster, keine Abschwächung der Bulk-Matrix): `disk_total_json` = **53802 + 2592 + 2592** = **58986**, `by_combo_disk["5m_PD5"]` = **2592**.

---

## 7. 15m / 5m PD1-Regression

### ClickHouse (read-only)

| Line | Publish count |
|------|----------------:|
| `…_15mpd1_v1` | **17568** |
| `…_5mpd1_v1` | **2592** |
| `…_5mpd5_v1` | **0** (noch kein CH-Import) |

### Datei-Baseline 5m PD1

Referenz `production/results/2026-09/5m_PD1/20260916T000000Z.json` SHA256 **`484b95d1d923a199…`** — unverändert.

---

## 8. Geänderte Bestände (Liste)

1. **+2592** JSON unter `production/results/2026-09/5m_PD5/` (byte-identisch aus Staging, `shutil.copy2`).
2. **`production/provenance_index.json`** — +2592 Einträge, `copied_count` +2592.
3. **`production/provenance_lookup.json`** — Rebuild via `write_provenance_lookup` (+2592 Keys).
4. **`research_charts/tests/test_batch_import_phase4a.py`** — Disk-Inventar um isoliertes **5m_PD5** erweitert.
5. **Logs** — Runner + Backup + JSON-Artefakte (oben).

**Nicht geändert:** `coverage_matrix.json`, `catalog_manifest.json`, 15m/5m-PD1-JSON, CH, Viewer, PD10.

---

## 9. Nächster Pilot-Scope (nach separater Freigabe)

**5m PD5 isolierter ClickHouse-Import** auf Line `historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd5_v1`:

- Gleiche **2592** Stems aus `doge_apr_sep_2026_v1_history` (jetzt catalog-ready).
- Muster: `phase6xt_5m_pd1_isolated_ch_import_pilot_v1` → optional 50-Close-Pilot, dann `run_completion_import.py` mit Batch-Readback-Gates.
- `ISOLATED_IMPORT_COMBOS` / Contract-5m-PD5 vor Writer-Freigabe ergänzen (Code, nicht in dieser Phase).
- **Kein** PD10 parallel; **kein** unsupervised Completion ohne Freigabe.

**STOP** — auf Freigabe CH-Import warten.
