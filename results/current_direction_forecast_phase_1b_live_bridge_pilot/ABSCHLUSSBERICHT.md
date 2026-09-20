# CURRENT_DIRECTION_FORECAST — Phase 1B Live Bridge Pilot

## Verdict

`CURRENT_DIRECTION_FULL_OB_BRIDGE_LIVE_V1_READY_WITH_GAPS`

## Summary

Controlled activation of the Full-OB cache bridge succeeded (Unix socket `0600`, no TCP, healthy collector after one restart, ≥590s coverage, gap/overflow 0). The single authorized BTC `freeze_pre_roll` returned **`BOOK_ANCHOR_MISSING`** / `data_complete=false` because the ringbuffer retains deltas before the oldest periodic checkpoint. Exact `REPLAY_PARITY` therefore failed. Bridge left **enabled**; no rollback.

## Repositories

| Repo | Branch | HEAD | Dirty |
|------|--------|------|-------|
| spread_recovery_hedge_short_dev | feature/btc-doge-research-db | see final_git_checks.txt | yes (pre-existing + new results/) |
| orderbook_analyse | feature/strategy-lab-phase1 | see final_git_checks.txt | yes (Phase-1 bridge + `.env` bridge keys) |

## Service / Restart

- Service: `bybit-full-ob-raw-archive-btc-doge.service`
- Old MainPID: `1902763`
- New MainPID: `1978678`
- Restarts this pilot: **1** (activation only; no rollback restart)
- Active after restart: `active` / `running`
- Old PID gone: `True`

## Activated Env (non-secret)

```
FULL_OB_CACHE_BRIDGE_ENABLED=true
FULL_OB_CACHE_BRIDGE_SOCKET_PATH=/run/user/1000/full_ob_cache_bridge.sock
FULL_OB_CACHE_BRIDGE_DUMP_ROOT=/home/telgenbuescher/projects/orderbook_analyse/data/orderbook_raw_shadow/full_ob_cache_bridge_dumps
FULL_OB_CACHE_BRIDGE_SYMBOLS=BTCUSDT,DOGEUSDT
FULL_OB_CACHE_BRIDGE_MAX_PRE_ROLL_SEC=600
FULL_OB_CACHE_BRIDGE_MAX_PAYLOAD_BYTES=100663296
FULL_OB_CACHE_BRIDGE_MAX_CONCURRENT=1
FULL_OB_CACHE_BRIDGE_MIN_INTERVAL_SEC=2
FULL_OB_CACHE_BRIDGE_CHECKPOINT_INTERVAL_SEC=60
```

- Env file SHA256 before: `b1d4763a432cb4865f0068a3bc3904455baf24f2682d780c491822887a2d462c`
- Env file SHA256 after: `a1054818a2f3bd3fcd528f69ccb8762a89ecff2401bf81bc1fd59b0c16c26950`

## Socket

- Path: `/run/user/1000/full_ob_cache_bridge.sock`
- Mode: `0o600`
- Owner: `telgenbuescher:telgenbuescher`
- TCP listen: false
- Dump root: `/home/telgenbuescher/projects/orderbook_analyse/data/orderbook_raw_shadow/full_ob_cache_bridge_dumps` (mode 0700)

## Preflight

- Tests: 67 passed; Phase-1 code fingerprint match: true
- Precheck: `PASS`
- Pre RSS ~`246.8` MiB; gaps 0; reconnects 0; bridge was off

## Warm-up

- Elapsed to gate: `599` s
- Actual retention BTC/DOGE: `596.947` / `596.488` s (≥590)
- Checkpoints: `10` / `10`
- Protocol: `full_ob_cache_bridge_protocol_v1`
- Instance: `collector-1978678`
- Replay capability (status warmness): `RAW_FULL_BOOK_REPLAY`
- Gap/overflow both symbols: 0 / 0

## BTC Freeze (exactly one)

- Request ID: `phase1b_btc_freeze_20260905T162323Z`
- T0 UTC: `2026-09-05T16:23:23.328089Z`
- Actual pre-roll: `599.801233104` s
- Messages: `3000`
- Status: **`BOOK_ANCHOR_MISSING`**
- data_complete: `False`
- Exclusion: `['BOOK_ANCHOR_MISSING']`
- Gap/overflow/reconnect: `0` / `0` / `0`
- Payload MiB: `8.8331` (<96)
- Payload SHA256 OK: `True`
- Manifest SHA256 OK: `True`
- All recv ≤ T0: `True`
- Anchor present: `False`
- Second freeze limited: `REQUEST_BUSY` (`rate_limited`)
- Dump (outside git): `/home/telgenbuescher/projects/orderbook_analyse/data/orderbook_raw_shadow/full_ob_cache_bridge_dumps/phase1b_btc_freeze_20260905T162323Z`

## Anchor failure (known gap)

Bridge selects latest_at_or_before(max(target_start, first_ring_recv)). FR ring keeps deltas received before the oldest remaining periodic checkpoint, so no checkpoint satisfies receive_time_ns <= first_ring_recv. Freeze therefore returns BOOK_ANCHOR_MISSING with anchor=null.

Evidence: oldest checkpoint ≈ `24.400138351` s after buffer start; dump anchor null.

No second freeze and no code-fix restart were authorized in this pilot.

## Replay

- `REPLAY_PARITY`: **`False`**
- Reason: BOOK_ANCHOR_MISSING — dump contains no periodic book checkpoint anchor; exact RAW_FULL_BOOK_REPLAY not possible for this freeze

## Post-freeze health

- Healthy: `True`
- MainPID stable: `True` (`1978678`)
- RSS first→last (post samples): `328.15` → `331.89` MiB
- Swap first→last: `1410` → `1410` MiB
- Raw archive progressing: `True`
- Traceback in journal: `False`
- Bridge left enabled: `True`
- Rollback: `False`

## Resources

| Phase | Collector RSS (approx) |
|-------|------------------------|
| Before restart | ~246.8 MiB |
| Warm-up (min/max) | 168.85 / 321.66 MiB |
| Pre-freeze sample | 326.38 MiB |
| Post-freeze | 328.15–331.89 MiB |

Delta vs pre-restart ≈ **+85.1 MiB** (planned band ~50–150 MiB). Within/near band; not dressed up.

## Confirmations

- No Forecast / State Analyzer built
- No DB writes
- No dashboard restart
- No strategy / trading action
- No commit / push
- No package install
- No raw data deletion
- Exactly one activation restart; bridge remains enabled with documented freeze gap

## Next (out of scope)

Fix checkpoint-vs-ring selection so freeze chooses an anchor at/before the first *replayable* delta (or trims pre-anchor deltas), then re-run a controlled freeze pilot. Not done here.
