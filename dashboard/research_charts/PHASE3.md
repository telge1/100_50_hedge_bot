# Research Charts — Phase 3 (existing collector)

Decision: **RESEARCH_CHARTS_EXISTING_COLLECTOR_PHASE_3_READY**

No new Bybit WS, REST gap-fill, recovery, or MySQL live writer.

```
Research UI
        ↓
Research API  /api/research/symbols|candles|indicators|live-status
        ↓
ClickHouseResearchCandleSource     signal_generator.candles_1m FINAL
        ↓                          TRP aggregate / EMA / Stoch / LLD
ensure_live_collector(symbol)
        ↓
Existing CollectorControlService   :8787
        ↓
Live1mCollector.run_recovery()     (unchanged)
        ↓
candles_1m  →  5s incremental poll → Research Charts
```

## Source of Truth

Normal operation: ClickHouse `signal_generator.candles_1m` (closed 1m only).

`MySQLResearchCandleSource` remains as fallback (`RESEARCH_CANDLE_SOURCE=mysql`).

## Live semantics

`LIVE` means new **closed** 1m candles appear via 5s polling, not tick-by-tick forming candles.

Poll interval: **5000 ms**.

Incremental: `GET /api/research/candles?from=last_seen_time` then series.update (same open_time → update, newer → append).

SSE is not implemented; `/api/research/stream` stays a stub for a later replacement of polling.

## Cache

- Incremental (`from` set, no `to`): not cached
- Default latest window: 2s TTL
- Bounded historical `from`+`to`: 45s

## systemd (not installed/started)

UNIT_PATH: `Signal_Generator_Ralf/signal_generator_stoch_waves/deploy/systemd/bybit-live-1m-collector.service`

UNIT_VALID: yes (Type=simple, localhost:8787, live_universe.json, Restart=on-failure)

Recommended (not executed):

```
sudo cp .../bybit-live-1m-collector.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bybit-live-1m-collector.service
```
