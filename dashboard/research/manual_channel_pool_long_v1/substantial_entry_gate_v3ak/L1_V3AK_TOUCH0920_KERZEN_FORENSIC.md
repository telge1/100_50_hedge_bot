# L1 V3AK — Forensik Touch 09:20 vs. Chart · 30.08.2026

**READ-ONLY** · Support `d-ea081434c5d3` · Trade `te-d-ea081434c5d3-0003` · Entry **11:45 @ 0.08491**

## Zeichnungs-SSOT (Backtester)

| Feld | Wert |
|------|------|
| `start_timestamp` | 1788081300 → **2026-08-30 09:15:00 UTC** |
| `start_price` | 0.08430877233576727 |
| `end_timestamp` | 1788127500 → **2026-08-30 22:05:00 UTC** |
| `end_price` | 0.0858983768758056 |
| `extend_right` | False |
| Kerzen-Zuordnung | `classify_candle_interval(line, candle['time'], high, low)` — **Open-Zeitstempel** + Intervall **[t, t+300s)** |

Abweichung Chart↔Backtester: nur wenn Chart **andere Anker** oder **Close-Zeit** statt **Open-Zeit** für die Linie nutzt. Die L1-Forschung interpoliert `price_at(candle_open)` und `line_price_range_over_interval` über die Bar.

## Aufgabe 1 — Warum 09:20 als Touch?

**Kerze 09:20 UTC (OHLC 0.08456 / 0.08465 / 0.08449 / 0.08451):**

- Linienpreis **am Bar-Open:** **0.0843190944**
- **Low 0.08449 > Linie** → **kein** Wick-Durchstich am Open-Punkt; **kein** `low ≤ line ≤ high` an der Open-Linie.
- `classify_candle_interval`: Klasse **`NEAR_0_20`**, `touching=False`, Distanz **~0.20 %** (Mindestabstand Low → Linie am Open).
- `in_contact_zone`: **ja** — Contact = Klassen **TOUCH | NEAR_0_10 | NEAR_0_20** (≤ **0,20 %** Toleranz, `config.TOUCH_ZONE_MAX_DIST_PCT`).

**Antwort auf die Chart-Beobachtung:** Der Backtester meldet **keinen physischen Schnitt** (`interval_touching_flag=no`), sondern **Annäherung innerhalb 0,20 %**. Das ist **regelkonform**, nicht „Low berührt grüne Linie“.

**Erster physischer Linienband-Schnitt (moving line über [t,t+300)):** **2026-08-30 09:40:00 UTC**  
**Erster Backtester-Kontakt (0,20 %-Zone):** **2026-08-30 09:20:00 UTC**

## Aufgabe 2 — Touch-Sequenz 09:15–10:20

Volle Tabelle: **`l1_v3ak_0915_1020_kerzen_forensic.csv`**

- **09:20:** `NEW_TOUCH_EVENT` → **disarmed** bis Re-Arm (Close ≥ Linie+0,30 %).
- **09:25–10:15:** weitere **`in_contact_zone=yes`**, aber **kein** neues Event (**disarmed**).
- **10:20:** Re-Arm + zweites Touch-Event `…-0002`.
- **10:25:** C1 für 10:20 scheitert (bearish).

**Ja (B):** Der **09:20-„Touch“ (NEAR_0_20)** hat **disarmed** — relevante Folge-Kontakte erzeugen **keine** neuen Touch-IDs, bis Re-Arm.

## Aufgabe 3 — EMA20 (hypothetisch, kein L1-Gate)

- **Erstes Close > EMA20** (nach 09:25-Kontaktphase): **2026-08-30 09:55:00 UTC**
  - Close **0.08456** · EMA20 **0.0845486439** · vorher Close ≤ EMA20
  - Close > Linie @ Open: **True** · bullisch: **True**
  - Hypothetischer Entry (nur Modell): **2026-08-30 10:00:00 UTC @ 0.08456**
  - **Kein** L1-Gate — ersetzt **nicht** Touch+C1.

## Aufgabe 4 — 11:45 vs. hypothetisch EMA20

| | Tatsächlich (11:45) | Hypothetisch (EMA-Cross+1) |
|---|---|---|
| Entry UTC | 2026-08-30 11:45:00 UTC | 2026-08-30 10:00:00 UTC |
| Entry-Preis | 0.08491 | 0.08456 |
| Δ Entry % | — | 0.4139% vs. 11:45 |
| SL (−1,2 %) | 0.0838910800 | 0.0835452800 |

**Regelkonformität hypothetischer Früheinstieg:**  
Selbst mit EMA20-Cross **ersetzt das nicht** die L1-Pipeline **Touch-Event → C1 (bullish, close>line) → Entry+1 Bar**.  
Am EMA-Cross-Zeitpunkt war kein gültiges C1 nach **armed** Touch-Event abgeschlossen (09:20-C1 **failed**; danach **disarmed** Kontakte).

## Aufgabe 5 — A–E

| Frage | Antwort |
|-------|---------|
| **A** 0,20 %-Zone → Touch 09:20 ohne physischen Kontakt? | **Ja** — Klasse `NEAR_0_20`, Low **~0,20 %** über Linie (Open-Preis). |
| **B** DISARMED → spätere Kontakte ignoriert? | **Ja** — 11 Kerzen 09:25–10:15 in Zone, **kein** neues Event. |
| **C** EMA20-Close hätte früheren Entry erlaubt? | **Nur hypothetisch** — **nicht** in aktueller Strategie; würde **C1/Touch** nicht ersetzen. |
| **D** Chart vs. Backtester-Geometrie? | Dieselbe Snapshot-Linie (Anker **09:15 / 22:05**). **Erster physische Schnitt** (Open-Linie in H/L oder Intervall): **09:40** (`low=0.08427` &lt; `line_open=0.084360`). **09:25** nur **NEAR_0_10** (~0,024 % Abstand Low→Linie). **09:20** ist **reine 0,20 %-Toleranz** ohne Schnitt — Chart-Zweifel **berechtigt**. |
| **E** Bug oder Regel? | **Korrekte Umsetzung** von `classify_distance` + `in_contact_zone` — kein Implementierungsfehler. |

## Event `te-d-ea081434c5d3-0001`

- Touch-UTC: **2026-08-30 09:20:00 UTC**
- C1-Bar 09:25: **bearish** (`close ≤ open`) → `NO_C1_CONFIRM_AFTER_FIRST_TOUCH`
- Erstes **C1+Entry**: Touch **11:35** → Confirm **11:40** → Entry **11:45** (Event `…-0003`)

**STOP** — READ-ONLY.
