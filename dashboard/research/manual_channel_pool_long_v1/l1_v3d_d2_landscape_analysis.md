# L1 V3D — ACTIVE Upper-D2-Landschaft (Forschungsdiagnose)

- D2: `pd1_exact_density_trp_adapter.computeExactBandsForSide-parity-v1`, min_depth=2
- SSOT: `load_historical_pool_state_at_close` 5m PD1 @ **decision_close** (5m bar close)
- Pool-SSOT @ decision_close: A **2026-08-25 21:15:00 UTC** (confirm `2026-08-25 21:10:00 UTC`), B **2026-08-26 15:10:00 UTC** (confirm `2026-08-26 15:05:00 UTC`)
- Kanal oben: `d-22f8d9f720ce` @ **Entry-Bar-Open** (Geometrie), nicht @ Pool-Close
- Diagnose-Merge: (1) gap≤0.15% v. Entry **und** gleiche Depth; (2) gap≤0.15% **oder** ≤8e-05 Preis, Depth egal
- drawings SHA256: `3e7041de2814607b4bd4ad4a07df143c155072b09473b96ff151fe1e2e0a1cc4`

## 9. Vergleichstabelle Referenzen

| Kennzahl | 25.08. 21:15 (A) | 26.08. 15:10 (B) |
| --- | --- | --- |
| Entry-Preis | 0.085850 | 0.084440 |
| Oberer Kanal @ Entry | 0.09290103693796704 | 0.09189479070579458 |
| Kanalabstand % | 8.2132 | 8.8285 |
| D2-Bänder gesamt | 55 | 69 |
| D2 im Kanal | 28 | 35 |
| Nächste D2 % | 3.6109 | 1.8948 |
| Nächste subst. (depth≥3) % | 3.6225975538730366 | 1.918522027475126 |
| Abdeckung Kanal % | 13.1328 | 15.2117 |
| Ø / max Depth (Kanal) | 2.79 / 5 | 2.77 / 5 |
| Nächste subst. (2./3., depth≥3) % | [3.6225975538730366, 4.0419336051252195, 4.1001747233547] | [1.918522027475126, 1.989578398863099, 2.0416864045476117] |
| Anteil Kanal bis 1. D2 % | 43.97 | 21.46 |
| Getrennte Rohzonen (1 Band = 1) | 28 | 35 |
| Merge depth+gap / gap-only Zonen | 27/7 | 34/9 |
| Längste Lücke % | 3.610949330227147 | 2.136428233064898 |
| Abschnitte mit D2 (0–25…75–100) | 3/4 | 4/4 |
| D2-Verteilung Bänder/Abschnitt | 0/6/12/11 | 6/8/10/11 |

### Substanz-Definitionen (nur Diagnose)

- **depth_ge_3**: nächste Zone A=3.6225975538730366 %, B=1.918522027475126 %
- **depth_ge_2_and_width_ge_0.12pct**: nächste Zone A=4.267909143855563 %, B=6.009000473709137 %
- **depth_ge_2_and_width_ge_0.08pct**: nächste Zone A=4.267909143855563 %, B=2.0677404073898598 %

## 8. Snapshot-Evolution

| snapshot | close UTC | ref close | D2 total | in ch | nearest % | coverage % |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| EV_25_08_2110 | 2026-08-25 21:15:00 UTC | 0.086030 | 55 | 28 | 3.3941648262234176 | 13.48 |
| EV_26_08_0000 | 2026-08-26 00:05:00 UTC | 0.085520 | 57 | 30 | 1.613657623947622 | 13.99 |
| EV_26_08_0600 | 2026-08-26 06:05:00 UTC | 0.086940 | 76 | 29 | — | 16.01 |
| EV_26_08_1200 | 2026-08-26 12:05:00 UTC | 0.086250 | 63 | 29 | 0.22028985507247492 | 16.16 |
| EV_26_08_1505 | 2026-08-26 15:10:00 UTC | 0.084110 | 69 | 35 | 2.2946141956961137 | 14.57 |

## 10. Interpretation

1. **Näher am Preis (B vs A)?** Ja (1.89% vs 3.61%).
2. **Mehr substanzielle Zonen im Kanal?** Depth≥3 count: A=14 B=18; in-channel bands: A=28 B=35.
3. **Höhere Dichte?** Abdeckung Kanal: A=13.1% B=15.2%; max depth A=5 B=5.
4. **Verteilung über Abschnitte?** A=0/6/12/11 (0–25 leer), B=6/8/10/11; B hat D2 bereits im untersten Viertel, A nicht.
5. **Größere Lücken bei A?** Ja — längste Lücke %: A=3.61 vs B=2.14 (entspricht fast dem Abstand zur ersten D2 bei A).
6. **Landschaft baut sich auf?** Teilweise: zwischen 25.08 21:15 und 26.08 12:00 steigen Bandanzahl und Unterviertel-Abdeckung; nahe D2-Zone schwankt mit Kurs (06:00 ohne Pool oberhalb Close; 12:00 sehr nah). Beim B-Entry (15:10) wieder ~1,89 % zur ersten Zone — nicht monoton dichter, aber strukturell früher belegt als A.
7. **Stärkste Unterscheider:** (a) `nearest_d2_pct`, (b) Abschnitt 0–25 % (0 vs 6 Bänder), (c) `first_d2_share_of_channel_pct`.

## 13. Kurzfassung

**A (25.08.):** 55 D2-Bänder, 28 im Kanal; erste Unterkante +3,61 %; **0–25 %-Viertel ohne D2**; längste Lücke ≈ erste Zone (3.61 %).

**B (26.08.):** 69 D2-Bänder, 35 im Kanal; erste Zone +1,89 %; **alle vier Viertel mit D2** (Staffelung 6/8/10/11); Abdeckung 15.2 %.

**C.** Hauptunterschied: **Nähe der ersten D2-Zone** + **Abdeckung/Staffelung im unteren Kanalviertel**, nicht nur Gesamtzahl.

**D. Später testbare Filter (ohne Schwellen):** `nearest_d2_in_channel_pct`, `coverage_in_channel_pct`, `sections_with_d2_in_lower_half`.

CSVs: `l1_v3d_d2_reference_bands.csv`, `l1_v3d_d2_distribution.csv`, `l1_v3d_d2_snapshot_evolution.csv`.
