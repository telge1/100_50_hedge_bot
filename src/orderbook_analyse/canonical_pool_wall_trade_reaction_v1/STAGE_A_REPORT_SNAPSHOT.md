# Stage A — CLEAR_POOL_SELECTION_RULE_V1

Generated: `2026-08-31T18:42:07Z`
Rule: `CLEAR_POOL_SELECTION_RULE_V1`

> Große HTF-Zonen, die beim Anlauf wirklich voll mit Book-Liquidität sind — der Rest ist nachrangig. Entscheidung = Verhalten dieser Zonen-Liquidität.

## Gate

- A1–A6: BTCUSDT · TF ∈ ['15m', '30m'] · P≥5 · mid inside zone
- A7 SoT: **raw_ob200_zone_depth** (≥2 levels + qty>0 in `[lower,upper]`)
- **Not used as A7:** 1s `wall_in_pool` dominant-wall proxy

## Counts

- A1–A6 scanned in raw window: **226**
- A7 pass (candidates): **126**
- A7 reject: **100**
- Among pass, 1s proxy YES: **25** / NO: **101**
- Median zone levels at touch: **91.0**
- Median zone notional at touch: **1873715.39835**

### A7 reject reasons

- `empty_or_thin_zone`: **56**
- `no_raw_book`: **44**

## Files

- `stage_a_candidates.csv` — feed these to Stage B
- `stage_a_rejects.csv`
- `summary.json`
