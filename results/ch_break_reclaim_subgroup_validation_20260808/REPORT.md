# CH Break/Reclaim Subgroup Validation

**Primary Decision:** `EARLY_SIGNAL_NOT_ROBUST_AFTER_DISTANCE_CONTROL`

Research only. No live gate. No productive thresholds.

## Verdict

Die zuvor auffälligen Early-AUCs (besonders bearish, imbalance/depth) sind **überwiegend Price-Proximity / Distance-getrieben**.  
Orderbook-only Scores bleiben informativ, schlagen Distance-only in den entscheidenden bearish Gruppen aber **nicht**.

Damit ist **kein EARLY_GATE_CANDIDATE** für einen Hedge-Bot Permission-Gate gerechtfertigt.

## Subgroup counts (DATA_VALID, EXCLUDED dropped; n=52)

| subgroup | n | BREAK | RECLAIM_FAST | RECLAIM_SLOW | HOLD | ok vs RF | ok vs rest |
|---|---:|---:|---:|---:|---:|---:|---:|
| APT_bearish | 12 | 10 | 1 | 1 | 0 | 0 | 0 |
| APT_bullish | 7 | 4 | 3 | 0 | 0 | 1 | 1 |
| DOGE_bearish | 17 | 9 | 3 | 1 | 4 | 1 | 1 |
| DOGE_bullish | 16 | 8 | 3 | 0 | 5 | 1 | 1 |
| all_bearish | 29 | 19 | 4 | 2 | 4 | 1 | 1 |
| all_bullish | 23 | 12 | 6 | 0 | 5 | 1 | 1 |

## Per-subgroup classification

| subgroup | class | reason |
|---|---|---|
| APT_bearish | `INSUFFICIENT_SAMPLE` | RECLAIM_FAST n=1 / rest n=2 |
| APT_bullish | `WEAK_SIGNAL` | Jackknife unstable |
| DOGE_bearish | `WEAK_SIGNAL` | OB does not beat distance-only |
| DOGE_bullish | `WEAK_SIGNAL` | Jackknife unstable |
| all_bearish | `WEAK_SIGNAL` | OB does not beat distance-only |
| all_bullish | `WEAK_SIGNAL` | AUC CI too wide |

## Distance baseline — all_bearish @ PRE_TOUCH_10S (vs RECLAIM_FAST)

| model | AUC | CI | n_break/n_other |
|---|---:|---|---|
| distance_only | **1.00** | [1.00, 1.00] | 19/4 |
| signed_distance univariate | 0.95 | [0.84, 1.00] | 19/4 |
| ob_only (depth+imb+flow rank) | 0.86 | [0.64, 0.99] | 19/4 |
| ob_plus_distance | 0.96 | [0.88, 1.00] | 19/4 |

Interpretation: OB liefert Zusatzstruktur, aber **Distance allein reicht** für die Trennung in dieser Stichprobe. Ein Gate auf Imbalance/Depth ohne Distance-Kontrolle würde Proximity messen.

## Early scorecard (depth pressure + support_frac + signed flow)

DOGE_bearish @ PRE_TOUCH_10S vs RECLAIM_FAST:

- AUC ≈ **0.93**, CI ≈ [0.70, 1.00], n=9/3
- Jackknife max_drop ≈ 0.04 (stabil innerhalb DOGE)
- Transfer DOGE→APT deskriptiv konsistent (APT train insufficient for reverse)

Trotzdem: Distance-only auf derselben Gruppe ≈ **1.0** → kein OB-Mehrwert-Nachweis für ein Early Gate.

## Symbol transfer

- APT→DOGE bearish: **INSUFFICIENT_TRAIN** (APT RECLAIM_FAST n=1)
- DOGE→APT bearish scorecard: Richtung konsistent, aber APT-Seite hat zu wenig RECLAIM_FAST für belastbaren Transfer-Test

Eher direction-skizziert (bearish) als sauber symbol-übertragbar — nach Distance-Kontrolle ohnehin nicht gate-tauglich.

## Jackknife

Hohe univariate AUCs (bis 1.0) auf DOGE_bearish bei n_other=3 sind vorsichtig zu lesen; bullish Teilgruppen mit LOO-Drops ~0.15–0.17.

## Practical hedge-bot reading

| Frage | Antwort |
|---|---|
| EARLY_GATE_CANDIDATE irgendwo? | **Nein** |
| Bestes frühes Fenster (deskriptiv) | PRE_TOUCH_10–30S / FIRST_TOUCH |
| Confirmation-only Features | Support pull/refill ≥ +20s bleibt später |
| Nächster Research-Schritt | Mehr RECLAIM_FAST-Events **oder** distance-conditioned OB residuals |

Artifacts: `results/ch_break_reclaim_subgroup_validation_20260808/`
