# Trend Direction Move Measurement Audit

## Primärentscheidung

**SMALL_MFE_VALUES_ARE_CAUSED_BY_EPISODE_FRAGMENTATION**

## Code-Path Inventory

Siehe `code_path_inventory.json`.

- Prozent: **Percent points** nach genau einem `* 100` (0.27 = 0.27%).
- Entry: **next_open** nach Confirm-Close; Signalcandle nicht in Forward-High/Low.
- Prior 0.27%/0.58%: **fixe Horizonte**, nicht Raw-Episode; Interpretation korrekt als kleine Moves.

## Signal-Deduplizierung

| Definition | count |
|---|---:|
| ALL_TRANSITIONS_TO_DIRECTION | 16782 |
| CLUSTER_START_ONLY (15m bridge) | 7364 |
| MAJOR_FLIPS_ONLY | 3696 |

Same-direction Resume nach UNCLEAR: 13086 / 16783 (0.7797175713519633)
Opposite after UNCLEAR: 3693 (0.2200440922361914)
UNCLEAR Dauer Anteile ≤15m/30m/60m: 0.7176905201692189 / 0.9062146219388667 / 0.9877852588929273

## Median-MFE nach Definition

| Mode | median duration | median MFE | target_first 0.25% | 0.50% | 1.00% |
|---|---:|---:|---:|---:|---:|
| RAW_EPISODE | 35.0 | 0.3091190108191699 | 0.37593850554165176 | 0.26868072935287807 | 0.14158026456918127 |
| CLUSTER_15M | 95.0 | 0.4292462517970774 | 0.4608509116910976 | 0.38487665355738293 | 0.24699082350137053 |
| CLUSTER_30M | 150.0 | 0.5123856829565288 | 0.47372184483375046 | 0.4217018233821952 | 0.30204981527827435 |
| UNTIL_OPPOSITE | 235.0 | 0.643518600838372 | 0.47705875342629006 | 0.4412465737099273 | 0.3525801453938744 |

## Feste Horizonte (ALL transitions, RAW frame — horizons independent of episode)

| H | median MFE | p75 | p90 | median MAE |
|---|---:|---:|---:|---:|
| 60m | 0.2745746841199326 | 0.5664077312019927 | 1.0063761799455084 | 0.2705850031555712 |
| 240m | 0.5793313804064837 | 1.1739609909458966 | 2.0576236421965786 | 0.5802403056627603 |
| 480m | 0.8494891672620963 | 1.7460363707521047 | 2.923482314209461 | 0.8440853732063158 |

## Manuelle APT-Fälle

Siehe `manual_case_recalculations.csv` und `apt_20260411_reconstruction.csv`.

## Invarianten

Verletzungen: 0 (siehe `invariant_violations.csv`)

## Antworten

1. Prozentrechnung korrekt? **Ja** (manuell 0.9→0.873 = 3.00%).
2. next_open korrekt? **Ja**.
3. UNCLEAR schneidet Raw-Episode früh ab? **Ja** — Cluster/Until-Opposite erhöhen Dauer und MFE.
4. Wie viele der ~16.8k nur Wiederaufnahmen? Cluster-Starts=7364 → Differenz zu ALL = Wiederaufnahmen/Fragment-Starts.
5–8. Siehe Tabellen oben.
9. 0.25/0.50/1.00 unter RAW vs CLUSTER/OPP: siehe Tabelle.
10. Dedup ändert 1%-Bewertung: siehe `threshold_comparison.csv` (CLUSTER_START / MAJOR_FLIPS).
11. Fünf APT-Fälle: manuelle CSV mit Formel.
12. 0.27/0.58: korrekte Fixed-Horizon-Percent-Points; klein, weil typische Post-Signal-Move klein **und** viele kurze Resume-Signale.
13. Scanner als Richtungsfilter: weiterhin weiche Bias, nicht 1%-Entry.
14. **Keine Scannerregel verändert.**

Runtime: 909.0s
