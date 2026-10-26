# PHASE BT-D54 — PERFECT DOWNTREND SHORT ENTRY FORENSICS

**Datum:** 2026-10-26  
**Verdict:** `PERFECT_SHORT_PATTERN_FORENSICS_PASS`

**Scope:** Read-only Forschung. Keine Änderungen an Backtester, Dashboard, Dichte-SSOT, Entry/SL/TP oder PnL.

**Symbol / Setup:** DOGEUSDT · 5m · PD1 · Exact Unique-Origin Density · Depth 2  
**Dichte:** `canonical_provider` (ResearchDensityRunV1) für 2026-09-09 — der D9-`validated_cache` deckt nur 2026-09-11/12 ab; **gleiche** Depth-2-Math, nur anderer Lese-Pfad.  
**Warmup:** Preise ab `2026-09-02T16:00:00Z` (≥168h vor Fenster 1); Snapshots nur an Fenster-Decision-Closes (19 Bars).

**Tool:** `tools/phase_bt_d54_perfect_downtrend_short_entry_v1/run_forensics.py`  
**Artefakte:** `results/phase_bt_d54_perfect_downtrend_short_entry_v1/`

---

## Untersuchungsfenster (UTC)

| ID | Zeitraum | Decision-Closes |
|----|----------|-----------------|
| **window1** | 2026-09-09 16:30–17:25 | 12 Bars |
| **window2** | 2026-09-09 20:55–21:25 | 7 Bars |

---

## A) Strukturelle Momentaufnahme (Kern)

### Fenster 1 — stärkstes Muster bei **16:30**

| Kennzahl | Wert |
|----------|------|
| Close | **0.08924** |
| Nächster Upper über Close | **0.089324–0.089358** |
| **Lowest eligible** Entry-Pool (BT-D21-Gates) | **0.089324–0.089358** (Breite ~3.4×10⁻⁵, 3 Origins) |
| **Nächstes atomares Upper-Level** darüber | **0.0904–0.09049** |
| **Gap** (abs / %) | **0.001042 / 1.166%** |
| Bänder zwischen Entry und nächstem Level | **0** |
| `large_gap_gate_pass` (Union-Gap ≥0,5%) | **True** |
| Strict-Overlap-Komponente | **1** Band |
| Touch-Komponente | **3** Member, **UNRESOLVED** (D53-Logik) |
| Upper-Bands gesamt / eligible | 36 / 20 |

Ab **16:35** rutscht der globale Lowest-Eligible-Pool auf **0.08919–0.08923**; das **nächste** Band berührt die obere Kante (**Gap 0%** im atomaren Nachfolger), während das **Union-Gap-Gate** weiterhin **True** bleibt (größere Lücke weiter oben in der Landschaft). Das entspricht „Preis testet kleines Level / hängt am Stack“ — weniger „perfekt“ im Forschungs-Flag.

### Fenster 2 — stärkstes Muster bei **20:55**

| Kennzahl | Wert |
|----------|------|
| Close | **0.08698** |
| Lowest eligible Entry-Pool | **0.08703–0.087052** |
| Nächstes atomares Upper-Level | **0.08856–…** |
| **Gap** | **0.001508 / 1.732%** |
| Bänder dazwischen | **0** |
| Upper-Bands / eligible | 36 / 22 (analog strukturell dicht unten, klarer Sprung nach oben) |

Ab **21:00** wieder Touch-Stack mit **Gap 0%** zum nächsten Nachbarband; Forschungs-Flag `perfect_likely` nur noch auf dem **ersten** Bar jedes Fensters.

---

## B) Potenzielle Entry-Signale (pro Close)

Vollständige Tabelle: `candidate_entries.csv` / `structural_snapshots.csv`.

**Forschungs-Flag** `research_perfect_pattern_likely` (nicht produktiv):  
True genau bei **16:30** und **20:55** — kleiner naher Pool, **kein** Zwischenband bis zum nächsten atomaren Upper, Union-Gap ≥0,5%, Distanz Close→Entry-Unterkante &lt;1,5%.

---

## C) Trend- / EMA-Kontext (forensisch)

Quelle: EMA auf 5m-Close ab Warmup (`ema_context.csv`).

**In beiden Fenstern durchgängig:**

- Close **unter EMA200** (`close_below_ema200 = True` auf allen 19 Bars)
- **EMA9 &lt; EMA20 &lt; EMA59 &lt; EMA200** (`ema9_lt_20_lt_59_lt_200 = True`)

Das passt zum Nutzerbild „Downtrend / unter weißer Linie“, ist hier aber **nur dokumentiert**, nicht als Filter aktiviert.

---

## D) Vergleich mit BT-D50/52/53 (11.09.)

Siehe `comparison_vs_bt_d50_cases.md`.

| Merkmal | 09.09. „perfekt“ (16:30 / 20:55) | 11.09. problematisch (12:05/12:30) |
|---------|-----------------------------------|-------------------------------------|
| Nähe Entry zum Close | ~0,09–0,22% bzw. ~0,06% | 12:05-Pool weit unter Shelf; 12:30 knapp, aber falscher Kontext |
| Gap zum **nächsten atomaren** Upper | **~1,17% / ~1,73%**, 0 Zwischenbänder | Dichter Touch-Stack bis ~0,08438 (D53: 30 Member, articulation) |
| Lowest-eligible = „kleines Level am Preis“ | Ja am **Fensterstart** | Oft **globales Minimum** weit weg oder **kleinstes** Band im Stack |
| Touch-Komponente | Klein (3–5), am Anfang großer Gap | Sehr groß, UNRESOLVED |

---

## E) „Relevanter Pool“ — Datenbefund

**Gestützt** für die markierten Fensteranfänge:

1. Kleiner Upper-Pool **direkt über** dem Close (niedrigste eligible `pool_lower`).
2. **Klarer freier Raum** zum nächsten atomaren Upper (**≥1%**, Gate 0,5% erfüllt).
3. **Kein** chaotischer Zwischen-Stack (`upper_bands_between_entry_and_next = 0`).
4. **Nicht** gestützt als dauerhaftes Regelbild innerhalb des Fensters: sobald der Preis das Level testet, kollabiert die atomare Gap-Metrik auf **0%** zum Touch-Nachbarn — während Union-Gap weiter „groß“ sein kann.

---

## F) Keine Regelsetzung

Es wurde **nichts** an Entry, SL, Composite-Policy, EMA-Filter oder Execution geändert.

---

## Deliverables

| Datei | Inhalt |
|-------|--------|
| `summary.md` | Kurzüberblick |
| `window1_2026_09_09_1630_1725.json` | Snapshots Fenster 1 |
| `window2_2026_09_09_2055_2125.json` | Snapshots Fenster 2 |
| `candidate_entries.csv` | Alle Kandidaten pro Close |
| `structural_snapshots.csv` | Vollständige Strukturmetriken |
| `gap_analysis.csv` | Gap-Fokus |
| `ema_context.csv` | EMA-Lage |
| `comparison_vs_bt_d50_cases.md` | Gegenüberstellung 11.09. |

**Re-run:**

```bash
python3 tools/phase_bt_d54_perfect_downtrend_short_entry_v1/run_forensics.py
```

---

## Schlusssektion

### Gemeinsame Merkmale beider „perfekten“ Beispiele (Fensteranfang)

- Downtrend-EMA-Bild: Preis unter EMA200, fallende EMA-Reihenfolge.
- **Kleiner** lowest-eligible Upper-Pool **nah** am Close.
- **Großer** atomarer Abstand (~**1,2–1,7%**) zum nächsten Upper-Level, **ohne** Bänder dazwischen.
- Union-Gap-Gate (0,5%) erfüllt.

### Optional / kontextabhängig

- Touch-Kette mit wenigen Bändern (3–5) — im Gegensatz zum 11.09.-Monster-Stack.
- Exakte EMA200-Unterlage in **allen** Bars der Fenster (nicht nur am Entry-Moment).

### Plausible zukünftige Regeln (noch nicht implementiert)

- Zusätzlich zum Lowest-Eligible: **Mindest-Gap** zum nächsten **atomaren** Upper (nicht nur Union-Gap).
- „Relevanz“: Entry-Pool muss **Nähe** zum Close haben (z. B. max. Abstand in %).
- Optional später: EMA200-Filter nur wenn in Replay bestätigt.

### Unklar / offen

- Welcher **exakte** Bar im Fenster der Nutzer als Entry meint (wir analysieren das ganze Fenster; stärkstes Muster = erster Close).
- Ob nach dem ersten Touch **Gap 0%** zum Nachbarn das Setup beendet oder noch „gültig“ ist.
- `pool_age_bars` in dieser Phase nur innerhalb der Fenster-Replay-Schleife (Pilot: `pool_min_age_bars=0`).

---

## Vorschlag nächste Phase (noch nicht umsetzen)

**BT-D55 — Read-only Regel-Definition + Replay-Pilot:**  
Formale Spezifikation eines „Perfect Short Shelf“-Profils (atomarer Gap + Proximity + optional EMA-Kontext) und **Replay nur auf 2026-09-09** gegen BT-D21-Selektion — ohne BT-D46 zu ändern. Alternativ: Short-vs-Long-Kontext auf denselben Fenstern.

---

## Verdict

**`PERFECT_SHORT_PATTERN_FORENSICS_PASS`** — Beide Nutzerfenster liefern am **jeweiligen Start-Close** ein konsistentes strukturelles Bild (kleiner naher Pool + großer freier Upper-Gap + Downtrend-EMA), klar unterscheidbar von den 11.09.-Fällen mit dense Touch-Stack und falscher/globaler Pool-Wahl.
