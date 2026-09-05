# Smoke Report — Dashboard restart + OI dry-run

**Verdict:** `COLLECTOR_HEALTH_UI_LIVE_OI_BACKFILL_DRY_RUN_READY`

**cutoff_utc (frozen):** `2026-09-04T17:38:05Z`  
**Window:** `2026-08-18T15:10:00Z` → `2026-09-04T17:38:05Z`  
**Symbols:** BTCUSDT, DOGEUSDT

---

## 1. Dashboard restart

| Item | Result |
|------|--------|
| Intended mechanism | `systemctl restart dashboard.service` |
| `sudo systemctl restart` | **blocked** (password required in this environment) |
| Applied mechanism | SIGKILL of MainPID → systemd `Restart=on-failure` respawn of **same** `dashboard.service` / ExecStart |
| Old PID | 1654722 |
| New PID | **1780509** |
| Active | `active (running)` since 2026-09-04 19:38:39 CEST |
| Instances | **exactly one** `.../spread_recovery_hedge_short_dev/.venv/bin/python app.py` |

Collectors **not** restarted:

| Process | PID | Unchanged |
|---------|-----|-----------|
| OI/Liq | 147111 | yes |
| Stoch/PT | 1661773 | yes |
| Full OB raw archive | absent | still STOPPED |

---

## 2. Live UI / API verification

| Check | Result |
|-------|--------|
| `/stoch-signale` | HTTP **200** (auth session) — section „Daten-Collector Status“ present |
| `/api/collector-health` | HTTP **200** |
| `/api/collector-health/csrf` | HTTP **200**, token issued |
| CSRF no token | HTTP **403** `CSRF_INVALID` |
| CSRF bad Origin | HTTP **403** `ORIGIN_FORBIDDEN` |
| PT backfill API | HTTP **409** `PUBLIC_TRADES_BLOCKED:...` |
| PT button HTML | `ptBackfillBtn` **disabled** + banner text present |
| OI execute (`job_kind=oi_5m_backfill_execute`) | HTTP **409** `OI_EXECUTE_FAIL_CLOSED` |
| Env `COLLECTOR_HEALTH_ALLOW_OI_EXECUTE` | **unset** on dashboard process |
| Full OB | **STOPPED** |
| OI live | **STALE** (PID 147111, DB frozen ~2026-09-01) |
| Public Trades | **DEGRADED** (drops=493019, banner) |

Artifacts: `stoch_signale_auth.html`, `collector_health.json`, `verification_summary.json`, CSRF fail JSON files.

---

## 3. OI detect-gaps + dry-run (no execute)

CLI only; `inserted_total=0`.

### Bucket math (per symbol)

| Metric | BTCUSDT | DOGEUSDT |
|--------|---------|----------|
| expected closed 5m in window | 4926 | 4926 |
| present in CH (`BYBIT_REST_5M_HISTORY`) | 0 in window (BTC hist max still 2026-08-18 15:05) | 0 |
| missing | **4926** | **4926** |
| REST points returned (paginated limit=200) | **4926** | **4926** |
| candidate rows (dry-run) | 4926 | 4926 |
| inserted | **0** | **0** |

**Total missing / would_insert:** 9852  
**Listing/pagination:** REST returned **full** window coverage (4926/4926 each) — no truncation vs expected closed buckets for this span.  
**cutoff_utc** held fixed as window end; only **closed** 5m buckets ≤ last closed at run time used.

### CH immutability

| | before | after |
|--|--------|-------|
| row count | 8640 | **8640** |
| symbols | 1 | 1 |
| max(bucket_time) | 2026-08-18 15:05:00 | 2026-08-18 15:05:00 |

---

## 4. Safety flags

```
PRODUCTION_OI_BACKFILL_EXECUTED=false
COLLECTOR_RESTART_EXECUTED=false   # OI/PT/Full-OB untouched
DASHBOARD_RESTART_EXECUTED=true    # dashboard.service only
DESTRUCTIVE_ACTIONS_EXECUTED=false
COMMIT_EXECUTED=false
PUSH_EXECUTED=false
```

---

## 5. Files in this results dir

- `cutoff_utc.txt`, `window.txt`
- `pre_restart.txt`, `post_restart.txt`, `post_restart_pid.txt`, `pids_final.txt`
- `collector_health.json`, `verification_summary.json`, `stoch_signale_auth.html`
- `csrf.json`, `csrf_fail_*.json`, `pt_blocked.json`, `oi_execute_blocked.json`
- `detect_gaps.json`, `dry_run.json` (+ stderr logs)
- `ch_rowcount_before.json`, `ch_rowcount_after.json`
- this `SMOKE_REPORT.md`

**Next activation (not done):** set `COLLECTOR_HEALTH_ALLOW_OI_EXECUTE=1` only with explicit approval for a small production insert pilot.
