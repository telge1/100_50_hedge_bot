# PHASE A AUDIT — Collector Health & Backfill

**Results dir:** `/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/results/collector_health_backfill_audit_v1/`  
**Mode:** read-only only. No collector restart, no dashboard restart, no prod backfill, no commit, no push.  
**Evidence window:** 2026-09-04 ~17:15–17:19 UTC.

---

## A1. Repository / Git proof

| Repo | Absolute path | Branch | HEAD | Dirty lines (approx) |
|------|---------------|--------|------|----------------------|
| Research/Dashboard (SR) | `/home/telgenbuescher/projects/spread_recovery_hedge_short_dev` | `feature/btc-doge-research-db` | `f7125a7ec9e3e455390234fb496ee40db3be15cd` | ~144 |
| Collectors/CH (OA) | `/home/telgenbuescher/projects/orderbook_analyse` | `feature/strategy-lab-phase1` | `bcf13edb8613570ed3c5addab6af08f93d99f45e` | ~269 |
| Stoch live / PT (SG) | `/home/telgenbuescher/projects/Signal_Generator_Ralf/signal_generator_stoch_waves` | `main` | `8b906363255f492d2bb7d6f2779083024b39763c` | ~12 |

### Dirty overlap relevant to this task

**OA (critical):**

- `M src/orderbook_analyse/oi_liquidation_collector/{collector,settings,writer}.py`
- `M deploy/systemd/bybit-oi-liquidation-collector.service`
- `?? .../health_snapshot.py`, `?? .../spool.py`
- `backfill.py` itself **not** listed dirty → reusable without touching foreign hunks

**SG (critical for PT completion):**

- Dirty: `run_public_trades_7d_backfill.py`, `live/collector.py`, `trade_buffer.py`, `public_trades/{backfill_*,downloader}.py`, `db/client.py`, tests
- Untracked: 30d pipeline, window helpers, extra tests

**SR (dashboard health UI):**

- Heavy dirty under research charts / market profile / btc_ob_fight
- `dashboard/app.py`, `stoch_signale.html`, `stoch_signale.js` — **clean** at audit time (safe additive surface if kept additive)

Foreign dirty trees fully respected; no resets/stashes.

---

## A2. Collector inventory (summary)

See `collector_inventory.csv` for full columns.

| collector_id | PID | Process | Data verdict |
|--------------|-----|---------|--------------|
| full_ob_raw_archive | ~~1692334~~ **gone** | STOPPED | STOPPED |
| oi_liquidation_live | **147111** (systemd user since 2026-08-18) | RUNNING | **STALE** (DB/health frozen ~2026-09-01 16:46) |
| oi_5m_rest_history | N/A | idle historical | gap since 2026-08-18 (BTC only) |
| public_trades_live + candles_1m | **1661773** | RUNNING LIVE | PT **DEGRADED** (fresh DB but 493019 drops); candles recovery OK |
| dashboard_web | **1654722** | RUNNING `:8080` | N/A |
| research_oi_mysql | N/A | batch only | UNKNOWN live counts (ACL) |

**Start mechanisms:** systemd user `bybit-oi-liquidation-collector.service`; SG `run_live_collector_service.py`; Full-OB was nohup/lock (now stopped); dashboard `python app.py`.

**Assumption rejected:** PID alive ≠ healthy (OI proves it).

---

## A3. Freshness dual measurement (proof)

| Metric | T0 17:16:56Z | T1 17:17:24Z | Result |
|--------|--------------|--------------|--------|
| PT `max(trade_ts)` | 17:16:51.024 | 17:17:23.415 | **advances** |
| PT rows / 10m | 127322 | 130442 | **grows** |
| PT symbols / 10m | 51 | 51 | coverage OK |
| OI5s `max(bucket_time)` | 2026-09-01 16:46:50 | same | **frozen** |
| OI health `max(event_ts)` | 2026-09-01 16:46:05 | same | **no heartbeat** |
| Liq max | 2026-09-01 16:41:23 | same | stale with OI |

**API corroboration** (`GET http://127.0.0.1:8787/api/collector/status`):

- `state=LIVE`, `public_trades_enabled=true`, 51 symbols
- PT lag ≈ 0.09s, `insert_failures=0`, `queue_depth=2`
- `dropped_events=493019`, `last_error=queue_full_dropped_event` → **DEGRADED**
- Recovery: candles only (`recovery_gap_count=51`, `candles_inserted=102` at 2026-09-03 start)

**Freshness limits used** (derived, documented in `oi_schema_semantics.md` / plan): PT lag warn 30s / stale 120s; OI/Liq heartbeat 60s; OI value flat allowed if heartbeat fresh.

**OI live logs:** `SESSION_IS_LOCKED` insert failures ~2026-09-01; ongoing reconnect warnings through 2026-09-04 while process still up.

---

## A4. OI schema / semantics

See `oi_schema_semantics.md`.

**Headline:**

- Live 51-coin OI/Liq → ClickHouse `orderbook_analysis.*`
- Research 5m OI → MySQL `research_open_interest_5m` (`derivatives_5m_v1`), last-in-bucket from 1m source
- `oi_change_*` not in OI tables; gap-safe in feature code
- Research live COUNT blocked this session (ACL)

---

## A5. Bybit REST OI parity

See `oi_rest_parity.csv`.

**Sample BTCUSDT** overlapping CH hist end (4h → 49 points):

- REST fields: `openInterest`, `singleOpenInterest`, `timestamp` (no `openInterestValue` in sample)
- Sort: newest-first; pagination cursor empty at n&lt;200
- `timestamp` ms **equals** CH `bucket_time` (no +5m shift)
- `openInterest` **exact Decimal match** 49/49

**Decision for CH `open_interest_5m_history`:**

### `DIRECT_5M_COMPATIBLE`

Caveats:

- Map `openInterest` → `open_interest` only (stock; never sum/mean).
- `open_interest_value` may be null if REST omits it.
- Research MySQL target would be `COMPATIBLE_AFTER_SEMANTIC_CONVERSION` (different columns/provenance) — **out of scope** until ACL + design.

DOGE: no CH overlap rows → no numeric parity; REST endpoint still applicable for future backfill.

**Gaps:** BTC missing **4921** closed 5m buckets from `2026-08-18 15:10` → `2026-09-04 17:10` (`oi_gap_report.csv`). Plus 50 symbols never in 5m hist.

---

## A6. Public trades backfill

See `public_trades_backfill_audit.md`.

### `PUBLIC_TRADES_BACKFILL_PARTIALLY_READY`

Restart recovers **candles**, not archive trades. CLI backfill exists; live `gap_fill` unused; queue drops not auto-repaired.

---

## Phase A artifacts checklist

| File | Status |
|------|--------|
| `PHASE_A_AUDIT.md` | this file |
| `collector_inventory.csv` | written |
| `collector_runtime_evidence.csv` | written |
| `collector_database_freshness.csv` | written |
| `symbol_coverage.csv` | written |
| `oi_schema_semantics.md` | written |
| `oi_rest_parity.csv` | written |
| `oi_gap_report.csv` | written |
| `public_trades_backfill_audit.md` | written |
| `implementation_plan.md` | written |
| `commands_read_only.log` | appended |

Auxiliary: `_freshness_raw.json`, `_collector_status.json` (may contain non-secret operational metrics).

---

## Phase A → B gates

| Gate | Result |
|------|--------|
| OI CH semantics + REST parity | **PASS** |
| Research MySQL live proof | **FAIL / UNKNOWN** (ACL) |
| PT classification | **PASS** (PARTIALLY_READY) |
| Dirty overlap resolvable | **CONDITIONAL** — new-file-only + CH OI target |
| Staging/tests possible | **PASS** |

### STOP — Phase B not started

**Interim verdict:** proofs sufficient for a **CH-targeted** OI backfill + dashboard health **if** human approves new-file-only scope. Full brief (research MySQL + editing dirty OA/SG) remains:

- `IMPLEMENTATION_BLOCKED_BY_DIRTY_OVERLAP` (if dirty modules must change), and/or  
- `IMPLEMENTATION_BLOCKED_BY_UNPROVEN_SEMANTICS` (if research MySQL is mandatory SoT without ACL).

**Confirmations required before Phase B:**

1. OI SoT for backfill/UI = CH `open_interest_5m_history`?  
2. New-file-only implementation OK?  
3. PT button remains disabled until READY re-audit?

**Confirmations of safety this phase:**

```
PRODUCTION_BACKFILL_EXECUTED=false
COLLECTOR_RESTART_EXECUTED=false
DASHBOARD_RESTART_EXECUTED=false
DESTRUCTIVE_ACTIONS_EXECUTED=false
PUSH_EXECUTED=false
COMMIT_EXECUTED=false
```
