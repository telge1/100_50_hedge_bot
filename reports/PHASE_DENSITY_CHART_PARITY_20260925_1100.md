# PHASE — Density Chart Parity 2026-09-25 11:00 UTC

## Provenance

```json
{
  "as_of_request_utc": "2026-09-25T11:00:00Z",
  "t_draw_ms_utc": "2026-09-25T11:00:00+00:00",
  "causal_decision_close_utc": "2026-09-25T11:00:00Z",
  "timestamp_semantics": "5m chart grid: poolActiveAt uses wall-clock ms at floor5m(as_of); membership/evaluation uses causal 5m decision close (slot floor). Same as PD1ExactDensityTrpAdapter.causalDecisionCloseMs + poolActiveAt.",
  "min_unique_origins": 2,
  "pipeline_line_id": "historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1",
  "config_hash": "4aeb36fe22ef3ab24fc65c8e7bc33b6ca499dd51759d07936c96fee781c641c9",
  "context_from_utc": "2026-09-16T11:00:00Z",
  "visible_from_utc": "2026-09-23T11:00:00Z",
  "visible_to_utc": "2026-09-25T11:05:00Z",
  "warmup_hours": 168,
  "pools_in_window": 621,
  "y_min": 0.06729833399999999,
  "y_max": 0.10737366000000001,
  "source": "build_landscape_window \u2192 pools (SSOT) + JS-identical computeExactBandsForSide",
  "api_equivalent": "POST /live-charts/viewer/api/window (same backend)",
  "upper_band_count": 39,
  "lower_band_count": 179,
  "max_unique_origin_density": 9
}
```

### Table A — UPPER bands (unique origin density ≥ 2)

**Count:** 39

| segment_id | side | lower_price | upper_price | unique_origin_count | origin_ids |
|---|---|---:|---:|---:|---|
| pd1-eod-2026-09-25T11:00:00Z-upper-1 | Upper | 0.0971 | 0.09712 | 2 | `lldv2:DOGEUSDT:5m:upper:1790328600|lldv2:DOGEUSDT:5m:upper:1790333400` |
| pd1-eod-2026-09-25T11:00:00Z-upper-2 | Upper | 0.09712 | 0.09717 | 3 | `lldv2:DOGEUSDT:5m:upper:1790328600|lldv2:DOGEUSDT:5m:upper:1790333400|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-upper-3 | Upper | 0.09717 | 0.097172 | 4 | `lldv2:DOGEUSDT:5m:upper:1790328600|lldv2:DOGEUSDT:5m:upper:1790329200|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-upper-4 | Upper | 0.097172 | 0.09724 | 3 | `lldv2:DOGEUSDT:5m:upper:1790329200|lldv2:DOGEUSDT:5m:upper:1790333400|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-upper-5 | Upper | 0.09724 | 0.097248 | 4 | `lldv2:DOGEUSDT:5m:upper:1790266500|lldv2:DOGEUSDT:5m:upper:1790329200|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-upper-6 | Upper | 0.097248 | 0.09726000000000001 | 3 | `lldv2:DOGEUSDT:5m:upper:1790266500|lldv2:DOGEUSDT:5m:upper:1790329200|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-upper-7 | Upper | 0.09726000000000001 | 0.09732600000000001 | 2 | `lldv2:DOGEUSDT:5m:upper:1790266500|lldv2:DOGEUSDT:5m:upper:1790329200` |
| pd1-eod-2026-09-25T11:00:00Z-upper-9 | Upper | 0.09756 | 0.09788799999999999 | 2 | `lldv2:DOGEUSDT:5m:upper:1790266500|lldv2:DOGEUSDT:5m:upper:1790268600` |
| pd1-eod-2026-09-25T11:00:00Z-upper-12 | Upper | 0.09988 | 0.09997 | 2 | `lldv2:DOGEUSDT:5m:upper:1790170200|lldv2:DOGEUSDT:5m:upper:1790172000` |
| pd1-eod-2026-09-25T11:00:00Z-upper-13 | Upper | 0.09997 | 0.09999 | 3 | `lldv2:DOGEUSDT:5m:upper:1790159700|lldv2:DOGEUSDT:5m:upper:1790170200|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-upper-14 | Upper | 0.09999 | 0.10006 | 4 | `lldv2:DOGEUSDT:5m:upper:1790159700|lldv2:DOGEUSDT:5m:upper:1790170200|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-upper-15 | Upper | 0.10006 | 0.10007 | 6 | `lldv2:DOGEUSDT:5m:upper:1790156700|lldv2:DOGEUSDT:5m:upper:1790157000|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-upper-16 | Upper | 0.10007 | 0.10008 | 7 | `lldv2:DOGEUSDT:5m:upper:1790156700|lldv2:DOGEUSDT:5m:upper:1790157000|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-upper-17 | Upper | 0.10008 | 0.100086 | 9 | `lldv2:DOGEUSDT:5m:upper:1790156700|lldv2:DOGEUSDT:5m:upper:1790157000|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-upper-18 | Upper | 0.100086 | 0.100128 | 8 | `lldv2:DOGEUSDT:5m:upper:1790156700|lldv2:DOGEUSDT:5m:upper:1790157000|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-upper-19 | Upper | 0.100128 | 0.10018 | 7 | `lldv2:DOGEUSDT:5m:upper:1790156700|lldv2:DOGEUSDT:5m:upper:1790157000|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-upper-20 | Upper | 0.10018 | 0.100186 | 8 | `lldv2:DOGEUSDT:5m:upper:1790156700|lldv2:DOGEUSDT:5m:upper:1790157000|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-upper-21 | Upper | 0.100186 | 0.10019 | 7 | `lldv2:DOGEUSDT:5m:upper:1790156700|lldv2:DOGEUSDT:5m:upper:1790157000|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-upper-22 | Upper | 0.10019 | 0.100194 | 8 | `lldv2:DOGEUSDT:5m:upper:1790155200|lldv2:DOGEUSDT:5m:upper:1790156700|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-upper-23 | Upper | 0.100194 | 0.100204 | 7 | `lldv2:DOGEUSDT:5m:upper:1790155200|lldv2:DOGEUSDT:5m:upper:1790156700|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-upper-24 | Upper | 0.100204 | 0.100206 | 6 | `lldv2:DOGEUSDT:5m:upper:1790155200|lldv2:DOGEUSDT:5m:upper:1790156700|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-upper-25 | Upper | 0.100206 | 0.100208 | 5 | `lldv2:DOGEUSDT:5m:upper:1790155200|lldv2:DOGEUSDT:5m:upper:1790156700|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-upper-26 | Upper | 0.100208 | 0.10027 | 4 | `lldv2:DOGEUSDT:5m:upper:1790155200|lldv2:DOGEUSDT:5m:upper:1790156700|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-upper-27 | Upper | 0.10027 | 0.100272 | 5 | `lldv2:DOGEUSDT:5m:upper:1790155200|lldv2:DOGEUSDT:5m:upper:1790155500|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-upper-28 | Upper | 0.100272 | 0.10028 | 4 | `lldv2:DOGEUSDT:5m:upper:1790155200|lldv2:DOGEUSDT:5m:upper:1790155500|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-upper-29 | Upper | 0.10028 | 0.10035 | 3 | `lldv2:DOGEUSDT:5m:upper:1790155200|lldv2:DOGEUSDT:5m:upper:1790155500|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-upper-30 | Upper | 0.10035 | 0.10038400000000001 | 2 | `lldv2:DOGEUSDT:5m:upper:1790155500|lldv2:DOGEUSDT:5m:upper:1790161200` |
| pd1-eod-2026-09-25T11:00:00Z-upper-33 | Upper | 0.10168 | 0.101708 | 2 | `lldv2:DOGEUSDT:5m:upper:1790151300|lldv2:DOGEUSDT:5m:upper:1790151600` |
| pd1-eod-2026-09-25T11:00:00Z-upper-37 | Upper | 0.10283 | 0.10288000000000001 | 2 | `lldv2:DOGEUSDT:5m:upper:1790145600|lldv2:DOGEUSDT:5m:upper:1790145900` |
| pd1-eod-2026-09-25T11:00:00Z-upper-40 | Upper | 0.10388 | 0.104028 | 2 | `lldv2:DOGEUSDT:5m:upper:1790132100|lldv2:DOGEUSDT:5m:upper:1790139300` |
| pd1-eod-2026-09-25T11:00:00Z-upper-42 | Upper | 0.10426 | 0.10429 | 2 | `lldv2:DOGEUSDT:5m:upper:1790132100|lldv2:DOGEUSDT:5m:upper:1790136600` |
| pd1-eod-2026-09-25T11:00:00Z-upper-43 | Upper | 0.10429 | 0.1043 | 3 | `lldv2:DOGEUSDT:5m:upper:1790132100|lldv2:DOGEUSDT:5m:upper:1790136600|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-upper-44 | Upper | 0.1043 | 0.10436 | 4 | `lldv2:DOGEUSDT:5m:upper:1790132100|lldv2:DOGEUSDT:5m:upper:1790135100|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-upper-45 | Upper | 0.10436 | 0.10444 | 5 | `lldv2:DOGEUSDT:5m:upper:1790132100|lldv2:DOGEUSDT:5m:upper:1790135100|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-upper-46 | Upper | 0.10444 | 0.104462 | 4 | `lldv2:DOGEUSDT:5m:upper:1790135100|lldv2:DOGEUSDT:5m:upper:1790135400|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-upper-47 | Upper | 0.104462 | 0.10448400000000001 | 3 | `lldv2:DOGEUSDT:5m:upper:1790135100|lldv2:DOGEUSDT:5m:upper:1790135400|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-upper-48 | Upper | 0.10448400000000001 | 0.104572 | 2 | `lldv2:DOGEUSDT:5m:upper:1790135100|lldv2:DOGEUSDT:5m:upper:1790135400` |
| pd1-eod-2026-09-25T11:00:00Z-upper-51 | Upper | 0.10576 | 0.105888 | 2 | `lldv2:DOGEUSDT:5m:upper:1790049600|lldv2:DOGEUSDT:5m:upper:1790050800` |
| pd1-eod-2026-09-25T11:00:00Z-upper-53 | Upper | 0.10594 | 0.10661200000000001 | 2 | `lldv2:DOGEUSDT:5m:upper:1790049600|lldv2:DOGEUSDT:5m:upper:1790049900` |

### Table B — LOWER bands (unique origin density ≥ 2)

**Count:** 179

| segment_id | side | lower_price | upper_price | unique_origin_count | origin_ids |
|---|---|---:|---:|---:|---|
| pd1-eod-2026-09-25T11:00:00Z-lower-1 | Lower | 0.067456 | 0.0676 | 2 | `lldv2:DOGEUSDT:5m:lower:1785609900|lldv2:DOGEUSDT:5m:lower:1785610200` |
| pd1-eod-2026-09-25T11:00:00Z-lower-4 | Lower | 0.068064 | 0.0682 | 2 | `lldv2:DOGEUSDT:5m:lower:1786023000|lldv2:DOGEUSDT:5m:lower:1786569900` |
| pd1-eod-2026-09-25T11:00:00Z-lower-6 | Lower | 0.068554 | 0.068566 | 2 | `lldv2:DOGEUSDT:5m:lower:1786042200|lldv2:DOGEUSDT:5m:lower:1786569900` |
| pd1-eod-2026-09-25T11:00:00Z-lower-7 | Lower | 0.068566 | 0.06857400000000001 | 3 | `lldv2:DOGEUSDT:5m:lower:1786042200|lldv2:DOGEUSDT:5m:lower:1786569900|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-8 | Lower | 0.06857400000000001 | 0.068606 | 4 | `lldv2:DOGEUSDT:5m:lower:1786042200|lldv2:DOGEUSDT:5m:lower:1786042500|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-9 | Lower | 0.068606 | 0.06861 | 5 | `lldv2:DOGEUSDT:5m:lower:1786026600|lldv2:DOGEUSDT:5m:lower:1786042200|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-10 | Lower | 0.06861 | 0.068628 | 3 | `lldv2:DOGEUSDT:5m:lower:1786026600|lldv2:DOGEUSDT:5m:lower:1786569900|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-11 | Lower | 0.068628 | 0.068636 | 4 | `lldv2:DOGEUSDT:5m:lower:1786026600|lldv2:DOGEUSDT:5m:lower:1786044300|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-12 | Lower | 0.068636 | 0.06866 | 5 | `lldv2:DOGEUSDT:5m:lower:1786026600|lldv2:DOGEUSDT:5m:lower:1786044300|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-13 | Lower | 0.06866 | 0.06868 | 4 | `lldv2:DOGEUSDT:5m:lower:1786026600|lldv2:DOGEUSDT:5m:lower:1786045800|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-14 | Lower | 0.06868 | 0.06868199999999999 | 3 | `lldv2:DOGEUSDT:5m:lower:1786026600|lldv2:DOGEUSDT:5m:lower:1786569900|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-15 | Lower | 0.06868199999999999 | 0.06868999999999999 | 4 | `lldv2:DOGEUSDT:5m:lower:1786026600|lldv2:DOGEUSDT:5m:lower:1786047300|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-16 | Lower | 0.06868999999999999 | 0.06869 | 5 | `lldv2:DOGEUSDT:5m:lower:1786026600|lldv2:DOGEUSDT:5m:lower:1786047300|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-17 | Lower | 0.06869 | 0.06870399999999999 | 5 | `lldv2:DOGEUSDT:5m:lower:1786047300|lldv2:DOGEUSDT:5m:lower:1786047600|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-18 | Lower | 0.06870399999999999 | 0.06871 | 6 | `lldv2:DOGEUSDT:5m:lower:1786047300|lldv2:DOGEUSDT:5m:lower:1786047600|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-19 | Lower | 0.06871 | 0.06872 | 4 | `lldv2:DOGEUSDT:5m:lower:1786080600|lldv2:DOGEUSDT:5m:lower:1786569900|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-20 | Lower | 0.06872 | 0.06876800000000001 | 3 | `lldv2:DOGEUSDT:5m:lower:1786080600|lldv2:DOGEUSDT:5m:lower:1786571700|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-21 | Lower | 0.06876800000000001 | 0.06878 | 4 | `lldv2:DOGEUSDT:5m:lower:1786080600|lldv2:DOGEUSDT:5m:lower:1786571700|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-22 | Lower | 0.06878 | 0.06879 | 3 | `lldv2:DOGEUSDT:5m:lower:1786571700|lldv2:DOGEUSDT:5m:lower:1786572000|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-23 | Lower | 0.06879 | 0.06897 | 2 | `lldv2:DOGEUSDT:5m:lower:1786571700|lldv2:DOGEUSDT:5m:lower:1786916400` |
| pd1-eod-2026-09-25T11:00:00Z-lower-27 | Lower | 0.06934 | 0.06938 | 2 | `lldv2:DOGEUSDT:5m:lower:1786919700|lldv2:DOGEUSDT:5m:lower:1786926300` |
| pd1-eod-2026-09-25T11:00:00Z-lower-29 | Lower | 0.069398 | 0.06942 | 2 | `lldv2:DOGEUSDT:5m:lower:1786926000|lldv2:DOGEUSDT:5m:lower:1786926300` |
| pd1-eod-2026-09-25T11:00:00Z-lower-31 | Lower | 0.06943799999999999 | 0.06945 | 2 | `lldv2:DOGEUSDT:5m:lower:1786926000|lldv2:DOGEUSDT:5m:lower:1787021700` |
| pd1-eod-2026-09-25T11:00:00Z-lower-33 | Lower | 0.069488 | 0.06951 | 2 | `lldv2:DOGEUSDT:5m:lower:1787021700|lldv2:DOGEUSDT:5m:lower:1787031300` |
| pd1-eod-2026-09-25T11:00:00Z-lower-34 | Lower | 0.06951 | 0.06953 | 3 | `lldv2:DOGEUSDT:5m:lower:1787021100|lldv2:DOGEUSDT:5m:lower:1787021700|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-35 | Lower | 0.06953 | 0.06956 | 2 | `lldv2:DOGEUSDT:5m:lower:1787021100|lldv2:DOGEUSDT:5m:lower:1787031300` |
| pd1-eod-2026-09-25T11:00:00Z-lower-37 | Lower | 0.06958199999999999 | 0.06959 | 2 | `lldv2:DOGEUSDT:5m:lower:1787021100|lldv2:DOGEUSDT:5m:lower:1787040600` |
| pd1-eod-2026-09-25T11:00:00Z-lower-39 | Lower | 0.069598 | 0.06963 | 2 | `lldv2:DOGEUSDT:5m:lower:1787040300|lldv2:DOGEUSDT:5m:lower:1787040600` |
| pd1-eod-2026-09-25T11:00:00Z-lower-41 | Lower | 0.069668 | 0.069684 | 2 | `lldv2:DOGEUSDT:5m:lower:1787061000|lldv2:DOGEUSDT:5m:lower:1787061300` |
| pd1-eod-2026-09-25T11:00:00Z-lower-42 | Lower | 0.069684 | 0.069698 | 3 | `lldv2:DOGEUSDT:5m:lower:1787059800|lldv2:DOGEUSDT:5m:lower:1787061000|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-43 | Lower | 0.069698 | 0.0697 | 4 | `lldv2:DOGEUSDT:5m:lower:1787041800|lldv2:DOGEUSDT:5m:lower:1787059800|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-44 | Lower | 0.0697 | 0.06971 | 3 | `lldv2:DOGEUSDT:5m:lower:1787041800|lldv2:DOGEUSDT:5m:lower:1787059800|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-45 | Lower | 0.06971 | 0.069712 | 2 | `lldv2:DOGEUSDT:5m:lower:1787041800|lldv2:DOGEUSDT:5m:lower:1787059800` |
| pd1-eod-2026-09-25T11:00:00Z-lower-46 | Lower | 0.069712 | 0.06973 | 3 | `lldv2:DOGEUSDT:5m:lower:1787041800|lldv2:DOGEUSDT:5m:lower:1787059800|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-47 | Lower | 0.06973 | 0.06974 | 2 | `lldv2:DOGEUSDT:5m:lower:1787059800|lldv2:DOGEUSDT:5m:lower:1787062200` |
| pd1-eod-2026-09-25T11:00:00Z-lower-49 | Lower | 0.06977599999999999 | 0.069806 | 2 | `lldv2:DOGEUSDT:5m:lower:1787106000|lldv2:DOGEUSDT:5m:lower:1787120100` |
| pd1-eod-2026-09-25T11:00:00Z-lower-50 | Lower | 0.069806 | 0.06981000000000001 | 3 | `lldv2:DOGEUSDT:5m:lower:1787106000|lldv2:DOGEUSDT:5m:lower:1787119500|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-51 | Lower | 0.06981000000000001 | 0.06982 | 4 | `lldv2:DOGEUSDT:5m:lower:1787105700|lldv2:DOGEUSDT:5m:lower:1787106000|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-52 | Lower | 0.06982 | 0.069822 | 2 | `lldv2:DOGEUSDT:5m:lower:1787105700|lldv2:DOGEUSDT:5m:lower:1787119500` |
| pd1-eod-2026-09-25T11:00:00Z-lower-53 | Lower | 0.069822 | 0.06983 | 3 | `lldv2:DOGEUSDT:5m:lower:1787105700|lldv2:DOGEUSDT:5m:lower:1787119500|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-57 | Lower | 0.069954 | 0.069958 | 2 | `lldv2:DOGEUSDT:5m:lower:1787134500|lldv2:DOGEUSDT:5m:lower:1787148000` |
| pd1-eod-2026-09-25T11:00:00Z-lower-58 | Lower | 0.069958 | 0.06997 | 3 | `lldv2:DOGEUSDT:5m:lower:1787134200|lldv2:DOGEUSDT:5m:lower:1787134500|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-60 | Lower | 0.06997199999999999 | 0.06998 | 2 | `lldv2:DOGEUSDT:5m:lower:1787135400|lldv2:DOGEUSDT:5m:lower:1787148000` |
| pd1-eod-2026-09-25T11:00:00Z-lower-63 | Lower | 0.070472 | 0.07052 | 2 | `lldv2:DOGEUSDT:5m:lower:1787149200|lldv2:DOGEUSDT:5m:lower:1787149500` |
| pd1-eod-2026-09-25T11:00:00Z-lower-65 | Lower | 0.07186200000000001 | 0.07189 | 2 | `lldv2:DOGEUSDT:5m:lower:1787159700|lldv2:DOGEUSDT:5m:lower:1787160600` |
| pd1-eod-2026-09-25T11:00:00Z-lower-68 | Lower | 0.07211200000000001 | 0.07215 | 2 | `lldv2:DOGEUSDT:5m:lower:1787162700|lldv2:DOGEUSDT:5m:lower:1787163000` |
| pd1-eod-2026-09-25T11:00:00Z-lower-74 | Lower | 0.074118 | 0.074346 | 2 | `lldv2:DOGEUSDT:5m:lower:1787178600|lldv2:DOGEUSDT:5m:lower:1787179500` |
| pd1-eod-2026-09-25T11:00:00Z-lower-75 | Lower | 0.074346 | 0.07435 | 3 | `lldv2:DOGEUSDT:5m:lower:1787178600|lldv2:DOGEUSDT:5m:lower:1787179500|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-76 | Lower | 0.07435 | 0.074358 | 2 | `lldv2:DOGEUSDT:5m:lower:1787178600|lldv2:DOGEUSDT:5m:lower:1787200200` |
| pd1-eod-2026-09-25T11:00:00Z-lower-77 | Lower | 0.074358 | 0.07437 | 3 | `lldv2:DOGEUSDT:5m:lower:1787178600|lldv2:DOGEUSDT:5m:lower:1787181900|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-78 | Lower | 0.07437 | 0.07438 | 2 | `lldv2:DOGEUSDT:5m:lower:1787181900|lldv2:DOGEUSDT:5m:lower:1787200200` |
| pd1-eod-2026-09-25T11:00:00Z-lower-79 | Lower | 0.07438 | 0.07443 | 3 | `lldv2:DOGEUSDT:5m:lower:1787181900|lldv2:DOGEUSDT:5m:lower:1787200200|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-80 | Lower | 0.07443 | 0.07443400000000001 | 2 | `lldv2:DOGEUSDT:5m:lower:1787181900|lldv2:DOGEUSDT:5m:lower:1787206500` |
| pd1-eod-2026-09-25T11:00:00Z-lower-81 | Lower | 0.07443400000000001 | 0.07446 | 3 | `lldv2:DOGEUSDT:5m:lower:1787181900|lldv2:DOGEUSDT:5m:lower:1787205900|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-82 | Lower | 0.07446 | 0.07451 | 2 | `lldv2:DOGEUSDT:5m:lower:1787181900|lldv2:DOGEUSDT:5m:lower:1787205900` |
| pd1-eod-2026-09-25T11:00:00Z-lower-86 | Lower | 0.074926 | 0.07499 | 2 | `lldv2:DOGEUSDT:5m:lower:1787210100|lldv2:DOGEUSDT:5m:lower:1787211000` |
| pd1-eod-2026-09-25T11:00:00Z-lower-89 | Lower | 0.07635800000000001 | 0.07638 | 2 | `lldv2:DOGEUSDT:5m:lower:1787219400|lldv2:DOGEUSDT:5m:lower:1787220300` |
| pd1-eod-2026-09-25T11:00:00Z-lower-91 | Lower | 0.076448 | 0.076462 | 2 | `lldv2:DOGEUSDT:5m:lower:1787220300|lldv2:DOGEUSDT:5m:lower:1787227500` |
| pd1-eod-2026-09-25T11:00:00Z-lower-92 | Lower | 0.076462 | 0.07649 | 3 | `lldv2:DOGEUSDT:5m:lower:1787220300|lldv2:DOGEUSDT:5m:lower:1787227500|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-93 | Lower | 0.07649 | 0.07657 | 2 | `lldv2:DOGEUSDT:5m:lower:1787227500|lldv2:DOGEUSDT:5m:lower:1787229300` |
| pd1-eod-2026-09-25T11:00:00Z-lower-95 | Lower | 0.076638 | 0.07664 | 2 | `lldv2:DOGEUSDT:5m:lower:1787227500|lldv2:DOGEUSDT:5m:lower:1787232900` |
| pd1-eod-2026-09-25T11:00:00Z-lower-97 | Lower | 0.07668799999999999 | 0.07685 | 2 | `lldv2:DOGEUSDT:5m:lower:1787232600|lldv2:DOGEUSDT:5m:lower:1787232900` |
| pd1-eod-2026-09-25T11:00:00Z-lower-100 | Lower | 0.077428 | 0.077502 | 2 | `lldv2:DOGEUSDT:5m:lower:1787235900|lldv2:DOGEUSDT:5m:lower:1787236800` |
| pd1-eod-2026-09-25T11:00:00Z-lower-101 | Lower | 0.077502 | 0.077506 | 3 | `lldv2:DOGEUSDT:5m:lower:1787235600|lldv2:DOGEUSDT:5m:lower:1787235900|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-102 | Lower | 0.077506 | 0.07754 | 4 | `lldv2:DOGEUSDT:5m:lower:1787235600|lldv2:DOGEUSDT:5m:lower:1787235900|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-103 | Lower | 0.07754 | 0.07762 | 3 | `lldv2:DOGEUSDT:5m:lower:1787235600|lldv2:DOGEUSDT:5m:lower:1787236800|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-104 | Lower | 0.07762 | 0.07763 | 2 | `lldv2:DOGEUSDT:5m:lower:1787235600|lldv2:DOGEUSDT:5m:lower:1789497900` |
| pd1-eod-2026-09-25T11:00:00Z-lower-106 | Lower | 0.078038 | 0.07806 | 2 | `lldv2:DOGEUSDT:5m:lower:1787375700|lldv2:DOGEUSDT:5m:lower:1789497900` |
| pd1-eod-2026-09-25T11:00:00Z-lower-107 | Lower | 0.07806 | 0.07818800000000001 | 3 | `lldv2:DOGEUSDT:5m:lower:1787375700|lldv2:DOGEUSDT:5m:lower:1789497900|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-108 | Lower | 0.07818800000000001 | 0.078198 | 4 | `lldv2:DOGEUSDT:5m:lower:1787375700|lldv2:DOGEUSDT:5m:lower:1789497900|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-109 | Lower | 0.078198 | 0.07824 | 5 | `lldv2:DOGEUSDT:5m:lower:1787375700|lldv2:DOGEUSDT:5m:lower:1789497900|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-110 | Lower | 0.07824 | 0.07831 | 4 | `lldv2:DOGEUSDT:5m:lower:1787375700|lldv2:DOGEUSDT:5m:lower:1789497900|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-111 | Lower | 0.07831 | 0.07833 | 3 | `lldv2:DOGEUSDT:5m:lower:1787375700|lldv2:DOGEUSDT:5m:lower:1789580700|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-112 | Lower | 0.07833 | 0.0787 | 2 | `lldv2:DOGEUSDT:5m:lower:1787375700|lldv2:DOGEUSDT:5m:lower:1789583700` |
| pd1-eod-2026-09-25T11:00:00Z-lower-114 | Lower | 0.07883799999999999 | 0.07901 | 2 | `lldv2:DOGEUSDT:5m:lower:1787375700|lldv2:DOGEUSDT:5m:lower:1789586700` |
| pd1-eod-2026-09-25T11:00:00Z-lower-116 | Lower | 0.07945799999999999 | 0.07976399999999999 | 2 | `lldv2:DOGEUSDT:5m:lower:1787375700|lldv2:DOGEUSDT:5m:lower:1789596000` |
| pd1-eod-2026-09-25T11:00:00Z-lower-117 | Lower | 0.07976399999999999 | 0.07977 | 3 | `lldv2:DOGEUSDT:5m:lower:1787375700|lldv2:DOGEUSDT:5m:lower:1789596000|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-118 | Lower | 0.07977 | 0.07978 | 2 | `lldv2:DOGEUSDT:5m:lower:1787375700|lldv2:DOGEUSDT:5m:lower:1789599600` |
| pd1-eod-2026-09-25T11:00:00Z-lower-119 | Lower | 0.07978 | 0.07981600000000001 | 3 | `lldv2:DOGEUSDT:5m:lower:1787375700|lldv2:DOGEUSDT:5m:lower:1789597200|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-120 | Lower | 0.07981600000000001 | 0.07982 | 4 | `lldv2:DOGEUSDT:5m:lower:1787375700|lldv2:DOGEUSDT:5m:lower:1789597200|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-121 | Lower | 0.07982 | 0.07984 | 5 | `lldv2:DOGEUSDT:5m:lower:1787375700|lldv2:DOGEUSDT:5m:lower:1789597200|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-122 | Lower | 0.07984 | 0.07986 | 4 | `lldv2:DOGEUSDT:5m:lower:1787375700|lldv2:DOGEUSDT:5m:lower:1789597200|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-123 | Lower | 0.07986 | 0.07988 | 3 | `lldv2:DOGEUSDT:5m:lower:1787375700|lldv2:DOGEUSDT:5m:lower:1789597200|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-125 | Lower | 0.08037399999999999 | 0.08041799999999999 | 2 | `lldv2:DOGEUSDT:5m:lower:1787375700|lldv2:DOGEUSDT:5m:lower:1789606800` |
| pd1-eod-2026-09-25T11:00:00Z-lower-126 | Lower | 0.08041799999999999 | 0.080422 | 3 | `lldv2:DOGEUSDT:5m:lower:1787375700|lldv2:DOGEUSDT:5m:lower:1789606800|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-127 | Lower | 0.080422 | 0.080444 | 4 | `lldv2:DOGEUSDT:5m:lower:1787375700|lldv2:DOGEUSDT:5m:lower:1789601700|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-128 | Lower | 0.080444 | 0.08045 | 5 | `lldv2:DOGEUSDT:5m:lower:1787375700|lldv2:DOGEUSDT:5m:lower:1789601700|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-129 | Lower | 0.08045 | 0.080452 | 4 | `lldv2:DOGEUSDT:5m:lower:1787375700|lldv2:DOGEUSDT:5m:lower:1789601700|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-130 | Lower | 0.080452 | 0.08049200000000001 | 5 | `lldv2:DOGEUSDT:5m:lower:1787375700|lldv2:DOGEUSDT:5m:lower:1789601700|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-131 | Lower | 0.08049200000000001 | 0.0805 | 6 | `lldv2:DOGEUSDT:5m:lower:1787375700|lldv2:DOGEUSDT:5m:lower:1789601700|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-132 | Lower | 0.0805 | 0.08051 | 5 | `lldv2:DOGEUSDT:5m:lower:1787375700|lldv2:DOGEUSDT:5m:lower:1789601700|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-133 | Lower | 0.08051 | 0.08052 | 4 | `lldv2:DOGEUSDT:5m:lower:1787375700|lldv2:DOGEUSDT:5m:lower:1789610700|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-134 | Lower | 0.08052 | 0.08059 | 3 | `lldv2:DOGEUSDT:5m:lower:1787375700|lldv2:DOGEUSDT:5m:lower:1789610700|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-135 | Lower | 0.08059 | 0.08064 | 2 | `lldv2:DOGEUSDT:5m:lower:1787375700|lldv2:DOGEUSDT:5m:lower:1789610700` |
| pd1-eod-2026-09-25T11:00:00Z-lower-137 | Lower | 0.080904 | 0.08092999999999999 | 2 | `lldv2:DOGEUSDT:5m:lower:1787375700|lldv2:DOGEUSDT:5m:lower:1789653900` |
| pd1-eod-2026-09-25T11:00:00Z-lower-138 | Lower | 0.08092999999999999 | 0.08105 | 3 | `lldv2:DOGEUSDT:5m:lower:1787375700|lldv2:DOGEUSDT:5m:lower:1789653900|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-139 | Lower | 0.08105 | 0.081058 | 3 | `lldv2:DOGEUSDT:5m:lower:1787375700|lldv2:DOGEUSDT:5m:lower:1789653900|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-140 | Lower | 0.081058 | 0.08108 | 4 | `lldv2:DOGEUSDT:5m:lower:1787375700|lldv2:DOGEUSDT:5m:lower:1789653900|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-141 | Lower | 0.08108 | 0.081086 | 3 | `lldv2:DOGEUSDT:5m:lower:1787375700|lldv2:DOGEUSDT:5m:lower:1789657800|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-142 | Lower | 0.081086 | 0.08111 | 4 | `lldv2:DOGEUSDT:5m:lower:1787375700|lldv2:DOGEUSDT:5m:lower:1789657800|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-143 | Lower | 0.08111 | 0.08113 | 3 | `lldv2:DOGEUSDT:5m:lower:1787375700|lldv2:DOGEUSDT:5m:lower:1789657800|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-144 | Lower | 0.08113 | 0.08117 | 2 | `lldv2:DOGEUSDT:5m:lower:1787375700|lldv2:DOGEUSDT:5m:lower:1789683300` |
| pd1-eod-2026-09-25T11:00:00Z-lower-146 | Lower | 0.081228 | 0.081268 | 2 | `lldv2:DOGEUSDT:5m:lower:1787375700|lldv2:DOGEUSDT:5m:lower:1789685700` |
| pd1-eod-2026-09-25T11:00:00Z-lower-147 | Lower | 0.081268 | 0.0813 | 3 | `lldv2:DOGEUSDT:5m:lower:1787375700|lldv2:DOGEUSDT:5m:lower:1789685400|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-148 | Lower | 0.0813 | 0.08131200000000001 | 3 | `lldv2:DOGEUSDT:5m:lower:1787375700|lldv2:DOGEUSDT:5m:lower:1789685400|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-149 | Lower | 0.08131200000000001 | 0.08132 | 4 | `lldv2:DOGEUSDT:5m:lower:1787375700|lldv2:DOGEUSDT:5m:lower:1789685400|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-150 | Lower | 0.08132 | 0.08134 | 3 | `lldv2:DOGEUSDT:5m:lower:1787375700|lldv2:DOGEUSDT:5m:lower:1789691100|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-151 | Lower | 0.08134 | 0.08136 | 2 | `lldv2:DOGEUSDT:5m:lower:1787375700|lldv2:DOGEUSDT:5m:lower:1789692000` |
| pd1-eod-2026-09-25T11:00:00Z-lower-153 | Lower | 0.082132 | 0.08223 | 2 | `lldv2:DOGEUSDT:5m:lower:1787375700|lldv2:DOGEUSDT:5m:lower:1789697400` |
| pd1-eod-2026-09-25T11:00:00Z-lower-157 | Lower | 0.083598 | 0.08367 | 2 | `lldv2:DOGEUSDT:5m:lower:1789702800|lldv2:DOGEUSDT:5m:lower:1789705800` |
| pd1-eod-2026-09-25T11:00:00Z-lower-160 | Lower | 0.084008 | 0.084022 | 2 | `lldv2:DOGEUSDT:5m:lower:1789710600|lldv2:DOGEUSDT:5m:lower:1789710900` |
| pd1-eod-2026-09-25T11:00:00Z-lower-161 | Lower | 0.084022 | 0.08403 | 3 | `lldv2:DOGEUSDT:5m:lower:1789710300|lldv2:DOGEUSDT:5m:lower:1789710600|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-162 | Lower | 0.08403 | 0.084034 | 2 | `lldv2:DOGEUSDT:5m:lower:1789710300|lldv2:DOGEUSDT:5m:lower:1789710600` |
| pd1-eod-2026-09-25T11:00:00Z-lower-163 | Lower | 0.084034 | 0.084046 | 3 | `lldv2:DOGEUSDT:5m:lower:1789710300|lldv2:DOGEUSDT:5m:lower:1789710600|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-164 | Lower | 0.084046 | 0.08406 | 4 | `lldv2:DOGEUSDT:5m:lower:1789710300|lldv2:DOGEUSDT:5m:lower:1789710600|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-165 | Lower | 0.08406 | 0.084076 | 3 | `lldv2:DOGEUSDT:5m:lower:1789710300|lldv2:DOGEUSDT:5m:lower:1789714500|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-166 | Lower | 0.084076 | 0.084084 | 4 | `lldv2:DOGEUSDT:5m:lower:1789710300|lldv2:DOGEUSDT:5m:lower:1789712400|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-167 | Lower | 0.084084 | 0.08409 | 5 | `lldv2:DOGEUSDT:5m:lower:1789710300|lldv2:DOGEUSDT:5m:lower:1789712400|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-168 | Lower | 0.08409 | 0.084144 | 3 | `lldv2:DOGEUSDT:5m:lower:1789712400|lldv2:DOGEUSDT:5m:lower:1789718700|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-169 | Lower | 0.084144 | 0.08416 | 4 | `lldv2:DOGEUSDT:5m:lower:1789712400|lldv2:DOGEUSDT:5m:lower:1789718700|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-170 | Lower | 0.08416 | 0.08418 | 3 | `lldv2:DOGEUSDT:5m:lower:1789718700|lldv2:DOGEUSDT:5m:lower:1789719000|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-171 | Lower | 0.08418 | 0.08421 | 2 | `lldv2:DOGEUSDT:5m:lower:1789718700|lldv2:DOGEUSDT:5m:lower:1789900800` |
| pd1-eod-2026-09-25T11:00:00Z-lower-173 | Lower | 0.084226 | 0.08428 | 2 | `lldv2:DOGEUSDT:5m:lower:1789894200|lldv2:DOGEUSDT:5m:lower:1789900800` |
| pd1-eod-2026-09-25T11:00:00Z-lower-176 | Lower | 0.084512 | 0.084576 | 2 | `lldv2:DOGEUSDT:5m:lower:1789902000|lldv2:DOGEUSDT:5m:lower:1789902900` |
| pd1-eod-2026-09-25T11:00:00Z-lower-177 | Lower | 0.084576 | 0.08458 | 3 | `lldv2:DOGEUSDT:5m:lower:1789902000|lldv2:DOGEUSDT:5m:lower:1789902900|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-179 | Lower | 0.084632 | 0.084636 | 2 | `lldv2:DOGEUSDT:5m:lower:1789904100|lldv2:DOGEUSDT:5m:lower:1789913700` |
| pd1-eod-2026-09-25T11:00:00Z-lower-180 | Lower | 0.084636 | 0.084664 | 3 | `lldv2:DOGEUSDT:5m:lower:1789904100|lldv2:DOGEUSDT:5m:lower:1789911600|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-181 | Lower | 0.084664 | 0.08469600000000001 | 4 | `lldv2:DOGEUSDT:5m:lower:1789904100|lldv2:DOGEUSDT:5m:lower:1789911000|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-182 | Lower | 0.08469600000000001 | 0.0847 | 5 | `lldv2:DOGEUSDT:5m:lower:1789904100|lldv2:DOGEUSDT:5m:lower:1789907400|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-183 | Lower | 0.0847 | 0.08472 | 3 | `lldv2:DOGEUSDT:5m:lower:1789907400|lldv2:DOGEUSDT:5m:lower:1789911000|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-184 | Lower | 0.08472 | 0.08476 | 2 | `lldv2:DOGEUSDT:5m:lower:1789907400|lldv2:DOGEUSDT:5m:lower:1789911000` |
| pd1-eod-2026-09-25T11:00:00Z-lower-187 | Lower | 0.08523399999999999 | 0.08531 | 2 | `lldv2:DOGEUSDT:5m:lower:1789917600|lldv2:DOGEUSDT:5m:lower:1789917900` |
| pd1-eod-2026-09-25T11:00:00Z-lower-190 | Lower | 0.085828 | 0.08606 | 2 | `lldv2:DOGEUSDT:5m:lower:1789938000|lldv2:DOGEUSDT:5m:lower:1789938600` |
| pd1-eod-2026-09-25T11:00:00Z-lower-193 | Lower | 0.08629999999999999 | 0.08633400000000001 | 2 | `lldv2:DOGEUSDT:5m:lower:1789940400|lldv2:DOGEUSDT:5m:lower:1789940700` |
| pd1-eod-2026-09-25T11:00:00Z-lower-194 | Lower | 0.08633400000000001 | 0.08642 | 3 | `lldv2:DOGEUSDT:5m:lower:1789939500|lldv2:DOGEUSDT:5m:lower:1789940400|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-198 | Lower | 0.08692399999999999 | 0.08697200000000001 | 2 | `lldv2:DOGEUSDT:5m:lower:1789955700|lldv2:DOGEUSDT:5m:lower:1789956000` |
| pd1-eod-2026-09-25T11:00:00Z-lower-199 | Lower | 0.08697200000000001 | 0.087034 | 3 | `lldv2:DOGEUSDT:5m:lower:1789942800|lldv2:DOGEUSDT:5m:lower:1789955700|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-200 | Lower | 0.087034 | 0.08703599999999999 | 4 | `lldv2:DOGEUSDT:5m:lower:1789942800|lldv2:DOGEUSDT:5m:lower:1789947900|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-201 | Lower | 0.08703599999999999 | 0.087038 | 5 | `lldv2:DOGEUSDT:5m:lower:1789942800|lldv2:DOGEUSDT:5m:lower:1789946100|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-202 | Lower | 0.087038 | 0.08704 | 6 | `lldv2:DOGEUSDT:5m:lower:1789942800|lldv2:DOGEUSDT:5m:lower:1789945500|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-203 | Lower | 0.08704 | 0.08704200000000001 | 5 | `lldv2:DOGEUSDT:5m:lower:1789945500|lldv2:DOGEUSDT:5m:lower:1789946100|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-204 | Lower | 0.08704200000000001 | 0.0871 | 6 | `lldv2:DOGEUSDT:5m:lower:1789945500|lldv2:DOGEUSDT:5m:lower:1789946100|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-205 | Lower | 0.0871 | 0.087102 | 5 | `lldv2:DOGEUSDT:5m:lower:1789945500|lldv2:DOGEUSDT:5m:lower:1789947900|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-206 | Lower | 0.087102 | 0.08715 | 6 | `lldv2:DOGEUSDT:5m:lower:1789945500|lldv2:DOGEUSDT:5m:lower:1789947000|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-207 | Lower | 0.08715 | 0.08715800000000001 | 5 | `lldv2:DOGEUSDT:5m:lower:1789947000|lldv2:DOGEUSDT:5m:lower:1789947900|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-208 | Lower | 0.08715800000000001 | 0.08717 | 6 | `lldv2:DOGEUSDT:5m:lower:1789947000|lldv2:DOGEUSDT:5m:lower:1789947900|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-209 | Lower | 0.08717 | 0.08718 | 4 | `lldv2:DOGEUSDT:5m:lower:1789955400|lldv2:DOGEUSDT:5m:lower:1789955700|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-210 | Lower | 0.08718 | 0.08737 | 2 | `lldv2:DOGEUSDT:5m:lower:1789955400|lldv2:DOGEUSDT:5m:lower:1789957800` |
| pd1-eod-2026-09-25T11:00:00Z-lower-215 | Lower | 0.088186 | 0.0882 | 2 | `lldv2:DOGEUSDT:5m:lower:1789970400|lldv2:DOGEUSDT:5m:lower:1789970700` |
| pd1-eod-2026-09-25T11:00:00Z-lower-216 | Lower | 0.0882 | 0.088206 | 3 | `lldv2:DOGEUSDT:5m:lower:1789970400|lldv2:DOGEUSDT:5m:lower:1789970700|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-217 | Lower | 0.088206 | 0.08826 | 4 | `lldv2:DOGEUSDT:5m:lower:1789969500|lldv2:DOGEUSDT:5m:lower:1789970400|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-218 | Lower | 0.08826 | 0.08833 | 3 | `lldv2:DOGEUSDT:5m:lower:1789969500|lldv2:DOGEUSDT:5m:lower:1789970400|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-219 | Lower | 0.08833 | 0.088342 | 2 | `lldv2:DOGEUSDT:5m:lower:1789969500|lldv2:DOGEUSDT:5m:lower:1789973100` |
| pd1-eod-2026-09-25T11:00:00Z-lower-220 | Lower | 0.088342 | 0.08837 | 3 | `lldv2:DOGEUSDT:5m:lower:1789969500|lldv2:DOGEUSDT:5m:lower:1789972800|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-221 | Lower | 0.08837 | 0.08846 | 2 | `lldv2:DOGEUSDT:5m:lower:1789972800|lldv2:DOGEUSDT:5m:lower:1789973100` |
| pd1-eod-2026-09-25T11:00:00Z-lower-224 | Lower | 0.088714 | 0.08877 | 2 | `lldv2:DOGEUSDT:5m:lower:1789974600|lldv2:DOGEUSDT:5m:lower:1789975800` |
| pd1-eod-2026-09-25T11:00:00Z-lower-227 | Lower | 0.09029 | 0.09101 | 2 | `lldv2:DOGEUSDT:5m:lower:1789984200|lldv2:DOGEUSDT:5m:lower:1790179200` |
| pd1-eod-2026-09-25T11:00:00Z-lower-229 | Lower | 0.09101000000000001 | 0.091088 | 2 | `lldv2:DOGEUSDT:5m:lower:1789984200|lldv2:DOGEUSDT:5m:lower:1790242500` |
| pd1-eod-2026-09-25T11:00:00Z-lower-230 | Lower | 0.091088 | 0.09113 | 3 | `lldv2:DOGEUSDT:5m:lower:1789984200|lldv2:DOGEUSDT:5m:lower:1790242200|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-231 | Lower | 0.09113 | 0.09124600000000001 | 2 | `lldv2:DOGEUSDT:5m:lower:1790242200|lldv2:DOGEUSDT:5m:lower:1790242500` |
| pd1-eod-2026-09-25T11:00:00Z-lower-232 | Lower | 0.09124600000000001 | 0.091356 | 3 | `lldv2:DOGEUSDT:5m:lower:1790183100|lldv2:DOGEUSDT:5m:lower:1790242200|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-233 | Lower | 0.091356 | 0.09137 | 4 | `lldv2:DOGEUSDT:5m:lower:1790183100|lldv2:DOGEUSDT:5m:lower:1790197200|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-234 | Lower | 0.09137 | 0.09145 | 3 | `lldv2:DOGEUSDT:5m:lower:1790183100|lldv2:DOGEUSDT:5m:lower:1790197200|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-235 | Lower | 0.09145 | 0.09146 | 2 | `lldv2:DOGEUSDT:5m:lower:1790197200|lldv2:DOGEUSDT:5m:lower:1790242200` |
| pd1-eod-2026-09-25T11:00:00Z-lower-238 | Lower | 0.091678 | 0.091726 | 2 | `lldv2:DOGEUSDT:5m:lower:1790244600|lldv2:DOGEUSDT:5m:lower:1790245800` |
| pd1-eod-2026-09-25T11:00:00Z-lower-239 | Lower | 0.091726 | 0.091738 | 3 | `lldv2:DOGEUSDT:5m:lower:1790244300|lldv2:DOGEUSDT:5m:lower:1790244600|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-240 | Lower | 0.091738 | 0.09176 | 4 | `lldv2:DOGEUSDT:5m:lower:1790244300|lldv2:DOGEUSDT:5m:lower:1790244600|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-241 | Lower | 0.09176 | 0.09185 | 3 | `lldv2:DOGEUSDT:5m:lower:1790244300|lldv2:DOGEUSDT:5m:lower:1790244600|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-243 | Lower | 0.092008 | 0.0921 | 2 | `lldv2:DOGEUSDT:5m:lower:1790253000|lldv2:DOGEUSDT:5m:lower:1790253300` |
| pd1-eod-2026-09-25T11:00:00Z-lower-248 | Lower | 0.09435 | 0.094372 | 2 | `lldv2:DOGEUSDT:5m:lower:1790306700|lldv2:DOGEUSDT:5m:lower:1790319900` |
| pd1-eod-2026-09-25T11:00:00Z-lower-249 | Lower | 0.094372 | 0.0945 | 3 | `lldv2:DOGEUSDT:5m:lower:1790306700|lldv2:DOGEUSDT:5m:lower:1790318100|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-250 | Lower | 0.0945 | 0.09457 | 2 | `lldv2:DOGEUSDT:5m:lower:1790306700|lldv2:DOGEUSDT:5m:lower:1790319900` |
| pd1-eod-2026-09-25T11:00:00Z-lower-251 | Lower | 0.094714 | 0.09481 | 2 | `lldv2:DOGEUSDT:5m:lower:1790321700|lldv2:DOGEUSDT:5m:lower:1790322000` |
| pd1-eod-2026-09-25T11:00:00Z-lower-254 | Lower | 0.095792 | 0.09582 | 2 | `lldv2:DOGEUSDT:5m:lower:1790325900|lldv2:DOGEUSDT:5m:lower:1790326200` |
| pd1-eod-2026-09-25T11:00:00Z-lower-257 | Lower | 0.095978 | 0.09599200000000001 | 2 | `lldv2:DOGEUSDT:5m:lower:1790330400|lldv2:DOGEUSDT:5m:lower:1790331600` |
| pd1-eod-2026-09-25T11:00:00Z-lower-258 | Lower | 0.09599200000000001 | 0.09606 | 3 | `lldv2:DOGEUSDT:5m:lower:1790330400|lldv2:DOGEUSDT:5m:lower:1790331300|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-259 | Lower | 0.09606 | 0.09607 | 4 | `lldv2:DOGEUSDT:5m:lower:1790328000|lldv2:DOGEUSDT:5m:lower:1790330400|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-260 | Lower | 0.09607 | 0.0961 | 3 | `lldv2:DOGEUSDT:5m:lower:1790328000|lldv2:DOGEUSDT:5m:lower:1790330400|lldv2:D...` |
| pd1-eod-2026-09-25T11:00:00Z-lower-261 | Lower | 0.0961 | 0.09618 | 2 | `lldv2:DOGEUSDT:5m:lower:1790328000|lldv2:DOGEUSDT:5m:lower:1790330400` |
