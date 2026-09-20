# Full-OB Cache Access Audit

**Generated (UTC):** `2026-09-05T14:32:25Z`  
**Verdict contribution:** `CACHE_BRIDGE_REQUIRED`

## A. Ringbuffer ownership (proven)

| Item | Evidence |
|------|----------|
| Owning process | systemd user unit `bybit-full-ob-raw-archive-btc-doge.service` |
| MainPID | **1902763** (`systemctl --user show … -p MainPID`) |
| Command | `.venv/bin/python -m orderbook_analyse.orderbook_v2_live --mode raw-archive-only --symbols BTCUSDT,DOGEUSDT …` |
| Start script | `orderbook_analyse/scripts/run_orderbook_v3_raw_archive_btc_doge_foreground.sh` |
| Role | **Collector** (raw-archive + on-demand Full-OB + in-process Flight Recorder) |
| Not | Dashboard process; EdgeWatcher is **in-process** sidecar, not separate PID |
| Separate OI collector | PID 1880066 — no Full-OB ringbuffer |

FR env on PID 1902763:

- `OB_V3_FULL_OB_FLIGHT_RECORDER_ENABLE=true`
- `OB_V3_FULL_OB_FR_SYMBOLS=BTCUSDT,DOGEUSDT`
- `OB_V3_ON_DEMAND_ENABLE=true`
- `OB_V3_ON_DEMAND_SOCKET_PATH=/run/user/1000/orderbook_ob1000.sock`
- No `OB_V3_FULL_OB_FR_RINGBUFFER_MIN` override → default **10.0 minutes**

## Retention and contents

| Setting | Value | Source |
|---------|-------|--------|
| `ringbuffer_minutes` | 10.0 | `FlightRecorderSettings` default |
| `pre_seconds` | **600** | property `ringbuffer_minutes * 60` |
| `max_buffer_messages` | 50_000 | config |
| `max_buffer_bytes` | 256 MiB | config |
| Runtime coverage BTC/DOGE | **~600.0–600.2 s** | health `prebuffer_coverage_seconds` |
| Runtime messages | **3001** each | health `runtimes.*.buffer_messages` |
| Runtime bytes | BTC ~9.6 MiB; DOGE ~1.8 MiB | health |
| Overflow | **0** | health `buffer_overflow` |

**Stored items:** compact raw WS **delta envelopes** (`RawItem`: `receive_time_ns`, `payload`, `kind` ∈ delta|lifecycle|trade|meta). Explicitly **not** a reconstructed full book and **not** derived features (`record_envelope.py`).

**Live reconstructed Full OB** lives separately in `FullBookOnDemandManager` / `FullBookState` (REST snapshot + applied deltas), same process. Health: both symbols `subscription_state=live`, `book_ready=True`, `gap_count=0`, `raw_bids/asks` tens of thousands → **true full depth**, not OB200.

## Time / sequence / eviction

- **Receive-time:** `receive_time_ns` / `local_receive_time_ns` (ring eviction + coverage).
- **Event-time:** envelope `ts` / `cts` kept in payload; book `event_ts_ms`.
- **Validation:** `FullBookState.apply_delta` — stale/dup `u`, decreasing `seq`, `u > local+1` → GAP → clear + resync.
- **Reconnect without open capture:** FR clears ringbuffer → `pre_trigger_incomplete`.
- **Eviction:** age > window, then max messages/bytes; counts `dropped_oldest` / `overflow_count`.
- Collector health: `sequence_gaps_total=0`, full-book `gap_count=0` at audit time.

## Can the buffer reconstruct a past state?

- **Deltas:** yes within retention via `BoundedRawRingBuffer.snapshot()` (in-process).
- **Book at past T0:** requires applying flushed deltas onto a REST/checkpoint snapshot; FR does this on **edge capture start** (`flush()` → disk), not continuously for arbitrary T0.
- Cross-process: **no** dump API today.

## B. Cross-process CLI access

| Question | Answer |
|----------|--------|
| Existing IPC to ringbuffer? | **No** |
| Unix socket `/run/user/1000/orderbook_ob1000.sock` | Live book ops: `acquire`/`heartbeat`/`release`/`status`/`snapshot` with field **`operation`** (not `op`). Depth 0 = Full OB **point-in-time** book. **No pre-roll.** |
| Proven status call | `{"operation":"status","symbol":"BTCUSDT","depth":0}` → `ok=true`, `subscription_state=live` |
| FR on-demand pre-roll CLI? | **No** — `flush()` only on edge CROSS_IN capture start |
| Separate CLI can read existing RAM ringbuffer? | **No** |
| CLI own Full-OB WS? | Technically possible but **capacity already 2/2** (`full_book_active_topics=2`); high live risk; cold start has **zero** pre-roll until warmup |

## Access variant comparison

| Variante | Pre-Roll | Live Full-OB | Prozesskopplung | Latenz | Konsistenz | Impl-Risiko | Live-Risiko | Empfehlung |
|----------|----------|--------------|-----------------|--------|------------|-------------|-------------|------------|
| A Existing socket snapshot | Nein | Ja (aggregiert; optional full_levels) | weich | ms | point-in-time only | keines | niedrig | Ergänzend für Live-Book/Mid |
| **B Localhost RO bridge/dump** | **Ja** (`buf.snapshot()`) | Ja (wrap A) | weich–mittel | ms–100ms | gleiche Clocks | niedrig–mittel | niedrig wenn RO+bounded | **Primäre Empfehlung** |
| C FR edge dump | Nur nach Trigger | episodisch | FR-Lifecycle | hoch | Research nach Finalize | hoch für `--now` | mittel (flush side effects) | Nicht für generisches `--now` |
| D CLI eigene Full-OB + Warmup | Nach ≥10 min | Ja (Duplikat) | unabhängig | Warmup hoch | eigene Continuity | hoch | **hoch** (Topics voll) | Vermeiden |

**Empfehlung:** Variante **B** (minimaler read-only Bridge-Op, z.B. `operation=ringbuffer_snapshot` / dump under lock) + Variante **A** für aktuelles Buch. Keine zweite Full-OB-Subscription.

## Least collector change

Smallest safe change: add one RO socket operation that copies `BoundedRawRingBuffer.snapshot()` (+ metadata coverage/overflow/gap) without enabling write paths, without flush/clear, without new WS. Separate approval required — **not implemented in Phase 0**.
