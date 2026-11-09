# L1 V3V — Single trade pool TP audit (READ-ONLY)

**Trade:** DOGEUSDT 5m · LONG · Support `d-a6d9a05da2f1` (retrospective)  
**Entry:** `2026-08-26 15:10:00 UTC` @ `0.08444`  
**C1 confirm bar:** `2026-08-26 15:05:00 UTC` → Pool-SSOT **decision_close** = `2026-08-26 15:10:00 UTC` (5m `bar_close`)  
**V3U backtest:** SL `0.08342672` · geplanter TP `0.09496` · Exit SL `2026-08-30 22:20:00 UTC` (−1.20%)  
**Daten:** 5m OHLC (gleiche Quelle wie L1-Pilot) · PD1 ACTIVE · Exact Unique-Origin D2 · depth ≥ 2  

Artefakte: `l1_v3v_upper_d2_entry_snapshot.csv`, `l1_v3v_upper_d2_timeline.csv`, `l1_v3v_tp_comparison.csv`, `l1_v3v_price_path.csv`

---

## 1. Warum TP `0.09496`?

Zum Entscheidungszeitpunkt (geschlossene Kerze `15:10 UTC`):

| Schritt | Ergebnis |
|--------|----------|
| Kanaldeckel V3U (`channel_upper_at`, 12%-Fenster, Support ausgeschlossen) | **keiner** → Modus **pool-only** |
| ACTIVE Upper-D2-Bänder (depth≥2) | **69** qualifiziert (Abstand ≥ 0,50 %) |
| Auswahlregel `pick_tp` | **`max(lower_bound)`** unter den qualifizierten Bändern |
| Gewähltes Ziel | Unterkante **`0.09496`** · Band `0.09496–0.095186` · depth **2** · Pool-ID `d2-exact:0.0949600000:0.0951860000:d2` |
| Abstand Entry | **+12,4586 %** |

**68 nähere** qualifizierte Unterkanten (inkl. der User-Zone ~5,85–6,01 %) wurden übersprungen — bewusst durch die implementierte Regel, nicht durch fehlende Daten.

Zum Vergleich **V3P-Zeichnungsstand** (unverändert gelesen aus Snapshot): Oberkanal `d-22f8d9f720ce` @ **`0.09179`** → `POOL_BOTTOM_NEAR_CHANNEL` TP **`0.09151`** (max Pool unter Deckel innerhalb 2 %-Nähe).

---

## 2. User-Zone `0.0894–0.0900` beim Entry

**Ja — bereits ACTIVE** am Entry-Entscheid:

- **6** ACTIVE Upper-Pool-Member überlappen die Zone (6 verschiedene Origin-IDs).
- Exact-D2-Slices in der Zone (depth≥2): **4** zusammenhängende Segmente, **max depth 5** bei `0.08938–0.08941`.
- Gesamtspanne der Konzentration (D2-merge): ca. **`0.08938–0.08962`** (Breite ~0,24 %).
- Substanz vs. frühere Bänder: ab `0.08938` steigt depth auf **4–5** (mehr Unique Origins als die dünnen 2er-Bänder darunter).

Näher liegt das **nächste qualifizierte** D2-Ziel bei **`0.08604`** (+1,89 %) — deutlich unter der markierten Zone.

---

## 3. Kurs & Zone (Entry → SL)

| Metrik | Wert |
|--------|------|
| Max High | `0.09027` @ **2026-08-28 01:30:00 UTC** |
| MFE | **+6,9043 %** |
| Erster Intrabar-Touch `0.0894–0.0900` | **2026-08-27 08:45:00 UTC** (High erreicht Zone) |
| Erster Close in Zone | **08:45 UTC** (dieselbe Kerze) |
| Erstes Close **unter** Zonenunterkante danach | **2026-08-27 08:50:00 UTC** |
| SL | **2026-08-30 22:20:00 UTC** |

**Hinweis Ausführung:** Limit-TP bei `0.0894` wäre in der **08:45**-Kerze (High ≥ TP) nach Backtester-Logik **intrabar** ausführbar — bevor ein rein close-basiertes Signal dieselbe Information hätte.

---

## 4. Isolierter TP-Vergleich (gleicher SL, nur dieser Entry)

Siehe `l1_v3v_tp_comparison.csv`. Kurzfassung:

| Ref | TP | Dist % | Erreicht? | Erster Exit UTC | Brutto % | SL zuerst? |
|-----|-----|--------|-----------|-----------------|----------|------------|
| **A** Backtester V3U | 0.09496 | 12,46 | Nein | 30.08. 22:20 SL | −1,20 | Ja |
| **B** User-Unterkante *(post-hoc)* | 0.0894 | 5,87 | Ja | 27.08. 08:45 | **+5,87** | Nein |
| **C** User-Oberkante *(post-hoc)* | 0.0900 | 6,58 | Ja | 28.08. 01:30 | **+6,58** | Nein |
| **D** Nächstes qual. D2 (Entry) | 0.08604 | 1,89 | Ja | 26.08. 21:15 | +1,89 | Nein |
| **E** „Substanziell“ | — | — | *Keine validierte objektive Regel im Code* | — | — | — |

B/C sind **explizit nachträgliche Screenshot-Referenzen**, keine kausalen Strategieparameter.

---

## 5. V3P vs V3U — obere Kanallinie

| | V3P Snapshot | V3U Snapshot |
|--|-------------|--------------|
| Oberlinie @ Entry | **`d-22f8d9f720ce`** @ ~0.0918 | **fehlt** (in V3P entfernt / nicht im aktuellen Bestand) |
| Geplanter TP @ Entry | 0.09151 (Pool unter Deckel) | 0.09496 (max D2) |
| Historischer Exit (ältere Läufe, gleicher Entry) | **CHANNEL_UPPER** ~0.08997 @ **28.08. 01:30** (+6,54 %) | **SL** −1,20 % |
| `d-22f8d9f720ce` @ Max-High-Bar (28.08. 01:30) | ~**0.08976** (Deckel unter Kurs) | n/a |

**Kanal hätte** im V3P-Stand einen **früheren Gewinn-Exit** geliefert (28.08.), nicht schon beim ersten Zonentouch (27.08.).

---

## 6. Pool-Zeitleiste (Zone `0.0894–0.0900`)

Siehe `l1_v3v_upper_d2_timeline.csv`: Zone bleibt bis zum ersten Touch substanziell (depth bis 5); beim Max-High-Zeitpunkt ist der Kurs **oberhalb** der Zone — dort schneidet die fixe 0.0894–0.0900-Fensterauswertung **0** aktive Members (Pools liegen unter dem aktuellen Preis).

---

## 7. Offene Punkte (keine Strategieänderung)

- Ob „substanziell“ für TP jenseits von D2-depth≥2 formal sein soll (V3-Landschaftsfilter = Research, nicht Exit-Regel).
- Limit vs. Close-only Exits an dichter Pool-Zone.
- Kanalpaarung vs. `max(price)`-Heuristik bei mehreren Oberlinien.

**STOP — READ-ONLY, keine Optimierung.**
