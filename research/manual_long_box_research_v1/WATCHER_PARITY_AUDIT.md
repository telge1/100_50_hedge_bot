# Manual Long Box V1 — Watcher-Parität vs. L1 Trendline-Touch (READ-ONLY)

**Datum:** 2026-11-10  
**Mandat:** Keine Codeänderungen; reine Code- und Replay-Analyse.  
**Fragestellung:** Verwendet Manual Long Box V1 **dieselbe** Watcher- und A2-Entry-Logik wie die L1-Trendlinienforschung — nur mit horizontalem Entry statt Linie?

---

## Kurzantwort

**Nein.** Manual Long Box V1 verwendet **nicht** dieselbe Watcher-Implementierung. Es gibt:

- eine **logische Nachbildung** der Funktion `confirm_a2` (identischer Ausdruck, **separate Kopie** in `l1_confirm.py`, **kein Import** aus `watcher_fsm.py`);
- einen **eigenen, vereinfachten FSM** in `simulator.py`, der **nicht** `_replay_watcher_core` aufruft.

**Replay-Parität:** **PARTIAL / FAIL** — abhängig vom Szenario (Details §7).

---

## 1. Referenz: Originale Trendlinien-Entry-Logik

### Welche Version ist die Referenz?

| Spur | Rolle | Referenz für Trendlinien-Entry |
|------|--------|--------------------------------|
| **L1 V3AN** | `touch_watcher_research_v3an/watcher_fsm.py` | **Kanonische Watcher-FSM** (`_replay_watcher_core`, `replay_watcher_from_config`) |
| **L1 V3AO** | `watcher_integration_v3ao/v3ao_context.py` | **Aktuelle Integrations-Default-Config** für Watcher-Pending (`DEFAULT_WATCHER_CONFIG`: **A2**, **12 Bars**, **0,03 %** Touch-Toleranz) |
| **L1 V3AK** | `substantial_entry_gate_v3ak/` | **Downstream** „substantial entry“ / ältere **LEGACY_C1**-Spur — **nicht** identisch mit V3AO-Default-A2; separater Research-Pfad |
| **L1 A2 / A3 / B** | `watcher_fsm.py` | Modi innerhalb **derselben** FSM (`confirm_a2`, `confirm_a3`, `B_RECLAIM`-Zweig) |

**Für den Vergleich „bestehende Trendlinienstrategie mit A2 + 12-Bar-Watcher“:**  
Referenz = **`watcher_fsm._replay_watcher_core`** mit Config wie **`v3ao_context.DEFAULT_WATCHER_CONFIG`** (A2, W12, 0,03 %, `require_approach_from_above=True` per `TouchWatcherConfig`-Default).

Dokumentation: `touch_watcher_research_v3an/L1_V3AN_CONFIG_AND_B_RECLAIM_SUPPLEMENT.md` (45-Config-Regression, Default-Matrix).

---

## 2. Original: Regeln (mit Datei / Funktion / Zeilen)

### Touch-Erkennung

- **Datei:** `spread_recovery_hedge_short_dev/dashboard/research/manual_line_touch_probe_v1/line_geometry.py`
- **Funktion:** `classify_candle_interval` (ca. Z. 139–185)
- **Logik:** Linienpreisband über `[open, open+5m)`; **Interval-Overlap** Kerze ↔ Linienband; Toleranz über Distanz-% (`distance_pct_at_open`, `classify_distance`).
- **Diagonal:** zeitabhängiges `line.price_at(t)`; **hline:** konstantes Niveau.

- **Datei:** `…/touch_watcher_research_v3an/watcher_fsm.py`
- **Funktion:** `touch_on_bar_tolerance` (Z. 89–103) → ruft `classify_candle_interval`.

**Default-Toleranz V3AO:** `touch_tolerance_pct=0.03` (`v3ao_context.py` Z. 31–37).  
**Legacy 0,20 %:** Klassen TOUCH/NEAR_0_10/NEAR_0_20.

### Approach (Pflicht im Default)

- **Funktion:** `approach_from_above` (Z. 136–149)
- **Regel:** Vorkerze-`close` > Linienpreis(Vorkerze) + `approach_epsilon_pct` (Default **0,05 %**)
- **Anwendung:** `_replay_watcher_core` Z. 306–323 — Touch verworfen wenn `require_approach_from_above` (Default **True**, `watcher_config.py` Z. 32).

### Wann Watcher aktiv / Touch-Kerze

- **Z. 303–341:** Im Zustand `IDLE`, bei gültigem Touch:
  - `touch_idx = i` (Index der **abgeschlossenen** 5m-Kerze `i`)
  - `bars_since_touch = 0`
  - Zustand `WATCHING_ABOVE` wenn `close >= line`, sonst `WATCHING_BELOW`
  - **`continue`** — **keine** Bestätigung auf der Touch-Kerze.

### Watcher-Dauer (12 Bars)

- **Z. 344–360:** Ab **nächster** Kerze: `bars_since_touch += 1`; bei `> watch_window` (12) → `EXPIRED`, Rückgabe.
- **Zählweise:** Bis zu **12 Kerzen nach** der Touch-Kerze mit Bestätigungsversuchen; Touch-Kerze zählt **nicht** in `bars_since_touch`.

### A2-Bestätigung

- **Funktion:** `confirm_a2` (Z. 114–115): `close > lp` **und** `close > prev_close` (**kein** explizites `bullish()`).
- **Z. 362–405:** Bestätigung nur in **`WATCHING_ABOVE`** für A2 (nicht in `WATCHING_BELOW`).
- Bei `close < lp` während WATCH: Wechsel zu `WATCHING_BELOW` (Z. 363–378), **kein** A2-Confirm dort.
- Erfolg → `pending_entry_idx = i + 1` (Fill ab **nächster** Kerze Open).

### Fill

- **Z. 274–296:** Wenn `i >= pending_entry_idx`: Entry = `open` Kerze `i`; SL = `entry * (1 - sl_pct/100)` (**config**, Default 1,20 %), **nicht** manuelle Box-SL.

### Abbruch / Reset / Mehrfach-Touch

- **EXPIRED:** Fenster überschritten (Z. 345–360).
- **Kein** erneuter Touch im selben Lauf nach Entry wenn `one_entry_only=True` (Z. 294–296).
- **V3AO-Pipeline:** Slot-Konkurrenz mehrerer Linien separat (`run_v3ar_slot_competition.py`); pro Linie ein FSM-Lauf.

### Linien-Gültigkeit

- **Z. 267–270:** `line_px` / `line.active_for_candle` — außerhalb Anker/ohne `extend_right` → Kerze übersprungen (**kein** Touch).

---

## 3. Manual Long Box V1: Call-Chain

```
run_single_doge_v1.py
  → replay_long_boxes (simulator.py)
      → entry_level_touch (entry_touch.py)
      → evaluate_confirmation → confirm_a2 (l1_confirm.py)   # Kopie, kein Import V3AN
      → WatcherPool (watcher_pool.py)                       # leichte Zustandsmap, kein FSMState
      → box_active_at (Zeitfenster der Zeichnung)
      → validate_long_geometry_at_fill + Box-SL/TP
```

**Es gibt keinen Aufruf von** `replay_watcher_from_config`, `_replay_watcher_core`, `touch_on_bar_tolerance`, `approach_from_above`, oder `LineDef`.

---

## 4. Vergleichstabelle

| Regel | Original Trendline-Watcher (V3AN FSM + V3AO-Default) | Manual Long Box V1 | Identisch? |
|---|---|---|---|
| Touch-Erkennung | `classify_candle_interval` + Linienband | `entry_touch.entry_level_touch`: `low <= entry <= high` | **Nachbau** (hline ≈ ähnlich bei 0 % Tol); **nicht** gleicher Code |
| Touch-Toleranz | Default **0,03 %** (V3AO) | Default **0,0 %** (`simulator.py` / `WatcherPool`) | **Abweichend** |
| Approach von oben | Default **pflicht** (`require_approach_from_above=True`) | **Nicht implementiert** | **Fehlt** |
| Aktivierungszeitpunkt | Touch auf **abgeschlossener** Kerze `i` | Touch auf Kerze `i` (bar open timestamp) | **Gleich kausal** (5m close-basiert im Loop) |
| Watcher-Start | `WATCHING_ABOVE` / `WATCHING_BELOW` | `WATCHING` (+ Events TOUCH/WATCH) | **Abweichend** (kein Above/Below) |
| Watcher-Dauer | `bars_since_touch > 12` **nach** Touch-Kerze | `watch_bars_left` startet **12**, decrement **inkl. Touch-Kerze** | **Abweichend** (Zählweise) |
| Erste Bestätigungskerze | **Erst Kerze `i+1`** (Touch: `continue` Z. 341) | **Möglich auf Touch-Kerze** (gleiche Iteration Z. 121–144) | **Abweichend** |
| A2-Confirm | `confirm_a2` in `watcher_fsm.py` Z. 114–115 | **Kopie** in `l1_confirm.py` Z. 14–15 | **Logisch gleich**, **nicht identischer Code**; **A1** in Original mit `bullish()` Z. 110–111 |
| Ablaufbedingung | `EXPIRED` bei Fensterende | `EXPIRED` bei `watch_bars_left <= 0` | **Nachbau**, andere Zählung |
| Reset/Rearm | FSM `IDLE` nach Entry (one_entry_only) | Box-Phase `FILLED`/`EXPIRED`; global 1 Long | **Abweichend** |
| Mehrfache Touches | Neuer Lauf / andere Linie | `IDLE`→`WATCHING` einmal pro Box | **Vereinfacht** |
| Fill nächstes Open | `pending_entry_idx = i+1`, Fill `open` Z. 274–275 | `pending_entry = (did, i+1, …)`, Fill `open` Z. 56–58 | **Gleich** (wenn Confirm auf gleicher Kerze) |
| Offene Long-Position | Pro-Replay `one_entry_only`; V3AO-Slots separat | `WatcherPool.global_position_open` max 1 | **Analog**, andere Mechanik |
| SL/TP | `sl_pct` aus Config | **Manuelle** `stop_price`/`target_price` der Box | **Absichtlich anders** (Scope Box) |
| Zeit-Gültigkeit | `LineDef.active_for_candle` / Anker | `box_active_at(start_ts, end_ts)` | **Andere Semantik** |
| Preis vor Box-Zeitfenster | Linie kann vor Anker inaktiv sein | Kein Touch außerhalb `[start,end]` | **Unterschiedlich** |

---

## 5. Entry-Level vs. Trendlinie (geometrisch)

| Aspekt | Trendlinie | Long-Box-Entry |
|--------|------------|----------------|
| Niveau | Zeitabhängig (`price_at(t)`) | Konstant (`entry_price`) |
| Nach Touch | `lp` pro Kerze neu | Festes Level für Confirm |
| Gap/Durchbruch | Below-Line → `WATCHING_BELOW`, Confirm blockiert (A2) | Kein Below/Above-Zweig |
| Sofort-Entry | Nein (Confirm + Fill +1) | Nein beim Touch allein; **Ja möglich: Confirm auf Touch-Kerze** (Abweichung) |
| Level vor Zeichnung | Linie ggf. inactive | Touch nur wenn `start_ts <= bar_time <= end_ts` — **kein** Touch „vor Box“, aber Simulation kann Kerzen **vor** `start_ts` nicht triggern |

**Manuelle Box-Zeitkoordinaten:** `start_timestamp`/`end_timestamp` aus Zeichnung (UTC); Research-FSM filtert strikt (`entry_touch.box_active_at`).

---

## 6. Bestätigungsvarianten (A2 / A3 / B)

| Frage | Befund |
|-------|--------|
| Welche Variante in Manual Box? | Nur **A2** hardcodiert in `run_single_doge_v1.py` / `replay_long_boxes(..., confirmation_mode="A2")` |
| Exakt bestätigte A2-Implementierung? | **Nein** — Kopie in `l1_confirm.py`, nicht gebunden an V3AN-Regression |
| Mehr Zustände im Original? | **Ja:** `WATCHING_ABOVE`/`BELOW`, `RECLAIM_PENDING`, `B_RECLAIM`, `LEGACY_C1` |
| 12 Bars gleich gezählt? | **Nein** (siehe Tabelle) |
| Confirm auf Touch-Kerze im Original? | **Nein** (`continue` nach Touch) |
| Confirm auf Touch-Kerze Manual? | **Ja** (möglich) |
| Fehlende Original-Bedingungen | `approach_from_above`, `touch_tolerance` 0,03 %, Above/Below-FSM, `active_for_candle` vs. Box-Fenster |

**Keine Festlegung**, ob A2 oder A3 produktiv wird — Manual Box implementiert **nur A2-Kopie**.

---

## 7. READ-ONLY Replay-Parität (isoliert, gleiche 5m-Kerzen)

Skript: ephemeraler Python-Lauf (nicht im Repo), gleiches `entry=0.10`, horizontale `hline` = Box-Entry.

### Szenario A — Touch Bar 0, Confirm Bar 1, Fill Bar 2

Kerzen: Touch 0, A2-Confirm 1, Fill-Open 2.

| Metrik | L1 (`tol=0`, `require_approach=False`) | Manual Box (`tol=0`) |
|--------|----------------------------------------|----------------------|
| Touch-Idx | 0 | 0 (TOUCH/WATCH) |
| Confirm-Idx | 1 | 1 |
| Fill-Idx | 2 @ open 0,104 | 2 @ open 0,104 |

**Ergebnis:** **PASS** (zeitliche Signatur übereinstimmend), nur wenn Toleranz 0 % und **ohne** Approach — **entspricht nicht** V3AO-Production-Default.

### Szenario B — Touch + A2 auf derselben Kerze

| L1 | Manual Box |
|----|------------|
| **0 Entries** (Confirm erst ab Kerze nach Touch) | **CONFIRM auf Kerze 0**, Fill Kerze 1 |

**Ergebnis:** **FAIL** — Unterschied durch **Watcher-FSM**, nicht nur Geometrie.

### Szenario C — V3AO-Default (`tol=0.03`, `require_approach=True`)

Erste Kerze ohne Vorkerze: Touch **abgelehnt** (`touch_rejected_approach:NO_PRIOR_BAR`).

**Ergebnis:** **FAIL** vs. Manual (kein Approach-Filter).

### Gesamt-Replay-Parität

| Test | PASS/FAIL |
|------|-----------|
| A (idealisiert, tol=0, no approach) | **PASS** |
| B (same-bar confirm) | **FAIL** |
| C (V3AO default config) | **FAIL** |
| **Gesamt vs. behaupteter „dieselbe Logik“** | **FAIL** |

---

## 8. Bewertung: Verwendet Long Box V1 denselben Watcher?

| Kriterium | Erfüllt? |
|-----------|----------|
| Import / Aufruf `watcher_fsm` | **Nein** |
| Identischer Touch-Pfad | **Nein** |
| Identische FSM-Zustände | **Nein** |
| Identische A2-Zeile Code | **Nein** (Kopie) |
| Gleiche Default-Parameter (0,03 %, Approach, W12-Zählung) | **Nein** |
| Gleicher Fill-Timing **wenn** Confirm auf Kerze nach Touch | **Ja** (nächstes Open) |

**Fazit:** Manual Long Box V1 ist eine **vereinfachte Parallel-Implementierung** mit **A2-ähnlicher** Close-Regel, **kein** „derselbe Watcher“ im technischen Sinne.

---

## 9. Minimale Änderungen für echte Parität (nur Empfehlung — **nicht implementiert**)

1. **Touch + Watcher:** `replay_watcher_from_config` mit `LineDef` **hline** bei `entry_price` **oder** Adapter, der horizontales Level in `touch_on_bar_tolerance` speist.
2. **Config:** Gleiche `TouchWatcherConfig` wie `DEFAULT_WATCHER_CONFIG` (0,03 %, Approach, W12).
3. **Kein eigener FSM** in `simulator.py` für Entry-Pfad — nur Box-SL/TP und `box_active_at` als **zusätzliche** Hülle.
4. **Confirm:** `from watcher_fsm import confirm_a2` (ein Modul, eine Wahrheit).
5. **Same-bar-Regel** und **WATCHING_ABOVE/BELOW** automatisch mitübernehmen.
6. **Fill:** L1-SL nur für Pilot; Box behält manuelle `stop_price`/`target_price` **nach** Fill (explizit dokumentiert als einzige erlaubte Abweichung).

---

## 10. STOP

READ-ONLY-Audit abgeschlossen. **Keine** Repository-Änderungen aus diesem Auftrag.

**Referenz-Dateien (Auszug):**

- `…/touch_watcher_research_v3an/watcher_fsm.py` — `_replay_watcher_core` (Z. 239–529)
- `…/touch_watcher_research_v3an/watcher_config.py` — `TouchWatcherConfig` (Z. 23–35)
- `…/watcher_integration_v3ao/v3ao_context.py` — `DEFAULT_WATCHER_CONFIG` (Z. 31–37)
- `…/manual_line_touch_probe_v1/line_geometry.py` — Touch-Geometrie (Z. 139–185)
- `pools+ob+delta_bot/research/manual_long_box_research_v1/simulator.py` — eigener Replay-Loop (Z. 52–147)
- `pools+ob+delta_bot/research/manual_long_box_research_v1/l1_confirm.py` — A2-Kopie (Z. 14–15)
