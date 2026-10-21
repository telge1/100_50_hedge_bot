# PHASE 6XT – 5M PD10 CONTROLLED FILE REGISTRATION V1

**Date:** 2026-10-21  
**VERDICT:** **`5M_PD10_FILE_REGISTRATION_PASS`**

Freigabe: Datei-SSOT + Provenance für **5m PD10** (Sep-9–17-Fenster), gleiche Pipeline wie [5m PD5 File Registration](PHASE_6XT_5M_PD5_FILE_REGISTRATION_V1.md). Keine ClickHouse-Writes.

**Artefakte:** `logs/historical_import/overnight_2026/phase6xt_5m_pd10_file_registration_v1/`

| Datei | Inhalt |
|-------|--------|
| `run_pd10_file_registration.py` | Preflight, `copy2`, Index-Append, Lookup-Rebuild |
| `preflight.json` | PASS, 2592 Stems |
| `import_result.json` | Ergebnis + Batch-UUID |
| `provenance_index.json.backup` | Index vor Append |

**Batch:** `phase6xt_5m_pd10_file_registration_v1_11f0790a-c2b2-4aa3-b7d4-f19a9fa2a46c`

---

## 1. Scope

| Feld | Wert |
|------|------|
| Symbol / TF / PD | DOGEUSDT / 5m / **10** |
| Fenster | `2026-09-09T00:00:00Z` … `2026-09-17T23:55:00Z` |
| Closes | **2592** (2016 Kontext + 576 sichtbar) |
| Frozen-Input | `fe549309ef047ea092bd1fa3eba5c16fae647db7f3fad95dc567b47c66ebcb66` |
| Config-Hash | `bd9ac4f30e177321b688f5adb0fe2681ebb4966d51701e3b323498bfcd0d06c2` |
| Ziel | `production/results/2026-09/5m_PD10/` |

---

## 2. Source-Inventar

| Quelle | Sep 9–17 |
|--------|----------|
| Staging `…/phase6xt_5m_pd10_jan_sep_v1/results/2026-09/5m_PD10/` | **2592/2592**, 5m-Raster ohne Lücken |
| Manifest `…/phase6xt_5m_pd10_jan_sep_v1/run/manifest.json` | `config_hash` = PD10-Hash, September `fe549309…` |

Preflight: `_validate_snapshot_loose` je Stem, `frozen_input.snapshot_id` = `fe549309…`, keine Zielkonflikte vor Copy.

---

## 3. Production-Inventar

| Pfad | Anzahl |
|------|--------:|
| `…/2026-09/5m_PD10/*.json` | **2592** |
| Eindeutige Stems | **2592** |

**Nicht angefasst:** `5m_PD1`, `5m_PD5`, 15m/4h-Bulk unter `production/results/`.

---

## 4. SHA256-Abgleich

| Prüfung | Ergebnis |
|---------|----------|
| Staging ↔ Ziel (alle 2592) | **0** Abweichungen |
| Index `source_sha256` = `dest_sha256` | **2592/2592** |
| Lookup `verify_file` (volle Schleife) | **2592/2592** OK |

Stichprobe `20260916T000000Z`: `active_state_fingerprint` `bf502265863df1fa7cfa…`, `pool_detail` **10**.

---

## 5. Provenance

| Layer | Vorher | Nachher |
|-------|--------:|--------:|
| `provenance_index.json` Einträge (gesamt) | 49838 | **52430** (+**2592** nur `…/5m_PD10/…`) |
| `copied_count` | 49838 | **52430** |
| `provenance_lookup.json` `entry_count` | 58986 | **61578** (+2592) |
| `coverage_matrix.json` SHA256 | `17aedaa34…` | **unverändert** `17aedaa34…` |

---

## 6. Tests

```text
pytest test_batch_import_phase4a.py::test_catalog_inventory_matches_manifest
pytest test_historical_lmv2_pd_contract_v1.py
→ 5 passed
```

Disk-Inventar: **53802 + 2592×3** = **61578** (`5m_PD1` + `5m_PD5` + `5m_PD10` isoliert September).

---

## 7. Baseline-Regression (ClickHouse, read-only)

| Line | Publish count |
|------|----------------:|
| `…_15mpd1_v1` | **17568** |
| `…_5mpd1_v1` | **2592** |
| `…_5mpd5_v1` | **2592** |
| `…_5mpd10_v1` | **0** |

Keine CH-Mutationen in dieser Phase.

---

## 8. Geänderte Bestände

1. **+2592** JSON unter `production/results/2026-09/5m_PD10/`.
2. **`provenance_index.json`** — +2592 Einträge.
3. **`provenance_lookup.json`** — Rebuild (+2592 Keys).
4. **`test_batch_import_phase4a.py`** — Disk-Count um **5m_PD10** erweitert (PD5-Muster).
5. **Logs** — Runner + Backup + JSON.

---

## 9. Nächster Pilot-Scope (nach separater Freigabe)

**Isolierter CH-Import-Pilot (50 Closes)** auf Line `historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd10_v1`:

| Feld | Wert |
|------|------|
| Zeitraum | `2026-09-09T00:00:00Z` … `2026-09-09T04:05:00Z` |
| Stems | `20260909T000000Z` … `20260909T040500Z` (**50**) |
| Quelle | `production/results/2026-09/5m_PD10/` (jetzt catalog-ready) |
| Voraussetzung | `ISOLATED_IMPORT_COMBOS` += `StrategyCombo("5m", 10)` (minimal, wie PD5) |
| CH-Ziel vor Pilot | **0** Publish auf `…_5mpd10_v1` (bestätigt) |

Kein Completion (2542) ohne eigene Freigabe nach erfolgreichem Pilot-Readback.

**STOP** — auf Freigabe CH-Pilot warten.
