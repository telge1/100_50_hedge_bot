# L1 V3X — Support-Trendlinienbruch als zusätzlicher Long-Exit

Stand: 2026-11-09 (finale Vereinfachung)  
Status: Analyse und technischer Plan. **Keine Implementierung, kein Backtest.**  
Bezugslauf: L1 V3U `INDEPENDENT_SUPPORT`, Run `20261108T125450Z`, Zeichnungs-Snapshot SHA256 `d05d218646aced84ffc0adbb8ca66a6a8f92048190e72dc9cea3140270fb594c`.  
Kerzenquelle August-Referenztrade: `l1_v3v_price_path.csv` (1239 lückenlose 5m-Kerzen Entry → SL). Es wurde nichts an Strategie, Zeichnungen oder Backtestern geändert.

---

## 1. Handelsidee (kurz)

Situation A/B wie bisher: Entry-Support bleibt Referenz; Pool-/Kanal-TP und fester SL bleiben aktiv. Ein **bestätigter** Bruch derselben Linie ist ein **zusätzlicher** Exit. Es gewinnt das chronologisch erste ausführbare Ereignis (SL, TP, Kanal-TP oder bestätigter Supportbruch).

Keine weiteren Indikatoren, Trendfilter oder C4/Struktur-Zusatzlogik.

---

## 2. Finale Exit-Regel V3X (verbindliche Forschungsfassung)

`d = 0.002` (0,20 %) ist der zu testende Kandidat, **kein** fest optimierter Parameter.

### 2.1 Überwachung

- Während der Position gilt nur die **Entry-`support_drawing_id`** (eingefrorene `LineDef` mit Ankern zum Laufzeitpunkt).
- Prüfung nur auf **abgeschlossenen** 5m-Kerzen, wenn `active_for_candle()` für diese Linie true ist.
- **Bruchkerze:** erster abgeschlossener 5m-**Close** **unter** `price_at(Close-Zeitpunkt)`. Wick unter der Linie bei Close darüber zählt nicht.
- Docht-only bleibt ohne Signal.

### 2.2 Folgekerze gegen den Schluss der Bruchkerze

Sei `break_close` = Close der Bruchkerze, `next_close` = Close der **unmittelbar folgenden** 5m-Kerze:

| Bedingung | Bedeutung |
|---|---|
| `next_close <= break_close * (1 - d)` | Abwärts bestätigt → **Long-Exit-Signal** |
| `next_close >= break_close * (1 + d)` | Aufwärts → **Long bleibt offen**, Support weiter überwachen |
| sonst (±d-Band) | **neutral**, deterministisch wie Abschnitt 2.3 |

**Ausführung:** Fill frühestens am **Open der nächsten** Kerze nach der Bestätigungskerze (Bruchkerze B, Bestätigung B+1, Fill-Open B+2). Nicht am Bruch-Close, nicht am High/Low.

### 2.3 Neutraler Bereich (Forschungskandidat, fest im Plan)

Wenn `next_close` weder ≥ `+d` noch ≤ `−d` gegen `break_close` liegt:

1. Schließt `next_close` **auf oder über** der Supportlinie → zurück zu **freier Überwachung** (neue Bruchkerze erst bei erneutem Close unter der Linie).
2. Schließt `next_close` **weiter unter** der Linie → **dieselbe Kerze wird die neue Bruchkerze** (`break_close` = dieser Close). Die **nächste** Kerze wird wieder gegen diesen Schluss mit ±`d` geprüft (Rollen).

Ein bullishes `+d` gegen den Bruch-Close **ohne** Linien-Reclaim beendet den Versuch ebenfalls: Long bleibt offen, zurück zur freien Überwachung (Punkt 2.2, zweite Zeile). **Kein** dauerhaftes Abschalten der Linie.

### 2.4 Zustände (minimal)

Nur zur Implementierung, ohne Extra-Logik:

| Zustand | Bedeutung |
|---|---|
| `MONITOR_SUPPORT` | Warten auf Close unter der Linie |
| `BREAK_PENDING` | Bruchkerze gesetzt; genau **eine** Folgekerze wird gegen `break_close` bewertet; bei Neutral unter der Linie: Rollen → weiter `BREAK_PENDING` mit neuem `break_close` |
| (Position zu) | Nach bestätigtem Exit oder SL/TP |

Nach Reclaim (Close ≥ Linie) oder bullischem `+d` gegen `break_close`: wieder `MONITOR_SUPPORT`, **dieselbe** `support_drawing_id`.

### 2.5 Unverändert aktiv

- Ursprünglicher **Stop-Loss**
- **Pool-TP** / **Kanal-TP** wie in V3U  
Auf Bruch- und Bestätigungskerzen gelten SL/TP **intrabar wie heute**. Tritt SL oder TP dort zuerst auf, kein Support-Exit. Kein rückwirkender Fill.

---

## 3. Hat der bisherige Backtester diesen Exit schon?

**Nein.** `simulate_exit()` in `run_l1_pilot.py` kennt nur SL, Pool-TP, `CHANNEL_UPPER`, `AMBIGUOUS`, `OPEN_AT_WINDOW_END`. Kein `SUPPORT_TRENDLINE_BREAK_EXIT`, keine Nachführung der Entry-Linie.

---

## 4. Historischer Referenztrade (26.08.2026)

| | |
|---|---|
| Entry | `2026-08-26 15:10:00 UTC` @ `0.08444` |
| Support | `d-a6d9a05da2f1` (`RETROSPECTIVE_DRAWING`) |
| SL / TP | `0.08342672` / `0.09496` |
| V3U-Exit | `2026-08-30 22:20` SL **−1,20 %** |
| MFE | `0.09027` @ `28.08. 01:30` (+6,90 %) |

### 4.1 Linie (Kurz)

Snapshot `l1_v3u_drawings_snapshot_d05d218646aced84.json`; ID fehlt in aktueller `drawings.json`. Trend, `extend_right=false`, Anker `2026-08-26 12:55` … `2026-08-28 06:00`, steigend. Geometrie aktiv bis einschließlich Open `06:00`. Signale vor dem zweiten Anker sind **retrospektive** Geometrie, kein Live-Handeln.

### 4.2 Anwendung der V3X-Regel (chronologisch, alle relevanten Episoden)

SL/TP vor keinem bestätigten Bruch-Exit berührt.

**Episode 1 — Fehlausbruch nach Entry**

| Kerze | Rolle | Close | Linie (ca.) | vs. vorheriger `break_close` | Ergebnis |
|---|---|---:|---:|---|---|
| `16:35` | Bruch | 0.08393 | 0.08400 | — | `BREAK_PENDING` |
| `16:40` | Folge | 0.08383 | 0.08401 | **−0,119 %** | neutral → Rollen |
| `16:45` | — | 0.08422 | 0.08403 | — | **über Linie** → `MONITOR_SUPPORT` |

Kein Exit. Hypothetischer Fill bei sofortiger Bestätigung wäre Open `16:45` ≈ −0,72 % gewesen; die Regel verhindert das.

**Episode 2 — `27.08. 21:25`**

Bruch `0.08840`. Folge `21:30` Close `0.08853` = +0,147 % (neutral), **über Linie** → `MONITOR_SUPPORT`.

**Episode 3 — `27.08. 21:35`–`21:45`**

Bruch `21:35` → neutral `21:40` (0 %) unter Linie → Rollen auf `21:40`. Folge `21:45` +0,294 % und über Linie → `MONITOR_SUPPORT`.

**Episode 4 — `28.08. 00:05`–`00:15`**

Analog: Roll `00:10`, Reclaim `00:15` über Linie.

**Episode 5 — `28.08. 00:20`**

Folge `00:25` +0,282 % **und** über Linie → `MONITOR_SUPPORT`.

**Episode 6 — bestätigter Bruch (nachhaltig unter der Linie)**

| Kerze | Close | vs. `break_close` | Linie | Ergebnis |
|---|---:|---|---|---|
| `01:55` | 0.08906 | — (neue Bruchkerze) | darunter | pending |
| `02:00` | 0.08903 | −0,034 % | darunter | neutral → Roll auf `02:00` |
| `02:05` | 0.08887 | −0,180 % | darunter | neutral → Roll auf `02:05` |
| `02:10` | 0.08849 | **−0,428 %** vs. `02:05` | darunter | **Exit-Signal** |

OHLC Bestätigung:

- Bruchkerze `02:05`: O/H/L/C = 0.08903 / 0.08908 / 0.08879 / **0.08887**
- Bestätigung `02:10`: Close **0.08849**

**Erster bestätigter V3X-Exit:** Fill Open **`2026-08-28 02:15`** = **`0.08849`** → Brutto **+4,7963 %** vs. Entry. SL/TP waren nicht zuvor erreicht. Bisheriger V3U-Exit bleibt SL −1,20 %.

Bis `06:00` kein Close mehr über der Linie; weitere Bruch-/Roll-Ketten unter der Linie würden erst nach einem erneuten Exit relevant sein (Position dann geschlossen). Ab `06:05` ist die Linie inaktiv; nur SL/TP weiter.

### 4.3 Abgrenzung zu alter „sofort beim Close“-Logik

Ein sofortiger Exit beim ersten Close unter der Linie hätte diesen Trade früh bei etwa −0,6 % geschlossen. Die V3X-Regel mit Folgekerze und Rollen im Neutralbereich lässt die Rally und den späteren bestätigten Exit im Plus offen — **an diesem einen retrospektiven Trade**.

---

## 5. Exit-Priorität (kausal)

Pro 5m-Kerze in fester Reihenfolge:

1. Bestehende SL/TP/Kanal-Prüfung auf **dieser** Kerze (wie V3U).
2. Erst wenn die Kerze abgeschlossen ist: Bruch-/Bestätigungs-Logik aktualisieren (kein Lookahead).
3. Bestätigter Bruch: Ausführung am **folgenden** Open, danach keine weitere Support-Prüfung.
4. Linie endet (`extend_right=false` nach `t_hi`): keine neuen Bruchsignale; SL/TP laufen weiter.
5. Fehlende/ungültige Zeichnung: fail-closed, nur SL/TP.

Technisch: Erweiterung von `simulate_exit()` + Übergabe der eingefrorenen Entry-`LineDef` aus `execute_variant()` — **erst nach Freigabe**, nicht jetzt.

---

## 6. Architektur (unverändert, minimal)

| Wiederverwenden | Ort |
|---|---|
| `LineDef`, `price_at`, `active_for_candle` | `manual_line_touch_probe_v1/line_geometry.py` |
| `line_from_drawing()` | `run_l1_pilot.py` |
| SL/TP-Konfliktmodell | `simulate_exit()` / `pilot_engine.execute_variant()` |

| Neu (nur nach Freigabe) | Inhalt |
|---|---|
| Position-Felder | `support_drawing_id` + eingefrorene `LineDef` (oder Snapshot-Bezug) |
| Kleine Zustandsmaschine | `MONITOR_SUPPORT` / `BREAK_PENDING` gemäß Abschnitt 2 |
| Hilfsfunktion | `classify_follow_bar(break_close, next_close, next_line_price, d)` → bear / bull / neutral_roll / reclaim |
| Exit-Kind | `SUPPORT_TRENDLINE_BREAK_EXIT` |

Keine Duplikation der Liniengeometrie. Keine Änderung an Entry, `pick_tp`, C1, Varianten A/B/C, V3U, V3W, Charts, Zeichnungen, P17, LONG V3, SHORT MAIN V2, Live.

---

## 7. Isolierter Forschungsrunner (später)

Modul z. B. `support_break_research_v1/`:

- Liest eingefrorene V3U-Tradezeilen + Zeichnungs-Snapshot (nicht lebende `drawings.json`, wenn ID fehlt).
- Implementiert **nur** die Regel aus Abschnitt 2 gegen historische 5m-Kerzen.
- Vergleicht pro Trade: V3U-Exit vs. V3X-Exit, Zeitpunkt, Brutto-%, retrospektiv-Flag.
- **Kein** Portfolio-Re-Backtest, **kein** Patch von `simulate_exit()` in V3U.

---

## 8. Tests (für die eine Regel)

- Bruch → Folge ≤ −0,20 % → Fill nächster Open.
- Bruch → Folge ≥ +0,20 % → kein Exit, wieder `MONITOR_SUPPORT`.
- Bruch → Folge neutral, weiter unter Linie → Referenz rollt, spätere Bestätigung möglich (August `01:55`–`02:10`).
- Bruch → Folge neutral, Close über Linie → `MONITOR_SUPPORT` ohne Exit (`16:45`, `21:30`).
- Fakeout `16:35`/`16:40`/`16:45`: kein Exit.
- SL/TP auf Bruch- oder Bestätigungskerze vor Fill: SL/TP gewinnt.
- Wiederholter Bruch nach Reclaim: neuer Zyklus, gleiche ID.
- Wick unter Linie, Close darüber: kein Bruch.
- Linie endet während `BREAK_PENDING`: Pending verfällt, kein `price_at` nach Segment.
- Fehlende `support_drawing_id`: kein Support-Exit.

---

## 9. Risiken / offen

- **0,20 %** nur als Testkandidat; nicht optimieren in diesem Schritt.
- August-Exitergebnis **+4,80 %** hängt an retrospektiver Linie (zweiter Anker nach allen Signalen).
- Viele knappe Closes unter steiler Linie → häufiges Rollen, erst späte Bestätigung (gewollt vs. Chart-Eindruck).
- Trade-Zeilen speichern ID, nicht Anker; Forschungsrunner braucht Snapshot-Freeze.

---

## 10. Verständnisbestätigung

Die finale V3X-Logik ist wie folgt verstanden und im Plan verankert:

1. **Nur** die Entry-`support_drawing_id` überwachen.  
2. **Bruchkerze** = abgeschlossener Close unter der Linie.  
3. **Folgekerze** gegen Bruch-**Close** mit ±0,20 %: unten = Exit-Signal, oben = halten, dazwischen = neutral.  
4. **Fill** = nächster 5m-Open nach der Bestätigungskerze.  
5. Nach **Aufwärts**-Bestätigung oder **Reclaim über die Linie** bleibt der Long offen; dieselbe Linie wird weiter beobachtet; **spätere** bestätigte Brüche sind möglich.  
6. **Neutral unter der Linie:** letzte Kerze wird neue Bruchkerze (Rollen); **neutral/über der Linie:** Überwachung von vorne.  
7. **SL und Pool-/Kanal-TP** bleiben parallel aktiv; erstes gültiges Exit-Ereignis gewinnt.  
8. **Keine** Zusatzindikatoren oder verschachtelten BR-Varianten in der Zielregel.

**STOP — keine Implementierung, kein Backtest, keine Strategieänderung.**
