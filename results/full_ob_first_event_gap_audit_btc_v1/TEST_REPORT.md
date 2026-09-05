# TEST_REPORT — first event u-gap audit BTC

## Executed
1. PID / single-collector proof; open vs finalized inventory
2. SHA256 of finalized sources (pre/post); live metadata excluded from stability claim
3. Persisted u-gap scan (markers excluded) → 4 forward gaps
4. ≥100 deltas before/after first gap → `gap_neighborhood.csv`
5. Health reconnect_count alignment; nohup `stale_market_data` logs
6. FullBookState replay until GAP
7. Isolated CH neighborhood import + gap visibility
8. Dedup demo without OPTIMIZE (physical 406 / logical 203 / ratio 2.0)

## Outcome
- Verdict: `RECONNECT_RESYNC_BOUNDARY_CONFIRMED`
- Replay: `EVENT_NOT_SELF_CONTAINED_ACROSS_GAP`
- CH gap parity: True
- Dedup contract: True
- Code/tests changed: none
- Collector/OI untouched: True / True
