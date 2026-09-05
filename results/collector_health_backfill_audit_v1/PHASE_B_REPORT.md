# Phase B Report — Offline Implementation

**Verdict:** `COLLECTOR_HEALTH_AND_OI_BACKFILL_READY_ACTIVATION_REQUIRED`

## Decisions applied

1. OI SoT = CH `open_interest_5m_history` (REST 5m)
2. Dirty worktrees untouched (new files + clean integrations only)
3. PT backfill UI disabled + banner
4. Full OB left STOPPED (no start)

## New files

### Package `dashboard/collector_health/`

- `__init__.py`, `contract.py`, `csrf.py`, `ch_config.py`, `probes.py`
- `oi_backfill.py` — gap detect, pagination, dry-run default, advisory lock, closed buckets only
- `service.py` — evidence health assembly + short cache
- `jobs.py` — argv-safe job runner, PT blocked, execute fail-closed
- `api.py` — FastAPI router

### Other

- `scripts/oi_5m_history_backfill.py`
- `dashboard/tests/test_collector_health_phase_b.py`
- `dashboard/tests/test_collector_health_api.py`
- `results/collector_health_backfill_audit_v1/collector_health_contract.md`
- `results/collector_health_backfill_audit_v1/phase_b_test_results.txt`
- this report

## Minimal clean integrations

- `dashboard/app.py` — `include_router(collector_health)`
- `dashboard/templates/stoch_signale.html` — section „Daten-Collector Status“
- `dashboard/static/js/stoch_signale.js` — poll + OI detect/dry-run (PT button disabled)

## Dirty patches NOT applied (not required)

None. Existing OA `oi_liquidation_collector/{collector,settings,writer}.py` and SG PT dirty files were **not** edited.

Optional future (document only, not applied): wire live OI writer reconnect/session fixes in dirty `writer.py` — separate approval.

## Tests

Offline pytest: see `phase_b_test_results.txt` (17 passed after cache-clear).

CLI read-only `--detect-gaps` sample (BTCUSDT 2026-08-18 15:00–16:00Z): status OK, missing_buckets=11, `inserted_total=0`.

## Readiness

| Item | Status |
|------|--------|
| OI backfill | **READY** (dry-run/detect); execute needs `COLLECTOR_HEALTH_ALLOW_OI_EXECUTE=1` + activation |
| Public trades backfill | **NOT READY** (gate PARTIALLY_READY; button disabled) |
| Collector health | **READY** (code); dashboard process not restarted → UI live after activation restart |
| Full OB | **STOPPED** (unchanged) |

## Runtime safety

```
PIDs unchanged at check: 147111 (OI), 1661773 (Stoch/PT), 1654722 (dashboard)
Full-OB raw archive: absent
PRODUCTION_BACKFILL_EXECUTED=false
COLLECTOR_RESTART_EXECUTED=false
DASHBOARD_RESTART_EXECUTED=false
DESTRUCTIVE_ACTIONS_EXECUTED=false
PUSH_EXECUTED=false
COMMIT_EXECUTED=false
```

## Activation still required

1. Dashboard restart to load new routes/UI (explicit approval)
2. Optional small BTCUSDT/DOGEUSDT OI dry-run then execute pilot with env flag
3. No Full-OB / OI-live / PT collector restarts in this package
