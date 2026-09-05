# Full-OB Combined Restart Live v1

**Verdict:** `FULL_OB_COMBINED_RESTART_LIVE_SMOKE_PASS`  
**Audit time:** 2026-09-04T~11:45Z

```text
DESTRUCTIVE_ACTIONS_EXECUTED=false
DB_UNCHANGED=true
COMMIT_PUSH=false
```

---

## Cutover

| | Value |
|--|--|
| Old collector PID | **1565672** (SIGTERM, exited ~2s) |
| New collector PID | **1692334** |
| OI PID | **147111** unchanged |
| Exactly one collector | **yes** |
| Start script | `scripts/start_orderbook_v3_raw_archive_btc_doge.sh` |
| Symbols | BTCUSDT, DOGEUSDT only |

Env activated: `OB_V3_FULL_OB_FLIGHT_RECORDER_ENABLE=true`, `OB_V3_FULL_OB_FR_NESTED_SIGNALS_ENABLE=true`, `OB_V3_FULL_OB_FR_MAX_PROFILE_WATCHES=8`.

---

## Feature activation (live)

| Feature | Active |
|---------|--------|
| Resync checkpoint | **yes** — metrics present; `resync_checkpoint_failure_count=0`; no forced reconnect |
| Nested signals | **yes** — `nested_profile_signals_enabled=true` |
| Signal isolation | **yes** — `analysis_isolation_contract=nested_signal_analysis_isolation_v1`; overlap clusters written |

```text
SIGNAL_LEVEL_ANALYSIS_ISOLATION=true
OVERLAP_CLUSTERING_IMPLEMENTED=true
```

---

## Full book

| Symbol | book_ready | raw_bids | raw_asks | gap_count |
|--------|------------|----------|----------|-----------|
| BTCUSDT | true | ~40626 | ~23628 | 0 |
| DOGEUSDT | true | ~5336 | ~13804 | 0 |

Topics confirmed: `orderbook.full.BTCUSDT`, `orderbook.full.DOGEUSDT` (+ OB200 archive topics).  
UI depth=0 snapshot uses `MAX_UI_BARS_PER_SIDE=600` (not `levels_capped_at_1000`); raw full depth remains uncapped in memory.

---

## Writer / queue

| Metric | Value |
|--------|-------|
| queue_drop_count | **0** |
| writer_error_count | **0** |
| resync_checkpoint_failure_count | **0** |
| transport_reconnect_count | **0** |

---

## Socket regression

| Depth | Result |
|-------|--------|
| 0 (full) | ok, ~5–19 ms, book_mode=full |
| 1000 | ok after acquire, ~2 ms, 1000/1000 levels |
| 200 | on-demand socket rejects (`only_depth_1000_supported`); OB200 verified via continuous raw-archive health (200/200 levels, LIVE) |

See `SOCKET_REGRESSION.csv`.

---

## Bootstrap / ringbuffer

- Startup: `BOOTSTRAP_ALREADY_IN_EDGE_ZONE` → **no** persistent capture until real arm/cross.
- After restart, natural crosses occurred before 600 s wall-clock fill:
  - BTC parent `pre_trigger_seconds_actual ≈ 82 s`
  - DOGE max observed prebuffer in smoke samples ≈ 210 s before its parent trigger
- Not a feature failure: early genuine signals after cold start. Bootstrap gate worked.

---

## Natural signals (observed)

| Event | Nested signals | Isolation |
|-------|----------------|-----------|
| `BTCUSDT_20260904T112735Z_eb6191222e` | 1× LOWER nested | parent+nested contracts + overlap cluster |
| `DOGEUSDT_20260904T113003Z_bb7dfca728` | 2× (LOWER + UPPER) | contracts + overlap clustering |

- **1 parent capture per symbol** (no second Full-OB event dir)
- Nested ledgers: `nested_profile_signals.jsonl`
- Isolation: `signal_analysis_contracts.jsonl`
- `SECONDARY_OBSERVATIONS_SUPPRESSED` flood not present as thousands of markers
- Open `.tmp` only on these **active** FIGHT_ACTIVE events (expected)
- Sep‑3 orphan `.tmp` (6) **untouched**

---

## DB

`research_full_ob_smoke.full_ob_packets_smoke_v1` still **1514** rows — no CH writes this run.

---

## Artefacts

```text
results/full_ob_combined_restart_live_v1/
├── PRE_RESTART_GATES.json
├── PROCESS_CUTOVER.json
├── LIVE_FEATURE_FLAGS.json
├── FIFTEEN_MINUTE_SMOKE.json
├── SOCKET_REGRESSION.csv
├── RINGBUFFER_STATUS.json
├── LIVE_HEALTH_FINAL.json
├── NATURAL_SIGNALS_LIVE.json
└── REPORT.md
```

**Collector left running** (PID 1692334). No second restart.
