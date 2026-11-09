# L1 V3Z — Variante B Chart-Audit (V3Y Read-Only)

Quelle: eingefrorene V3Y-Artefakte (`l1_v3y_test_b_*.csv`, `l1_v3y_test_b_summary.csv`) plus Kerzen-Rekonstruktion der V3X/V3Y-FSM (kein neuer Strategie-Backtest).  
Variante: **V3E-B** chronologisch, max. ein offener Long.  
Alle Trades: **RETROSPECTIVE_DRAWING** (`retrospective=yes` in CSV).

---

## 8. Gesamtstatistik (exakt aus CSV)

| Kennzahl | Wert |
|---|---:|
| Trades | 9 |
| Gewinntrades (PnL > 0) | 7 |
| Verlusttrades (PnL < 0) | 2 |
| SL-Exits V3Y | 0 |
| SUPPORT_BREAK_EXIT | 5 |
| Σ Gesamtbrutto | **+39.7269 %** |
| Σ Gewinne | +40.7792 % |
| Σ Verluste | −1.0520 % |
| Ø pro Trade | +4.4141 % |
| Längste Verlustserie | 1 |
| Σ nur Supportbruch-Exits | +7.9639 % |
| Neue Entries vs. V3U (6→9) | **3** |

V3U-Baseline (chrono): 6 Trades, Σ **+25.9831 %**, 3 Gewinner, **3 SL**, 0 Supportbruch.

---

## 2. Alle neun Trades (V3Y Variante B)

| Nr. | Entry UTC | Entry | TP | SL | Exit UTC | Exit | Exit-Grund | PnL % | Support-ID | Upper-Kanal-ID | TP-Modus | Haltedauer | MFE % | MAE % | Retro |
|---:|---|---:|---:|---:|---|---:|---|---:|---|---|---|---|---:|---:|:---:|
| 1 | 2026-08-26 15:10:00 UTC | 0.0844400000 | 0.0949600000 | 0.0834267200 | 2026-08-28 02:15:00 UTC | 0.0884900000 | SUPPORT_BREAK_EXIT | +4.7963 | d-a6d9a05da2f1 | — | POOL_BOTTOM_NO_CLEAR_CHANNEL | 35.08 h | +6.9043 | −0.7461 | ja |
| 2 | 2026-08-28 18:55:00 UTC | 0.0841800000 | 0.0883000000 | 0.0831698400 | 2026-08-30 16:40:00 UTC | 0.0860150748 | CHANNEL_UPPER | +2.1799 | d-9b6e62dc884c | d-2f6effea5ff3 | POOL_BOTTOM_NEAR_CHANNEL | 45.75 h | +2.1858 | −0.1426 | ja |
| 3 | 2026-08-31 00:25:00 UTC | 0.0823300000 | 0.0859360000 | 0.0813420400 | 2026-08-31 02:55:00 UTC | 0.0816000000 | SUPPORT_BREAK_EXIT | −0.8867 | d-9b6e62dc884c | d-0e5e7004dccd | POOL_BOTTOM_NEAR_CHANNEL | 2.50 h | +0.4008 | −1.1539 | ja |
| 4 | 2026-08-31 03:00:00 UTC | 0.0817600000 | 0.0859360000 | 0.0807788800 | 2026-09-01 12:40:00 UTC | 0.0824500000 | SUPPORT_BREAK_EXIT | +0.8439 | d-e1c6e07765fe | d-0e5e7004dccd | POOL_BOTTOM_NEAR_CHANNEL | 33.67 h | +2.6908 | −0.0978 | ja |
| 5 | 2026-09-02 02:00:00 UTC | 0.0806600000 | 0.0831660000 | 0.0796920800 | 2026-09-03 02:30:00 UTC | 0.0831660000 | TP | +3.1069 | d-3bd7446b7a5b | d-e1c6e07765fe | POOL_BOTTOM_NEAR_CHANNEL | 24.50 h | +3.1366 | −0.8306 | ja |
| 6 | 2026-09-16 18:05:00 UTC | 0.0796200000 | 0.0912300000 | 0.0786645600 | 2026-09-19 17:15:00 UTC | 0.0912300000 | TP | +14.5818 | d-1f70c9798935 | — | POOL_BOTTOM_NO_CLEAR_CHANNEL | 71.17 h | +14.6948 | −1.1555 | ja |
| 7 | 2026-09-20 12:10:00 UTC | 0.0852500000 | 0.0953900000 | 0.0842270000 | 2026-09-21 15:00:00 UTC | 0.0953900000 | TP | +11.8944 | d-1f70c9798935 | — | POOL_BOTTOM_NO_CLEAR_CHANNEL | 26.83 h | +15.2023 | −0.6452 | ja |
| 8 | 2026-09-24 10:40:00 UTC | 0.0921300000 | 0.1059400000 | 0.0910244400 | 2026-09-26 20:50:00 UTC | 0.0952400000 | SUPPORT_BREAK_EXIT | +3.3757 | d-1f70c9798935 | — | POOL_BOTTOM_NO_CLEAR_CHANNEL | 58.17 h | +8.3143 | −0.0326 | ja |
| 9 | 2026-09-27 06:40:00 UTC | 0.0967800000 | 0.1059400000 | 0.0956186400 | 2026-09-27 15:00:00 UTC | 0.0966200000 | SUPPORT_BREAK_EXIT | −0.1653 | d-1f70c9798935 | — | POOL_BOTTOM_NO_CLEAR_CHANNEL | 8.33 h | +2.1595 | −0.6096 | ja |

`touch_event_id`: (1) te-d-a6d9a05da2f1-0001, (2) **te-d-9b6e62dc884c-0003** neu, (3) te-d-9b6e62dc884c-0006, (4) **te-d-e1c6e07765fe-0003** neu, (5) te-d-9b6e62dc884c-0017, (6) te-d-1f70c9798935-0002, (7) te-d-1f70c9798935-0007, (8) te-d-1f70c9798935-0014, (9) **te-d-1f70c9798935-0022** neu.

---

## 4A. Gewinntrades (PnL > 0)

| Entry UTC | Entry | TP | SL | Exit UTC | Exit | Exit-Grund | Gewinn % |
|---|---:|---:|---:|---|---:|---|---:|
| 2026-08-26 15:10:00 UTC | 0.08444 | 0.09496 | 0.08342672 | 2026-08-28 02:15:00 UTC | 0.08849 | SUPPORT_BREAK_EXIT | +4.7963 |
| 2026-08-28 18:55:00 UTC | 0.08418 | 0.08830 | 0.08316984 | 2026-08-30 16:40:00 UTC | 0.0860150748 | CHANNEL_UPPER | +2.1799 |
| 2026-08-31 03:00:00 UTC | 0.08176 | 0.085936 | 0.08077888 | 2026-09-01 12:40:00 UTC | 0.08245 | SUPPORT_BREAK_EXIT | +0.8439 |
| 2026-09-02 02:00:00 UTC | 0.08066 | 0.083166 | 0.07969208 | 2026-09-03 02:30:00 UTC | 0.083166 | TP | +3.1069 |
| 2026-09-16 18:05:00 UTC | 0.07962 | 0.09123 | 0.07866456 | 2026-09-19 17:15:00 UTC | 0.09123 | TP | +14.5818 |
| 2026-09-20 12:10:00 UTC | 0.08525 | 0.09539 | 0.084227 | 2026-09-21 15:00:00 UTC | 0.09539 | TP | +11.8944 |
| 2026-09-24 10:40:00 UTC | 0.09213 | 0.10594 | 0.09102444 | 2026-09-26 20:50:00 UTC | 0.09524 | SUPPORT_BREAK_EXIT | +3.3757 |

## 4B. Verlusttrades (PnL < 0)

| Entry UTC | Entry | TP | SL | Exit UTC | Exit | Exit-Grund | Verlust % |
|---|---:|---:|---:|---|---:|---|---:|
| 2026-08-31 00:25:00 UTC | 0.08233 | 0.085936 | 0.08134204 | 2026-08-31 02:55:00 UTC | 0.08160 | SUPPORT_BREAK_EXIT | −0.8867 |
| 2026-09-27 06:40:00 UTC | 0.09678 | 0.10594 | 0.09561864 | 2026-09-27 15:00:00 UTC | 0.09662 | SUPPORT_BREAK_EXIT | −0.1653 |

---

## 5. Vergleich V3U ↔ V3Y (gemeinsame Entries)

| Entry UTC | V3U Exit / % | V3Y Exit / % | Δ % | Anmerkung |
|---|---|---:|---|
| 2026-08-26 15:10 | SL 2026-08-30 22:20 @ 0.08342672 / **−1.2000** | SUPPORT 2026-08-28 02:15 @ 0.08849 / **+4.7963** | **+5.9963** | Support rettet vor späterem SL |
| 2026-08-31 00:25 | SL 2026-09-01 18:25 @ 0.08134204 / **−1.2000** | SUPPORT 2026-08-31 02:55 @ 0.08160 / **−0.8867** | **+0.3133** | kleinerer Verlust, früher raus |
| 2026-09-02 02:00 | TP +3.1069 | TP +3.1069 | 0 | identisch |
| 2026-09-16 18:05 | TP +14.5818 | TP +14.5818 | 0 | identisch |
| 2026-09-20 12:10 | TP +11.8944 | TP +11.8944 | 0 | identisch |
| 2026-09-24 10:40 | SL 2026-10-02 18:35 @ 0.09102444 / **−1.2000** | SUPPORT 2026-09-26 20:50 @ 0.09524 / **+3.3757** | **+4.5757** | Support statt späterem SL |

### Nur V3Y (kein V3U-Pendant bei gleichem Entry)

| Entry UTC | Exit / % | Exit-Grund | touch_event_id |
|---|---:|---|---|
| 2026-08-28 18:55:00 UTC | +2.1799 | CHANNEL_UPPER | te-d-9b6e62dc884c-0003 (**neu**) |
| 2026-08-31 03:00:00 UTC | +0.8439 | SUPPORT_BREAK_EXIT | te-d-e1c6e07765fe-0003 (**neu**) |
| 2026-09-27 06:40:00 UTC | −0.1653 | SUPPORT_BREAK_EXIT | te-d-1f70c9798935-0022 (**neu**) |

Früherer Support-/Kanal-Exit bei Trade 1 schafft ab 28.08. freie Kapazität für Trade 2; analog Kette 31.08. / 27.09.

---

## 3. Fünf SUPPORT_BREAK_EXIT — Bruch- & Bestätigungskerzen

Regel: Close unter Entry-Support (`price_at` Kerzenende); Folge-Close ≤ Bruch-Close×(1−0,002); Fill = Open der nächsten Kerze.  
Alle fünf: `confirm_rule=True`, keine vorgezogene SL/TP/Kanal-Treffer auf Bruch-/Bestätigungs-/Fill-Kerze (FSM-Replay).

### S1 — Referenz (Nr. 1)

| Feld | Wert |
|---|---|
| Entry | 2026-08-26 15:10:00 UTC @ 0.0844400000 |
| Support-ID | d-a6d9a05da2f1 (= touch_drawing_id Entry) |
| Bruch UTC | **2026-08-28 02:05:00 UTC** |
| Bruch OHLC | O 0.08903 H 0.08908 L 0.08879 **C 0.08887** |
| Linie @ Bruch | 0.0891209784 (Close **0,028 %** unter Linie) |
| Bestätigung UTC | **2026-08-28 02:10:00 UTC** |
| Bestätigung OHLC | O 0.08887 H 0.08893 L 0.08849 **C 0.08849** |
| Δ Close Bruch→Bestätigung | **−0.4276 %** (≤ −0,20 %) |
| Exit (Fill-Open) | **2026-08-28 02:15:00 UTC** @ **0.0884900000** |
| PnL | +4.7963 % |
| TP / SL (unverändert) | 0.09496 / 0.08342672 |

### S2 — Nr. 3 (Verlust-Supportbruch)

| Feld | Wert |
|---|---|
| Entry | 2026-08-31 00:25:00 UTC @ 0.0823300000 |
| Support-ID | d-9b6e62dc884c |
| Bruch UTC | **2026-08-31 02:45:00 UTC** |
| Bruch OHLC | O 0.08194 H 0.08199 L 0.08178 **C 0.08180** |
| Linie @ Bruch | 0.0818048875 (Close **0,006 %** unter Linie — Haarlinien-Bruch) |
| Bestätigung UTC | **2026-08-31 02:50:00 UTC** |
| Bestätigung OHLC | O 0.08180 H 0.08182 L 0.08158 **C 0.08160** |
| Δ Close | **−0.2445 %** |
| Exit | **2026-08-31 02:55:00 UTC** @ **0.0816000000** |
| PnL | −0.8867 % |
| TP / SL | 0.085936 / 0.08134204 |

### S3 — Nr. 4 (neuer Entry)

| Feld | Wert |
|---|---|
| Entry | 2026-08-31 03:00:00 UTC @ 0.0817600000 |
| Support-ID | d-e1c6e07765fe |
| Bruch UTC | **2026-09-01 12:30:00 UTC** |
| Bruch OHLC | O 0.08280 H 0.08281 L 0.08262 **C 0.08269** |
| Linie @ Bruch | 0.0827084508 |
| Bestätigung UTC | **2026-09-01 12:35:00 UTC** |
| Bestätigung OHLC | O 0.08269 H 0.08269 L 0.08235 **C 0.08245** |
| Δ Close | **−0.2902 %** |
| Exit | **2026-09-01 12:40:00 UTC** @ **0.0824500000** |
| PnL | +0.8439 % |
| TP / SL | 0.085936 / 0.08077888 |

### S4 — Nr. 8

| Feld | Wert |
|---|---|
| Entry | 2026-09-24 10:40:00 UTC @ 0.0921300000 |
| Support-ID | d-1f70c9798935 |
| Bruch UTC | **2026-09-26 20:40:00 UTC** |
| Bruch OHLC | O 0.09607 H 0.09615 L 0.09539 **C 0.09562** |
| Linie @ Bruch | 0.0958098048 |
| Bestätigung UTC | **2026-09-26 20:45:00 UTC** |
| Bestätigung OHLC | O 0.09562 H 0.09580 L 0.09523 **C 0.09524** |
| Δ Close | **−0.3974 %** |
| Exit | **2026-09-26 20:50:00 UTC** @ **0.0952400000** |
| PnL | +3.3757 % |
| TP / SL | 0.10594 / 0.09102444 |

### S5 — Nr. 9 (neuer Entry, leichter Verlust)

| Feld | Wert |
|---|---|
| Entry | 2026-09-27 06:40:00 UTC @ 0.0967800000 |
| Support-ID | d-1f70c9798935 |
| Bruch UTC | **2026-09-27 14:50:00 UTC** |
| Bruch OHLC | O 0.09679 H 0.09690 L 0.09666 **C 0.09684** |
| Linie @ Bruch | 0.0971143843 |
| Bestätigung UTC | **2026-09-27 14:55:00 UTC** |
| Bestätigung OHLC | O 0.09684 H 0.09684 L 0.09662 **C 0.09662** |
| Δ Close | **−0.2272 %** |
| Exit | **2026-09-27 15:00:00 UTC** @ **0.0966200000** |
| PnL | −0.1653 % |
| TP / SL | 0.10594 / 0.09561864 |

---

## 6. Chart-Prüfliste (GO TO)

### Supportbruch-Exits zuerst

| Entry UTC (GO TO) | Support-ID | Bruch UTC | Bestätigung UTC | Exit UTC | Exit-Preis | PnL % |
|---|---|---|---|---|---:|---:|
| 2026-08-26 15:10 | d-a6d9a05da2f1 | 2026-08-28 02:05 | 2026-08-28 02:10 | 2026-08-28 02:15 | 0.08849 | +4.7963 |
| 2026-08-31 00:25 | d-9b6e62dc884c | 2026-08-31 02:45 | 2026-08-31 02:50 | 2026-08-31 02:55 | 0.08160 | −0.8867 |
| 2026-08-31 03:00 | d-e1c6e07765fe | 2026-09-01 12:30 | 2026-09-01 12:35 | 2026-09-01 12:40 | 0.08245 | +0.8439 |
| 2026-09-24 10:40 | d-1f70c9798935 | 2026-09-26 20:40 | 2026-09-26 20:45 | 2026-09-26 20:50 | 0.09524 | +3.3757 |
| 2026-09-27 06:40 | d-1f70c9798935 | 2026-09-27 14:50 | 2026-09-27 14:55 | 2026-09-27 15:00 | 0.09662 | −0.1653 |

**Visuelle Referenz (Close/Linie):**  
S1 Bruch-C 0.08887 vs Linie 0.089121; Bestätigung-C 0.08849.  
S2 Bruch-C 0.08180 vs Linie 0.081805; Bestätigung-C 0.08160.  
S3 Bruch-C 0.08269 vs Linie 0.082708; Bestätigung-C 0.08245.  
S4 Bruch-C 0.09562 vs Linie 0.095810; Bestätigung-C 0.09524.  
S5 Bruch-C 0.09684 vs Linie 0.097114; Bestätigung-C 0.09662.

### Übrige vier Trades

| Entry UTC (GO TO) | Support-ID | Exit UTC | Exit-Preis | PnL % | Exit-Grund |
|---|---|---|---:|---:|---|
| 2026-08-28 18:55 | d-9b6e62dc884c | 2026-08-30 16:40 | 0.0860150748 | +2.1799 | CHANNEL_UPPER |
| 2026-09-02 02:00 | d-3bd7446b7a5b | 2026-09-03 02:30 | 0.083166 | +3.1069 | TP |
| 2026-09-16 18:05 | d-1f70c9798935 | 2026-09-19 17:15 | 0.09123 | +14.5818 | TP |
| 2026-09-20 12:10 | d-1f70c9798935 | 2026-09-21 15:00 | 0.09539 | +11.8944 | TP |

---

## 7. Plausibilitätskontrolle

| Prüfpunkt | Ergebnis |
|---|---|
| Überwachte Linie = Entry-`support_drawing_id` / `touch_drawing_id` | **OK** — alle 5× identisch |
| Linie beim Bruch aktiv (`active_for_candle`) | **OK** |
| Entscheid nur auf 5m-Close (kein Wick-only) | **OK** — Bruch jeweils Close < Linie |
| 0,20-%-Bestätigung (Folge-Close vs Bruch-Close) | **OK** — Δ ∈ [−0,43 % … −0,23 %] |
| Exit = Open Kerze nach Bestätigung | **OK** — Fill-UTC = Bestätigung + 5 min |
| SL/TP/Kanal nicht übersehen | **OK** auf Exit-Pfad (intrabar vor FSM) |
| RETROSPECTIVE_DRAWING | **OK** — alle 9 |
| Regel nur Entry-Support | **OK** |

**Auffälligkeiten (fachlich, kein Implementierungsfehler):**

1. **Haarlinien-Brüche** S2/S3: Close nur minimal unter der Linie (Größenordnung 10⁻⁵–10⁻⁴) — regelkonform, im Chart sorgfältig zoomen.  
2. **Zwei Supportbruch-Verluste** (Nr. 3, 9): Exit-Grund ≠ Gewinn — korrekt in Verlusttabelle geführt.  
3. **Nr. 9**: trotz MFE +2,16 % enger Supportbruch mit −0,17 % — kurze Haltedauer (8,3 h).  
4. **Neue Trades 2, 4, 9**: kein direkter V3U-Vergleich auf gleichem Entry — nur gesondert ausgewiesen.

---

## Dateien

- Bericht: `L1_V3Z_VARIANT_B_CHART_AUDIT.md` (diese Datei)
- Review-CSV: `l1_v3z_variant_b_chart_review.csv`
- Unverändert: `l1_v3y_test_b_*.csv`, `l1_v3y_full_report.md`

**STOP** — keine Strategieänderung.
