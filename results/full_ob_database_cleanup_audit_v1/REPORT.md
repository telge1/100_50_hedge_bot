# Full-OB Database Cleanup Audit v1 (Read-Only)

**Audit time (UTC):** 2026-09-04T~11:10Z (local session)  
**Verdict:** `FULL_OB_DATABASE_CLEAN_PRODUCTION_SMOKE_ISOLATED`  
**Destructive actions executed:** `false`

```text
DESTRUCTIVE_ACTIONS_EXECUTED=false
PRODUCTION_FULL_OB_TEST_CONTAMINATION=false
```

Collector PID **1565672** verified running | OI PID **147111** verified running  
No restart, no DROP/TRUNCATE/DELETE/OPTIMIZE, no `.tmp` mutation, no JSONL changes.

---

## Executive answers

| # | Question | Answer |
|---|----------|--------|
| 1 | Which CH DBs/tables were written? | Only `research_full_ob_smoke` (5 tables + 1 view) |
| 2 | Isolated smoke only? | **Yes** |
| 3 | Production contaminated? | **No** (query_log + probes; OA catalog unloadable caveat) |
| 4 | Valid / invalid / test? | Smoke = `SMOKE_TEST_ONLY` / `VALID_TECHNICAL_ONLY`; gap table has 3 physical Replacing dups; no nested CH rows |
| 5 | Safe to delete later? | Optional: smoke DB / dedupdemo after backup; **never** JSONL.zst |
| 6 | Must keep? | All FR `*.jsonl.zst`, REST seeds, manifests, finalized segments for live events |

---

## Phase A — Inventory

### Databases seen

`btc_doge_research`, `default`, `orderbook_analysis`, `research_full_ob_smoke`, `signal_generator`, `system`, …

### Full-OB / smoke related (only isolated DB)

| Table | Engine | Phys | Logical (FINAL) | Storage bytes | Events |
|-------|--------|------|-----------------|---------------|--------|
| `full_ob_packets_smoke_v1` | ReplacingMergeTree | 1514 | 1514 | 6 316 812 | `BTCUSDT_20260904T080534Z_1fd9a66d36` |
| `full_ob_level_changes_smoke_v1` | ReplacingMergeTree | 376 553 | 376 553 | 3 518 481 | (BTCUSDT via symbol) |
| `full_ob_gap_audit_packets_v1` | ReplacingMergeTree | 206 | 203 | 209 643 | (topic orderbook.full.BTCUSDT; no fight_event_id col) |
| `full_ob_gap_audit_packets_v1_dedupdemo` | ReplacingMergeTree | 203 | 203 | 205 536 | demo |
| `full_ob_multi_epoch_packets_v1` | ReplacingMergeTree | 5 | 5 | 3 598 | epochs 0/1 |
| `v_full_ob_gap_audit_packets_dedup` | View | — | — | 0 | dedup view |

**Total smoke storage ≈ 10.25 MB.**

No `nested_signal` / `signal_analysis` ClickHouse tables exist.

`orderbook_analysis`: **SHOW TABLES / system.parts blocked** by pre-existing broken `orderbook_deltas` attach (`ASYNC_LOAD_WAIT_FAILED`, too many broken parts). Documented as ops caveat — not caused by Full-OB smoke tests.

Details: `clickhouse_inventory.csv`, `table_schema_inventory.json`.

---

## Phase B — Write traces

| Workstream | Verdict |
|------------|---------|
| `full_ob_clickhouse_smoke_btc_v1` | `WRITTEN_TO_ISOLATED_SMOKE_ONLY` |
| `full_ob_first_event_gap_audit_btc_v1` | `WRITTEN_TO_ISOLATED_SMOKE_ONLY` |
| `full_ob_resync_checkpoint_v1` | `WRITTEN_TO_ISOLATED_SMOKE_ONLY` (+ offline unit tests) |
| `nested_profile_edge_signal_v1` | `NO_DATABASE_WRITE` |
| `nested_signal_analysis_isolation_v1` | `NO_DATABASE_WRITE` |

`system.query_log`: Insert/Create targets for `full_ob` / event needles all resolve under `research_full_ob_smoke.*`.  
`orderbook_analysis` inserts with needles: **0**.  
`btc_doge_research` inserts with needles: **0**.

See `write_trace_audit.json`.

---

## Phase C — Row classification (summary)

| Class | Where |
|-------|-------|
| `SMOKE_TEST_ONLY` | packets_smoke, level_changes_smoke, dedupdemo, dedup view |
| `VALID_TECHNICAL_ONLY` | gap_audit_packets, multi_epoch_packets (regression) |
| `DUPLICATE_PHYSICAL` | gap_audit: phys 206 vs FINAL 203 (3 sha groups) |
| `DUPLICATE_LOGICAL` | none detected beyond Replacing unmerged |
| Nested/isolation CH | none |
| Orphans in CH | none (sources exist) |

Parent raw event on disk: `research_eligible=false`, `INCOMPLETE_PERSISTED_U_GAP` — **technical keep**, not `VALID_RESEARCH`. Smoke window intentionally cut before first gap (parity artifact).

---

## Phase D — Source lineage

Event root:

`.../full_ob_edge_flight_recorder/BTCUSDT/2026-09-04/BTCUSDT_20260904T080534Z_1fd9a66d36/`

| Artifact | Status |
|----------|--------|
| `full_ob_raw_deltas.jsonl.zst` | exists; sha256 computed; **MUST_KEEP** |
| `rest_full_snapshot.json.zst` | exists; sha256 computed; **MUST_KEEP** |
| `event_manifest.json` | exists |
| Segment dirs `cont_*` | exist; finalized (`actual_final_ts` set) |
| Open `.tmp` on this event | **0** |
| DB replayable from source | **yes** (smoke import scripts + manifests) |

See `source_lineage_audit.csv`.

---

## Phase E — Smoke DB decision

```text
KEEP_FOR_REGRESSION
```

Alternate under disk pressure: `SAFE_TO_REMOVE_AFTER_BACKUP` (fully reproducible from JSONL).  
Export before drop recommended; existing `results/full_ob_*` artifacts already document parity.

---

## Phase F — Production contamination

```text
PRODUCTION_FULL_OB_TEST_CONTAMINATION=false
```

Probes: `btc_doge_research` metadata tables, all `signal_generator` tables, query_log.  
Caveat: cannot SELECT `orderbook_analysis.*` while `orderbook_deltas` is broken — no Insert evidence of smoke writes there.

---

## Phase G — Cleanup plan (proposal only)

1. **Keep** all JSONL.zst / REST / manifests / segments.  
2. **Optional later:** `DROP` `full_ob_gap_audit_packets_v1_dedupdemo`.  
3. **Optional later:** `DROP DATABASE research_full_ob_smoke` after export.  
4. **Do not touch** Sep‑3 orphan `.tmp` (6 files) without separate approval.  
5. **No** production cleanup for Full-OB smoke.

Commands only in `PROPOSED_CLEANUP_COMMANDS_NOT_EXECUTED.sql` — **not executed**.

---

## Phase H — Restart readiness (status only)

| Item | Status |
|------|--------|
| Active parent captures | **No** — BTC/DOGE `COOLDOWN` |
| Today events | Finalized at hard-cap ~11:05 UTC; `research_eligible=false` |
| Queue backlog / drops | 0 / 0 |
| Writer errors | 0 |
| Open `.tmp` today | 0 |
| Orphan `.tmp` Sep 3 | 6 (untouched) |
| Restart this audit | **No** |

Combined restart (resync + nested + isolation) still requires **explicit** approval.

---

## Artefacts

```text
results/full_ob_database_cleanup_audit_v1/
├── REPORT.md
├── clickhouse_inventory.csv
├── table_schema_inventory.json
├── write_trace_audit.json
├── row_classification.csv
├── source_lineage_audit.csv
├── production_contamination_check.json
├── smoke_database_assessment.json
├── cleanup_plan.json
├── PROPOSED_CLEANUP_COMMANDS_NOT_EXECUTED.sql
└── live_restart_readiness.json
```
