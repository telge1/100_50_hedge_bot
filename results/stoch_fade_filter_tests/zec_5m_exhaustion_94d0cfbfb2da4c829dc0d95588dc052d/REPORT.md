# ZEC 5m exhaustion entry-block test

Final label: **ZEC_5M_EXHAUSTION_FILTER_INCONCLUSIVE**

Rule: `BLOCK_5M_EXHAUSTED_IN_TRADE_DIRECTION`  
Feature (frozen): last fully closed 5m StochRSI %K; LONG if K>80; SHORT if K<20; missing K → not blocked.  
D and phase-turning are **not** part of this flag. Copied into `rule_manifest.json` from the ZEC context analysis.  
Outcomes unchanged: original TP/SL, NO_BE50, SL_FIRST, no max-hold, full 1m scan.  
No ClickHouse writes. No strategy change. Validation/Test on ZEC are `EXPLORATORY_NOT_PRISTINE_OOS`.

## 1. Wie viele Trades blockiert die Regel?

ZEC: **584** of 1158 (50.43%).  
Kept: **574**.

## 2. Wie viele Losses und Wins werden blockiert?

Blocked wins: **246**. Blocked losses: **338**. Blocked open: **0**.  
Loss/Win ratio among blocked: 1.374.

## 3. Verbessert sich Net Sum und Net PF?

| | BASELINE | KEPT (filter) |
|---|---:|---:|
| Gross sum (pp) | -16.500 | 20.000 |
| Gross PF | 0.979 | 1.058 |
| Fees (pp) | 127.160 | 62.920 |
| Net sum (pp) | -143.660 | -42.920 |
| Net PF | 0.833 | 0.887 |
| Winrate | 45.59% | 49.13% |
| Net mean | -0.124 | -0.075 |
| Net median | -1.110 | -1.110 |
| Longest loss streak | 12 | 9 |

Missed net profit: 377.940 pp. Avoided net loss: 478.680 pp.  
Net sum removed by blocking: -100.740 pp (negative means the blocked set was a net loser).

## 4. Bleiben genügend Trades?

Kept closed: 572 of 1156 closed. Open remaining: 2.

## 5. Hält die Wirkung zeitlich?

ZEC splits are Development / Temporal Validation / Temporal Test, all `EXPLORATORY_NOT_PRISTINE_OOS` because the 5m hypothesis was seen on the full ZEC population.

- Development (EXPLORATORY_NOT_PRISTINE_OOS): n=694 blockrate=49.42% winrate 44.96% → 51.00% net_sum -102.340 → -10.110 (Δ 92.230) net_pf 0.804 → 0.955 blocked W/L 133/210
- Temporal Validation (EXPLORATORY_NOT_PRISTINE_OOS): n=231 blockrate=52.38% winrate 48.48% → 46.36% net_sum -8.910 → -15.100 (Δ -6.190) net_pf 0.945 → 0.803 blocked W/L 61/60
- Temporal Test (EXPLORATORY_NOT_PRISTINE_OOS): n=233 blockrate=51.50% winrate 44.59% → 45.95% net_sum -32.410 → -17.710 (Δ 14.700) net_pf 0.813 → 0.767 blocked W/L 52/68

## 6. Hält die Wirkung auf anderen Coins?

Same frozen rule. No coin dropped.

- SOLUSDT: n=625 block=46.56% blocked W/L 137/152 winrate 45.82%→44.44% net_sum -82.420→-71.130 net_pf 0.822→0.707
- HYPEUSDT: n=990 block=46.97% blocked W/L 197/267 winrate 41.90%→41.41% net_sum -253.680→-155.140 net_pf 0.678→0.613
- XAUTUSDT: n=228 block=49.56% blocked W/L 54/56 winrate 49.55%→50.00% net_sum -10.140→-6.540 net_pf 0.935→0.907
- LINKUSDT: n=650 block=45.38% blocked W/L 140/153 winrate 47.38%→47.04% net_sum -42.780→-42.550 net_pf 0.908→0.826
- DOGEUSDT: n=611 block=45.34% blocked W/L 132/142 winrate 47.12%→46.25% net_sum -51.270→-48.130 net_pf 0.882→0.791
- BNBUSDT: n=375 block=46.40% blocked W/L 74/99 winrate 49.20%→54.73% net_sum -25.140→4.390 net_pf 0.905→1.036
- SUIUSDT: n=808 block=42.95% blocked W/L 168/178 winrate 45.96%→44.01% net_sum -101.050→-87.990 net_pf 0.829→0.738
- ADAUSDT: n=740 block=49.46% blocked W/L 165/201 winrate 44.23%→43.40% net_sum -131.070→-89.810 net_pf 0.769→0.672
- AVAXUSDT: n=717 block=50.21% blocked W/L 186/172 winrate 50.14%→48.31% net_sum 6.960→-23.160 net_pf 1.014→0.904
- ALL_EXCLUDING_ZEC: n=5744 block=46.80% blocked W/L 1253/1420 winrate 46.18%→45.57% net_sum -690.590→-520.060 net_pf 0.836→0.759

## 7. Was passiert durchschnittlich nach 4h?

Market path (outcome not rewritten). Share still in trade-direction / median aligned return / still open:

- BASELINE: in-dir=52.03%, median=0.089 pp, still-open=17.63%, TP-touch=38.03%, SL-touch=44.34%, n_ok=1157 unavailable=1
- KEPT: in-dir=54.45%, median=0.137 pp, still-open=13.79%
- BLOCKED: in-dir=49.66%, median=-0.005 pp, still-open=21.40%

Median market MFE/MAE BASELINE: 1.367 / 1.317 pp.

## 8. Was passiert durchschnittlich nach 6h?

- BASELINE: in-dir=51.60%, median=0.068 pp, still-open=10.29%, TP-touch=41.23%, SL-touch=48.40%, n_ok=1157 unavailable=1
- KEPT: in-dir=53.58%, median=0.237 pp
- BLOCKED: in-dir=49.66%, median=-0.010 pp

Median market MFE/MAE BASELINE: 1.664 / 1.640 pp.

## 9. Wie viele frühe SLs erholen sich später?

Recovery = SL exit at or before the horizon, then aligned market return from entry at that horizon is > 0 (would have been in profit if not stopped). Outcome stays SL.

- 4h: SL-before=513, then aligned=139 (share 27.10%)
- 6h: SL-before=560, then aligned=180 (share 32.14%)
- WIN with MAE then TP-touch by 4h (approx): 433
- LOSS with MFE then SL by 4h (approx): 621

## 10. Werden schnelle Gewinner überproportional blockiert?

Blocked share among all ZEC wins: 46.68%.  
Among wins with hold ≤ 15m: 33.77% (n=77).  
Median hold blocked wins: 4830.0 s vs kept wins 3060.0 s.

## 11. Werden die beiden untersuchten ZEC-Losses blockiert?

### 2026-08-16T05:31:00Z SHORT `8c914b1f-c154-58e6-a8ec-5f8014234267`
- Decision: **BLOCKED** (exhausted=True)
- 5m K=18.76612708533856 D=43.38218646928329 bar 2026-08-16T05:25:00Z → 2026-08-16T05:30:00Z
- 1m: phase=BULL_MOMENTUM K=59.164142713241176 exhausted=False
- Outcome unchanged: LOSS hold_s=30780 gross=-0.9999999999999996 net=-1.1099999999999997
- Price 1h/2h/4h/6h/12h: 485.7 / 487.23 / 489.26 / 486.18 / 493.46
- Aligned 4h/6h: -0.4681917121852997 / 0.16427779374923226
- In-trade MFE/MAE 4h: 0.6735389543718489 / 0.47435212945090194; TP/SL touch False/False
- Post-exit 4h aligned from entry: nan

### 2026-08-16T09:46:00Z SHORT `188fabf8-ddcd-5bed-96c4-586e7cce26f4`
- Decision: **BLOCKED** (exhausted=True)
- 5m K=12.413350897543664 D=45.38503376998789 bar 2026-08-16T09:40:00Z → 2026-08-16T09:45:00Z
- 1m: phase=OVERSOLD_TURNING_UP K=14.151765868585658 exhausted=True
- Outcome unchanged: LOSS hold_s=15480 gross=-1.0000000000000044 net=-1.1100000000000045
- Price 1h/2h/4h/6h/12h: 485.59 / 486.04 / 486.9 / 493.45 / 485.83
- Aligned 4h/6h: -0.004107788366739609 / -1.3494084784751876
- In-trade MFE/MAE 4h: 0.540174170226749 / 0.2526289845547195; TP/SL touch False/False
- Post-exit 4h aligned from entry: nan


## 12. Ist die Regel stabil genug für einen größeren Backtest?

Label **ZEC_5M_EXHAUSTION_FILTER_INCONCLUSIVE**. A larger backtest is justified only if ZEC net sum and net PF both improve, enough trades remain, temporal deltas do not flip sign, and the external-coin basket (excluding ZEC) also improves. Otherwise keep the rule frozen for diagnosis only.

## Quality

- Lookahead: 0
- Stored vs recomputed ZEC flag mismatches: 0
- Stored vs formula(K) mismatches: 0
- Baseline gross sum check: True
- Tests passed: True
- ClickHouse writes: 0
