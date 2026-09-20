# Semantic Contract — GENERAL_MARKET_BEHAVIOR_RESEARCH_ENGINE_V1

## Scope
Observe Bybit linear perps (primary BTCUSDT, secondary DOGEUSDT). No claim of global causality or hidden liquidity completeness.

## Source of Truth hierarchy
1. **Full depth book:** FS `full_ob_v1` continuous archive (RAW). Flight-recorder / cache-bridge are event/live adjuncts, not calendar SoT.
2. **Depth-limited continuous book (bridge history):** FS `ob200_v3` / `ob1000_v1`; CH `research_ob*_snapshots_1s` are DERIVED and may lag.
3. **Trades:** `orderbook_analysis.public_trades_canonical` (RAW). Research table DERIVED and currently lags to 2026-08-31.
4. **Candles:** `signal_generator.candles_1m` (RAW).
5. **OI:** `open_interest_5s` (+ events) RAW; research DERIVED/lagging.
6. **Liquidations:** `all_liquidations` RAW; research DERIVED/lagging.
7. **Ticker samples:** NOT trusted.

## Coverage classes (mandatory)
- **FULL:** required RAW sources present without gap markers for the analysis claim (Full-OB COMPLETE segment if full-depth claimed).
- **PARTIAL:** usable with explicit flags (e.g. OB200-only, GAP segment with known hole, OI carried).
- **GAP:** analysis claim forbidden for features needing the missing source.

Never promote GAP → FULL by interpolation across book sequence holes.

## Side semantics
- Public trade `Buy`/`Sell` = **taker aggressor**.
- Liquidation: map to liquidated position side and forced flow via existing liquidation_flow_contract; do not equate raw Buy/Sell with “buyer control”.
- Book bids/asks: resting liquidity; size=0 means remove level.

## Book validity
After apply: best_bid < best_ask; sizes ≥ 0; respect `u` continuity within epoch; resync = new epoch, never stitch as contiguous without marker.

## Attribution honesty
Book size decrease ∩ overlapping taker notional → `EXECUTED_LIKELY`.
Book size decrease without matching trades → `CANCEL_LIKELY`.
Ambiguous overlap/timing → `MIXED_OR_UNKNOWN`.
Never assert exact cancel vs execute from book delta alone.

## Causality
Features/regime/episode **start** use only data with event_time ≤ decision_time (and receive_time constraints for late data policy). Outcomes strictly after decision_time. No future OI/trade backfill into past states.
