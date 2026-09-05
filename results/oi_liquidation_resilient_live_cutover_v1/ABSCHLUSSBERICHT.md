# ABSCHLUSSBERICHT — OI/Liq Resilient Live Cutover v1

## Verdict

```text
OI_LIQ_LIVE_VALIDATION_FAILED
```

## Summary

Controlled cutover of the OI/Liquidation collector onto resilience code completed via user-systemd (`SIGTERM` stop → start). New PID **1786328** replaced **147111**. End-to-end OI source→DB progress resumed after multi-day freeze, but the 15-minute smoke **failed hard gates** due to a live spool metadata rename race (`meta.json.tmp`), which produced writer errors and WebSocket reconnect churn.

Phase F OI-5m catch-up was **not** run (fail-closed after failed smoke).

## Checklist answers

1. Old/new PID: **147111 → 1786328**
2. Restart method: `systemctl --user stop` + `start` (SIGTERM only)
3. Instance count: **1**
4. WebSocket/topics: alive with reconnect storm (28 reconnects); liq subscription treated active
5. Writer: alive (`writer_alive=true`), but `writer_error_count=7`
6. Heartbeat: health/insert timestamps continuous during smoke (pass)
7. OI source progress: **true**
8. OI DB progress: **true** (Δ 5237 rows in smoke window)
9. Liquidations: `LIVE_EVENTS_OBSERVED`
10. Queue drops: **0**
11. Writer errors: **7** (gate fail)
12. Spool unacked (end health): 8 records / 7696 bytes
13. Persistence lag: max 0.833s (within contract)
14. ClickHouse errors: post-restart `SESSION_IS_LOCKED=0`; spool FileNotFoundError on meta rename
15. Nachgezogene 5m-Buckets: **0** (Phase F skipped)
16. Verbleibende Lücken: OI-5m hist still cutoff **2026-09-04T17:35:00Z**; ~9 closed buckets/symbol pending; live 5s briefly stalled during reconnects
17. Other PIDs unchanged: Dashboard **1780509**, PT **1661773**
18. Full OB weiterhin gestoppt: **true**
19. `DESTRUCTIVE_ACTIONS_EXECUTED=false`

## Failed gates

- `WRITER_ERRORS`
- `SPOOL_HEALTHY`

## Next safe action (manual; not executed)

Fix spool `meta.json` atomic replace race in OA worktree (exclusive lock / retry on ENOENT), re-run unit tests, then a **single** controlled restart under a new cutover work package. Do not SIGKILL; do not start a second instance.

## Artifacts

`results/oi_liquidation_resilient_live_cutover_v1/`
