# Sweep Event Interface

## Winner config (frozen)

- `config_id`: `2eab613f172d928e`
- `reference_price=close`, `volume_threshold=1.3`, `volatility_threshold=20`
- `leverages=[25,50,100]`
- `cluster_distance_pct=0.15`, `cluster_min_level_count=1`, `cluster_min_total_strength=4`
- Primary confirmed universe: **upper / short / 50x / immediate_reclaim**
- Counts: Full 2696 / IS 1824 / OOS 872; OOS `confirmed_better_than_matched_control`

## Replay producer

| Step | Function | File |
|------|----------|------|
| Level + sweep replay | `replay_liquidation_levels` | `liquidation_levels.py` |
| Upper sweep + reclaim + optional HTF tags | `build_upper_squeeze_events` | `short_squeeze_continuation_audit.py` |
| Lite upper events (optimizer/control) | `build_lite_upper_events` | `liquidation_optimizer.py` |
| Winner ValidationEvent set | `build_winner_events` | `liquidation_control_validation.py` |
| Frozen config | `frozen_winner_config` | `liquidation_control_validation.py` |

## How a 50x upper sweep is recognized

1. Levels created causally when volume/volatility triggers fire; upper price = `ref * (1 + 1/leverage)`.
2. While active, a level is swept when `_is_swept` holds on that bar (`strict_cross`: `high > level` and `low < level`).
3. Winner primary filters events where the swept level’s `leverage == 50` (candle may also sweep 25/100 — recorded in combination fields).

## Immediate reclaim

`_find_bearish_reclaim`:

- If **sweep-bar close < level_price** → `immediate_reclaim` (delay 0; `signal_index = sweep_index`).
- Else search closes on bars `sweep_index+1 .. +window` for close < level → `delayed_reclaim_1_to_3`.
- Else `no_reclaim`.

Winner validation keeps only `exclusive_reclaim_group == "immediate_reclaim"`.

## Existing event fields (map)

### `ShortSqueezeEvent` (rich)

- Identity: `event_id`, `timestamp`, `candle_index`
- Level: `leverage`, `level_id`, `level_price`, `level_strength`, `level_age`
- Candle: OHLCV + `high_above_level_pct`, `close_relative_to_level_pct`, body/wick pcts
- Combo: `swept_level_count`, `swept_total_strength`, `leverage_combination`
- Reclaim: `reclaim_class`, `exclusive_reclaim_group`, `reclaim_index`, `reclaim_delay_candles`
- Path-audit entry: `signal_index`, `signal_timestamp`, `entry_index`, `entry_timestamp`, `entry_price`
- Local HTF tags: `trend_t1/t2/t3`, `trend_15m_*`, `trend_30m_*`
- Sample flags: `sample`, March window flags

### `ValidationEvent` (control set)

- `event_id`, `signal_index`, `signal_timestamp`
- Legacy path entry: `entry_index`, `entry_timestamp`, `entry_price`
- `side="upper"`, `direction="short"`, `leverage=50`
- `swept_level_count`, `swept_total_strength`, `swept_leverages`
- `cluster_center_price`, `cluster_distance_pct`
- Match covariates: sample/month/hour/buckets/atr_pct/volume_ratio/leverage groups

### Replay level objects

`LiquidationLevel`: creation + `swept_index` / `swept_timestamp` / `strength` / `status`

## Proposed dataclass (design only — not implemented)

```python
@dataclass(frozen=True)
class SweepTriggerEvent:
    event_id: str
    signal_index: int                 # sweep bar index for immediate reclaim
    signal_timestamp: pd.Timestamp    # sweep bar open timestamp (repo convention)
    side: str                         # "upper"
    primary_leverage: int             # 50
    swept_leverages: tuple[int, ...]
    swept_level_count: int
    swept_total_strength: int
    cluster_center_price: float | None
    reclaim_status: str               # "immediate_reclaim"
    close_relative_to_level: float    # pct or signed distance
    source_config_id: str             # "2eab613f172d928e"
    # Explicit non-fields for analysis window design:
    # - no entry_index / entry_price (deferred to later phases)
    # - analysis_window_start_index = signal_index + 1 (first closed 5m AFTER sweep)
```

### Stability notes

- Treat sweep as **trigger only**; do not carry path-audit entry fields into the analysis SM.
- Prefer strength-weighted `cluster_center_price` when multiple upper levels sweep same candle.
- Keep `source_config_id` mandatory to prevent silent config drift.
