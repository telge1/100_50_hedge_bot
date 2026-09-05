# PREFLIGHT — OI/Liq Resilient Live Cutover

**UTC:** 2026-09-04T18:04:13Z

## Gates

| Gate | Result |
|------|--------|
| Resilience verdict | `OI_LIQ_WRITER_HEARTBEAT_SPOOL_READY_RESTART_REQUIRED` |
| Tests | **33 passed** (`test_oi_liquidation_resilience_v1` + collector) |
| Worktree has spool/writer/health | yes |
| Running process loads new code | **no** (in-memory since 2026-08-18) — restart required |
| Dirty hunks | present on collector modules — **not modified** this cutover; they ARE the resilience code |

## Process before

| Field | Value |
|-------|-------|
| PID | **147111** |
| Cmd | `.venv/bin/python -m orderbook_analyse.oi_liquidation_collector --mode live --duration 0` |
| CWD | `/home/telgenbuescher/projects/orderbook_analyse` |
| Mechanism | `systemctl --user` unit `bybit-oi-liquidation-collector.service` |
| Live Restart policy | `Restart=always` (old) |
| Prepared policy | `Restart=on-failure`, `TimeoutStopSec=60` |
| Lock/PID files | `logs/oi_liquidation_collector.lock` / `.pid` = 147111 |
| Instances | **1** real collector (pgrep noise from shells) |
| CLOSE-WAIT to CH :8123 | **yes** (fd 12) — matches known SESSION_IS_LOCKED failure mode |
| Spool dir | writable; empty segments; 352G free |

## CH before (stale)

| Table | max exchange ts | notes |
|-------|-----------------|-------|
| open_interest_5s | 2026-09-01 16:46:50 | 51 symbols frozen |
| open_interest_events | 2026-09-01 16:46:23 | frozen |
| all_liquidations | 2026-09-01 16:41:23 | frozen |
| oi_liquidation_health | 2026-09-01 16:46:05 | frozen |
| open_interest_5m_history | 2026-09-04 17:35:00 | REST backfill SoT (2 symbols) |

## Protected PIDs

- Dashboard **1780509** — do not touch  
- Public trades **1661773** — do not touch  
- Full OB — ABSENT / STOPPED  

## Restart plan

1. Install prepared unit → `~/.config/systemd/user/bybit-oi-liquidation-collector.service`  
2. `systemctl --user daemon-reload`  
3. `systemctl --user stop` (SIGTERM, TimeoutStopSec=60)  
4. Confirm PID 147111 gone, lock free  
5. `systemctl --user start`  
6. Validate single instance + health file + CH progress  

**PREFLIGHT_PASS=true**
