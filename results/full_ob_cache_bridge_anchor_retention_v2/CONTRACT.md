# Contract — `full_ob_cache_bridge_anchor_retention_v2`

## Identity

```text
contract_id: full_ob_cache_bridge_anchor_retention_v2
protocol_version: full_ob_cache_bridge_protocol_v1   # wire protocol unchanged
semantics: anchor_retention_v2
```

## Analysis window

```text
requested_pre_roll_sec = 600
freeze_t0                = collector wall clock at freeze (authoritative)
requested_pre_roll_start_ts = freeze_t0 - requested_pre_roll_sec
analysis_start_ts        = requested_pre_roll_start_ts
```

## Time roles (explicit)

| Name | Meaning |
|------|---------|
| `freeze_t0` | Instant of freeze; feature cutoff; no post-T0 receives in payload |
| `analysis_start_ts` / `requested_pre_roll_start_ts` | Start of the **analysis** pre-roll (600 s before T0) |
| `anchor_checkpoint_ts` | Receive-time of selected book checkpoint |
| `payload_replay_start_ts` | Start of replay material (= `anchor_checkpoint_ts`); **may be older** than `analysis_start_ts` |

Prefix deltas with `anchor_checkpoint_ts < recv < analysis_start_ts` are required to reconstruct the book exactly at `analysis_start_ts` and are allowed in the payload.

## Retention inequality

```text
delta_retention_sec      = 600   # FR ring / max analysis window (unchanged)
checkpoint_interval_sec  = 60
safety_margin_sec        = 30

checkpoint_retention_sec
  >= delta_retention_sec + checkpoint_interval_sec + safety_margin_sec
  = 600 + 60 + 30
  = 690
```

### Safety margin derivation

Worst-case phase offset between periodic checkpoints ≈ `checkpoint_interval_sec` (checkpoint just after the sliding edge is evicted).  
Additional `safety_margin_sec = 30` covers:

- receive-time vs wall-clock used in eviction,
- store latency / observer scheduling,
- sub-second jitter at the ring edge.

Not a silent magic number: **30 s** is the documented default; override only via `FULL_OB_CACHE_BRIDGE_CHECKPOINT_SAFETY_MARGIN_SEC`.

### Checkpoint count floor

```text
max_checkpoints_per_symbol
  >= ceil(checkpoint_retention_sec / checkpoint_interval_sec) + 2
  = ceil(690/60) + 2
  = 14
```

## Anchor selection

```text
anchor = latest checkpoint where
  checkpoint_ts <= requested_pre_roll_start_ts
  AND checkpoint.epoch is valid for that timeline region

NEVER invent an anchor.
NEVER substitute a later checkpoint.
If none → BOOK_ANCHOR_MISSING, DATA_COMPLETE=false, REPLAY_PARITY=false
```

## Initial checkpoint

On first `book_ready=true` for an epoch:

- Force-store full book checkpoint kind `INITIAL_CHECKPOINT`
- Logical order: checkpoint **before** first accepted post-ready ring delta for replay (`u` already embodied; deltas with `u <= anchor.u` skipped)
- Metadata: symbol, epoch_id, event/cts/receive times, u, seq, bid/ask level counts, book_content_hash, kind

JSON/compress/socket I/O remain outside the full-book lock (copy snapshot under lock only).

## Periodic checkpoints

Store every `checkpoint_interval_sec` while ready; retain for `checkpoint_retention_sec`.

## Freeze payload contents

1. Selected anchor checkpoint  
2. All deltas after that checkpoint through `analysis_start_ts` (replay prefix)  
3. All deltas of the analysis window through `freeze_t0`  
4. Epoch / resync markers present in the retained checkpoint timeline  

Sort / apply by existing `u`/`seq` continuity (`enforce_continuity=True`).

## Reconnect / resync

```text
RESYNC_BOUNDARY (reconnect mark)
→ RESYNC_CHECKPOINT (forced on next book_ready)
→ held / live deltas of new epoch
```

- Each epoch has its own consistent checkpoint.
- Multi-epoch freeze is replayable only if every epoch in `[payload_replay_start_ts, freeze_t0]` has a reconstructible checkpoint.
- Missing resync checkpoint → `RESYNC_CHECKPOINT_MISSING` (not presented as continuous feed).
- A bare reconnect-in-window without resync material → fail-closed (not `DATA_COMPLETE`).

## Fail-closed statuses (v2)

| Status | Meaning |
|--------|---------|
| `BOOK_ANCHOR_MISSING` | No checkpoint ≤ analysis start |
| `STARTUP_PRE_ROLL_INCOMPLETE` | Buffer cannot cover full requested analysis window |
| `ANCHOR_EPOCH_MISMATCH` | Anchor epoch incompatible with delta epoch timeline |
| `DELTA_CONTINUITY_GAP` | `u`/`seq` gap during replay (alias surface; wire may still use `SEQUENCE_GAP`) |
| `RESYNC_CHECKPOINT_MISSING` | Reconnect/resync in window without usable resync checkpoint |
| `PAYLOAD_LIMIT_EXCEEDED` | Over `MAX_PAYLOAD_BYTES` |
| `BOOK_HASH_MISMATCH` | Replay book hash ≠ frozen T0 book hash |
| `FREEZE_CONCURRENCY_LIMIT` | Concurrent freeze rejected (`REQUEST_BUSY` / busy) |

Partial reconstruction must never set `data_complete=true`.

## Exact replay parity

Offline replay of anchor + payload deltas must match frozen T0 book on:

- final `u`, final `seq`
- bid/ask level counts
- book content hash
- best bid / best ask
- book not crossed

## Resource bounds

- Analysis window max 600 s  
- Checkpoint prefix bounded by retention formula (not unbounded RAM)  
- `MAX_PAYLOAD_BYTES=100663296`  
- `MAX_CONCURRENT=1`  
- BTC/DOGE separate rings  
- No full-book JSON/zstd under `_book_lock`
