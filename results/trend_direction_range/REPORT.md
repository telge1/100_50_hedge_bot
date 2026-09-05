# Trend Direction Range Runner

## Primärentscheidung

**TREND_DIRECTION_RANGE_RUNNER_READY**

## Wiederverwendete Kernlogik

- `parse_decision_timestamp`, `normalize_symbol`, `run_c34b_on_ohlcv`, `decide_from_structure`, `map_structure_to_direction`, `reason_for_direction` aus `research/regime_scanner/trend_direction_at.py`
- CLI: `scripts/query_trend_direction_range.py`
- Core: `research/regime_scanner/trend_direction_range.py`

## Effiziente Verarbeitung

1. Ein MySQL-Load aller Closed-Candles bis `end` (für Identität mit dem Einzelrunner: volle Historie bis End)
2. Ein C3.4B-Lauf über die chronologische Serie
3. Pro Decision-Time Prefix-Slice `close_time <= T` → `decide_from_structure`

Decision-Fenster: **`start <= decision_time <= end`** (inklusiv), 5m-aligned.

Hinweis Runtime: APT/DOGE ~19s; BTC ~451s (≈156k 5m-Bars Historie).

## UNCLEAR `direction_since_utc`

Zeigt auf den Beginn des kontinuierlichen UNCLEAR-Laufs (APT Sticky: `2026-04-11T20:15:00Z`), nicht auf den alten Major seit 18:45.

## Artefakte

`results/trend_direction_range/run_<utc>/`:

- `direction_timeline.csv`
- `direction_transitions.csv`
- `summary.json`
- `REPORT.md`

## Forward Returns

`--include-forward-returns` in v1 **nicht** implementiert (nächster Schritt, EX_POST_EVALUATION).

## Smoke

| Symbol | rows | transitions | runtime | causality_failures |
|---|---:|---:|---:|---:|
| APTUSDT | 133 | 8 | ~18.8s | 0 |
| DOGEUSDT | 133 | 10 | ~18.9s | 0 |
| BTCUSDT | 133 | 10 | ~450.7s | 0 |

APT Sticky Match (Einzel `20:31` ≡ Range-Zeile `last_close=20:30`):

- `UNCLEAR` / `MAJOR_CHALLENGED:bearish_internal_break` / `bearish_internal_break` / since `20:15`

## Tests

`test_trend_direction_at.py` + `test_trend_direction_range.py` → **34 passed**

## Geänderte / neue Dateien

- `research/regime_scanner/trend_direction_at.py` (UNCLEAR-since + `MAJOR_CONFIRMED`)
- `research/regime_scanner/trend_direction_range.py` (neu)
- `scripts/query_trend_direction_range.py` (neu)
- `research/regime_scanner/tests/test_trend_direction_range.py` (neu)

## CLI

```bash
PYTHONPATH=. python scripts/query_trend_direction_range.py \
  --symbol APTUSDT \
  --start "2026-04-11T17:00:00Z" \
  --end "2026-04-12T04:00:00Z" \
  --step 5m \
  --transitions-only
```
