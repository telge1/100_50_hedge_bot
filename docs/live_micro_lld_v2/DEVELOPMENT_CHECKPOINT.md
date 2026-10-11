# LMV2 Development Checkpoint — Historical CH Import

Stand: Phase **4H** (Apr 2026 `4h_PD10` regulatory batches validated).
Dieses Dokument beschreibt den versionierten **Import-Stack** und **externe** Daten-/CH-Voraussetzungen. Keine Credentials.

## Importer-Stand

| Komponente | Pfad |
|------------|------|
| Batch / orchestration | `backtester/short/dashboard/research_charts/live_micro_lld_v2_ch_test/historical_json_import/` |
| Publish / staged write | `.../active_snapshot/publisher.py`, `staged_writer.py`, `published_reader.py` |
| Historical provider | `backtester/short/dashboard/research_charts/historical_lmv2/` (+ `mixed_catalog.py`, `provenance_lookup.py`) |
| Runbook (first batch) | `historical_json_import/docs/phase4d_first_batch_runbook.md` |

Pipeline (Apr–Sep production line):
`historical_mixed_frozen_doge_apr_sep_2026_v1_4hpd10_v1`

Ablauf pro Close: JSON validieren → meta/member staged → catalog → **provenance sidecar** → publish → readback (`fail_closed`, `require_sidecar`).

## Erfolgreich validierte CH-Imports (nicht in Git)

| Batch | `import_batch_id` | Snapshots | Hinweis |
|-------|-------------------|-----------|---------|
| Pilot | `historical_mixed_frozen_doge_15m_pd10_aug2026_pilot_v1` | **10** | Aug 2026 15m PD10 |
| Phase 4F | `phase4f_first_50_apr2026_4hpd10_v1` | **50** | Apr `4h_PD10` |
| Phase 4H | `phase4h_second_100_apr2026_4hpd10_v1` | **100** | Apr `4h_PD10` stems `[50:150]` |

Sidecar-Integrationstest (1 Zeile, Phase 4E) bleibt in CH bis explizit bereinigt.

## Git-Abhängigkeiten (dieser Checkpoint)

- Gesamtes Paket `historical_json_import/` (Python + technische Docs).
- `historical_lmv2`: `mixed_catalog.py`, `provenance_lookup.py`, sowie Deltas an `catalog_registry`, `decision_snapshot`, `errors`, `provider`.
- `active_snapshot`: Publisher-Split (`commit_publish_ledger`), Catalog-Readback in `published_reader`, UUID-Guard in `staged_writer`.
- Tests: `test_historical_json_import.py`, `test_batch_import_phase4a.py`, `test_phase4b_hardening.py`, `test_phase4c_provenance_before_publish.py`, `test_phase4d_ch_sidecar_store.py`, `test_phase4g_hardening.py`, `test_historical_import_recovery.py`, `test_readback_audit_pools.py`, `test_phase2c_integrity.py`, `test_historical_lmv2_mixed_catalog.py`.
- Phasen-Doku: `docs/live_micro_lld_v2/` (inkl. Backup-Readiness, Pilot-Runbook).

**Nicht** in diesem Checkpoint: EMA-Signale V2, Scanner-Resume, Dashboard-API/JS außerhalb des Import-Stacks.

## Externe Katalogdaten (`CATALOG_ROOT`)

Default:

`backtester/short/dashboard/research_charts/live_micro_lld_v2_ch_test/doge_apr_sep_2026_v1_history`

| Artefakt | Relativer Pfad | In Git? | Externe Sicherung |
|----------|----------------|---------|-------------------|
| Snapshot-JSON (~53k) | `production/results/**` | **Nein** (~43 GiB, `.gitignore`) | Host-Tree oder Re-Run `run_p4p5_production.py` |
| Katalog-Manifest | `production/catalog_manifest.json` | **Nein** (lokal unter `production/`) | Kopie neben Backup-Tag; mit Katalog-Release versionieren |
| Coverage-Matrix | `production/coverage_matrix.json` | **Nein** | Wie Manifest |
| Provenance-Lookup | `production/provenance_lookup.json` | **Nein** (`.gitignore`) | SHA256-Index für Sidecar; **mit Manifest sichern** |
| Provenance-Index | `production/provenance_index.json` | **Nein** | Optional; groß |
| Frozen-Input-Manifest | `inputs/*.manifest.json` | **Ja** (klein, bereits versioniert) | — |
| Pilot-Samples | `pilot/*.json` | **Ja** (9 Dateien, begrenzt) | — |

**Frischer Git-Checkout ohne befülltes `production/`:** Code und viele Unit-Tests sind nutzbar; **Batch-Validierung und Phase-4-Integrationstests, die `CATALOG_ROOT` lesen, schlagen fehl**, bis der Katalog wiederhergestellt ist.

## ClickHouse

| Item | Wert |
|------|------|
| Datenbank | `signal_generator` (via `research_charts/clickhouse_config.py` + Collector-`.env` auf dem Host) |
| Core-Tabellen | LMV2 meta / member / catalog / publish (bestehendes Schema) |
| Sidecar (Import-Provenance) | `research_lmv2_snapshot_import_provenance_v1` |
| Writes | Nur mit `HISTORICAL_JSON_IMPORT_ALLOW_WRITE=1` und `HISTORICAL_JSON_IMPORT_ALLOW_SIDECAR_WRITE=1`; exklusiver `historical_import_write_lock` |

## Backup / Restore

- Referenz-Backup vor 4H-100er-Batch: ID `e134b0fa-9cba-4c0f-b8d9-60125001de50`, Tag `lmv2_pre_4h_100batch_20261011T082002Z` (5 Tabellen inkl. Sidecar).
- Details: `docs/live_micro_lld_v2/PHASE_3B_BACKUP_READINESS.md` (Host-Pfade, `BACKUP`/`RESTORE` ohne Passwörter im Repo).
- Nach Restore: Readback-Gates und Publish-Counts auf Produktionslinie prüfen; **kein** automatisches DELETE ohne Freigabe.

## Tests

Aus `backtester/short/dashboard/`:

```bash
pytest research_charts/tests/test_historical_json_import.py \
  research_charts/tests/test_batch_import_phase4a.py \
  research_charts/tests/test_phase4b_hardening.py \
  research_charts/tests/test_phase4c_provenance_before_publish.py \
  research_charts/tests/test_phase4d_ch_sidecar_store.py \
  research_charts/tests/test_phase4g_hardening.py \
  research_charts/tests/test_historical_import_recovery.py \
  research_charts/tests/test_readback_audit_pools.py \
  research_charts/tests/test_phase2c_integrity.py \
  research_charts/tests/test_historical_lmv2_mixed_catalog.py \
  research_charts/tests/test_historical_lmv2_baseline.py \
  research_charts/tests/test_historical_lmv2_decision_snapshot.py \
  research_charts/tests/test_historical_lmv2_guard_parity.py -q
```

| Kategorie | Beispiele | Braucht `CATALOG_ROOT`? |
|-----------|-----------|-------------------------|
| Guards, In-Memory-Sidecar, Recovery | `test_phase4g`, `test_phase4d` (Teile), `test_historical_import_recovery` | Meist **nein** |
| Batch dry-run / regulatory spec | `test_phase4b`, `test_phase4c`, `test_historical_json_import` | **Ja** |
| Readback / pools | `test_readback_audit_pools`, `test_phase2c_integrity` | **Ja** (oder Mocks) |
| Historical LMV2 baseline | `test_historical_lmv2_*` | Teilweise Pilot-JSON in Git |

CH-Integration in Tests: Sidecar-Store-Tests nutzen Env-Flags; kein Produktions-Write in pytest ohne explizite Freigabe.

## Masterplan (verbindlich, Kurz)

1. **Historischer Import:** Weitere Batches nur mit Preflight, Backup, UUID-`test_run_id`, Readback — je Batch Freigabe (April Rest ~30 `4h_PD10`, dann weitere Monate/Kombos per `full_import_plan.py`).
2. **Kein Live-Cutover** ohne separaten Gate; Pilot-Linie 15m bleibt isoliert.
3. **Live-Fortführung:** Rolling-Runner / aktive Pipeline unverändert durch Import-Commits; CH-Publish-Lines strikt trennen.
4. **Research:** Heatmap / Chart-Parity optional eigener Commit (`api.py`, `research_charts.js`) — nicht Teil dieses Checkpoints.
5. **Ops:** Keine Service-Restarts, keine Engine-Neuberechnung der Original-JSONs im selben Schritt wie Import.
