# L1 V3Q — Kritischer Audit: Long-Trades auf `d-ac60572bfc69`

**Modus:** READ-ONLY · **Basis:** V3P-Snapshot `l1_v3p_drawings_snapshot_76f70d9d43b85c27.json`  
**Live-`drawings.json`:** SHA256 identisch (`76f70d9d43b85c273052e8f7b87cbbb5fab3930d43336dbecaa86c9866a21239`) — kein Snapshot-Drift.

---

## 2. Zeichnungsdefinition `d-ac60572bfc69` (JSON-Original)

| Feld | Wert |
|---|---|
| **drawing_id** | `d-ac60572bfc69` |
| **drawing_type** | `trend` |
| **Farbe** | `#fafafa` (fast weiß, `opacity` 0.95) |
| **extend_right** | `false` (`metadata` leer → `run_l1_pilot.line_from_drawing`) |
| **visible** | `true` |

| Anker | UTC | Preis |
|---|---|---|
| JSON `start_timestamp` / `start_price` | **2026-09-15 01:50:00** | **0.0810554602** |
| JSON `end_timestamp` / `end_price` | **2026-09-02 05:50:00** | **0.0938060293** |

**Hinweis:** Anker liegen in JSON in **umgekehrter Zeitfolge** (`start` später als `end`). Trend-Linien werden beim Speichern **nicht** wie Positions-Zeichnungen chronologisch normalisiert (`trading_research_platform/drawings/models.py`: Swap nur bei `POSITION_TYPES`, nicht bei `trend`).

**Backtester-Geometrie (`LineDef`):**

| Begriff | Wert |
|---|---|
| `t_lo` (geometrischer Anfang) | 2026-09-02 05:50:00 UTC |
| `t_hi` (geometrisches Ende, `extend_right=false`) | 2026-09-15 01:50:00 UTC |
| Steigung | ≈ −1.15×10⁻⁸ Preis/s (Preis fällt mit der Zeit) |
| Aktivitätsfenster Touches | Kerzen-Open ∈ \[`t_lo`, `t_hi`\] (`line_geometry.active_for_candle`) |

**Linienpreis an Entry-Zeitpunkten (Backtester `price_at`):**

| Entry UTC | Linienpreis |
|---|---:|
| 2026-09-05 19:55 | 0.0902423556 |
| 2026-09-06 09:50 | 0.0896662341 |
| 2026-09-07 07:45 | 0.0887589290 |
| 2026-09-09 20:00 | 0.0862647024 |
| 2026-09-09 20:40 | 0.0862371038 |
| 2026-09-11 13:00 | 0.0845673864 |
| 2026-09-12 01:15 | 0.0840602615 |
| 2026-09-13 22:35 | 0.0821835544 |

---

## 3. Acht fragliche Trades (Variante A, frische V3P-Simulation)

| Entry UTC | Linienpreis | Entry-Preis | Innerhalb \[t_lo,t_hi\]? | extend_right ok? | Touch gültig (Engine)? | Touch UTC | C1-Confirm UTC |
|---|---:|---:|---|---|---|---|---|
| 2026-09-05 19:55 | 0.09024 | 0.09066 | ja | ja (kein Ray) | ja (NEAR_0_10, 0.034%) | 2026-09-05 19:45 | 2026-09-05 19:50 |
| 2026-09-06 09:50 | 0.08967 | 0.09013 | ja | ja | ja (TOUCH) | 2026-09-06 09:40 | 2026-09-06 09:45 |
| 2026-09-07 07:45 | 0.08876 | 0.08917 | ja | ja | ja (NEAR_0_10, 0.050%) | 2026-09-07 07:35 | 2026-09-07 07:40 |
| 2026-09-09 20:00 | 0.08626 | 0.08695 | ja | ja | ja (NEAR_0_20, 0.160%) | 2026-09-09 19:50 | 2026-09-09 19:55 |
| 2026-09-09 20:40 | 0.08624 | 0.08680 | ja | ja | ja (NEAR_0_10, 0.030%) | 2026-09-09 20:30 | 2026-09-09 20:35 |
| 2026-09-11 13:00 | 0.08457 | 0.08512 | ja | ja | ja (NEAR_0_20, 0.184%) | 2026-09-11 12:50 | 2026-09-11 12:55 |
| 2026-09-12 01:15 | 0.08406 | 0.08439 | ja | ja | ja (NEAR_0_20, 0.158%) | 2026-09-12 01:05 | 2026-09-12 01:10 |
| 2026-09-13 22:35 | 0.08218 | 0.08256 | ja | ja | ja (TOUCH) | 2026-09-13 22:25 | 2026-09-13 22:30 |

Alle acht: `trend_support_from_above` → `TREND_SUPPORT_FROM_ABOVE` (Prior-Bar-Close über Linie). `drawing_causality` in V3P: durchgängig **`RETROSPECTIVE_DRAWING`** (Touch jeweils vor `t_hi` = 2026-09-15 01:50).

---

## 4. Research-Chart vs. L1-Backtester

| Prüfpunkt | Chart | Backtester | Gleich? |
|---|---|---|---|
| Ankerpunkte | `start_*` / `end_*` aus JSON unverändert (`drawings/compose._segment`) | `t1,p1,t2,p2` aus JSON (`line_from_drawing`) | ja |
| Liniengleichung | Gerade durch beide Anker | `price_at`: `p1 + slope·(t−t1)` | ja |
| `extend_right` | Nur Zone-Overlays; Trend-`segment` **ohne** Ray-Verlängerung (`chart.js` `renderSegment` = nur Strecke Anker₁→Anker₂) | `extend_right=false` → aktiv nur in \[`t_lo`,`t_hi`\] | ja (beide enden am zeitlichen Max-Anker) |
| Lebensdauer ohne Ray | Sichtbares Segment deckt **min..max** der Ankerzeit ab | Aktiv in **min..max** der Ankerzeit | ja |
| 5m-Kerzenzeit | Bar-Open UTC | Bar-Open UTC (`load_candles`) | ja |

**Sichtbarkeit im Chart (Sep 5–13):** Liegt zeitlich **auf** dem gezeichneten Segment (zwischen 2026-09-02 und 2026-09-15). Die Diagonale ist **geometrisch darstellbar**.

**Warum Nutzer „keine Linie“ sieht (wahrscheinlichste Ursache):**

- Stilfarbe **`#fafafa`** bei hoher Opacity auf hellem Chart-Hintergrund → praktisch **unsichtbar** (`style` im Snapshot).
- Kein separates „Support“-Label: jede sichtbare Trendlinie wird im Backtester als Kandidat behandelt (siehe §5).

**Kein** Nachweis für eine abweichende Interpolationsformel zwischen Chart und Backtester auf diesem Datensatz.

---

## 5. Support-Rolle

| Frage | Ergebnis |
|---|---|
| Linie unter Kurs vor Touch? | ja (Prior-Bar `TREND_SUPPORT_FROM_ABOVE`) |
| Touch von oben? | ja (Engine-Klassifikation + Approach-Check) |
| Untere Unterstützung vs. obere Begrenzung? | Code wertet nur „von oben“; **keine** Prüfung „bewusst als Unterstützung gezeichnet“ |
| Nur geometrische Berührung? | ja + C1-Bestätigung + globales Positionslimit |
| Gebrochene alte Unterstützung? | nicht separat modelliert |

**`support_line_selection.py` / `drawings_auto_window.is_candidate_support_trend`:** Jede Diagonale außer `d-22f8d9f720ce` mit gültigen Ankern → Support-Kandidat — **ohne** Rollen-Metadaten.

**`LINE_SELECTION_INDEPENDENT`:** `assess_long_touch_eligibility` überspringt den Legacy-Vergleich zur Hauptoberseite; reicht Approach + aktiv am Touch (`run_l1_pilot.py`).

**Kausalität:** `line_causality_at_touch` markiert Touches **vor** `t_hi` als `RETROSPECTIVE_DRAWING`, blockiert Entry aber **nicht** (nur Reporting).

---

## 6. Lebensdauer / `extend_right=false`

- `metadata.extend_right` fehlt → **false**.
- Backtester: nach `t_hi` = **2026-09-15 01:50** inaktiv (`AFTER_SECOND_ANCHOR`).
- Alle acht Touches liegen **zwischen** Sep 2 und Sep 15 → **innerhalb** des finite Segments (min/max-Anker).

**Interpretationsfalle (Policy, nicht Implementierungs-Bug):** „Zweiter Anker“ im UI-Sinn = JSON-`end_timestamp` (**2026-09-02**). Wenn man Touches **strikt nach dem zweiten Klick** verbieten wollte, wären alle acht Trades **nach** Sep 2 05:50 — der Code nutzt aber **`max(t1,t2)`** als rechtes Ende (`t_hi`), nicht das JSON-Feld `end_timestamp` isoliert.

---

## 7. Bewertung je Trade (acht Einträge)

| Entry UTC | Bewertung | Begründung (kurz) |
|---|---|---|
| 2026-09-05 19:55 | **CHART_BACKTEST_MISMATCH**, **WRONG_LINE_ROLE** | Geometrie aktiv; Chart-Segment vorhanden aber **#fafafa** schwer sichtbar; jede Diagonale = Support-Kandidat (`is_candidate_support_trend`) |
| 2026-09-06 09:50 | wie oben | + klassischer TOUCH; weiterhin RETROSPECTIVE |
| 2026-09-07 07:45 | wie oben | |
| 2026-09-09 20:00 | wie oben | |
| 2026-09-09 20:40 | wie oben | |
| 2026-09-11 13:00 | wie oben | |
| 2026-09-12 01:15 | wie oben | |
| 2026-09-13 22:35 | wie oben | |

Keiner der acht: `INVALID_GEOMETRY` (Engine konsistent mit `LineDef`), `DRAWING_SNAPSHOT_MISMATCH`, `INCONCLUSIVE`.

**Nicht** `VALID_VISIBLE_SUPPORT_TOUCH`: Nutzerwahrnehmung „keine Linie“ wird durch Chart-Styling und fehlende Support-Rollen-Semantik gestützt; fachliche „nur sichtbare manuelle Supportlinie“-Regel ist im Code nicht abgebildet.

---

## 8. Alle V3P-A-Trades (17) — gleiche Abweichungsprüfung

| Prüfung | Ergebnis |
|---|---|
| `active_for_candle` am Touch + Entry | **0 Verstöße** (alle 17) |
| `drawing_id` im Snapshot | alle vorhanden |
| Geometrisch inaktiv bei Entry | **keiner** |
| Chart-Segment sichtbar (min..max Anker) | alle 17 zeitlich innerhalb des Segments |

| drawing_id | Trades A | Anker-Reihenfolge in JSON | Auffälligkeit |
|---|---:|---|---|
| `d-ac60572bfc69` | **8** | **invers** | Farbe **#fafafa**; einzige Linie mit 8 SL-Trades in Sep-Cluster |
| übrige 8 IDs | 9 | gemischt | andere helle Farben; gleiche RETROSPECTIVE-Kennzeichnung |

Kein weiterer Trade auf einer **geometrisch inaktiven** Linie. Kein Snapshot-Mismatch.

---

## 9. Abschlussantworten

1. **Existierte die Linie aktiv?** Ja — im eingefrorenen JSON, `visible: true`, Backtester-`active_for_candle` = true an allen acht Touch-/Entry-Zeiten.

2. **Im Research-Chart sichtbar / darstellbar?** **Darstellbar** ja (Segment Sep 2–Sep 15); **praktisch oft unsichtbar** wegen `#fafafa`. Gleiche Anker wie Backtester.

3. **Korrekt als Unterstützung erkannt?** Nur im **engen Code-Sinn** (Touch von oben + INDEPENDENT diagonal). **Nicht** im Sinne einer explizit markierten Support-Rolle.

4. **Warum acht Longs?** Globales Positions-Re-Arm nach jedem Exit; alle Sep-Touches auf dieser Linie passieren Approach-Gate + C1 + Pipeline; **kein** Filter auf Kausalität/Sichtbarkeit/Rolle; Linie ist Kandidat via `is_candidate_support_trend`.

5. **Fehlerhafte Code-Stelle(n)?** Kein Interpolations-Bug. **Policy-Lücken:**
   - `drawings_auto_window.is_candidate_support_trend` — jede Trendlinie außer Upper-Main
   - `assess_long_touch_eligibility` — INDEPENDENT ohne Main-Upper-Check
   - `line_causality_at_touch` — RETROSPECTIVE blockiert nicht
   - Optional UX: `chart.js` / Styling — Trend fast weiß

6. **Minimale Korrektur (Vorschlag, nicht implementiert):**
   - **A)** Entries nur bei `drawing_causality.label == "CAUSAL"` (oder Touch ≥ `t_hi`), **oder**
   - **B)** Support nur mit Metadata/Rollen-Flag oder nach visuellem Mindestkontrast / `visible`+Nutzer-Tag, **oder**
   - **C)** Trend-Anker beim Laden chronologisch normalisieren + Dokumentation `t_hi` = rechter Anker; ggf. Touches nur links vom chronologisch letzten Anker wenn `extend_right=false`.

---

## Code-Referenzen

| Thema | Datei / Symbol |
|---|---|
| Linien laden, `extend_right` aus `metadata` | `run_l1_pilot.py` → `line_from_drawing` |
| Aktivität / Preis | `line_geometry.py` → `active_for_candle`, `price_at`, `t_lo`/`t_hi` |
| Support-Kandidaten | `drawings_auto_window.py` → `is_candidate_support_trend` |
| Long-Freigabe | `run_l1_pilot.py` → `assess_long_touch_eligibility`, `trend_support_from_above` |
| Kausalität (nur Label) | `support_line_selection.py` → `line_causality_at_touch` |
| Chart-Segment | `trading_research_platform/drawings/compose.py` → `_segment`; `charts/web/chart.js` → `renderSegment` |

**STOP — Audit ohne Implementierung.**
