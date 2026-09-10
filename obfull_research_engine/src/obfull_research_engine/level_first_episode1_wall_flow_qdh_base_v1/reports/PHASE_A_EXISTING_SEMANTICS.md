# Phase A — bestehende Semantik (Episode 1 Wall-Flow Vorarbeit)

## 1. Kanonische Public-Trade-Quelle
- CH: `orderbook_analysis.public_trades_canonical` via `outcomes/public_trade_index.py:load_public_trades_window`
- Episode-1 Freeze: `episode1_independent_derivation_inputs_v1/public_trades_zone_window.jsonl`
- SMS1 Persist-Kopie (ohne Receive): `sms1_.../public_trades.jsonl.zst`

## 2–4. Trade-ID / Dedup
- Identität: `trade_id` (stabil UUID-String)
- Mehrere Records = gleicher Trade nur bei gleichem `trade_id`
- Dedup: `PublicTradeIndex.from_trades` / CH `GROUP BY symbol,trade_id argMax(*, ingest_timestamp)`
- Wall-Flow: `canonical_trade_key = symbol|trade_id`

## 5. Trade-Zeitfelder
- `trade_ts` (Exchange/Event)
- `ingest_timestamp` / `collector_received_at` (Receive; im Freeze vorhanden)

## 6. Full-OB-Zeitfelder
- Raw: `event_time_ns`, `receive_time_ns`, `u`, `seq`, `cts`
- Persistiert: `event_time`, `event_available_at` (= ceil_100ms Research-Proxy), `u`, `seq`, `replay_epoch`
- `received_at` wird in sms1-Persist **nicht** geschrieben

## 7. cts/T/seq/u
- Replay-Ordnung: `event_time` + `apply_order` (+ Reset-Rank)
- `u`/`seq` Metadaten; `cts` an `apply_delta`, nicht kanonischer Order-Key

## 8–9. Bestehende Refill
- `drilldown/refill.py:detect_refills` — sign-basiert (Kandidaten)
- `refill_confirm.py:classify_refills` — Gross-Depletion + Exact-Restore
- **Nicht** identisch mit Wall-Flow `net_refill` / `residual_pull`

## 10. Wiederverwendung
- Independent Zone/Wall/Detection Anchors
- Persistierte `states_100ms` / `level_changes` / `initial_book`
- `event_available_at` Proxy, Golden-Parity-Pfad, E2E-Runner

## 11. Doppelzähl-Gefahren (vermieden)
- Kein zweiter Public-Trade-Flow
- Keine Footprint-Volumen-Addition auf Hits
- Exact vs Band getrennte Consumed-Sets
- Bestehende Refill-Confirm nicht als Mass-Balance-Net-Refill
