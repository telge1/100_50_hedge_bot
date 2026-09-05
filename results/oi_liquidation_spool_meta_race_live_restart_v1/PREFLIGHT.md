# PREFLIGHT — Spool Meta Fix Live Restart

**UTC:** 2026-09-04T18:45:08Z

## Gates

| Gate | Result |
|------|--------|
| Fix verdict | `OI_LIQ_SPOOL_META_RACE_FIXED_READY_RESTART_REQUIRED` |
| Tests | **52 passed** |
| Fix in worktree (`SpoolMetaError`, unique tmp, `_ack_spool_seqs`) | yes |
| Running PID | **1786328** (old in-memory code — restart required) |
| Instances | **1** |
| Spool files | not deleted/modified |
| Free disk | 352G |
| Orphan meta.tmp | none |
| Meta | last_acked=32531 next=32532 (no generation field — pre-fix format) |
| Unacked in segments | 0 |
| Protected PIDs | Dashboard 1780509, PT 1661773, Full OB ABSENT |

## CH before

- oi5s max: 2026-09-04 18:41:30 count=12367769
- oie historical extras since first cutover: 217
- liq parity: {'count': 16, 'uniq': 16}
- hist 5m BTC/DOGE max: 2026-09-04 17:35:00 / 2026-09-04 17:35:00

## Restart plan

`systemctl --user stop` (SIGTERM, TimeoutStopSec=60) → confirm PID gone → `systemctl --user start` (single shot)

**PREFLIGHT_PASS=true**
