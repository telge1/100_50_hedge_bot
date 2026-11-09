# L1 V3AJ — Master-Audit: Aktuelle LONG-Forschungsstrategie (READ-ONLY)

**Stand:** 2026-11-09  
**Scope:** Rekonstruktion aus vorhandenem Code und Dokumentation (V3U → V3AH). **Keine** Produktionsfreigabe.

---

# Teil A — Verständliche Erklärung

## Was ist „aktiv“?

Es gibt **keine eine verbindliche Live-Strategie**. Es gibt ein **gemeinsames L1-Pilot-Gerüst** (`run_l1_pilot.py`, `pilot_engine.py`) und **mehrere isolierte Forschungsläufe**, die unterschiedliche **TP-Regeln** und **Exits** kombinieren:

| Forschungsmodul | Rolle | Status |
|-----------------|-------|--------|
| **V3U** (Pilot) | Entry + SL/TP/Kanal-Exit, **ohne** Supportbruch | RESEARCH / Basis |
| **V3Y** | Wie Pilot, plus **Supportlinien-Bruch-Exit** | RESEARCH |
| **V3AB** | V3Y auf **neuem Zeichnungs-Snapshot** | RESEARCH (aktuelle Linien) |
| **V3W** | Gleicher Entry-Kontext, nur **TP-Modell-Vergleich** (TP1–TP5, M1–M5) | RESEARCH_ONLY |
| **V3AE** | V3Y + **V3W TP2** („first substantial“) + **Fallback max D2** | RESEARCH_ONLY |
| **V3AH** | V3Y + **CAP×Dominanz-TP** (BLOCK/ALLOW), **kein** Fern-Fallback | RESEARCH_ONLY |

**`run_v3ah.py`** führt **kein** neues Entry-System aus. Es:

1. lädt den **eingefrorenen V3AB-Kontext** (Touch → C1 → Pool-SSOT → D2-Gate → `pick_tp` als Baseline),
2. **ersetzt nur die Pool-TP-Zeilen** pro Szenario (`v3ah_tp.apply_realistic_pool_tp`),
3. simuliert Trades mit **`execute_variant_v3y`** (Supportbruch-Exit unverändert).

## Ablauf von Signal bis Exit (gemeinsam V3Y/V3AB/V3AE/V3AH)

1. **Linie berühren:** Eine manuelle **Trend-Supportlinie** (Diagonal, `drawing_type=trend`) wird in der **0,20-%‑Nähezone** berührt (klassifiziert als TOUCH / NEAR). Horizontale Linien lösen **keinen** Long aus.
2. **Re-Arming:** Nach einem Touch-Event ist die Linie „verbraucht“, bis eine Kerze **mindestens 0,30 % über** dem Linienpreis **schließt** — dann darf ein neuer Touch zählen.
3. **C1-Bestätigung:** Die **nächste** Kerze nach dem ersten Touch-Bar muss **bullisch** sein (`close > open`) und **über** dem Linienpreis **schließen**.
4. **Entry:** Fill am **Open der übernächsten** 5m-Kerze (Next-Bar-Open nach C1).
5. **Pool-Snapshot:** ACTIVE **5m PD1**-Members zum **Bar-Close** der C1-Bestätigungskerze (nicht Entry-Zeitpunkt).
6. **D2-Gate (Entry):** Aus SSOT werden kanonische **Exact Unique-Origin D2**-Upper-Bänder berechnet; mindestens **ein** Band mit Unterkante **> Entry** und Abstand **≥ 0,50 %**. Das sind **einzelne D2-Segmente**, keine Merge-Zonen.
7. **Varianten-Filter A/B/C** (siehe unten) — zusätzlich zu Touch/C1.
8. **TP festlegen** — modellspezifisch (V3AB: fernes D2-Maximum; V3AH: CAP+Dominanz; …).
9. **SL:** Fix **1,20 %** unter Entry-Preis, **einmal** beim Entry gesetzt, nicht nachgezogen.
10. **Exit-Simulation (V3Y+):** Bar für Bar: **SL** (Low), **Pool-TP** (High), optional **Kanal-Obergrenze**; parallel **Überwachung der Entry-Supportlinie** mit 0,20-%-Bruchbestätigung → Exit am **Open der Folgekerze**.

**Maximal ein offener Long:** Chronologisch sortierte Entries; wenn noch ein Trade läuft, wird der nächste mit `GLOBAL_MAX_ONE_OPEN_LONG` abgelehnt. Dasselbe Touch-Event-ID wird nur **einmal** gehandelt.

### Varianten A, B, C (V3E-Labels in V3AB/V3AH)

| Variante | Code | Entry-Filter (zusätzlich) |
|----------|------|-------------------------|
| **A** | `D2` | Kanalraum **≥ 1,50 %** (wenn Oberkanal auflösbar); mindestens **1 qualifiziertes D2-Band** (`_d2_gate_ok`) |
| **B** | `V3E-B` | Wie A + **nächstes D2-Band ≤ 2,50 %** über Entry (`d2_nearness_filter`; im Independent-Modus: erstes Band oberhalb Entry) |
| **C** | `V3E-C` | Wie B + mindestens **1 D2-Band im ersten Viertel** Entry→Oberkanal (`d2_early_staggering`) |

**V3AH** nutzt dieselben drei Varianten; TP wird getrennt per CAP-Szenario überschrieben.

**Nicht aktiv in V3AB/V3AH:** `pool_landscape` (Micro „substantial“, 2,2 %-Regel), `d2_research_filter` (D2+R), C2–C4-Bestätigung, Buy-Limit-Continuations aus `Long_logik.md`.

### PD1-D2 in Klartext

- **ACTIVE** = Katalog-Member-Liste @ 5m-Publish-Close, kausal ohne `include_remaining`.
- **D2** = Überlappung gleicher **Origin-IDs** auf der Preisachse (Chart-identisch via `pd1_exact_d2_adapter` / `computeExactBandsForSide`).
- **Merge-Zonen (0,15 % Gap)** und **∫Density** dienen **TP-Forschung** (V3W/V3AH), nicht dem Entry-D2-Gate.

### TP-Methoden (Kurz)

- **V3AB / Baseline:** `pick_tp` → ohne passenden Kanal-Oberrand: **höchstes** qualifiziertes D2-**Einzelband** (`max(lower_bound)`); mit Kanal: Pool nahe Kanal oder Kanallinie.
- **V3AE TP2a/b/c:** erste Merge-Zone **von unten** mit Mindest-**Breite**, **max_depth**, **origins**; sonst **Fallback max D2** (explizit in V3AE, **nicht** in V3AH).
- **V3AH:** Dominanz-Rang (∫Density, Top **20 %** oder **25 %** aller Merge-Zonen @ Entry), dann erste Zone in **Preisreihenfolge** mit Abstand **≤ CAP (3/5/6 %)** und **≥ 0,5 %**. **BLOCK:** kein Trade ohne passende Zone; **ALLOW:** Trade **ohne** Pool-TP (nur SL + Supportbruch).

### Supportbruch

Überwacht wird die **gleiche Supportlinie** wie beim Entry (`touch_drawing_id`). Bruch = Close **unter** Linienpreis am Bar-Close; Bestätigung = Folge-Close **≥ 0,20 %** unter dem Bruch-Close; dann Exit **nächster Open**. Reclaim (Close wieder über Linie oder +0,20 % Rally vom Bruch-Close) → zurück in Überwachung.

**Priorität** (eine Kerze): SL und TP/Kanal gleichzeitig → `AMBIGUOUS`; sonst SL vor Profit-Targets; Supportbruch-Engine läuft **ohne** TP, wenn `pool_tp` None und Kanal fehlt — aber **ALLOW** hat Supportlinie, also Trade möglich.

---

## Drei Beispiele

### 1) CAP6 @ 2026-08-26 20:30, 0.08491 (V3AB-Snapshot)

- **15** Merge-Zonen @ Entry; Violett **0.08929–0.089624**: ∫Density-Rang **3**, Abstand **5,16 %**.
- **CAP3 / CAP5:** Keine dominante Zone mit Abstand ≤ 3 % bzw. 5 % → `NO_QUALIFIED_TP` (nahe Zonen z. B. 0.08536 haben Rang 9, nicht D20-Top-3).
- **CAP6:** Erste preislich passende Zone in D20 → **0.08929**, Exit **TP** 2026-08-27 08:45 (+5,16 %). Baseline: **0.09496**, SBE +2,89 %.

### 2) Supportbruch 2026-08-31 00:25 @ 0.08233 (V3AA / älterer Snapshot)

**Hinweis:** Trade gehört zum **V3Y-Frozen-Snapshot** `d05d2186…`, **nicht** zum aktuellen V3AB-Snapshot `b0718d82…`. Entry Open **00:25** nach C1 **00:20**; dokumentierter V3Y-Supportbruch-Exit **~0.08160** (siehe `support_break_exit_v3y/L1_V3AA_ORIGINAL_SUPPORT_TRADE_AUDIT.md`).

### 3) BLOCK vs ALLOW (Referenz `te-d-c0791ea0c65e-0003`, CAP3 D20)

- **Entry-Bedingungen** (Touch, C1, D2-Gate, Variante B) sind **identisch** erfüllt.
- **CAP3_D20_BLOCK:** `apply_realistic_pool_tp` setzt `_pick_ok=False`, `reject_reason=NO_QUALIFIED_TP` → **kein Trade**.
- **CAP3_D20_ALLOW:** `_pool_tp=None`, `_pick_ok=True` → **Trade** wie Baseline ohne Fern-TP → **SBE +2,89 %**.

---

# Teil B — Technische Codeprüfung

## 1) `run_v3ah.py` — Aufrufkette

| Schritt | Quelle |
|---------|--------|
| `main()` | `realistic_pool_tp_v3ah/run_v3ah.py` |
| Kontext | `v3ah_context.build_context_from_snapshot` → `v3ab_context.build_context_from_snapshot` |
| Pending-Basis | `apply_d2_gate_to_pending`, `pe.enrich_pending_metrics`, `enrich_d2_channel_filter_fields` |
| TP-Patch | `v3ah_tp.pending_with_realistic_tp` / `apply_realistic_pool_tp` |
| Ausführung | `support_break_exit_v3y/engine.py` → `execute_variant_v3y` → `simulate_exit_v3y` |
| Varianten | `v3ah_context.VARIANTS` = `L1_V3E_VARIANT_A/B/C` (`config.py` L98–113) |

**Nicht importiert/aktiv in V3AH:** `pool_landscape.analyze_upper_landscape`, `resolve_tp2` (V3AE), `pick_tp` nur in Baseline-Pending vor Patch.

## 2) Long-Entry — Codeanker

| Thema | Implementierung |
|-------|-----------------|
| Erlaubte Linien | `assess_long_touch_eligibility` — `run_l1_pilot.py` ~291–353; V3AB: `LINE_SELECTION_INDEPENDENT` |
| Touch-Toleranz | `TOUCH_ZONE_MAX_DIST_PCT = 0.20` (`config.py` L11); Klassifikation via `classify_candle_interval` |
| Re-Arming 0,30 % | `TOUCH_RESET_ABOVE_LINE_PCT = 0.30` (`run_l1_pilot.py` L49, `price_reset_above_line` L168–172) |
| Touch-Events | `build_touch_events` L184–218 |
| C1 | `c1_confirm_indices` L356–370: Bar `i+1` bullish + close > line; Entry-Index `i+2` |
| Entry-Preis/SL | `build_pilot_context` L797–802: `entry = open[entry_idx]`; `sl = entry * (1 - SL_PCT/100)`; `SL_PCT=1.20` L44 |
| Pool-SSOT | `pools_at_confirm_open` → `load_historical_pool_state_at_close` L598+; `pool_d2_metrics.apply_d2_gate_to_pending` |
| D2 qualify | `qualify_upper_d2_bands` — `MIN_POOL_DIST_PCT=0.50`, depth via `D2_MIN_DEPTH=2` |
| `_pick_ok` | `pool_d2_metrics` L133–137: Gate + `pick_tp` liefert Pool oder Kanal |
| 1 offener Long | `engine.py` L91–96, L120–125; `pilot_engine.py` L172–177 |

### Varianten-Filter — `pilot_engine.apply_variant_entry_filters` (L34–81)

| Flag | Wirkung |
|------|---------|
| `channel_room` | `MIN_CHANNEL_ROOM_PCT=1.50`; Independent: Kanal optional |
| `d2_canonical` | `_d2_gate_ok` required |
| `d2_nearness_filter` | `_d2_nearness_ok`; Grund: `REJECTED_FIRST_D2_TOO_FAR` |
| `d2_early_staggering` | `_d2_stagger_ok`; `MIN_D2_BANDS_IN_FIRST_CHANNEL_QUARTER=1` |
| `pool_landscape` | `_pool_landscape_pass` — **nur** `L1_V3_VARIANT_B` (`B+POOL`), nicht V3E |

**Nearness-Berechnung:** `analyze_d2_channel_filters` `pool_d2_metrics.py` L211–253 — `first_dist > MAX_DISTANCE_TO_FIRST_D2_UPPER_PCT (2.50)` → Fail.

## 3) D2-Landschaft

| Stufe | Modul |
|-------|--------|
| SSOT | `research_charts/historical_pool_state_v1.load_historical_pool_state_at_close` |
| D2-Sweep | `pd1_exact_d2_adapter.compute_exact_bands_for_side` (Parität `pd1_exact_density_trp_adapter.js`) |
| Entry-Liste | `qualify_upper_d2_bands` → einzelne Bänder |
| Merge / ∫D | `tp_research_v3w/zone_features.merge_qualified_zones`, `zone_density._integrated_density` |
| D20/D25 | `ceil(n×0.20)` / `ceil(n×0.25)` stärkste ∫Density-Ränge (`zone_density.py` L71–76) |

## 4) Take-Profit — Code

### A) `pick_tp` — `run_l1_pilot.py` L436–518

- Pool-only: `max(qualified, key=lower_bound)` → `POOL_BOTTOM_NO_CLEAR_CHANNEL`.
- Kanal: `CHANNEL_NEAR_PCT=2.0` (% Abstand Kanal zu Pool-Unterkante); else `CHANNEL_UPPER_LINE`.
- Kein Paar-Lock Unter-/Oberkanal: `channel_upper_at` wählt Ober-Trend **ohne** Touch-Linie (`exclude_drawing_ids` L809–813).

### B) V3AE — `first_strong_upper_tp/v3ae_tp.py`

- `resolve_tp2` (`tp_targets.py` L99–135): erste Merge-Zone mit `dist≥0.5%`, `width≥`, `depth≥`, `origins≥` (Grid TP2a/b/c in `config_v3w.py` L21–25).
- **Fallback:** `resolve_tp4` = max D2 (`v3ae_tp.py` L41–42).

### C) V3AH — `zone_density.resolve_realistic_tp` L99–159, `v3ah_tp.py` L23–90

| Parameter | Wert |
|-----------|------|
| Merge-Gap | 0,15 % |
| Mindest-Abstand | 0,50 % |
| CAP | 3 / 5 / 6 % |
| Dominanz | D20: Top 20 % ∫Density; D25: Top 25 % |
| Auswahl | Aufsteigend **nach Preis**, erste Zone die CAP+Dominanz erfüllt |
| TP-Preis | **Unterkante** der Merge-Zone, **statisch** |
| NO_QUALIFIED | `res.ok=False` |
| BLOCK | `_pick_ok=False`, `reject_reason=NO_QUALIFIED_TP` |
| ALLOW | `_pool_tp=None`, `_pick_ok=True` (Support-Sim erlaubt, `support_sim.py` L54–55) |
| Fern-Fallback | **Nein** (im Gegensatz zu V3AE) |

## 5) Stop-Loss

- **Nur** fixer Prozentsatz: `SL_PCT = 1.20` (`run_l1_pilot.py` L44, L798).
- Kein poolbasierter/dynamischer SL im Codepfad V3Y/V3AH.
- Simulation: `low <= sl` (`simulate_exit_v3y` L70–82); gleichzeitig TP → `AMBIGUOUS`.

## 6) Supportbruch — `support_sim.py` L40–121

1. `support_line` = Linie aus `touch_drawing_id` (`engine.py` L14–16).
2. Linienpreis: `line_price_at_close` = Trend/Hline @ Bar-Ende (L34–37).
3. Zustände `MONITOR` / `BREAK_PENDING` (L57–117).
4. Bruch: `close < lp` (L101–104).
5. Bestätigung: `close <= break_close * (1 - CONFIRM_DELTA)`; `CONFIRM_DELTA` aus `v3y_config.CONFIRM_DELTA_PCT=0.20` (L8–9).
6. Neutral: engeres Range → `break_close` Update (L115–117).
7. Reclaim: `close >= lp` oder `>= break_close*(1+d)` → MONITOR (L109–114).
8. Exit: `schedule_fill_at_open` → **nächste** Kerze Open (L65–67, L109).
9. Linie inaktiv → Reset MONITOR (L91–94).
10. Priorität: SL/TP/Kanal **vor** Support-State pro Bar; Supportbruch kann **verlustreich** sein (PnL am Open).

---

## 8) Strategie-Matrix

| Regel | V3U | V3Y | V3AB | V3AE | V3AH BLOCK | V3AH ALLOW |
|-------|-----|-----|------|------|------------|------------|
| Trendlinien-Touch + C1 | ja | ja | ja | ja | ja | ja |
| Touch 0,20 % / Re-Arm 0,30 % | ja | ja | ja | ja | ja | ja |
| Pool-Entry-Gate A/B/C | konfig. | konfig. | **D2 / V3E-B / V3E-C** | **D2 / V3E-B / V3E-C** | **D2 / V3E-B / V3E-C** | gleich |
| Upper-D2 @ Entry | ≥1 Band, ≥0,5 % | ja | ja | ja | ja | ja |
| Max. 2,50 % zum 1. D2 (B/C) | B/C | B/C | B/C | B/C | B/C | B/C |
| Substantielle **Merge-Zonen** @ Entry | nein | nein | nein | nein | nein | nein |
| TP-Auswahl | `pick_tp` | `pick_tp` | `pick_tp` | TP2 + **Fallback max** | CAP+Dominanz | CAP+Dominanz oder **kein** TP |
| SL 1,20 % fix | ja | ja | ja | ja | ja | ja |
| Supportbruch 0,20 % | nein | **ja** | **ja** | **ja** | **ja** | **ja** |
| Pool-TP-Fallback | — | — | max D2 | **max D2** | **nein** | **nein** |
| Max. 1 Long | ja | ja | ja | ja | ja | ja |
| Snapshot | variabel | d05d218… | **b0718d82…** | b0718d82… | b0718d82… | b0718d82… |
| RETROSPECTIVE_DRAWING | Label | Label | Label | Label | ja (Research) | ja |

**V3W:** Entry wie V3AB-Kontext; nur TP-Resolver-Studie, typisch **ohne** V3Y-Supportbruch in `run_v3w` (Pilot-`simulate_exit`).

---

## 9) Dokumentation vs. Code (Auswahl)

| Anforderung | Quelle | Code-Ist | Variante | Bewertung |
|--------------|--------|----------|----------|-----------|
| Substanzielle Upper-**Zonen** als Entry-Gate | `Long_kanal+pools.md` §4–5 | Nur **≥1 D2-Einzelband** + optional 2,5 % Nearness (B) | V3AB/V3AH | **MISSING** (Merge/substantial nicht im Entry) |
| `pool_landscape` 2,2 % / 3 substantial zones | `config.py` L23–24, `l1_v3_pool_landscape.md` | Nur `PilotVariant` **B+POOL** | alt V3 | **RESEARCH_ONLY** |
| TP „naher substanzieller Pool“ | Nutzer-Intention / V3AD | V3AB: **max** D2; V3AE: TP2; V3AH: CAP+Dominanz | je Modul | **CONTRADICTORY** zwischen Modulen |
| V3AE Fern-Fallback | `v3ae_tp.py` | `resolve_tp4` | V3AE | **IMPLEMENTED** |
| V3AH kein Fallback | V3AH-Auftrag | Kein `resolve_tp4` | V3AH | **MATCH** |
| C2–C4 Bestätigung | `Long_kanal+pools.md` §5 | Nur **C1** | alle L1 Pilot | **MISSING** |
| Buy-Limit Continuation | `Long_logik.md` | Nicht in L1 Pilot | separat | **MISSING** |
| Kanonische D2-Density | Spez/L3AG | `pd1_exact_d2_adapter` | V3+ | **MATCH** |
| Near Upper-Pool ≤2,5 % | `MAX_DISTANCE_TO_FIRST_D2_UPPER_PCT` | `d2_nearness_filter` nur **V3E-B/C** | B/C | **MATCH** (nicht in A) |

---

## 10) Pflichtantworten

1. **Entry A/B/C:** A = D2-Gate + Kanalraum; B = + D2 ≤2,5 %; C = + ≥1 D2 in Q1 des Kanals. Touch/C1/global für alle gleich.
2. **Block ohne Upper-Pool:** Wenn `_d2_gate_ok` false → `D2_GATE_NO_QUALIFIED_UPPER_BAND` (oder SSOT-Fehler). **Nicht** wegen fehlender Merge-„Substanz“.
3. **Naher Upper-Pool 2,5 %:** **Aktiv nur Variante B und C** (`d2_nearness_filter`). Variante A: **nein**.
4. **CAP6-D20 BLOCK TP:** `resolve_realistic_tp` — Rang Top 20 % ∫Density, erste Zone mit 0,5–6 % → Zone-Unterkante (Referenz: 0.08929).
5. **SL / Supportbruch:** SL 1,20 % fix; Supportbruch 0,20 % Bestätigung, Exit nächster Open; Priorität SL/TP vor SBE-State-Machine.
6. **Fehlende beschlossene Regeln:** Substantielle Zonen @ Entry, C2–C4, Buy-Limit-Entry, einheitliche „Nutzer-TP“-Regel über alle Module.
7. **Widersprüche:** V3AB **max D2** vs. V3AE/V3AH **nah/dominant**; V3AE **Fallback** vs. V3AH **kein Fallback**; Entry-Gate **Segment** vs. TP **Merge-Zone**.
8. **Vor verbindlicher Version entscheiden:** Produktive TP-Regel; BLOCK vs ALLOW bei fehlendem TP; ob B/C Nearness/Staggering produktiv; ob Supportbruch Pflicht; ob Kanal-TP weiterhin parallel; Freigabe vs. RETROSPECTIVE_DRAWINGS.

---

## Referenzen (Pfade)

- Entry-Kern: `manual_channel_pool_long_v1/run_l1_pilot.py`, `pilot_engine.py`
- D2: `pool_d2_metrics.py`, `pd1_exact_d2_adapter.py`
- Exits: `support_break_exit_v3y/support_sim.py`, `support_break_exit_v3y/engine.py`
- V3AB: `support_break_exit_v3ab/v3ab_context.py`, `run_v3ab.py`
- V3AE: `first_strong_upper_tp_v3ae/v3ae_tp.py`
- V3AH: `realistic_pool_tp_v3ah/run_v3ah.py`, `zone_density.py`, `v3ah_tp.py`
- V3W TP: `tp_research_v3w/tp_targets.py`, `zone_features.py`
- Spez (unverbindlich): `pools+ob+delta_bot/Long_kanal+pools.md`, `Long_logik.md`

**STOP** — Nur Analyse; keine Code-, Parameter- oder Backtest-Änderungen.
