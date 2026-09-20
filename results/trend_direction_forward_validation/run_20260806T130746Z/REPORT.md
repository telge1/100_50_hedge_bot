# Trend Direction Forward Validation

## Primärentscheidung

**DIRECTION_CORRECT_BUT_1PCT_OFTEN_TOO_LARGE**

target_first@1%=14% but stop_first only 1%; median MFE60m≈0.27% << 1%; at 0.25% target_first≈38% and fav60≈53%. Direction bias exists; 1% target is too ambitious for 5m episodes.

Scannerregeln: **keine verändert**.

---

## Scope

- Symbole: APTUSDT, DOGEUSDT, BTCUSDT (volle MySQL-Coverage)
- Signale: nur echte Direction-Transitions → BULLISH/BEARISH
- Entry: `next_open` nach Confirm-Close
- Episode: bis UNCLEAR/Gegenseite oder 240m
- Schwellen: 0.25% / 0.50% / 1.00% (Hauptauswertung 1%)
- Artefakte: `/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/results/trend_direction_forward_validation/run_20260806T130746Z`

## Gesamt (1%)

| Metrik | Wert |
|---|---:|
| signal_count | 16782 |
| evaluable_count | 16782 |
| target_first | 2351 (14.0%) |
| stop_first | 170 (1.0%) |
| ambiguous | 0 |
| no_target (+ after ended) | 14256 (after_ended=2722) |
| median minutes to +1% | 55.0 |
| median MFE/MAE 60m | 0.275% / 0.271% |
| p75/p90 MAE 60m | 0.553% / 0.971% |
| median episode duration | 40.0 m |
| median move consumed before confirm | 0.086% |
| large impulse share | 25.1% |
| target_first large vs normal | 15.3% vs 13.6% |

Favorable-Touch-Raten (1% irgendwann im Fenster, nicht zwingend first):

- 15m: 2.0%
- 30m: 4.8%
- 60m: 10.1%
- 120m: 18.7%
- 240m: 30.6%

## Schwellenvergleich

| thr | target_first | stop_first | fav@60m | median min to fav |
|---|---:|---:|---:|---:|
| 0.25% | 37.6% | 14.4% | 53.0% | 15.0 |
| 0.50% | 26.8% | 5.3% | 28.9% | 30.0 |
| 1.00% | 14.0% | 1.0% | 10.1% | 55.0 |

## Impulse-Kerzen

Große Impulskerzen (p75 body/range) sind **leicht besser**, nicht systematisch schlechter.
BEARISH auf großen roten Kerzen (APT): target_first ≈25.8% vs normal ≈22.9%.
BULLISH auf großen grünen Kerzen (APT): ≈25.8% vs ≈22.0%.
BTC insgesamt schwächer (niedrigere Volatilität relativ zu 1%).

## APT 2026-04-11 Detail

`apt_20260411_detail.csv` — 20 Direction-Signalstarts an diesem Tag.
Beispiel sticky-Fall: Confirm BEARISH 20:45 → oft `TARGET_ONLY_AFTER_DIRECTION_ENDED` / spätes Target;
BULLISH 18:45 → `NO_TARGET_WITHIN_240M` innerhalb der Episode.

## Runtime

- APTUSDT: bars=60973, signals=3832, runtime_s=79.8
- DOGEUSDT: bars=52572, signals=3136, runtime_s=61.1
- BTCUSDT: bars=156241, signals=9814, runtime_s=739.6

Gesamt: 880.6s

## Antworten

1. **16782** echte BULLISH/BEARISH-Starts (APT 3832, DOGE 3136, BTC 9814).
2. **+1% favorable first:** 2351 (14.0%).
3. **−1% adverse first:** 170 (1.0%).
4. **Same-candle ambiguous @1%:** 0 (bei 0.25%: 1.0%).
5. **0.25/0.50/1.00%:** target_first 37.6% / 26.8% / 14.0%; 1% ist klar zu streng relativ zur typischen 5m-MFE.
6. **Median bis +1%:** 55.0 Minuten (nur Treffer).
7. **MFE/MAE:** 15m ≈0.128/0.129%; 30m ≈0.189/0.186%; 60m ≈0.275/0.271%; 120m ≈0.402/0.391%; 240m ≈0.579/0.58% (MAE positiv adverse).
8. **BEARISH auf großen roten Kerzen:** nicht schlechter; eher leicht besser (siehe impulse_candle_comparison.csv).
9. **BULLISH auf großen grünen Kerzen:** analog leicht besser, kein Bounce-Sonderdesaster im Aggregat.
10. **Move seit UNCLEAR bis Confirm:** Median consumed ≈0.086% — meist klein; trotzdem oft späte Confirm relativ zum Impuls.
11. **Als Richtungsfilter:** ja als weiche Bias/Regime-Info; nein als alleiniger Edge für 1%-Targets.
12. **Als Entry-Trigger:** **nein** ohne zusätzliches Timing/OB/Pullback.
13. **UNCLEAR für OB:** sinnvoll als frühes Beobachtungsfenster vor Confirm; Confirm allein ist oft zu spät für 1%.
14. **Scannerregel verändert?** **Keine Scannerregel verändert.**

## Tests

45 passed (`test_trend_direction_forward_validation` + existing at/range).
