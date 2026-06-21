## Phase 5 Router Contract

### Allowed Inputs
- `slow_state`
- `mid_state`
- `fast_state`
- `confidence` (meta only)
- `conflict_flags`
- `instability_flags`

### Forbidden Inputs
- raw market fields
- derived feature values
- pressure / participation / exhaustion scores
- price / OI / liquidation / spread direct interpretation

### Merge Rules
1. `fast_emergency_instability` has highest priority and routes to `emergency`.
2. If `mid_state` exists, it has priority over `slow_state` and `fast_state`.
3. Without a `mid_state`, the router only merges `slow_state` context with `fast_state` directionality:
   - supportive fast state -> continuation
   - opposing fast state -> pullback context
   - ambiguous fast state -> `range_unclear`
4. If there is no clear slow trend context, route to `range_unclear`.

### Conflict Handling
- Conflicts are declared, not re-interpreted.
- Examples:
  - `slow_trend_long` + `fast_impulse_short` => `slow_fast_direction_conflict`
  - `slow_trend_short` + `fast_impulse_long` => `slow_fast_direction_conflict`
  - ambiguous fast exhaustion inside a slow trend => `fast_exhaustion_ambiguous`

### Confidence Handling
- Confidence is assigned by merge clarity, not by market data.
- Suggested levels:
  - `0.95` emergency
  - `0.85` mid-priority routed states
  - `0.75` aligned trend continuation / pullback
  - `0.40` range / ambiguous

### Instability Override Rules
- `fast_emergency_instability` forces `emergency`.
- `fast_exhaustion_*` only sets ambiguity / instability metadata; it must not be reinterpreted with old pressure logic.

### No Domain Logic Rule
- The router must not reconstruct meaning from price, OI, liquidation, spread, ATR, volume, or scores.
- The router is a merge layer only: it decides from pre-computed states and meta flags.
