# CH Break/Reclaim Microstructure Audit

**Primary Decision:** `OB_FLOW_SIGNAL_SYMBOL_OR_DIRECTION_DEPENDENT`

Research only. No live gate, no trading rule, no trend-scanner changes.

## Scope

- Events: **54** (exactly CH-covered rows from coverage audit)
- Feature rows: 740
- Causal cutoffs: all features use data with timestamp ≤ observation time T
- Outcome labels may use future information (explicitly separated)

## 1. Coverage / data quality

- DATA_VALID: **53**
- DATA_WARNING: **0**
- DATA_INVALID: **1**

Main statistics use DATA_VALID only.

## 2. Outcome distribution

- `BREAK_ACCEPTED`: 31
- `RECLAIM_FAST`: 11
- `HOLD_NO_BREAK`: 9
- `RECLAIM_SLOW`: 2
- `EXCLUDED`: 1

### Taxonomy mapping

- `BREAKDOWN/BREAKOUT_CONFIRMED`, `BEARISH_ACCEPTANCE`, `RECLAIM_THEN_BREAK_CONTINUATION` → `BREAK_ACCEPTED`
- Reclaim/failed-break with ≤15m → `RECLAIM_FAST`, else `RECLAIM_SLOW`
- `UNRESOLVED_WITHIN_MAX_WINDOW` → `HOLD_NO_BREAK`
- `EVENT_DATA_INVALID` / unmapped → `EXCLUDED`

## 3. Strongest features (BREAK_ACCEPTED vs RECLAIM_FAST)

| feature | timepoint | AUC | orientation | median_A | median_B |
|---|---|---:|---|---:|---:|
| `signed_distance_beyond_bps` | `PRE_TOUCH_5M` | 0.8666666666666667 | higher→RECLAIM_FAST | -17.363513381244083 | -6.209684637749707 |
| `ask_depth_bps_0_5` | `PRE_TOUCH_30S` | 0.832258064516129 | higher→RECLAIM_FAST | 0.0 | 281082.08822000003 |
| `support_depth_change_10s` | `BREAK_PLUS_20S` | 0.8225806451612904 | higher→RECLAIM_FAST | -35652.36890000105 | 35631.30426550015 |
| `support_pull_10s` | `BREAK_PLUS_20S` | 0.8064516129032258 | higher→RECLAIM_FAST | -35652.36890000105 | 0.0 |
| `support_refill_10s` | `BREAK_PLUS_20S` | 0.8048387096774193 | higher→RECLAIM_FAST | 0.0 | 35631.30426550015 |

## 4. EARLIEST_USEFUL_TIME (top features)

- `signed_distance_beyond_bps` → **PRE_TOUCH_5M** (auc≈0.8666666666666667)
- `ask_depth_bps_0_5` → **PRE_TOUCH_2M** (auc≈0.72)
- `support_depth_change_10s` → **BREAK_PLUS_20S** (auc≈0.8225806451612904)
- `support_pull_10s` → **BREAK_PLUS_20S** (auc≈0.8064516129032258)
- `support_refill_10s` → **BREAK_PLUS_20S** (auc≈0.8048387096774193)

## 5. BREAK_ACCEPTED vs RECLAIM_FAST — core

Best single-feature separation: `signed_distance_beyond_bps` at `PRE_TOUCH_5M` (AUC=0.8666666666666667).

## 6. Symbol / direction strata (top feature)

| stratum | AUC vs RECLAIM_FAST | AUC vs RECLAIM/HOLD | n_break | n_rf |
|---|---:|---:|---:|---:|
| all | 0.8666666666666667 | 0.7968253968253968 | 30 | 10 |
| symbol=APTUSDT | 0.9464285714285714 | 0.9285714285714286 | 14 | 4 |
| symbol=DOGEUSDT | 0.8229166666666666 | 0.71875 | 16 | 6 |
| break_direction=bearish | 0.9736842105263158 | 0.9052631578947369 | 19 | 4 |
| break_direction=bullish | 0.6818181818181818 | 0.6363636363636364 | 11 | 6 |

## 7. Deep-dive events

- `ev_1h_protected_low_0.6052_20260726T050000` | APTUSDT | bearish | BREAK_ACCEPTED | level=0.6052
- `ev_1h_protected_low_0.6008_20260725T170000` | APTUSDT | bearish | BREAK_ACCEPTED | level=0.6008
- `APTUSDT_PL_20260726T115000_0p6298` | APTUSDT | bearish | BREAK_ACCEPTED | level=0.6298
- `APTUSDT_PL_20260802T035500_0p5613` | APTUSDT | bearish | RECLAIM_FAST | level=0.5613
- `DOGEUSDT_PL_20260727T055000_0p07273` | DOGEUSDT | bearish | RECLAIM_FAST | level=0.07273
- `DOGEUSDT_PL_20260727T081500_0p07264` | DOGEUSDT | bearish | RECLAIM_FAST | level=0.07264
- `APTUSDT_PL_20260731T120500_0p561` | APTUSDT | bearish | RECLAIM_SLOW | level=0.561
- `DOGEUSDT_PL_20260730T133500_0p06995` | DOGEUSDT | bearish | HOLD_NO_BREAK | level=0.06995

See `deep_dive_timelines.csv` for compact relative timelines.

## 8. Primary questions

### Q1 — Saubere OB+Trade-Coverage?

Von 54 Events: VALID=53, WARNING=0, INVALID=1. Ausreichend für deskriptive Analyse.

### Q2 — Welche Features unterscheiden BREAK_ACCEPTED vs RECLAIM/HOLD?

Sichtbar (deskriptiv): `signed_distance_beyond_bps`@PRE_TOUCH_5M, `ask_depth_bps_0_5`@PRE_TOUCH_30S, `support_depth_change_10s`@BREAK_PLUS_20S, `support_pull_10s`@BREAK_PLUS_20S, `support_refill_10s`@BREAK_PLUS_20S.

### Q3 — Was ist erst nach Bestätigung sichtbar (zu spät)?

Spät / post-confirmation: `support_depth_change_10s`→BREAK_PLUS_20S, `support_pull_10s`→BREAK_PLUS_20S, `support_refill_10s`→BREAK_PLUS_20S, `flow_30s_n_trades`→BREAK_PLUS_20S

### Q4 — Kausales Signal früh genug für Hedge-Bot Block/Freigabe?

Primary decision `OB_FLOW_SIGNAL_SYMBOL_OR_DIRECTION_DEPENDENT` beantwortet das vorläufig. Noch keine Thresholds / Live-Logik — nur Evidence für einen späteren Gate-Design-Schritt.

## 9. Einschränkungen

- Kleine Stichprobe; Symbol/Richtung unbalanciert möglich
- C3 `break_available_at` ist Scanner-Close; Trade-through wurde zusätzlich abgeleitet
- Wall pull vs trade-consumption nur als Proxy (depth Δ vs aggressor flow)
- Kein ML; AUC ist univariater Rank-Score
- RECLAIM_FAST-Schwelle 15m ist a-priori, nicht optimiert

Artifacts: `results/ch_break_reclaim_microstructure_audit_20260808`

