# L1 V3AN — Ergänzung: Parametrisierte Watcher-Konfiguration und Replay-Prüfung

**Scope:** ausschließlich `touch_watcher_research_v3an/`. V3AK, P17, `run_l1_pilot`, produktiver Backtester und Research-Charts **unverändert**.

**Snapshot:** DOGEUSDT 5m, SHA256 `b0718d82a94e8ad12d27d570c0cd7ce97cf8088a2db21c4c92830cd80fc600cb` · Linie `d-ea081434c5d3` · Replay-Fenster **30.08.2026 09:15–12:00 UTC**.

---

## 1. Config-Schema (`touch_watcher/v1`)

| Feld | Typ | Validierung | Bedeutung |
|---|---|---|---|
| `schema_version` | string | muss `touch_watcher/v1` sein | Versionsanker |
| `touch_tolerance_pct` | float | `0 … 0.20` | `0` = nur physischer Kontakt; `0.20` = Legacy-Band (TOUCH/NEAR_0_10/NEAR_0_20); sonst `dist ≤ tol` oder Touch |
| `confirmation_mode` | enum | `A1`, `A2`, `A3`, `LEGACY_C1`, `B_RECLAIM` | Bestätigungs- bzw. Reclaim-Pfad |
| `confirmation_min_gain_pct` | float | `≥ 0` | Nur **A3**: Mindest-Steigerung Close vs. Vorkerze (%) |
| `watch_window_bars` | int | `≥ 1` | Bars nach Touch bis `EXPIRED` |
| `reclaim_confirmation_bars` | int | `≥ 2` | **B_RECLAIM**: aufeinanderfolgende Schlüsse **über** der Linie |
| `require_approach_from_above` | bool | — | Touch nur mit Vorkerze-Close über Linie (+ε) |
| `approach_epsilon_pct` | float | — | ε für Approach-Check |
| `sl_pct` | float | `> 0` | SL-Abstand für Entry-Kandidat (Forschung) |
| `preset_id` | string | optional | Lesbare Matrix-ID in CSV |

**Dateien**

- Einzel-Run: `configs/touch_watcher_example.yaml`
- Matrix-Dokumentation: `configs/v3an_preset_matrix_v1.yaml`
- Code: `watcher_config.py` (`TouchWatcherConfig`, `load_touch_watcher_config`, `preset_matrix_v1()`)
- Replay: `watcher_fsm.replay_watcher_from_config()` — **keine stillen Fallbacks** beim YAML-Load (alle Felder required)

**Touch-Raster (Forschung):** `0.00`, `0.01`, `0.02`, `0.03`, `0.05`, `0.20` % über `touch_tolerance_pct` (0.20 = Legacy-Klassen, nicht nur Distanz).

---

## 2. Reproduzierbarkeit der 45 V3AN-Konfigurationen

- Preset-Matrix: `preset_matrix_v1()` = 3 Touch × 5 Confirm × 3 Fenster = **45** Zeilen (`preset_id` z. B. `NEAR_003+A2+W12`).
- Eingefrorene Referenz: `l1_v3an_aug30_variant_summary_v1frozen.csv`
- Aktueller Lauf: `l1_v3an_aug30_variant_summary.csv` (zusätzliche Spalten: `touch_tolerance_pct`, `confirmation_min_gain_pct`, `reclaim_confirmation_bars`, `schema_version`)
- Validierung: `run_v3an_aug30_replay.py` → **`Regression OK: 45 configs match`** gegen Frozen (Entry-Zeit, Preis, Terminal, Touch/Confirm-UTC).

---

## 3. Szenario B — Warum B_RECLAIM mit W12 **expired** (30.08.2026)

### NEAR_003 (`touch_tolerance_pct = 0.03`) · W6 / W12 / W24

| Fenster | Touch UTC | Terminal | Erster `break_close_below` | Reclaim-Events | Entry |
|---|---|---|---|---|---|
| W6 | 09:25 | expired @ 10:00 | — | 0 | — |
| W12 | 09:25 | expired @ 10:30 | — | 0 | — |
| W24 | 09:25 | expired @ 11:30 | — | 0 | — |

**Kernerklärung:** Nach Touch **09:25** startet der Watcher in **`WATCHING_ABOVE`** (Close **0.08438** > Linie **~0.084329**). Im gesamten Beobachtungsfenster kommt **kein** Übergang `WATCHING_ABOVE → WATCHING_BELOW` mit Grund `break_close_below` vor.

Damit gilt:

- Es gibt **keine** Kerze mit **erstem bestätigten Schluss unter der Trendlinie** im B-Sinne — der Break-Schritt der B-FSM wird **nie** ausgelöst.
- Folge: **kein** `reclaim_close_*`, **keine** zwei aufeinanderfolgenden Schlüsse über der Linie im Reclaim-Pfad.
- **`EXPIRED`** entsteht rein durch **`watch_window`**, nicht durch gescheiterte Rückeroberung:
  - W12: letzte Transition **10:30 UTC**, Grund `watch_window>12` (Close **0.08475** weiter **über** Linie **~0.084464**).

**Hat W12 die Rückeroberung ausgeschlossen?** Nein — es gab **keinen Break**; W12 hat nur die Wartezeit auf einen Break/Reclaim beendet, ohne dass B überhaupt in `WATCHING_BELOW`/`RECLAIM_PENDING` war.

Vollständige Traces: `l1_v3an_b_reclaim_transitions_full.csv`, Summary: `l1_v3an_b_reclaim_forensic_summary.csv`.

### PHYSICAL_ONLY · W6 / W12 / W24

Touch jeweils **09:40** (physikalischer Kontakt). Gleiches Bild: durchgehend **`WATCHING_ABOVE`**, kein Break, **expired** nach W6/W12/W24 (10:15 / 10:45 / 11:45 UTC).

---

## 4. A1 / A2 @ 09:35 — Passt das zur gewünschten Kette?

**NEAR_003 + A1/A2 + W12** (identischer Pfad für A2 bei diesem Tag):

| Schritt | UTC | Inhalt |
|---|---|---|
| Touch | **09:25** | FSM: `IDLE → WATCHING_ABOVE`; Close über Linie (**Support hält am Schluss**); Kerze **bearish** (Open > Close) |
| Bestätigung | **09:30** | `confirm_A1`: **bullish** und Close **0.08452** > Linie; A2 zusätzlich Close > Vorkerze **0.08438** |
| Entry | **09:35** | Open nächster Kerze (**0.08452**) — `ENTRY_SCHEDULED` auf Bestätigungskerze |

**Interpretation:** Touch (Nähe/NEAR) → **Schluss bleibt über der Linie** → bullische Bestätigungskerze → Entry **+1 Bar**. Das entspricht der gewünschten A-Kette.

**Abweichung zur strengen V3AK-C1-Logik:** Der Watcher bestätigt in **`WATCHING_ABOVE`** auf der **ersten** passenden Kerze innerhalb des Fensters, nicht zwingend nur auf der unmittelbaren Folgekerze nach Touch (hier zufällig **+1 Bar**). Touch-Kerze selbst muss **nicht** bullisch sein.

Kerzenkontext: `l1_v3an_a1_touch_window_candles.csv` · Transition: `l1_v3an_a_forensic_NEAR_003_A1_W12.csv`.

**A3:** `confirmation_min_gain_pct` ist konfigurierbar (Default in Matrix weiterhin **0.20** für Vergleichbarkeit mit V1-Replay, nicht hardcoded im Config-Pfad).

---

## 5. Artefakte dieser Ergänzung

| Artefakt | Zweck |
|---|---|
| `watcher_config.py` | Schema, Validierung, Matrix, YAML-Load |
| `watcher_fsm.py` | `touch_on_bar_tolerance`, parametrisiertes A3, `replay_watcher_from_config` |
| `run_v3an_aug30_replay.py` | Config-gestütztes Replay + Regression |
| `run_v3an_b_reclaim_forensic.py` | B- und A-Forensik-CSVs |
| `test_watcher_config.py` | Matrix-Größe, YAML, Frozen-CSV |

---

## 6. Vorschlag: isolierter historischer Vergleichstest

**Ziel:** Parametergitter automatisiert, ohne Pilot/Charts.

1. **Runner:** neues Skript `run_v3an_historical_batch.py` (noch nicht angebunden — Empfehlung):
   - Eingabe: Manifest mit Datumsintervallen + Line-IDs + YAML (`touch_watcher` oder `preset_matrix_v1`).
   - Ausgabe: eine Zeile pro `(preset_id, day, line)` mit dokumentierten Config-Feldern + `terminal`, `entry_utc`, `entry_price`, `n_transitions`.
2. **Touch-Erweiterung:** zusätzliche Läufe mit `touch_tolerance_pct ∈ {0.01, 0.02, 0.05}` und A3-Sweep `confirmation_min_gain_pct ∈ {0.05, 0.10, 0.20}`.
3. **Abgleich:** optionaler Join gegen V3AK-Referenz-CSV (nur Read), Kennzahl „Δt Entry vs. Produktion“.
4. **Guardrails:** SHA-gepinntes Snapshot wie bisher; Abbruch bei Regression-Mismatch gegen bekannte Golden Days (30.08.2026).

---

**STOP** — Ergänzung abgeschlossen; keine Integration in produktive Pfade.
