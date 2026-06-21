## Global Rules

1. **Router darf niemals Rohdaten sehen.** Er liest ausschließlich `slow_state`, `mid_state`, `fast_state`, `confidence`, `conflict_flags` und `instability_flags`.
2. **Primitive Events entstehen nie aus einem isolierten Signal.** Jeder Event braucht mindestens zwei kontextgebende Inputs (z. B. Preisrichtung + Volume + ATR).
3. **`None` bedeutet „fehlendes Signal“, nicht `0`.** Zero-Data und Missing-Data müssen getrennt erkannt werden.
4. **Normalisierung vor Eventbildung.** Rohwerte ohne Vergleichsbasis dürfen keine Event-Entscheidung provozieren.
5. **Liquidation, microburst, spread, OI niemals isoliert verwenden.** Sie müssen immer von Preis-, Volume- oder ATR-Kontext begleitet werden.

---

## ATR Block

### 1. Purpose
- **Was**: Misst die Preisbewegung relativ zur aktuellen Volatilität.
- **Warum**: Stabilisiert das Regime-System gegen verzerrte Breakout-Signale, filtert dirty moves.
- **Layer**: Fast (Trigger-Qualität), Mid (Breakout-Validierung), Slow (Volatility-Regimekontext).

### 2. Raw Inputs
- **Pflicht**: `price_change_1m`, `atr_1m`, `spread_ratio`.
- **Optional**: `trade_volume_1m` (zur Kombi mit microburst).

### 3. Derived Features
1. **`price_move_vs_atr`**
   - Formel: `abs(price_change_1m) / max(atr_1m, EPS)`
   - Abhängigkeiten: `price_change_1m`, `atr_1m`
   - Guard: `atr_1m > 0`, sonst `None`
   - Wertebereich: `0..∞` (deutlich >1 = ungewöhnlich)
2. **`spread_vs_atr`**
   - Formel: `spread_ratio / max(atr_1m, EPS)`
   - Guard: `spread_ratio` existiert, `atr_1m > 0`
   - Interpretation: hoher Wert = unholy spread
3. **`atr_regime_zscore`**
   - Formel: Z-Score gegenüber Rolling ATR Profile
   - Guard: Profil vorhanden + `atr_1m` valid
   - None wenn Profil fehlt
4. **`volatility_expansion_score`**
   - Formel: `max(price_move_vs_atr, spread_vs_atr)` mit Rangbegrenzung
   - Guard: beide Teile valid

### 4. Normalization
- Z-Scores basierend auf `atr_1m` Rolling Profile (per Symbol).
- `spread_vs_atr` in `[-2, 2]` begrenzen.
- Missing `atr_1m` → Derived `None`.
- Zero-Data: `price_change_1m == 0` bleibt gültig, `price_move_vs_atr == 0` aber kein Event (nur Kontext).

### 5. Edge Cases
- fehlende ATR oder Spread
- `price_change_1m` sehr klein (no move) vs. echte Expansion
- extreme `spread_ratio` mit niedriger Teilnahme → ignore in Fast
- stale ATR (nur alte Minuten) → Flag setzen, nicht verwenden

### 6. Interpretation
- **hoch**: echter volatility breakout, Breakout-Fenster
- **niedrig**: strukturierter Trend, low-volatility Range
- **neutral**: default, nur als Kontext verwendet
- Signal stark, wenn auch `volume_spike_ratio` gestiegen ist

### 7. Conflict Cases
- hoher `price_move_vs_atr` + `spread_vs_atr` aber `trade_count_1m == 0` → keine Event-Trigger
- hoher `price_move_vs_atr` + negative ATR-Drama mit `avg_trade_size` null → Only contextual

### 8. Primitive Events Target
- `volatility_expansion` (Guard: `price_move_vs_atr` > threshold AND `trade_volume_1m` > min)
- `low_quality_expansion` (Guard: high ATR signal but low participation)
- `thin_orderflow_instability` (Guard: `spread_vs_atr` high + low volume)

### 9. Engine Usage
- Fast: Trigger wenn events valid
- Mid: zusätzlich als Breakout-Quality filter
- Slow: nur als regime-level context (volatility regime)

### 10. Test Cases
- positive: hoher `price_change_1m` + `atr_1m` normal → `volatility_expansion`
- negative: hoher `price_change_1m` + `atr_1m` hoch + zero volume → kein event
- edge: Spread hoch aber `atr_1m` low
- missing: `atr_1m` `None` → Derived `None`
- zero: `price_change_1m` = 0 → no event

---

## Open Interest Block

### 1. Purpose
- Misst Conviction und Build-Up in beiden Richtungen.
- Relevant für Slow Trendbestätigung + Mid Build-Up Validierung.
- Layer: Slow (Regime conviction), Mid (Build-Up/Reversal), Fast indirekt.

### 2. Raw Inputs
- Pflicht: `open_interest`, `oi_change`, `price_change_1m`.
- Optional: `trade_volume_1m`, `liquidation_density_5m`.

### 3. Derived Features
1. `oi_abs_zscore`: Rolling-Zscore von `open_interest`.
2. `oi_delta_zscore`: zscore von `oi_change` über Rolling.
3. `price_oi_alignment`: `sign(price_change_1m) == sign(oi_change)` (categorical).
4. `oi_volume_confirmation`: `oi_change / max(trade_volume_1m, EPS)`.

### 4. Normalization
- Z-Scores mit Historic OI Profile pro Symbol.
- Price alignment categorical (0/1).
- Missing OI → derived `None`.
- Zero data: `trade_volume_1m == 0` → treat `oi_volume_confirmation` as `None`.

### 5. Edge Cases
- `open_interest` None → skip
- OI flat (`oi_change ==0`) but price move → no build-up event
- stale OI snapshots → flag

### 6. Interpretation
- High `oi_abs_zscore` + price up = long conviction.
- Price up + OI down = short covering signal.
- Price down + OI up = fresh short build-up.

### 7. Conflict Cases
- Price up + OI up + `liquidation_density` high → check panic.
- OI signal but `microburst_score` low → require participation guard.

### 8. Primitive Events
- `fresh_long_build_up` (price ↑, OI ↑, volume > min)
- `fresh_short_build_up` (price ↓, OI ↑)
- `short_covering_rebound` (price ↑, OI ↓)
- `long_unwinding_flush` (price ↓, OI ↓)

### 9. Engine Usage
- Slow: conviction / trend confirmation.
- Mid: build-up vs exhaustion differentiation.
- Fast: only contextual flags (no strong trigger).

### 10. Test Cases
- pos: price ↑ + OI ↑ + volume high
- neg: price ↑ + OI ↓ but no volume
- edge: OI flat + price move
- missing: `open_interest` None
- zero: `trade_volume_1m` 0 -> dependent derived `None`

---

## Participation Block

### 1. Purpose
- Misst Aggression vs Retail Churn.
- Relevant für Fast Pressure und Mid Participation.
- Layer: Fast (Aggression), Mid (Participation quality).

### 2. Raw Inputs
- Pflicht: `trade_volume_1m`, `trade_count_1m`.
- Optional: `avg_trade_size` (calc), `microburst_score`.

### 3. Derived Features
1. `avg_trade_size`: `trade_volume_1m / max(trade_count_1m, 1)` with guard (None if trade_count_1m ==0).
2. `trade_intensity_score`: zscore of `trade_count_1m`.
3. `large_trade_aggression_score`: `avg_trade_size / typical_avg`.
4. `small_trade_churn_score`: `trade_count_1m` high but `avg_trade_size` low and `microburst_score` low.

### 4. Normalization
- Z-scores using profile stats.
- `avg_trade_size` None when `trade_count_1m == 0`.
- Microburst requires both trade_count > threshold and trade_volume > threshold.

### 5. Edge Cases
- `trade_count_1m == 0`
- `avg_trade_size` invalid (division by zero)
- microburst high but trade_count zero → treat as `None`

### 6. Interpretation
- High `avg_trade_size` + high `trade_intensity` = institutional build-up.
- High `trade_count` + low `avg_trade_size` = retail churn / noise.

### 7. Conflict Cases
- high microburst but low volume → ignore event.
- low participation but price moves → require other block (ATR) before event.

### 8. Primitive Events
- `high_participation_breakout`
- `weak_move_low_participation`
- `aggressive_large_order_push`
- `retail_churn_noise`

### 9. Engine Usage
- Fast: triggers aggression.
- Mid: quality filter (is move backed by participation?).
- Slow: not directly used.

### 10. Test Cases
- pos: many large trades + microburst valid
- neg: microburst high but volume zero
- edge: trade_count high but avg size low
- missing: trade_count 0 -> avg_trade_size `None`
- zero: trade_volume 0

---

## Liquidation Block

### 1. Purpose
- Captures panic / exhaustion / squeezes.
- Wichtig für Fast Panic und Mid Exhaustion, slow als baseline.

### 2. Raw Inputs
- Pflicht: `liquidation_density_5m`, `liquidation_cluster_score`.
- Optional: `price_change_1m`, `ai_change`, `spread_ratio`.

### 3. Derived Features
1. `panic_liq_score`: weighted sum of density + cluster + short/long ratio.
2. `exhaustion_reversal_score`: `panic_liq_score` normalized by ATR/price move.

### 4. Normalization
- Z-score gegen historical liquidation stats.
- Null-Density → None.
- Missing price or ATR → limit signal to context.

### 5. Edge Cases
- zero liquidations (skip event).
- conflicting price direction (liquidation up while price up) → categorize as covering.
- stale liquidation data → ignore.

### 6. Interpretation
- High `panic_liq_score` + price fall = panic.
- High score + price reversal = squeeze/reversal signal.

### 7. Conflict Cases
- liquidations high but `trade_volume` low + ATR high → ignore.
- price up + liquidation high + OI falling = cover, not panic.

### 8. Primitive Events
- `panic_liquidation_phase`
- `capitulation_flush`
- `squeeze_exhaustion_reversal`

### 9. Engine Usage
- Fast: immediate panic/exhaustion.
- Mid: reversal/exhaustion context.
- Slow: only to flag extremal baselines.

### 10. Test Cases
- pos: density high + price fall
- neg: density high + price quiet
- edge: liquidations high but price up
- missing: density null → no events
- zero: cluster score 0

---

## Spread Block

### 1. Purpose
- Misst Market Quality und Instability.
- relevant für Fast instability > Router conflict.

### 2. Raw Inputs
- Pflicht: `spread_ratio`, `trade_volume_1m`.
- Optional: `atr_1m`, `microburst_score`.

### 3. Derived Features
1. `spread_ratio_zscore`: Z-Score
2. `spread_stress_score`: normalized combination of `spread_ratio`, `spread` vs `volume`.
3. `spread_volume_dislocation`: `spread_ratio` / `max(trade_volume_1m, EPS)`

### 4. Normalization
- Z-Scores, guard on `trade_volume`.
- Missing spread or volume = `None`.
- Zero volume separate from missing.

### 5. Edge Cases
- Spread high but volume low → thin book
- Spread normal but ATR high → ignore
- stale spread data → skip

### 6. Interpretation
- High: dirty breakout risk, instability.
- Low: clean directional movement.

### 7. Conflict Cases
- High spread + high participation + high ATR = strong event.
- High spread + low volume but no other context = only warning flag (no event).

### 8. Primitive Events
- `thin_orderflow_instability`
- `spread_stress_phase`
- `dirty_breakout_risk`

### 9. Engine Usage
- Fast: instability triggers.
- Mid: quality filter for breakouts.
- Router: only via `instability_flags`.

### 10. Test Cases
- pos: high spread + volume high
- neg: high spread + zero volume
- edge: normal spread + heavy breakout
- missing: spread `None`
- zero: trade_volume 0

---

## Cross-Block Interaction Rules

1. **ATR + Spread**: `volatility_expansion` only valid when both `price_move_vs_atr` high and `spread_stress_score` elevated.
2. **OI + Price**: Conviction events (`fresh_*`, `short_covering`) require `price_oi_alignment` before they trigger.
3. **OI + Volume**: `oi_volume_confirmation` must exceed threshold; ohne Volume keine build-up.
4. **Liquidation + Price Direction**: `panic_liquidation_phase` nur wenn Liquidationen + Price in gleiche Richtung fallen.
5. **Participation + Microburst**: `high_participation_breakout` braucht microburst_valid PLUS `trade_intensity` hoch.
6. **Spread + Participation**: `dirty_breakout_risk` only when high spread + low participation quality.
