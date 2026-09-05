# Combined causal entry-warning filter research

Final label: **COMBINED_ENTRY_WARNING_FILTER_INCONCLUSIVE**

Frozen strategy `wave_fade_frozen_f16ae32_causal_entry_v1` unchanged.  
Evaluation `94d0cfbfb2da4c829dc0d95588dc052d`. Source job `f5909d14cba34fc9973a8b431530752d`.  
Original entries, TP, SL and outcomes unchanged. `NO_BE50`, `SL_FIRST`, no max-hold, full 1m scan.  
Fee 0.11 pp per closed kept trade.  
ZEC splits and all filter conclusions: `EXPLORATORY_NOT_PRISTINE_OOS`.  
No ClickHouse writes, no live actions, no commit, no push.  
No parameter search. Flags and R0–R9 frozen before looking at Validation/Test.

Output: `results/stoch_fade_filter_tests/combined_entry_warnings_94d0cfbfb2da4c829dc0d95588dc052d_v1/`

## Population reconciliation

ZEC: 1158 trades, 527 WIN, 629 LOSS, 2 OPEN, baseline gross **−16.5 pp**. HARD-FAIL checks passed.

Splits from stored `zec_trade_context.parquet`: development 694, validation 231, test 233.

W1 true on ZEC: **584**, identical to the previous 5m exhaustion test.  
W4 any-open on ZEC: **314**, identical to the prior context-analysis overlap count.

Other coins in the same evaluation (no trades dropped):

| Coin | n | WIN | LOSS | OPEN | Gross sum pp |
|---|---:|---:|---:|---:|---:|
| ZECUSDT | 1158 | 527 | 629 | 2 | −16.5 |
| SOLUSDT | 625 | 285 | 337 | 3 | −14.0 |
| HYPEUSDT | 990 | 414 | 574 | 2 | −145.0 |
| XAUTUSDT | 228 | 111 | 113 | 4 | +14.5 |
| LINKUSDT | 650 | 307 | 341 | 2 | +28.5 |
| DOGEUSDT | 611 | 286 | 321 | 4 | +15.5 |
| BNBUSDT | 375 | 184 | 190 | 1 | +16.0 |
| SUIUSDT | 808 | 370 | 435 | 3 | −12.5 |
| ADAUSDT | 740 | 326 | 411 | 3 | −50.0 |
| AVAXUSDT | 717 | 358 | 356 | 3 | +85.5 |

## Frozen flags

- **W1** `w1_5m_exhausted_in_trade_direction`: last closed 5m %K; LONG K>80, SHORT K<20; missing K → False. Not retuned.
- **W2** `w2_1m_turning_against_trade`: last closed 1m, OR of bearish/bullish K/D-cross against the trade, K−D spread moving against the trade, or phase `OVERBOUGHT_TURNING_DOWN` / `OVERSOLD_TURNING_UP`.
- **W3** `w3_pre_entry_tp_progress_ge_25pct`: direction-correct progress from `wave_end_price` to TP; flag if ≥ 0.25. Threshold not optimized.
- **W4** `w4_symbol_trade_already_open`: `previous_entry < current_entry < previous_exit`; OPEN treated as open until data end.

Warning score = count of known True. MISSING is not coerced to 0. In this evaluation every W1–W4 value was known (0 missing).

## 1. Wie verteilen sich Score 0–4?

ZEC:

- Score 0: 124 trades, 54/70/0, loss-rate 56.45%, net −16.64 pp
- Score 1: 339 trades, 158/181/0, loss-rate 53.39%, net −49.79 pp
- Score 2: 444 trades, 204/238/2, loss-rate 53.85%, net −48.62 pp
- Score 3: 213 trades, 95/118/0, loss-rate 55.40%, net −20.93 pp
- Score 4: 38 trades, 16/22/0, loss-rate 57.89%, net −7.68 pp

Most mass is at score 1–2. Score ≥2 is 695/1158 (60.0%).

ZEC flag rates: W1 584/1158, W2 472/1158, W3 648/1158, W4 314/1158.  
W2 subconditions: cross-against 158, spread-against 443, phase-against 169.

W3 diagnostic buckets (not used for tuning): ≤0 136; (0,25%) 374; [25,50%) 499; [50,75%) 128; [75,100%] 5; >100% 16.

## 2. Steigt die Loss-Rate mit dem Warning Score?

Nein, nicht monoton. Score 0 is *worse* than 1–3 (56.45% vs 53.4–55.4%). Score 4 is only slightly worse (57.89%) on a small n=38. A combined score is therefore not a clean severity ranking of bad entries.

## 3. Welche feste Regel verbessert ZEC Development?

**R1** (block W1 only) is the strongest Development rule by net sum:

- block 49.42% (343/694)
- winrate 44.96% → 51.00%
- net −102.34 → −10.11 pp (Δ **+92.23**)
- net PF 0.804 → 0.955

R5 (W1 AND W3) is second: Δ +79.46 pp, PF 0.804 → 0.928, block 34.0%.  
R2 (score ≥ 2) is third: Δ +82.76 pp, but PF only 0.804 → 0.898 and it removes 60% of Development.

## 4. Bleibt sie in ZEC Validation und Test stabil?

Nein. R1 Validation **kippt**:

- Validation: winrate 48.48% → 46.36%, net Δ **−6.19**, PF 0.945 → 0.803
- Test: net Δ +14.70, but PF 0.813 → 0.767 (worse)

R5 also kipps in Validation (net Δ −14.43, PF 0.945 → 0.778). Test net/PF improve (Δ +16.88, PF 0.813 → 0.877), but that does not repair the Validation flip.

R2 Validation is worse still (net Δ −18.32, PF 0.945 → 0.627).

Every R1–R9 Validation net-sum delta is ≤ 0. Combined warnings do not stabilize the already-inconclusive 5m result.

## 5. Verbessert sie die übrigen Coins gemeinsam?

For R1, ALL_EXCLUDING_ZEC net sum improves (−690.59 → −520.06, Δ **+170.53**), but **net PF falls** (0.836 → 0.759). Same pattern as the standalone 5m test.

R5: basket net Δ +82.74, PF 0.836 → 0.821 (still worse).  
R2: basket net Δ +296.66, PF 0.836 → 0.787 (worse, removes 55% of external trades).  
R8 (W2 AND W3) is the only rule whose external basket PF edges up (0.836 → 0.840) with net Δ +93.86, but on ZEC itself net PF *falls* (0.833 → 0.829) and Validation still kipps.

PROMISING requires both net sum **and** net PF up on ZEC *and* the external basket. No frozen rule meets that.

## 6. Wie viele Coins werden besser bzw. schlechter?

External coins (9), net-sum delta:

| Rule | better | worse | basket PF |
|---|---:|---:|---|
| R1 | 8 | 1 (AVAX) | 0.836 → 0.759 |
| R2 | 7 | 2 | 0.836 → 0.787 |
| R5 | 7 | 2 | 0.836 → 0.821 |
| R8 | 7 | 2 | 0.836 → 0.840 |

AVAX is repeatedly the loser under R1/R5 (R1 Δ −30.12 pp). HYPE contributes a large share of the basket net-sum gain (R1 Δ +98.54) while its PF also falls (0.678 → 0.613). The net-sum improvement is fee-driven (blocked trades stop paying 0.11 pp) more than edge-driven.

## 7. Wie viele WINs und LOSSes blockiert jede Regel?

ZEC:

| Rule | blocked | WIN | LOSS | OPEN | kept | net Δ pp | net PF after |
|---|---:|---:|---:|---:|---:|---:|---:|
| R0 | 0 | 0 | 0 | 0 | 1158 | 0.00 | 0.833 |
| R1 | 584 | 246 | 338 | 0 | 574 | +100.74 | 0.887 |
| R2 | 695 | 315 | 378 | 2 | 463 | +77.23 | 0.801 |
| R3 | 251 | 111 | 140 | 0 | 907 | +28.61 | 0.827 |
| R4 | 238 | 109 | 129 | 0 | 920 | +14.18 | 0.810 |
| R5 | 381 | 158 | 223 | 0 | 777 | +81.91 | 0.887 |
| R6 | 145 | 63 | 82 | 0 | 1013 | +20.95 | 0.836 |
| R7 | 71 | 33 | 38 | 0 | 1087 | +2.31 | 0.824 |
| R8 | 247 | 116 | 131 | 0 | 911 | +25.67 | 0.829 |
| R9 | 144 | 64 | 80 | 0 | 1014 | +21.34 | 0.836 |

R1/R5 improve ZEC net PF. R2 removes both OPEN trades and *lowers* ZEC net PF. Combined AND-rules remove fewer trades but also less net loss.

## 8. Welche Regel verbessert Net Sum und Net PF am stärksten?

On ZEC overall: **R1**, then **R5**. Both take net PF from 0.833 to 0.887. R1 has the larger net-sum Δ (+100.74 vs +81.91) because it blocks more trades (50.4% vs 32.9%).

R1 is the previously tested 5m filter. Combining W1 with W2/W3/W4 does not beat it on ZEC net sum.

## 9. Ist diese Verbesserung breit oder coin-/richtungsabhängig?

Nicht breit genug für PROMISING.

- External PF falls for R1/R5.
- AVAX moves the other way.
- All-coin R1 net Δ is LONG-heavy (LONG +227 pp, SHORT +44 pp).
- Signal-TF: R1 helps 15m/30m/1h net sum but 4h is slightly worse (−1.05). 30m and 1h PF fall. 15m PF is flat.
- Time: first half Δ +228, second half only +43. August 2026 R1 net Δ **−29.04** (PF 1.023 → 0.841). The latest month contradicts Development.

ZEC itself is more balanced (LONG R1 Δ +50.6, SHORT Δ +50.2), so the directional skew is an external-coin effect.

## 10. Wie verhalten sich BLOCKED und KEPT nach 4h und 6h?

Market path after entry; original outcomes never rewritten. A prior SL remains LOSS.

Score 0 vs score ≥2 (ZEC, in-direction share / median aligned pp):

- 4h: score 0 54.84% / +0.280 vs score ≥2 51.15% / +0.048
- 6h: score 0 58.87% / +0.356 vs score ≥2 51.44% / +0.071

R1 BLOCKED vs KEPT is clearer than R2:

- 4h in-dir: blocked 49.66% vs kept 54.45%; median −0.005 vs +0.137
- 6h in-dir: blocked 49.66% vs kept 53.58%; median −0.010 vs +0.237

R2 BLOCKED vs KEPT at 4h/6h is only a small gap (51.15% vs 53.35% at 4h; 51.44% vs 51.84% at 6h). Score ≥2 is a weak path separator. W1 alone still shows the modest 4h/6h underperformance already seen in the 5m test.

## 11. Bleibt der Unterschied nach 12h und 24h bestehen?

Partly for R1, not usefully for R2.

- Score 0 vs ≥2 at 12h: 53.23% vs 50.14%; at 24h: 54.03% vs 47.40% (gap remains, medians both negative at 24h).
- R1 blocked vs kept at 12h: 46.75% vs 50.00%; at 24h: 44.50% vs 49.13%. Blocked stay worse.
- R2 blocked vs kept **reverses** at 12h/24h (blocked 50.14% / 47.40% vs kept 45.67% / 45.89%). Score ≥2 is not a persistent path defect.

24h is unavailable for the last-day August trades (candle pin `2026-08-17T00:00Z`); those rows are `HORIZON_UNAVAILABLE`, not zero-filled.

## 12. Sind es verspätete Entries oder grundsätzlich falsche Trade-Ideen?

Mixed, closer to **late or noisy entries than a cleanly false idea**.

- Score ≥2 and R1-blocked paths are only a few points less often in trade direction at 4h/6h; many still go the right way.
- 27–36% of SLs hit before 4h/6h/12h/24h later show a positive aligned return. The original LOSS is kept. That is late adverse then recovery, i.e. timing/path, not a rewritten outcome.
- Score 0 is not a safe cohort (highest loss-rate among 0–3). Warnings do not isolate a distinct “wrong idea” cluster.

## 13. Werden schnelle Gewinner versehentlich blockiert?

Ja, in proportion to the block rate, not far above it.

ZEC wins ≤15m: n=77.

- R1 blocks 33.8% of those fast wins vs 46.7% of all wins (R1 is *less* aggressive on fast winners).
- R2 blocks 58.4% of fast wins vs 59.8% of all wins (almost proportional).
- R8 is the exception: 32.5% of fast wins vs 22.0% of all wins — it over-removes quick TPs.

R1/R2 do not specifically hunt fast winners; they also do not spare them.

## 14. Werden lange Verlusttrades gezielt reduziert?

Nur schwach.

ZEC losses ≥4h: n=116.

- R1 blocks 60.3% of long losses vs 53.7% of all losses.
- R2 blocks 63.8% vs 60.1%.
- Median hold of R1-blocked losses is 4200s vs 3240s kept — slightly longer, not a clean “slow bleed” filter.

AND-rules (R6/R8) do not concentrate on long losses.

## 15. Was zeigen die beiden August-ZEC-Fälle?

Both are 15m SHORT, original LOSS at the same SL time 2026-08-16 14:04Z. Last closed bars match the causal recipe (1m just closed, 5m 5 minutes earlier, 4h 04:00–08:00 for the 09:46 trade).

**05:31Z** `8c914b1f-…` score **2**: W1 true (5m K=18.8, OVERSOLD), W2 false (1m BULL_MOMENTUM, K=59.2), W3 false (progress 18.7% < 25%), W4 true (1 open ZEC trade). Blocks R1 and R2 only. 4h aligned −0.47 pp, 6h +0.16, 12h −1.33; 24h unavailable. A late short into 5m exhaustion plus an already-open trade; 1m was *not* turning against.

**09:46Z** `188fabf8-…` score **4**: W1 true (5m K=12.4), W2 true (spread + phase `OVERSOLD_TURNING_UP`), W3 true (progress 31.5%), W4 true (2 open). Every R1–R9 would block it. 4h aligned ~0, 6h −1.35, 12h +0.22; 24h unavailable. This is the textbook stacked-warning late entry; the later 12h bounce does not change the original SL.

## 16. Gibt es Lookahead-, Missingness- oder Datenqualitätsprobleme?

No lookahead. All ZEC snapshots have `available_at <= entry_time` on 1m/5m/15m/30m/1h/4h. Incomplete HTF buckets are only the still-open pin bar, discarded by Gold aggregation. ClickHouse: SELECT-only, writes 0.

Missingness: **0** on W1–W4 for all 6902 trades. Warning scores are complete.

W1 count 584 matches the previous 5m test. Unit tests: 13 passed.

Notes, not hard fails:

- 37 ZEC trades have `|pre_entry_progress| > 1` (7 with `|x| > 5`) because wave-end and TP can be close; denom ≠ 0 so W3 is still computed, not MISSING.
- 24h (and some 12h) near the pin are `HORIZON_UNAVAILABLE`.
- Stored context parquet was used for splits; W1 identity used the frozen definition plus the 584 count because the previous 5m `trade_decisions.parquet` is gitignored/absent here. Same-direction/opposite W4 counts are not exclusive (some trades overlap both).

## 17. Soll irgendeine Regel größer getestet werden?

**Nein.** Label is `COMBINED_ENTRY_WARNING_FILTER_INCONCLUSIVE`.

PROMISING would need ZEC net sum and net PF up, Validation/Test not clearly worse, external basket net sum **and** PF up, several coins, no single side/coin carrying the effect, and no lookahead/missingness hole.

R1/R5 fail Validation and external PF. R2 is worse: it blocks 60%, lowers ZEC PF, and Validation collapses. R8’s tiny external PF uptick does not survive ZEC PF or Validation. Combining W2/W3/W4 with the already-inconclusive 5m flag does not produce a confirmed filter.

Do not activate live. Do not retune thresholds on Validation/Test. The frozen strategy stays as-is.

## 18. Bleibt die Frozen-Strategie unverändert?

Ja.

- Strategie unverändert: **ja**
- ClickHouse-Writes: **0**
- Live-Aktionen: **0**
- Commit: **nein**
- Push: **nein**
