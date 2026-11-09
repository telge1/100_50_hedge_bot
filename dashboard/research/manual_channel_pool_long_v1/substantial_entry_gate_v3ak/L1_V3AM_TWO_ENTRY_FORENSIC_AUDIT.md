# L1 V3AM — Zwei E3-Long-Entries 30.08.2026

Snapshot SHA256 `b0718d82a94e8ad1…` · **READ-ONLY** · `RETROSPECTIVE_DRAWING`

## Kurzfazit

**Trade A (11:45)** war beim Entry **näher am Support**, mit **kleinerem CAP6-TP (0,52 %)** und stärkerer naher Upper-Konzentration (E3-Zone depth 7 / 17 Origins). 
**Trade B (17:40)** lag **höher im Kanal** (+1,13 % über A-Entry), TP **2,38 %** entfernt; E3-Zone am oberen 2,5 %-Rand (depth 5 / 6 Origins). 
Beide erfüllten formal **V3E-B + E3 + CAP6-D20-BLOCK**. Objektiv war B **strukturell „später/höher“** (mehr Vorlauf in 6 h, höhere EMA-Abstände), aber **kein einzelnes Gate** trennte Gewinner und Verlierer.

**Trade B endete am SL**, obwohl **nach** bestätigtem Supportbruch (20:25 / 20:35 UTC): `simulate_exit_v3y` prüft **SL vor** dem geplanten SBE-Open-Fill — die 20:40-Kerze trifft **SL (−1,2 %)** zuerst.

## Vergleichstabelle

| Merkmal | Trade A 11:45 | Trade B 17:40 |
|---|---|---|
| Entry-Preis | 0.0849100000 | 0.0858700000 |
| Supportlinie | d-ea081434c5d3 | d-ea081434c5d3 |
| Support-Abstand % | 0.3434 | 0.604 |
| Kursbewegung 6h vorher % | -0.1529 | 1.1542 |
| Nahe Upper (stärkste Merge) | 0.0853500000 | 0.0943600000 |
| E3-Entry-Zone | 0.08535-0.085814 | 0.08791-0.088314 |
| CAP6-TP-Zone unten | 0.0853500000 | 0.0879100000 |
| TP-Distanz % | 0.5182 | 2.3757 |
| Lower-Pools | nearest lower upper=0.08481 dist=0.118% depth=0 n_below=103 | nearest lower upper=0.08514 dist=0.850% depth=0 n_below=110 |
| EMA9-Abstand % | 0.0055 | 0.0191 |
| EMA20-Abstand % | 0.0711 | 0.0679 |
| MFE % | 0.5182 | 0.6521 |
| MAE % | -0.1649 | -1.4091 |
| Exit-Grund | TP | SL |
| Ergebnis % | 0.5182 | -1.2000 |

## Entry-Rekonstruktion (je Trade)

### Trade A · `te-d-ea081434c5d3-0003`

- **Support-Touch:** 2026-08-30 11:35:00 UTC (Linienpreis Touch ≈ 0.0845977913)
- **C1-Bestätigung:** 2026-08-30 11:40:00 UTC
- **Entry:** 2026-08-30 11:45:00 UTC @ 0.0849100000 (Regel: first touch + C1 confirm + next bar open (run_l1_pilot))
- **D2-Gate / B-Nearness:** pass / pass · erstes qual. Upper-D2 ≤2,5 %: **0.5181957366623435%**
- **E3-Substantial-Zone:** 0.08535-0.085814
- **CAP6-D20-TP:** 0.0853500000 · **SL:** 0.0838910800
- **Vorherige gültige Touches auf derselben Support-ID (chronologisch vor Entry):** 3

### Trade B · `te-d-ea081434c5d3-0012`

- **Support-Touch:** 2026-08-30 17:30:00 UTC (Linienpreis Touch ≈ 0.0853306610)
- **C1-Bestätigung:** 2026-08-30 17:35:00 UTC
- **Entry:** 2026-08-30 17:40:00 UTC @ 0.0858700000 (Regel: first touch + C1 confirm + next bar open (run_l1_pilot))
- **D2-Gate / B-Nearness:** pass / pass · erstes qual. Upper-D2 ≤2,5 %: **0.6288575754046801%**
- **E3-Substantial-Zone:** 0.08791-0.088314
- **CAP6-D20-TP:** 0.0879100000 · **SL:** 0.0848395600
- **Vorherige gültige Touches auf derselben Support-ID (chronologisch vor Entry):** 12

## Konkurrierende E3-Kandidaten am 30.08.

- 2026-08-30 11:45:00 UTC te-d-ea081434c5d3-0003 outcome_e3=executed
- 2026-08-30 12:00:00 UTC te-d-ea081434c5d3-0004 outcome_e3=skipped_max_one_open
- 2026-08-30 17:40:00 UTC te-d-ea081434c5d3-0012 outcome_e3=executed
- 2026-08-30 18:20:00 UTC te-d-ea081434c5d3-0013 outcome_e3=skipped_max_one_open

Trade A nutzte den ersten freien Slot nach E0-Serie; Trade B folgte 3 h nach A-TP (max. 1 Long).

## Poollandschaft @ `_pool_decision_close`

Details: `l1_v3am_pool_landscape_comparison.csv` (kanonische PD1 Exact D2 Density, `build_zones_with_density`).

- **A:** E3-Zone depth **7** / origins **17**; CAP6-TP rank **1** ∫D **0.001672**
- **B:** E3-Zone depth **5** / origins **6**; CAP6-TP rank **3** ∫D **0.001254**

Zwischen 11:45 und 17:40 **stieg der Markt** (~+1,1 % Entry-Niveau); die E3-Qualifikation wanderte von **naher** Zone (0,52 % über Entry) zu **weit** entfernter Substantial-Zone bei **2,38 %** (nahe 2,5 %-Cap).

**Besonderheit B:** Die **stärkste Merge-Zone** (D20-Rang 1, ∫D 0,001372) lag beim Entry bei **9,89 %** (`0.09436–0.09474`) — weit außerhalb CAP6. CAP6-D20 wählte die **erste aufsteigende dominante Zone innerhalb 6 %** → Rang **3** bei **2,38 %**. Das E3-Gate nutzte eine **andere** nahe Zone (`0.08791–0.08831`) als TP-Ziel — formal zulässig, strukturell „zwei verschiedene Pool-Referenzen“.

## Post-Entry & Exit B

- **A:** TP-Touch 2026-08-30 12:05:00 UTC · Haltedauer 20 min · MFE 0.5182%
- **B:** kein TP-Touch; break 2026-08-30 20:25:00 UTC confirm 2026-08-30 20:35:00 UTC; **SL** 2026-08-30 20:40:00 UTC — SBE-Fill wäre nächste Bar nach Confirm, aber **Low ≤ SL** auf Exit-Bar zuerst (support_sim Reihenfolge).

## Beantwortung der Prüffragen

1. **War B objektiv schwächer?** Teils ja: höherer Entry, längerer TP-Weg, mehr Vorlauf (6 h), EMA weiter über Trend; E3-Metriken (depth/origins) schwächer. Formal aber **weiterhin regelkonform**.
2. **Vor Entry erkennbar:** Höhe/Support-Abstand, TP-Distanz, E3-Zonenlage; bei **B** zusätzlich **+1,15 %** in 6 h (bei **A** nahezu flach **−0,15 %**). **Nicht** vorhersagbar: SL- vs. SBE-Race.
3. **Kein Vorhersage-Signal für Verlust:** Intrabar SL vor SBE; kurzes MFE (+0,65 %) reichte TP 2,38 % nicht.
4. **Filter-Ansatzpunkt (hypothetisch, nicht kalibriert):** „kein Entry wenn CAP6-TP-Distanz ≫ E3-nahe Zone“ oder „6h-Rally + Entry weit über EMA20“ — **nur Forschungsthese**.
5. **Risiko guter Blocker:** Trade A hätte z. B. ein „zu hoher Entry“-Filter **nicht** getroffen; ein starres „max TP-Distanz“ könnte **A** nicht blockieren (TP 0,52 %).

## GO-TO Chart

| Trade | Touch | C1 | Entry | TP touch | Break / Confirm | Exit |
|---|---|---|---|---|---|---|
| A | 2026-08-30 11:35:00 UTC | 2026-08-30 11:40:00 UTC | 2026-08-30 11:45:00 UTC @ 0.0849100000 | 2026-08-30 12:05:00 UTC | — | 2026-08-30 12:05:00 UTC TP 0.5182% |
| B | 2026-08-30 17:30:00 UTC | 2026-08-30 17:35:00 UTC | 2026-08-30 17:40:00 UTC @ 0.0858700000 | — | 2026-08-30 20:25:00 UTC / 2026-08-30 20:35:00 UTC | 2026-08-30 20:40:00 UTC SL -1.2000% |

Diagnose-PNG (lokal, unverändert am Research-Chart): `l1_v3am_diagnostic_chart.png`

## Artefakte

- `l1_v3am_entry_comparison.csv`
- `l1_v3am_pool_landscape_comparison.csv`
- `l1_v3am_chart_review.csv`

**STOP** — keine Strategieübernahme.