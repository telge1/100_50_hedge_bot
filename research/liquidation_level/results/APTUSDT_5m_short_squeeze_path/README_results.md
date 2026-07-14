# Short Squeeze Path / Excursion Audit — APTUSDT 5m

Geschätzte LuxAlgo-Style Upper-Levels — **keine** echten Exchange-Liquidationen.
Kein klassisches Stop-Loss-Fazit (Hedge-Bot hat keinen klassischen SL).

**Daten:** 2025-12-27 → 2026-06-27 · 52 569 Candles · IS-Cut Index 36 798  
**Events:** upper_50x = 4 147 · upper_25x = 3 487  
**Fokusgruppe unten:** immediate reclaim, Horizont 50 Candles (= 250 Minuten), sofern nicht anders gesagt.

---

## Wie weit steigt APT nach einem upper-50x-Sweep noch weiter?

Nach kausalem Short-Entry (Open der nächsten Candle nach bearish Reclaim):

| | Median | p75 | p90 | p95 |
|---|---:|---:|---:|---:|
| Weiterer Anstieg (adverse) | **1.26 %** | 2.35 % | 5.39 % | 9.27 % |

n = 2 106 (complete h=50).

## Wie weit steigt APT nach einem upper-25x-Sweep noch weiter?

| | Median | p75 | p90 | p95 |
|---|---:|---:|---:|---:|
| Weiterer Anstieg (adverse) | **1.34 %** | 3.26 % | 7.32 % | 9.58 % |

n = 1 747.

## Wie weit fällt APT anschließend maximal?

Vom Entry aus (favorable):

| | Median | p75 | p90 | p95 |
|---|---:|---:|---:|---:|
| 50x | **1.44 %** | 2.40 % | 3.55 % | 4.14 % |
| 25x | **1.60 %** | 2.64 % | 3.88 % | 4.29 % |

Anteil mit ≥0.50 % / ≥1 % / ≥2 % / ≥3 % Fall vom Entry (50x): ca. 82 % / 63 % / 34 % / 16 %.

## Ist der Fall nach dem zwischenzeitlichen Peak größer als vom Entry aus?

**Ja, typischerweise.** Medianer Drop vom Peak:

- 50x: **2.37 %** (p75 3.47 %, p90 4.60 %)
- 25x: **2.77 %** (p75 4.19 %, p90 4.87 %)

Median `drop_from_peak / max_adverse` ≈ **1.8** (Peak-Drop oft deutlich größer als der weitere Squeeze vom Entry).

Anteil Drop-from-Peak ≥0.50 % / ≥1 % / ≥2 % / ≥3 % (50x): ca. 96 % / 86 % / 59 % / 35 %.

## Nach wie vielen Minuten kommt typischerweise der Peak?

- 50x Median: **95 min** (~19 Candles); Mean ~109 min
- 25x Median: **85 min** (~17 Candles); Mean ~99 min

## Nach wie vielen Minuten kommt typischerweise das Tief?

- 50x Median: **125 min**; Peak→Tief Median **82.5 min**
- 25x Median: **145 min**; Peak→Tief Median **100 min**

## Wie oft sehen wir „erst hoch, dann runter“?

- 50x: **~56 %** Peak vor Trough (inkl. gleiche Candle)
- 25x: **~61 %**
- Sofortiger Fall ohne >0.25 % weiteren Anstieg und mit ≥0.50 % Fall: **~12–13 %**

Path-Kategorien (50x immediate reclaim):

| Kategorie | Anteil |
|---|---:|
| sideways_noise | 40 % |
| squeeze_then_drop | 24 % |
| deep_squeeze_then_drop | 20 % |
| immediate_drop | 13 % |
| immediate_breakout | 4 % |
| squeeze_without_drop | <1 % |

## Unterscheidet sich das im starken Downtrend (T3)?

**50x reclaim + T3 (n=83):** etwas stärkerer weiterer Squeeze (Median adverse 1.69 % vs 1.21 % ohne T3) und etwas größerer Fall (favorable 2.09 % vs 1.43 %; Drop-from-Peak 3.00 % vs 2.38 %). Der „kurzer Squeeze → danach Fall“-Ablauf ist sichtbar, aber **kein klarer Entry-Edge** und n ist klein.

**25x + T3:** nur **n=9** — nicht belastbar.

## Bestätigt OOS den Ablauf?

**Teilweise ja als Pfadform, nicht als Edge.** OOS 50x immediate reclaim: adverse-Median eher **niedriger** (1.02 % vs Full 1.26 %), favorable ähnlich/etwas höher (1.59 %), Drop-from-Peak ähnlich (2.36 %), Peak-then-trough sogar **~61 %**. Der Ablauf „weiter hoch, dann oft größerer Fall vom Peak“ bleibt sichtbar; Größenordnungen sind sample-abhängig.

## März 05.–10. / speziell 06.03.

- März-Fenster (reclaim 50x/25x, n=140): adverse-Median nur **0.44 %**, favorable **2.20 %**, Drop-from-Peak **2.46 %,** Peak-then-trough **~63 %** — eher sofortiger / flacher Squeeze, dann Fall.
- Nur 06.03. (n=14): adverse-Median **0.08 %**, favorable **3.42 %**, 100 % Peak-then-trough — sehr kleines n, anekdotisch „sofort runter“.

## Controls

Matched Controls (reclaim+T1): Events zeigen teils höheren weiteren Anstieg (50x: 1.69 % vs Control 1.11 %), aber Control hat oft ähnlichen oder größeren Drop-from-Peak / favorable. **Kein Signifikanz-Claim.**

## Ist das für die Hedge-Bot-Steuerung als Kontext nützlich?

**Ja als Pfad-Kontext, nein als Trade-Signal.**

Nützlich zu wissen:

1. Nach upper-50x/25x-Reclaim steigt APT im Median noch ~1.3 % weiter — oft über 1–2 Stunden.
2. Der anschließende Fall vom Zwischenhoch ist im Median ~2.4–2.8 % und oft größer als der Fall vom Entry.
3. „Erst Squeeze, dann Drop“ ist häufig (~56–61 %), aber ~40 % bleiben Rauschen; ~12 % fallen relativ sofort.

Nicht geeignet als alleinige Entry-Logik, kein klassisches SL-Sizing, keine Scanner-/Bot-Integration aus diesem Audit.
