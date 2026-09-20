# LEGACY CUTOVER REPORT — Full-OB Edge Flight Recorder 1h

**ENDVERDICT:** `NEW_RECORDER_LIVE_VALIDATION_FAILED`

Cutover and legacy recovery completed; the new collector is the only live `orderbook_v2_live` process and runs `timing_contract=full_ob_edge_capture_timing_v1`. Live smoke (≥15 min) **fails** the hard gate `queue_drop_count=0`: FR writer queue drops climb continuously (~66k total / ~33k per symbol after ~19 min).

No commit, no push, no second collector, no dashboard/OI restart, no DB writes, no trading, no deletion of originals.

---

## ENDVERDICT rationale

| Candidate | Why not / why |
|---|---|
| `LEGACY_EVENTS_RECOVERED_NEW_1H_RECORDER_LIVE` | Legacy not `COMPLETE_RECOVERED` (truncated zstd / incomplete JSON tail). |
| `LEGACY_EVENTS_PARTIALLY_RECOVERED_NEW_1H_RECORDER_LIVE` | Legacy *is* partial, but live smoke fails `queue_drop_count=0`. |
| `CUTOVER_BLOCKED_RECOVERY_NOT_SAFE` | Cutover was **not** blocked; recovery tool + tests passed; stop/start executed once. |
| **`NEW_RECORDER_LIVE_VALIDATION_FAILED`** | **Selected.** New recorder is up, but continuous FR `writer_queue_drops` violate Phase-F acceptance. |

Process left running as-is (PID **1481866**). No second restart performed (single controlled cutover only).

---

## Phase A — Legacy recovery tool (offline)

**Module:** `orderbook_analyse/src/orderbook_analyse/orderbook_v2_live/full_ob_edge_flight_recorder/legacy_tmp_recovery.py`

**Tests:** `orderbook_analyse/tests/test_legacy_tmp_recovery.py` — **7 passed** (re-checked after cutover).

Covered: complete-but-unfinalized zstd; truncated zstd tail; truncated JSON line; u-gap; stale updates; crossed book; fail-closed when fd still open; original byte-identical.

Behavior summary:

1. Recovery blocked if any `/proc/*/fd` still holds the original `.tmp`.
2. Never writes the original; SHA256 then immutable copy; recover from copy only.
3. Byte-wise zstd decode; keep complete JSONL lines only; never invent tail bytes/records.
4. Replay via full-book state + snapshot (`u`/`seq`).
5. `COMPLETE_RECOVERED` only if zstd complete, no incomplete JSON tail, no u-gaps, replay OK, book not crossed.
6. Else `INCOMPLETE_AT_LEGACY_RESTART` with trailing/offset/u/gap/replay/crossed/count fields.
7. Always `finalization_reason=INTERRUPTED_BY_LEGACY_COLLECTOR_RESTART`, `outcome_status=UNRESOLVED_INTERRUPTED_CAPTURE`, `natural_fight_outcome_complete=false`.

---

## Phase B — Pre-cutover inventory

Artifact: `PRE_CUTOVER_INVENTORY.json`

| Field | Value |
|---|---|
| PID | **1467869** (alive) |
| RSS | ~206 MB |
| Collector | exactly one `orderbook_v2_live --mode raw-archive-only` |
| `book_ready` | BTC/DOGE true |
| `full_book_active_topics` | 2 |
| `gap_count` | 0 / 0 |
| FR lifecycle | `FIGHT_ACTIVE` both |
| BTC event | `BTCUSDT_20260903T184212Z_4a22a89fe6` — deltas `.tmp` ~21.9 MB, **fd 18** |
| DOGE event | `DOGEUSDT_20260903T184212Z_f5d68293cd` — deltas `.tmp` ~2.4 MB, **fd 16** |
| SHA256 | marked **moment-in-time** (writer still appending) |
| Timing contract on old process | absent / null (legacy code path) |

---

## Phase C — Controlled stop

1. `kill -TERM 1467869` (no SIGKILL).
2. Process exited (~2 s).
3. Confirmed: PID gone; no second collector; **no fds** on legacy `.tmp`; sizes stable at BTC **22076432**, DOGE **2417702**.
4. New collector **not** started until those checks passed.

---

## Phase D — Final legacy quarantine + recovery

Artifacts under:

`results/btc_doge_full_ob_edge_flight_recorder_v1/edge_capture_1h_v1/legacy_recovery/`

| Path | Role |
|---|---|
| `_quarantine_bit_identical/` | Bit-identical quarantine copies |
| `BTCUSDT_20260903T184212Z_4a22a89fe6/` | `original_tmp_copy/`, `recovered_deltas.jsonl.zst`, `recovery_manifest.json`, `replay_report.json`, `SHA256SUMS` |
| `DOGEUSDT_20260903T184212Z_f5d68293cd/` | same |
| `PHASE_D_RECOVERY.json` | Combined Phase-D evidence |

### Recovery results

| Symbol | `data_quality` | Records | `u` range | `u_gap_count` | Replay | Crossed | `zstd_complete` | Incomplete JSON tail | Original unchanged |
|---|---|---|---|---|---|---|---|---|---|
| BTCUSDT | `INCOMPLETE_AT_LEGACY_RESTART` | 20529 | 4107240→4127768 | 0 | `COMPLETE_REPLAYABLE` (20527 applied, 2 stale/dup) | false | **false** | **true** | **true** |
| DOGEUSDT | `INCOMPLETE_AT_LEGACY_RESTART` | 20391 | 4106932→4127322 | 0 | `COMPLETE_REPLAYABLE` (20389 applied, 2 stale/dup) | false | **false** | **true** | **true** |

Shared markers:

- `finalization_reason=INTERRUPTED_BY_LEGACY_COLLECTOR_RESTART`
- `outcome_status=UNRESOLVED_INTERRUPTED_CAPTURE`
- `natural_fight_outcome_complete=false`

Final original SHA256 (post-stop, stable):

- BTC: `54a931a4224187325e6cbea6d3350976df19c54fc62d884ac042de15c38e6bb0`
- DOGE: `def94e1cf4b14a07c2382b69da2d99d3478d101d696b287e523297c72ac9a95a`

Originals were **not** renamed, deleted, or rewritten. Incomplete quality is expected for unflushed zstd frames after a process without FR shutdown finalization.

---

## Phase E — New collector start (single cutover)

| Field | Value |
|---|---|
| Start | `scripts/start_orderbook_v3_raw_archive_btc_doge.sh` |
| Env | `OB_V3_FULL_OB_FLIGHT_RECORDER_ENABLE=true`, symbols `BTCUSDT,DOGEUSDT` |
| New PID | **1481866** (`logs/orderbook_v3_raw_archive_only.pid`) |
| Old `.tmp` continued? | **No** (no fds on legacy paths; sizes still final) |
| New fight IDs | `BTCUSDT_20260903T195209Z_e8cb0f6198`, `DOGEUSDT_20260903T195209Z_dc0458f57a` |
| Trigger source | `BOOTSTRAP_ALREADY_IN_EDGE_ZONE` (price already in 20 bps zone at start — not a live CROSS_IN) |
| Timing on events | `minimum_capture_end_ts = trigger_ts + 3600`; `hard_capture_end_ts = trigger_ts + 10800` |
| Prebuffer | `pre_trigger_seconds_actual=0` → `PREBUFFER_EMPTY` / `data_quality=INCOMPLETE` at open |

OI collector PID **147111** left untouched. No dashboard restart.

---

## Phase F — Live smoke (≥15 minutes)

Evidence: `PHASE_F_SMOKE.json` (plus live health NDJSON).

Window: event start **2026-09-03T19:52:09Z** → smoke sample ~**19+ minutes** later.

### Passed

| Check | Result |
|---|---|
| Exactly one collector | PID **1481866** only |
| `book_ready` BTC/DOGE | true |
| `full_book_active_topics` | 2 |
| `u_gap_count` / `gap_count` | 0 |
| `seq` monotonic | true (sampled across FR-era health) |
| Lock offload | `full_book_lock_hold_ns_last` ~12–17 µs |
| `timing_contract` | `full_ob_edge_capture_timing_v1` |
| Prebuffer target | `pre_seconds=600` |
| `min_post_seconds` | 3600 |
| Segment / hard-cap (settings) | `segment_seconds=1800`, `hard_cap_seconds=10800` (`segment_minutes=30`, `maximum_event_minutes=180`) |
| depth=0 socket | OK, ~17–40 ms; `levels_capped_at_1000=false`; raw levels ~41k/21k |
| OB1000 socket | status OK (`stopped` without chart lease — unchanged semantics) |
| OB200 raw-archive | still writing open segments; feature writer disabled |
| Legacy originals | size + SHA256 unchanged; holders empty |
| New events ≠ legacy IDs | yes |

### Failed (acceptance)

| Check | Result |
|---|---|
| **`queue_drop_count=0`** | **FAIL** — health `writer_queue_drops` **0 → ~66630** over smoke; per-symbol sink drops ~**33315** each and still rising |
| Writer backlog “controlled” | Reported `writer_backlog=0` while drops increase (fail-closed `put_nowait` on full queue / writer slower than full-depth feed) |
| New event completeness | `data_quality=INCOMPLETE` (`PREBUFFER_EMPTY`; live also accumulating queue drops) |
| Real CROSS_IN signal | **None** during window (bootstrap only). Cannot prove live UPPER/LOWER cross, `first_persisted_ts < trigger_ts`, or segment-rotation non-termination on a natural fight |

Files under new events do grow (BTC deltas ~8 MB after ~19 min), so the writer thread is not fully dead — but drop rate fails the smoke contract.

### Real-signal checklist (N/A this window)

- Echter UPPER/LOWER CROSS_IN: not observed  
- `minimum_capture_end_ts = trigger + 3600`: true on bootstrap manifests  
- `first_persisted_ts < trigger_ts`: **null** / prebuffer empty  
- Segmentwechsel beendet Event nicht: not exercised  
- Kein Doppel-Event: one active fight ID per symbol  

---

## Forbidden actions (compliance)

- No parallel second collector  
- No DB writes  
- No dashboard restart  
- OI process unchanged (147111)  
- No trading  
- No automatic deletion  
- Legacy original `.tmp` unchanged  
- No commit / no push  
- Exactly one stop/start cutover  

---

## Remaining limits / next actions (not executed)

1. **Root-cause FR queue drops** under live full-depth (`depth=0`) load with queue_size=4096 — writer cannot sustain dual-symbol encode+zstd; needs offline fix + **separate** approved restart (out of scope for this cutover).
2. After a fix: allow ≥10 min prebuffer before expecting COMPLETE bootstrap/CROSS_IN captures.
3. Legacy events remain quarantine/recovery artifacts only; do not append or reopen originals.
4. Current PID **1481866** continues capturing incomplete events until deliberately stopped.

---

## Artifact index

```
results/btc_doge_full_ob_edge_flight_recorder_v1/edge_capture_1h_v1/
  PRE_CUTOVER_INVENTORY.json
  PHASE_F_SMOKE.json
  LEGACY_CUTOVER_REPORT.md          ← this file
  legacy_recovery/
    PHASE_D_RECOVERY.json
    _quarantine_bit_identical/
    BTCUSDT_20260903T184212Z_4a22a89fe6/
    DOGEUSDT_20260903T184212Z_f5d68293cd/
```

Code (orderbook_analyse):

- `.../full_ob_edge_flight_recorder/legacy_tmp_recovery.py`
- `tests/test_legacy_tmp_recovery.py` (7 passed)

---

## ENDVERDICT

```
NEW_RECORDER_LIVE_VALIDATION_FAILED
```
