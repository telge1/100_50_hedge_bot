# P4 — Code-Änderungen (ohne Commit)

**Pfad:** `spread_recovery_hedge_short_dev/dashboard/`  
**Cache-Bust:** `mc-load-p4-v1`, `research-mc-load-p4-v1`

## `static/js/research/research_charts.js`

- `mcLoadSnapshotHistoryLimit`, `mcLoadDiagPushPhaseDecision`, `mcLoadSkipEmaCatchUpAfterPaneBundle`
- `mcLoadHistoryWeeks`: `readHistoryFromUi()` zuerst
- Diagnose: Phase-A-Entscheidung nicht von Phase-B-skip überschreiben; `phase_b_decision`; HTTP-Patch mit `request_limit` / `history_weeks` / `phase_*_decision`
- EMA: skip `ema-post-catch-up` bei `_historyPhaseB`
- Exact Origin: async bei `paneLoadReleasedEarly`
- `sendJson` meta: `mcLoadReqLimit`, `mcLoadHistoryWeeks`

## `static/js/research/mc_load_diag_v1.js`

- Schema `mc_load_p4_v1`, `phase_decisions`, `phase_b_decision`, `mc_load_p4_code`

## `templates/research_charts.html`

- Query-Strings → p4-v1

## Tests

- `test_mc_load_p3_fast_first_v1.py` — P4-Symbole + Cache-Bust

## Backup (vor Edits, falls vorhanden)

`dashboard/backups/mc-load-p4-20261126T084820Z/`