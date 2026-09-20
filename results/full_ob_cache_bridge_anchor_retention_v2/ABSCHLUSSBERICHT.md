# ABSCHLUSSBERICHT — full_ob_cache_bridge_anchor_retention_v2

## Verdict

`FULL_OB_CACHE_BRIDGE_ANCHOR_RETENTION_V2_READY_RESTART_REQUIRED`

## 1. Root Cause
Checkpoint retention == delta retention (600s) plus anchor deadline tied to oldest ring delta.
Oldest periodic checkpoint lagged the ring edge -> systematic BOOK_ANCHOR_MISSING.

## 2. Changed files / functions
See IMPLEMENTATION_REPORT.md (config/protocol/checkpoints/freeze/service/dump + tests).

## 3. Delta- and Checkpoint-Retention
- Delta: **600 s** (unchanged FR analysis window)
- Checkpoint: **690 s** (= 600 + 60 + 30)

## 4. Safety Margin
**30 s** — covers recv/wall skew and store latency beyond worst-case interval phase offset (60 s). Documented in CONTRACT.md / config defaults.

## 5. Anchor selection
Latest checkpoint with checkpoint_ts <= requested_pre_roll_start_ts (= analysis_start). No later substitute.

## 6. Initial-Checkpoint order
Forced INITIAL_CHECKPOINT on first book_ready per epoch; replay skips u <= anchor.u.

## 7. Resync / Epoch
Reconnect bumps epoch without wiping history; next ready stores RESYNC_CHECKPOINT. Multi-epoch replay applies resync snapshots mid-stream. Missing resync -> RESYNC_CHECKPOINT_MISSING.

## 8. Offline-Replay
Synthetic 600 s analysis pre-roll with adverse early anchor; multi-epoch with resync — both DATA_COMPLETE.

## 9. Hash / Level / u / seq parity
exact_replay_parity.json: REPLAY_PARITY=True, levels=60000, u=601, seq=6010, hash_match=True, crossed=False

## 10. Lock-Zeit / RAM
Checkpoint build (~60k levels): 20.302 ms; orjson serialize: 1.839 ms (outside book lock). RSS delta approx 21.3 MiB in offline process. Payload 1844753 bytes < 96 MiB.

## 11. Tests
`88 passed in 4.49s` — failures: 0

## 12. Live PIDs (read-only)
- bybit-full-ob-raw-archive-btc-doge.service MainPID: **1978678**

## 13. Confirmations
- No collector restart
- No live freeze
- No live env change
- No DB writes
- No forecast / state analyzer
- No trading logic
- No commit / push
- Dirty worktrees preserved

## Next
Await explicit approval for exactly one controlled collector restart and one new BTC live freeze.
