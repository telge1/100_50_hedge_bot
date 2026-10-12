# May 2026 monthly readback (post-write, not executed in Phase 4W)

After an approved `write_and_readback` monthly run:

1. `may2026_monthly_runner.py` writes `logs/historical_import/may2026/final_readback.json` via `verify_manifest_file` per combo manifest.
2. Scope CH counts: `verify_manifest_scope_ch_counts(manifest, chunk_size=100)` — batched `snapshot_id` queries only.
3. April regression: distinct publish counts on April lines must remain 2880/2880/2880/180 (+ Aug pilot 10).
4. Orphans: `read_ch_import_preflight` → `orphan_member_snapshots` must be `[]`.

No full-month `snapshot_id IN (...)` over HTTP.
