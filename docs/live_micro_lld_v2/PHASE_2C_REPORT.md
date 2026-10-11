# Phase 2C — Integritätsabschluss (nachgewiesen)

**Stand:** 2026-10-10
**Scope:** Historischer JSON→CH-Importer (DOGEUSDT 15m PD10, Aug-2026-Pilotwoche), **ohne** ausgeführten Pilot-Write.

## Zusammenfassung

Phase 2C hat Catalog-/Membership-Identität, Snapshot-ID-Verhalten, Partial-Catalog-Recovery, Remaining-Prefix-Klassifikation und fail-closed Readback ergänzt. **16 Unit-Tests** bestanden; **VALIDATE-Dry-Run** für 10 Closes ohne CH-Zugriff.

**Nicht nachgewiesen:** Live-CH-Integrationstest des Write-Pfads; `run_pilot_readback(..., use_clickhouse=True)` vor erstem Import.

---

## Testbefehle (reproduzierbar)

```bash
cd /home/telgenbuescher/projects/pools+ob+delta_bot/backtester/short/dashboard

python3 -m pytest \
  research_charts/tests/test_historical_json_import.py \
  research_charts/tests/test_historical_import_recovery.py \
  research_charts/tests/test_phase2c_integrity.py -q

python3 research_charts/live_micro_lld_v2_ch_test/historical_json_import/cli.py
```

**Erwartung Tests:** `16 passed`
**Erwartung Dry-Run:** `errors: []`, `snapshot_count: 10`, `member_rows: 2750`, `catalog_rows: 2750`, `publish_rows: 10`

---

## Implementierte Integritäts-Gates

| Bereich | Module | Nachweis |
|---------|--------|----------|
| Catalog-ID-Menge (Pre-Publish) | `catalog_integrity.py`, `import_close.py`, `catalog_write.py` | `validate_catalog_rows_in_memory`, `verify_ch_catalog_identity` |
| Reader (ID-Menge, kein JSON-FP) | `published_reader.py` | `assert_member_catalog_id_sets` |
| JSON-Feldparität | `readback_parity.py` | `compare_catalog_rows_to_json_fields` |
| Partial Catalog | `import_close.py` | STOP bei `0 < count < expected`; `CatalogIncompleteError` |
| Snapshot-ID vs Membership | `fingerprint.py` | `deterministic_snapshot_id` ohne ACTIVE-Set; Test `test_snapshot_id_ignores_active_membership` |
| Readback fail-closed | `readback_errors.py`, `readback_parity.py` | `ReadbackGateFailure` wenn `use_clickhouse=True` und `ok=False` |
| Remaining Prefix | `remaining_prefix_audit.py`, `pilot_bar_verify.py` | 433 Pools `COMPLETE_MANIFEST_BACKED`; Pilot-Frozen `cfae257…` `COMPLETE_BAR_VERIFIED` (673×15m, 0 Lücken) |

---

## Pilot-Konfiguration (frozen)

- **Pipeline:** `historical_mixed_frozen_doge_15m_pd10_aug2026_pilot_v1`
- **Config hash (PD10):** `bd9ac4f30e177321b688f5adb0fe2681ebb4966d51701e3b323498bfcd0d06c2`
- **Stems / Snapshot-IDs:** siehe `pilot_config.py` und Dry-Run-Output `summary.snapshot_ids`

---

## Verbleibende Einschränkungen

1. CH speichert **kein** `dest_sha256` / relativen JSON-Pfad (Sidecar-Design: `provenance_sidecar_design.py`, keine DDL).
2. **Rolling-Runner** schreibt Catalog noch nach Publish; historischer Import: Catalog vor Publish (`import_historical_close`).
3. **Globale `snapshot_id`** — Membership nicht im Hash; Konfliktschutz über Publish-Ledger + Fingerprints.
4. Kein automatisches CH-Backup in diesem Repo verifiziert.

---

## Live-Fortführung (Kompatibilitätsanhang, nur Analyse)

| Anforderung | Status |
|-------------|--------|
| Ein kanonisches CH-Modell | Größtenteils ja (gleiche Tabellen/Reader) |
| Backfill ≠ Live-Signal | **Nicht** im Schema (`ingestion_mode` / Sichtbarkeitszeit fehlt) |
| Eine Chart-Quelle | Braucht Produktions-`pipeline_line_id` oder Union-Reader |
| Write-Reihenfolge Live vs Historisch | Angleichen an `import_historical_close` empfohlen |
| Provenance in CH | Sidecar noch offen |

**Keine Live-Pipeline in Phase 2C implementiert.**

---

## Referenz-Module

`backtester/short/dashboard/research_charts/live_micro_lld_v2_ch_test/historical_json_import/`
