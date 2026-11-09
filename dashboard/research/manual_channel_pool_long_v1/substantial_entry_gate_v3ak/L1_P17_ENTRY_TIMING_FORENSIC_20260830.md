# P17 / L1 — Entry-Timing-Forensik · 30.08.2026

**Trades:** `te-d-ea081434c5d3-0003` (11:45 Entry) vs `te-d-ea081434c5d3-0012` (17:40 Entry)

**Regelwerk:** L1 Manual-Channel (`run_l1_pilot`, `LINE_SELECTION_INDEPENDENT`) — **nicht** P17-EMA-WATCH aus `pools+ob+delta_bot`.
„WATCH“ unten = Supportlinie **für Kerzen aktiv** (`LineDef.active_for_candle`, Anchor ab Zeichnungsstart).

Parameter (unverändert): Touch-Zone ≤ **0.2%**, Re-Arm Close ≥ Linie + **0.3%**, 
Approach **Support von oben** (ε=0.05%), Bestätigung **C1**, Entry **Open der Kerze nach C1**.

## Kurzfazit

Der **11:45-Entry wirkt verspätet**, weil der sichtbare Anstieg **ohne armed Touch** in der Kontaktzone lief: 
Nach Touch **09:20** blockierte **Re-Arm** weitere Events bis **10:20**; Touch **10:20** scheiterte an **bearisher C1-Kerze**; 
von **10:30–11:34** lag der Kurs **über der 0,20%-Touch-Zone** (kein Kontakt, kein neues Event); erst **11:35** Touch #3 + C1 → **11:45** Entry.

**17:40** folgt **demselben** 3-Kerzen-Muster (Touch → bullisches C1 → Entry+1 Bar) — ohne Kontaktlücke, daher „zeitnah“.

**Kein Erkennungsfehler**, keine Verarbeitungsverzögerung — **bewusste L1-Touch/C1/Re-Arm-Regeln** + **RETROSPECTIVE_DRAWING** (Linie erst ab **09:15 UTC** aktiv).

## Vergleich

| Kriterium | Trade 1 (11:45) | Trade 2 (17:40) |
|---|---|---|
| WATCH (Linie aktiv) | **2026-08-30 09:15:00 UTC** | **2026-08-30 09:15:00 UTC** (dieselbe Linie) |
| Erster gültiger Touch (long_ok) | **09:20** (`…-0001`) | **17:30** (`…-0012`) |
| Erste C1-fähige Bestätigung | **11:40** (Touch 11:35) | **17:35** (Touch 17:30) |
| Frühester regelkonformer Entry *wenn C1 auf erstem Touch* | hypothetisch **09:25** | — |
| Frühester nach 2. Touch (10:20) *wenn C1* | hypothetisch **10:25** | — |
| Tatsächlicher Entry | **2026-08-30 11:45:00 UTC** | **2026-08-30 17:40:00 UTC** |
| Verzögerung Touch→Entry (5m-Bars) | **2** | **2** |
| Verzögernde Bedingung | Re-Arm + NO_C1 + **kein Kontakt** während Rally | keine zusätzliche (nur Standard-C1) |

## Trade 1 — Zeitablauf 09:00–12:00

### Zeichnung / WATCH

- Support `d-ea081434c5d3`: Anchor **09:15** → **22:05 UTC** (`extend_right=false`).
- **Vor 09:15:** Linie für Backtest **inaktiv** — kein Touch, kein Entry (Geometrie, kein Pilot-Bug).

### Touch-Events (nur strukturell gültige)

| UTC | Event | Ergebnis | Grund |
|---|---|---|---|
| 09:20 | `…-0001` | kein Entry | C1 **09:25**: close≤open (0,08438 ≤ 0,08451) |
| 10:20 | `…-0002` | kein Entry | C1 **10:25**: close<open (0,08468 < 0,08471) |
| 11:35 | `…-0003` | **Entry 11:45** | C1 **11:40** bullisch, close>Linie |
| 11:50 | `…-0004` | Entry 12:00 möglich | (max-1-Long in E3-Lauf) |

### Re-Arm und „verspätete“ Rally

- **09:20** erster Touch **disarmt** (`armed=false`).
- **09:25–10:15:** Preis **in Kontaktzone**, aber **kein neues Touch-Event** (11 Kerzen `contact(disarmed)`).
- Erst **10:20** Close ≥ Linie+**0,30%** → Re-Arm; gleichzeitig Touch #2.
- **10:25–11:34:** Kurs **nicht** in Kontaktzone (Klasse meist >0,20 % über Linie) → trotz mehrfacher Re-Arms **kein** Touch bis **11:35** Pullback.

Das erklärt die Chart-Wahrnehmung „Rally vor Entry“: Die Rally war **kein fehlender Trigger**, sondern Phase **außerhalb** der Touch-Zone bzw. **unter disarmed Kontakt**.

### Früherer Entry 09:30–11:00?

| Kandidat | Regelkonform? | Blockade |
|---|---|---|
| 09:25 Entry nach Touch 09:20 | nur wenn C1 bullisch | **C1 fail** (bearish confirm) |
| 10:25 Entry nach Touch 10:20 | nur wenn C1 bullisch | **C1 fail** (bearish confirm) |
| Touch während 09:25–10:15 Kontakt | nein (kein neues Event) | **Re-Arm** nach erstem Touch |
| Touch während 10:30–11:30 Rally | nein | **Preis nicht in Kontaktzone** |

**EMA:** L1-Pilot hat **keine** EMA-Entry-Bedingung für diese Trades (nur in separatem P17-Repo).
**Pools (V3E/E3):** Für `…-0001/0002` existieren **keine** `entry_utc` in Pending — Pipeline endet vor Pool-Gate an **NO_C1_CONFIRM_AFTER_FIRST_TOUCH**.

Kerzen-Trace: `l1_p17_aug30_entry_candles_0900_1200.csv`.

## Trade 2 — Referenz 17:30–17:40

| Kerze UTC | Rolle | O → C |
|---|---|---|
| **17:30** | Touch #12 | 0,08557 → 0,08579 (bullisch, Kontakt) |
| **17:35** | **C1** | 0,08579 → 0,08587 (bullisch, close>Linie) |
| **17:40** | **Entry Open** | 0,08587 |

Identische mechanische Verzögerung: **2 Bars** Touch→Entry — wirkt „frisch“, weil kein Re-Arm-Stau und kein Kontaktausfall dazwischen.

## Ursache (Taxonomie)

| # | Kategorie | Bewertung |
|---|---|---|
| 1 | Bewusste Strategiebedingung | **Ja** — C1, Next-Bar-Open, Touch-Re-Arm 0,30 %, Touch-Zone 0,20 %, Approach from above, Linien-Aktivität |
| 2 | Zu restriktiv | **Teilweise** — Re-Arm + striktes C1 verhindern Wiederholungstouches; Rally außerhalb Zone erzwingt Pullback |
| 3 | Touch/Candle-Bug | **Nein** — Replay stimmt mit `l1_v3ak` Pending überein |
| 4 | Verarbeitungsverzögerung | **Nein** |

### Hypothetische Änderungen (nur Analyse, nicht empfohlen)

- **C1 lockern / Entry auf Touch-Bar:** frühere Entries an 09:25/10:25 — mehr **False Positives** an allen Supports.
- **Re-Arm abschwächen:** Kontakte 09:25–10:15 würden **eigene Events** erzeugen — höhere Touch-Dichte global.
- **Linie früher aktiv (Zeichnung):** würde RETROSPECTIVE_DRAWING verändern — nicht Teil dieser READ-ONLY-Auswertung.

## GO-TO Chart

- Linie beginnt: **2026-08-30 09:15 UTC**
- Entscheidend Trade 1: **09:20**, **09:25**, **10:20**, **10:25**, **11:35**, **11:40**, **11:45**
- Trade 2: **17:30**, **17:35**, **17:40**

**STOP** — READ-ONLY, keine Code-/Strategieänderung.