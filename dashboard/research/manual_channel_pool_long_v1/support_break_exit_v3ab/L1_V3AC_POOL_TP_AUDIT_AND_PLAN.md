# L1 V3AC — Pool-TP vs. Supportbruch (READ-ONLY Audit & Plan)

**Auftrag:** Analyse und technischer Plan nur. Keine Implementierung, keine Strategieänderung, kein neuer Backtest.

**Referenz:** L1 V3AB-Backtest · Zeichnungs-Snapshot SHA256 `b0718d82a94e8ad12d27d570c0cd7ce97cf8088a2db21c4c92830cd80fc600cb` (eingefroren, nicht nachgeladen).

**Forensik-Artefakte (dieser Lauf):** `_v3ac_forensics.json`, `_v3ac_pool_timeline.json`

---

## Referenztrade (V3AB, Variante A/B)

| Feld | Wert |
|------|------|
| Symbol / TF | DOGEUSDT 5m |
| `touch_event_id` | `te-d-c0791ea0c65e-0003` |
| Support | `d-c0791ea0c65e` (RETROSPECTIVE_DRAWING) |
| C1 / Entry | `2026-08-26 20:25:00 UTC` / `2026-08-26 20:30:00 UTC` @ **0.08491** |
| Pool-SSOT `decision_close` | `2026-08-26 20:30:00 UTC` |
| Geplanter TP | **0.09496** (`tp_mode` **POOL_BOTTOM_NO_CLEAR_CHANNEL**) |
| SL | 0.08389108 (−1,20 %) |
| Exit | `2026-08-27 13:50:00 UTC` @ **0.08736**, **SUPPORT_BREAK_EXIT**, **+2,89 %** brutto |
| Oberkanal @ Entry | **keiner** (`channel_upper_at` → `null`, `dynamic_channel_exit=no`) |

---

## 1. Rekonstruktion der aktuellen TP-Logik

### 1.1 Vom Nutzer beschlossene / dokumentierte Regeln (L1-Pilot, V3L/V3Y)

| Thema | Beschluss (Dokumentation) |
|-------|---------------------------|
| Pool-SSOT | 5m PD1 **ACTIVE**, Exact **Unique-Origin** D2, `depth ≥ 2` |
| Mindestabstand | `MIN_POOL_DIST_PCT = 0.50 %` Entry → Pool-Unterkante |
| **Fall A** — Oberkanal vorhanden | TP: Pool-Unterkante **nahe** Kanaldeckel (`POOL_BOTTOM_NEAR_CHANNEL`) oder Deckel-Linie (`CHANNEL_UPPER_LINE`) |
| **Fall B** — nur Support | TP aus qualifizierten Upper-D2-Bändern (**implementiert als Fernziel**, s. unten) |
| **Fall C** — Supportbruch | Parallel zu TP/SL/Kanal; 0,20 %-Zweikerzen-Bestätigung → `SUPPORT_BREAK_EXIT`; chronologisch erstes ausführbares Exit-Ereignis |
| Variant B/C Zusatz | `REJECTED_FIRST_D2_TOO_FAR` (Nearness-Filter) — **Entry-Gate**, kein TP-Wechsel |

Quellen: `l1_v3n_channel_tp_architecture_audit.md`, `l1_v3m_tp_audit_report.md`, V3AB-Report (Strategie = V3Y).

### 1.2 Implementierter Code (V3AB = `apply_d2_gate_to_pending` + V3Y-Exit)

| Schritt | Modul / Funktion |
|---------|------------------|
| D2-Snapshot @ `decision_close` | `pool_d2_metrics.analyze_d2_landscape()` → `pd1_exact_d2_adapter.upper_d2_bands_at_close()` |
| Qualifikation | `qualify_upper_d2_bands()` — `bottom > entry`, Abstand ≥ 0,50 % |
| Kanaldeckel | `run_l1_pilot.channel_upper_at()` (12 %-Fenster über Entry, Support in V3L beim Build ausgeschlossen) |
| TP-Wahl | `run_l1_pilot.pick_tp()` |
| Persistenz pro Trade | `pool_d2_metrics.apply_d2_gate_to_pending()` — **einmal @ Entry**, kein Refresh |
| Exit-Simulation | `support_break_exit_v3y.support_sim.simulate_exit_v3y()` — pro Kerze: SL, dann TP/Kanal (`high ≥ Ziel`), dann Supportbruch-State-Machine |

**Kernregel ohne Oberkanal** (`pick_tp`, Zeilen 504–518 in `run_l1_pilot.py`):

```text
best = max(qualified, key=lambda m: float(m["lower_bound"]))
tp   = best["lower_bound"]
mode = "POOL_BOTTOM_NO_CLEAR_CHANNEL"   # gate-Pfad ohne independent_support-Flag
       # bzw. "UPPER_D2_POOL_ONLY" wenn independent_support=True (gleiche Zahl)
```

→ **Entfernteste** qualifizierte D2-Unterkante, **nicht** die nächste.

**Mit Oberkanal:** zuerst Pools innerhalb `CHANNEL_NEAR_PCT` unter dem Deckel; sonst Deckel-Linie selbst.

### 1.3 Nur Forschung (nicht in V3AB verdrahtet)

| Paket | Inhalt |
|-------|--------|
| `tp_research_v3w/` | TP1 nearest, TP4 (= Produktions-`max`), TP2 „substantial zone“, TP3 score, M1–M5 Multi-TP |
| V3-Landschaftsfilter | `apply_provisional_research_filter` — bewusst **nicht** kalibriert / aus |
| V3V-Post-hoc 0.0894 | Screenshot-Referenz, **keine** Strategieparameter |

### 1.4 Antworten auf die Prüffragen (3.x)

1. **Verbindliche TP-Bedingungen:** Qualifikation (ACTIVE Exact D2, depth≥2, dist≥0,5 %); mit Kanal Near-Pool oder Deckel; ohne Kanal Pool-Unterkante (faktisch max D2).
2. **In V3AB implementiert:** Ja, vollständig über Gate + `pick_tp` + `simulate_exit_v3y`.
3. **`POOL_BOTTOM_NO_CLEAR_CHANNEL`:** Ja — Label trotz fehlendem Kanal; Verhalten = Fern-D2 (`max`).
4. **Nächster vs. entferntester Pool:** Code wählt **`max(lower_bound)`** → **entferntester** qualifizierter Upper-D2.
5. **Warum TP 0.09496?** Höchste qualifizierte Unterkante unter 73 Bändern; Band `0.09496–0.095186`, depth 2, ID `d2-exact:0.0949600000:0.0951860000:d2`, Distanz **+11,84 %**.
6. **Nähere Pools ignoriert?** Ja — **66** nähere qualifizierte Unterkanten inkl. Violett-Cluster (~5,2 %).
7. **Substanz / Depth / Origins:** Gate nur **depth≥2** + Mindestabstand; **keine** Mindestbreite/Origins im Produktions-`pick_tp`. Origins fließen in Exact-D2-Merge ein, nicht als separates Exit-Kriterium.
8. **Forschungsvarianten:** TP1/TP2/TP3/M* nur in `tp_research_v3w` simuliert; **nicht** freigegeben für V3Y/V3AB.
9. **Support-only vs. Kanal:** `channel_upper_at` findet kein gültiges Deckel über Entry → reiner Pool-Pfad (Fall B).
10. **TP-Zeitpunkt:** **Statisch @ Entry-Entscheidung** (`decision_close`); **keine** dynamische TP-Aktualisierung im offenen Trade.

---

## 2. Historische ACTIVE Upper-D2-Kandidaten (kanonisch)

**Methode:** Gleicher Kontext wie V3AB — `build_context_from_snapshot` + Frozen Pool-Member-Snapshot @ `2026-08-26 20:30:00 UTC`.

### 2.1 Gesamtlandschaft @ Entry

| Metrik | Wert |
|--------|------|
| Qualifizierte Upper-D2-Bänder | **73** |
| Nächstes qual. Ziel (nur Distanz) | **0.08536** (+0,53 %) |
| Gewähltes Fernziel | **0.09496** (+11,84 %) |
| Violett-relevante qual. Segmente (`0.08929–0.089514`) | **6** (s. Tabelle) |

### 2.2 Violett-Zone ~0,0893–0,0895 (Exact-D2 @ Entry, alle qualifiziert)

| Unterkante | Oberkante | D2 depth | Breite % | Distanz Entry % | Pool-ID (Auszug) |
|------------|-----------|----------|----------|-----------------|------------------|
| 0.08929 | 0.08932 | 2 | 0.034 | **5.16** | `d2-exact:0.0892900000:0.0893200000:d2` |
| 0.08932 | 0.08937 | 3 | 0.056 | 5.19 | `d2-exact:0.0893200000:0.0893700000:d3` |
| 0.08937 | 0.08938 | 4 | 0.011 | 5.25 | `d2-exact:0.0893700000:0.0893800000:d4` |
| 0.08938 | 0.089412 | 5 | 0.036 | 5.26 | `d2-exact:0.0893800000:0.0894120000:d5` |
| 0.089412 | 0.089478 | 4 | 0.074 | 5.30 | `d2-exact:0.0894120000:0.0894780000:d4` |
| 0.089478 | 0.089514 | 3 | 0.040 | 5.37 | `d2-exact:0.0894780000:0.0895140000:d3` |

**Micro-Member** in der Zone @ Entry: **6** überlappende ACTIVE Upper-Pools (verschiedene Origin-Zeitstempel in IDs `1787664…`–`1787676…`); Spanne grob **0.08928–0.08962** (siehe `_v3ac_pool_timeline.json`).

### 2.3 Zeitpunkte (ACTIVE Micro in Zone, D2-merge konsistent)

An allen angefragten Wänden bleiben **6** ACTIVE Micro-Bänder in der Zone aktiv (nicht erst während des Trades „neu“):

| Wand (Anfrage) | `decision_close` (5m) | ACTIVE Micro in Zone |
|----------------|------------------------|----------------------|
| Vor Entry `20:25` | `20:30` | 6 |
| Entry `20:30` | `20:35` | 6 |
| `27.08. 08:00` | `08:05` | 6 |
| `27.08. 09:00` | `09:05` | 6 |
| Erste Annäherung `08:45` | `08:50` | 6 |
| Vor Supportbruch `13:45` | `13:50` | 6 |

**Fall 1 (beim Entry bereits ACTIVE):** Violett-Cluster war **bekannt und qualifiziert** — wurde aber **nicht** als TP gewählt (Fernziel-Regel).

**Fall 2 (nach Entry entstanden):** Für diese Zone **nicht zutreffend**; generell existiert **kein** Codepfad für dynamisches TP-Refresh.

### 2.4 Kurs vs. Zone / TP

| Ereignis | UTC | Detail |
|----------|-----|--------|
| Erster Wick ≥ **0.08536** (nächstes qual. Produktions-Ziel bei TP1) | `2026-08-26 21:00:00` | High = 0.08536 |
| Erster Wick in Violett (≥ **0.08929**) | **`2026-08-27 08:45:00`** | O/H/L/C = 0.08891 / **0.08955** / 0.08882 / 0.08941 |
| Max High vor Support-Exit | — | **0.08996** (< geplantes TP 0.09496) |
| Geplantes TP 0.09496 erreicht? | — | **Nein** |
| Tatsächlicher Exit | `2026-08-27 13:50:00` | SUPPORT_BREAK @ 0.08736 (+2,89 %) |

Backtester-Logik: Limit-TP bei `pool_tp` wenn `high ≥ tp` **in derselben Kerze** (vor Supportbruch-Logik, sofern SL nicht zuerst).

---

## 3. Begründung TP 0.09496 (Schritt für Schritt)

1. C1 bestätigt → Entry @ 20:30, `entry = 0.08491`.
2. Pool-Snapshot schließt mit Kerze **20:30 UTC**.
3. `channel_upper_at(entry_ts, 0.08491)` im V3AB-Snapshot (5 Linien, keine Fern-Oberkanäle im 12 %-Cap) → **kein Deckel**.
4. `analyze_d2_landscape` → 73 qualifizierte Bänder.
5. `pick_tp` ohne Kanal → `max(lower_bound)` → **0.09496**.
6. `apply_d2_gate_to_pending` schreibt `tp_price`, `tp_pool_id`, `tp_mode=POOL_BOTTOM_NO_CLEAR_CHANNEL`, `_pool_tp=0.09496`.
7. V3Y-Exit hält dieses Ziel **fix**; Kurspeaks reichen nicht aus → kein `TP`-Exit; Supportbruch gewinnt chronologisch.

**Näherliegendes substantielles Band (visuell Violett):** war **im Kandidatenpool**, fiel der **max()-Auswahl** zum Opfer — nicht wegen Inaktivität oder fehlender Qualifikation.

---

## 4. Zusammenspiel TP · Kanal · SL · Supportbruch (Ist vs. Soll)

| Gewünscht (Nutzer ABC) | Code V3AB/V3Y |
|------------------------|---------------|
| A: Kanal + D2 | Implementiert, wenn `channel_upper_at` trifft (hier **nein**) |
| B: Nur Support → ACTIVE Upper-D2-TP | Implementiert, aber Auswahl = **fernstes** qual. D2, nicht „nächstes sichtbares Band“ |
| C: Supportbruch parallel, erstes Ereignis | `simulate_exit_v3y`: SL → TP/Kanal intrabar → dann Support-FSM; **stimmt** |
| TP statisch @ Entry | **Ja** |

**Warum kein TP an Violett-Unterkante?** Weil der Trade nur bei **`high ≥ 0.09496`** als Pool-TP geschlossen hätte — nicht bei beliebigen qualifizierten Bändern unterwegs.

---

## 5. Fall 1 / Fall 2 & Forschungsfragen

### Fall 1 — Violett @ Entry ACTIVE & qualifiziert

- **Als TP nach Produktionsregel wählbar?** Nur als **eines von 73** Kandidaten; **gewählt wird explizit nicht** (max-Regel).
- **Hätte Violett-Unterkante den Trade früher profitabel geschlossen?** **Hypothetisch ja** (@ 08:45, ~**+5,16 %** auf Unterkante 0.08929) — aber **nicht** mit dem **tatsächlich gesetzten** Produktions-TP.

### Fall 2 — Dynamisches TP

- **Definierte Regeln:** keine im V3Y/V3AB-Pfad; V3W-Dokumentation: alle Modelle **„static at entry decision“** (`tp_targets.py` Header).
- **Nicht aktivieren** (wie beauftragt).

### Poolbreite, Substanz, Mindestdistanz

| Thema | Status |
|-------|--------|
| Mindestdistanz | **0,50 %** — Produktion |
| „Substanziell“ | Nur TP2-Forschung (Breite, depth, origins-Schwellen); **kein** Produktionskriterium |
| Risiko „nächster Pool zu früh“ | Produktion vermeidet das durch **Fernziel**; umgekehrtes Risiko: **verpasste Zwischenzonen** (dieser Trade) |
| TP1 nearest @ diesem Entry | **0.08536** — Exit schon 26.08. 21:00 (+0,53 %), **unter** finalem Supportbruch-Gewinn |

### V3W-Modell-Check (dieselbe Pending-Row, read-only)

| Modell | TP | Dist % | Trifft 0.0893-Zone? |
|--------|-----|--------|---------------------|
| **TP4 / V3U Produktion** | 0.09496 | 11.84 | Nein (Fernziel) |
| **TP1 nearest** | 0.08536 | 0.53 | Nein |
| **TP2a/b/c substantial** | 0.08536 / **0.08604** | 0.53 / 1.33 | Nein (erste Substanz-Zone **unter** Violett) |
| **TP3\*** | 0.09342–0.09436 | ~10 % | Nein |

Analoger Befund wie `V3W_TP_RESEARCH_REPORT.md` (Aug-26-Kontrollfall): **kein** freigegebenes V3W-Modell zielt automatisch auf **0.0893–0.0895**.

---

## 6. Bereits nutzbare vs. fehlende Regeln

| Regel | Nutzbar in V3AB? |
|-------|------------------|
| Fern-D2-TP (`max`) | **Ja** (aktuell) |
| Near-Channel-TP | Ja, wenn Zeichnung Oberkanal liefert |
| Supportbruch-Exit | **Ja** |
| Nearest-D2-TP (TP1) | Nur manuell / V3W — **nicht** angebunden |
| Substantial-zone TP (TP2) | Nur V3W |
| Dynamisches TP-Update | **Fehlt** |

---

## 7. Minimaler Implementierungsplan (isolierter Vorschlag, **nicht ausführen**)

**Ziel:** Fall B näher an Nutzererwartung („geeignetes ACTIVE Upper-D2-Band“), ohne Supportbruch zu schwächen.

**Vorschlag V3AC-Pool-TP-Patch (kleinste Fläche):**

1. **Neue Auswahlfunktion** (z. B. `pick_tp_support_only()` in `run_l1_pilot.py` oder Parameter an `pick_tp`):
   - Nur wenn `channel_upper is None`.
   - Eingabe: `qualified`, optional `MIN_TP_DIST_PCT` (≥ bestehend 0,5 %).
   - Kandidatenfilter „substantiell“ minimal: `d2_depth ≥ 3` **oder** merge-gap über `merge_qualified_zones` aus V3W (Breite/Origins-Schwellen **explizit vom Nutzer zu beschließen**).
   - Auswahl: **`min(lower_bound)`** unter substanziellen Zonen **oberhalb** einer optionalen Mindest-Fernschwelle (z. B. skip wenn > X % — Verknüpfung mit `REJECTED_FIRST_D2_TOO_FAR`-Philosophie).
2. **Nur** in neuem Sandbox-Runner (z. B. `support_break_exit_v3ac/`) `apply_d2_gate_to_pending` durch Version mit Flag ersetzen — **V3AB-Ordner unverändert lassen**.
3. Exit weiter `simulate_exit_v3y` — ein `_pool_tp` pro Trade, weiter **statisch**.
4. Tests: Smoke + **dieser** Referenztrade + V3M-September-Trades (Regression Fernziel vs. neu).

**Bewusst nicht im Minimalplan:** dynamisches TP, Multi-Leg (M1–M5), Chart-Screenshot-Preise.

---

## 8. Entscheidungen vor Implementierung

1. Ohne Oberkanal: **nearest** vs. **first substantial** vs. **max** (Status quo)?
2. Schwellen „substantiell“: `depth`, `width_pct`, `unique_origins` — welche Werte (TP2a/b/c-Grid als Vorschlag)?
3. Mindest-**und** Höchst-Distanz zum TP (Nearness / „too far“)?
4. Soll Violett-artige Zwischenzonen **vor** Fern-Clusters (0.09496) priorisiert werden, wenn depth≥5?
5. Label-Konsistenz: `POOL_BOTTOM_NO_CLEAR_CHANNEL` vs. `UPPER_D2_POOL_ONLY` bei Independent Support (`apply_d2_gate` übergibt heute **kein** `independent_support` an `pick_tp`).
6. Variant B: bleibt Entry-Reject `REJECTED_FIRST_D2_TOO_FAR` unabhängig von neuer TP-Auswahl?
7. Freigabe-Kriterium: nur Einzeltrade-Forensik oder V3W-ähnlicher Batch?

---

## 9. Schlussantwort (verbindlich, datenbasiert)

**Frage:** Hätte dieser Long nach einer **bereits definierten und historisch zulässigen** Pool-TP-Regel **vor** dem Supportbruch mit **höherem** Gewinn geschlossen werden können?

**Antwort: Nein.**

- Die **produktiv in V3AB/V3Y implementierte** Regel setzte TP **0.09496**; der Kurs erreichte das nicht (Max High **0.08996**). Ein Pool-TP-Exit an der Violett-Unterkante erfolgte daher **korrekt nicht**.
- Die einzige **ebenfalls implementierte** Alternative im selben `pick_tp`-Baustein (nächstes qual. D2 via `min`, V3W-TP1) hätte bei **0.08536** schon am **26.08. 21:00** geschlossen — mit ca. **+0,53 %**, also **niedriger** als der reale Supportbruch (**+2,89 %**).
- V3W-TP2/TP3 wählen für diesen Entry substanzielle Ziele bei **~0.08604–0.0863 %**, ebenfalls **unter** dem Supportbruch-Ergebnis — und sind **nicht** strategisch freigegeben.
- Ein Exit an **0.08929** (~**+5,16 %** @ 08:45) wäre nur mit einer **nicht freigegebenen** Zielvorgabe (Screenshot/post-hoc) oder einer **noch zu beschließenden** neuen Auswahlregel möglich gewesen — **nicht** mit dem aktuellen V3AB-TP-Pfad.

---

**STOP** — Analyse und Plan abgeschlossen. Keine Codeänderung, kein Backtest.
