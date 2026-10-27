# BT-D76 — Published ClickHouse Backtester Binding V1

**VERDICT:** `PUBLISHED_CH_FAST_PATH_PASS`

**Git:** `5d6ee8fa51bc97b6de519c2c3e2515f3d6162486`

## Components reused

- `density_bridge_v1.create_density_session` → `published_clickhouse`
- `PublishedActiveReader` / `load_historical_pool_state_at_close` (5m via 4X-AO import path)
- `build_landscape_window(..., density_only=True, member_cache=...)` (pools dashboard)
- `analyzer_depth2_landscape.js` via `node_bridge_v1`
- D8 `validated_cache` timing reference when bundle covers sample close

## Parity (canonical vs published_clickhouse)

```json
{
  "ok": true,
  "closes": [
    {
      "decision_close_utc": "2026-05-15T12:00:00Z",
      "parity_ok": true,
      "canonical_s": 9.9067,
      "published_ch_s": 9.4649,
      "speedup_x": 1.05,
      "band_compare": {
        "ok": true,
        "expected_count": 361,
        "actual_count": 361,
        "only_expected": [],
        "only_actual": [],
        "only_expected_total": 0,
        "only_actual_total": 0,
        "field_mismatches": [],
        "field_mismatch_total": 0
      },
      "fingerprint_canon": "9d754338b6280be8",
      "fingerprint_pub": "6305ffd568966bfd"
    },
    {
      "decision_close_utc": "2026-08-01T19:00:00Z",
      "parity_ok": true,
      "canonical_s": 8.3365,
      "published_ch_s": 8.3348,
      "speedup_x": 1.0,
      "band_compare": {
        "ok": true,
        "expected_count": 322,
        "actual_count": 322,
        "only_expected": [],
        "only_actual": [],
        "only_expected_total": 0,
        "only_actual_total": 0,
        "field_mismatches": [],
        "field_mismatch_total": 0
      },
      "fingerprint_canon": "959127d38662d7fd",
      "fingerprint_pub": "fb1f3c213f97fff0"
    },
    {
      "decision_close_utc": "2026-09-09T20:55:00Z",
      "parity_ok": true,
      "canonical_s": 5.8927,
      "published_ch_s": 5.799,
      "speedup_x": 1.02,
      "band_compare": {
        "ok": true,
        "expected_count": 202,
        "actual_count": 202,
        "only_expected": [],
        "only_actual": [],
        "only_expected_total": 0,
        "only_actual_total": 0,
        "field_mismatches": [],
        "field_mismatch_total": 0
      },
      "fingerprint_canon": "71d02bbb341f8488",
      "fingerprint_pub": "8236dac6cbafcdca"
    },
    {
      "decision_close_utc": "2026-09-23T12:00:00Z",
      "parity_ok": true,
      "canonical_s": 5.8639,
      "published_ch_s": 5.6967,
      "speedup_x": 1.03,
      "band_compare": {
        "ok": true,
        "expected_count": 212,
        "actual_count": 212,
        "only_expected": [],
        "only_actual": [],
        "only_expected_total": 0,
        "only_actual_total": 0,
        "field_mismatches": [],
        "field_mismatch_total": 0
      },
      "fingerprint_canon": "db719fad9401f35e",
      "fingerprint_pub": "2b9c52c3ad7671ba"
    },
    {
      "decision_close_utc": "2026-09-25T11:00:00Z",
      "parity_ok": true,
      "canonical_s": 5.8859,
      "published_ch_s": 5.784,
      "speedup_x": 1.02,
      "band_compare": {
        "ok": true,
        "expected_count": 215,
        "actual_count": 215,
        "only_expected": [],
        "only_actual": [],
        "only_expected_total": 0,
        "only_actual_total": 0,
        "field_mismatches": [],
        "field_mismatch_total": 0
      },
      "fingerprint_canon": "5946bc15fa412f6f",
      "fingerprint_pub": "679d254c955120a3"
    }
  ],
  "member_cache_keys": 10936
}
```

## Timing

```json
{
  "close": "2026-09-09T20:55:00Z",
  "canonical_cold_s": 5.8267,
  "published_ch_warm_s": 5.6407,
  "validated_cache_read_s": null
}
```

## Minimal wiring changes

| File | Change |
|------|--------|
| `density_bridge_v1.py` | `density_source=published_clickhouse` → `PublishedClickhouseDensitySessionV1` |
| `config_validate_v1.py` | Allow `published_clickhouse` only with `research_mode=depth2_density_research_v1` |
| `historical_window_v1.py` | `density_only` + shared `member_cache` for published fast window |
| `tools/phase_bt_d76_published_ch_binding_v1/*` | Provider, session, parity runner |

**Opt-in:** set `density.density_source: published_clickhouse` and optional `density.published_clickhouse: {}` in research YAML/JSON. Production default unchanged (`canonical_provider`).

## Performance interpretation

- **Single-close cold:** published path is still ~5.8–10 s (skips cluster/candle work; lifetimes + Node dominate). Measured speedup on samples: ~1.0–1.05×.
- **Session `member_cache`:** accumulates published membership across closes (parity run filled **10936** cache keys) — chronological replay reuses overlapping 48h+168h windows.
- **D8 `validated_cache`:** not benchmarked here (D9 bundle manifest absent on disk); still the fastest read path when pre-built.

## Mini chronological replay (EMA V3)

```json
{
  "bars": 5,
  "start_utc": "2026-09-09T20:50:00Z",
  "end_utc": "2026-09-09T21:10:00Z",
  "chronology_match": true,
  "canonical_digest": "{\"authorized\": 0, \"blocked\": 2, \"fills\": 0, \"pending\": null, \"po",
  "published_digest": "{\"authorized\": 0, \"blocked\": 2, \"fills\": 0, \"pending\": null, \"po"
}
```

## Coverage limits

- **April 2026:** no 5m PD1 publish on CH pipeline; 168h warmup before May 1 needs non-CH sources.
- Parity samples are bounded; not full May–Sep proof.
- Default `strategy_config_v1.yaml` remains `canonical_provider` (fail-closed).

## May–Sep fast backtest readiness

Published-CH session is ready for **research opt-in** on validated publish range (May–Sep 2026 5m PD1) when parity gate passes.
