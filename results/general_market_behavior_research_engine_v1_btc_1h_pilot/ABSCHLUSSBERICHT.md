# ABSCHLUSSBERICHT — GENERAL_MARKET_BEHAVIOR_V1 BTC 1h State Pilot

**Verdict:** `GENERAL_MARKET_BEHAVIOR_V1_BTC_1H_STATE_PILOT_PASS_WITH_PROXY_LIMITS`

## Selected window
- **Hour:** `2026-09-06T10:00:00Z` → `11:00:00Z` (UTC)
- **Symbol:** BTCUSDT
- **Why not 17Z:** Collector started ~`2026-09-05T17:39:35Z`. Segment is `REPLAY_CHAIN_COMPLETE` but **not** `FULL_WALL_CLOCK_COVERAGE` (start_lag ≈ 2379s). Never labeled FULL.
- **Why not Sep5 18Z+:** Wall-clock present but `in_segment_u_gap_count≥1` / GAP manifests → not `REPLAY_CHAIN_COMPLETE`.
- **Why not Sep6 03Z:** Full-OB COMPLETE + wall-clock OK, but **public trades count=0** (collector outage window).

## Rows / checks
- **3600** 1s rows in `btc_state_1s_v1.parquet`
- Asserts OK; Prefix parity OK (1800 prefix rows, 0 mismatches); Idempotency OK (same SHA256); GAP refusal OK on 17Z
- Attribution: removals → `MIXED_OR_UNKNOWN` (execute vs cancel not claimed exact) → PROXY_LIMITS

## Time contract
- `state_ts` = UTC second start
- Bucket `[state_ts, state_ts+1s)`; causal cut `event_time < state_ts+1s`
- No future fills; OI asof with hard age 120s

## Resources
- Wall ~84s total; build ~28s; RSS delta ~235 MB (peak ≪ 4 GB)

## Safety
No collector restart, no systemd change, no CH write/DDL, no commit/push, dirty trees preserved.

## Pflichtantworten
1. Selected hour: **2026-09-06T10:00:00Z** (earliest FULL_JOIN at/after preferred 18:00Z Sep5).
2. **17Z** only replayable from ~17:39 — **not** full wall-clock.
3. **3600** rows.
4. Yes: Full-OB, trades, mid/price, OI, liquidations source (2 liq events).
5. Exact: mid/spread/bands/flow aggregates/taker volumes/OI age/flags. Proxy: execute vs cancel, refill.
6. Execute vs cancel → **MIXED_OR_UNKNOWN** (no guessing).
7. Prefix parity: **yes**.
8. Idempotency: **yes**.
9. GAP/incomplete 17Z: **refused FULL** correctly.
10. No NaN/Inf; books valid (assert + book_valid_rate=1).
11. ~84s wall; ~235 MB RSS delta.
12. Reused: `FullBookState`, continuous archive `replay`/`iter_records`, CH public_trades/OI/liq read-only.
13. Before episodes: stronger attribution, refill heuristics, multi-hour watermarks.
14. Builder ready for more FULL_JOIN hours via same CLI; still one-hour pilot only.

Package: `orderbook_analyse/src/orderbook_analyse/research/general_market_behavior_v1/`  
CLI: `orderbook_analyse/scripts/run_general_market_behavior_research_v1.py --btc-1h-state-pilot`
