# Phase 4D — First 50-Close Batch Execution Runbook

**Separate approval required** for each step marked `WRITE`.

## Preconditions

- Phase 4D code merged locally; tests green (`54+` pytest).
- Backup verified (e.g. `lmv2_pilot_preflight_*` or successor) and restore drill documented.
- Preflight read-only checks on `signal_generator` (pilot 10 snapshots intact).

## Steps

1. **Backup + preflight (read-only)**
   Confirm backup `BACKUP_CREATED` and disk path. Re-run pilot readback gate if needed.

2. **Sidecar DDL (`WRITE` — DDL only)**
   Deploy `sidecar_ddl(database)` from `provenance_sidecar_design.py` to `signal_generator`.
   ORDER BY: `(snapshot_id, pipeline_line_id, relative_json_path)`.

3. **Schema validation (read-only)**
   `DESCRIBE TABLE signal_generator.research_lmv2_snapshot_import_provenance_v1`.

4. **Small sidecar integration (`WRITE` — 1 row)**
   Set `HISTORICAL_JSON_IMPORT_ALLOW_SIDECAR_WRITE=1`.
   Insert one test row or first close of batch in isolation; `read` + field parity; delete test row only if explicitly approved.

5. **50-close batch (`WRITE`)**
   `HISTORICAL_JSON_IMPORT_ALLOW_WRITE=1`
   `HISTORICAL_JSON_IMPORT_ALLOW_SIDECAR_WRITE=1`
   Single import host; `historical_import_write_lock` default path (or `HISTORICAL_JSON_IMPORT_LOCK_FILE`).
   Run `execute_batch_import` for `first_regulatory_batch_spec` (April 2026, 4h_PD10, 50 stems).

6. **CH readback incl. sidecar (read-only queries)**
   `run_batch_readback_gate(..., require_sidecar=True, batch_spec=spec)`.

7. **Progress vs CH**
   `reconcile_progress_with_ch`; publish count on production line must be 50.

8. **Metrics**
   Note wall time, `system.parts` / table bytes delta, peak memory from `docker stats` (single sample).

## Rollback stance

No automatic DELETE. Partial state → `import_recovery.classify_partial_import` + manual playbook.

## Not in scope

Full Apr–Sep import, October backfill, live cutover, engine/JSON changes.
