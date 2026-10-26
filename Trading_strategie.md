# DOGEUSDT – Short-Limit-Setup vom 28.09.2026

**Status:** Historischer, visuell ausgewählter Referenzfall; kein unabhängig validierter Trading-Vorteil.  
**Symbol:** DOGEUSDT · **Chart:** 15m · **Zeitangaben:** UTC  
**Kontext-/Entscheidungszeitpunkt:** 28.09.2026, 00:15  
**Geplante Ausführung:** Short-Limit an der Unterkante des ausgewählten oberen Liquiditätspools, nur wenn die Order vor dem Kontakt platziert wurde.  
**Richtung:** Short.

## 1. Meine Handelsidee

Der Short wurde **nicht erst nach einer bestätigten Ablehnung** gesucht. Entscheidend war, dass der Kontext und die relevanten Pools **bereits mehrere Kerzen vor dem erneuten Kontakt** erkennbar waren. Daher hätte man nach der Marktanalyse eine **Short-Limit-Order an der Unterkante der ausgewählten Upper-Pool-Zone** platzieren können. Ein **Stop-Loss 0,2 % oberhalb ihrer Oberkante** und das **Take-Profit an den damals bereits bekannten grünen Lower-Pools** bilden das hypothetische Setup.

Die Screenshot-Beobachtung „ungefähr fünf Kerzen vorher sichtbar“ ist als **Nutzerbeobachtung** festgehalten. Der genaue erste kausale Zeitpunkt, ab dem gerade **dieser Upper-Pool** und die Zielpools gemeinsam ACTIVE/bekannt waren, muss anhand der Original-IDs und `known_at`-Werte geprüft werden; fünf Kerzen dürfen nicht ohne Nachweis als bestätigte Lead-Time gelten.

## 2. Vorgeschichte und vollständiger Marktkontext

1. **27.09., ca. 12:45:** Ein Higher High mit einem High von ungefähr **0,09887** wurde in Phase 6V bestätigt (ca. +1,19 % gegenüber dem zurückliegenden 32×15m-Vergleich). Der Preis lag am damaligen 12:45-Close oberhalb EMA200. **Wichtig:** Die Annahme, EMA9, EMA20 und EMA59 hätten *damals* alle unter EMA200 gelegen, wurde widerlegt; alle drei lagen am 12:45-Close darüber.
2. Anschließend fiel der Preis wieder unter EMA200 und die Marktstruktur entwickelte sich weiter. Zum eigentlichen Analysezeitpunkt **28.09., 00:15** waren **EMA9, EMA20 und EMA59 tatsächlich unter EMA200** (Phase 6V2: `CONFIRMED_ASOF`). Das war das bearishe EMA-Umfeld für die geplante Limit-Order.
3. **Upper-Kontext:** Der Preis sollte beim erneuten Anstieg die ausgewählte, historisch bereits bekannte obere Poolzone erreichen. Die Poolunterkante bildet den beabsichtigten Short-Limit-Preis; ihre Oberkante ist die Referenz für den Stop-Loss. Die exakte **Original-Pool-ID, PD-Stufe, Unter-/Oberkante, Strength, ACTIVE-Status und `known_at`** müssen aus dem 00:15-As-of-Snapshot eindeutig zugeordnet werden.
4. **Lower-Kontext:** Grüne Lower-Pools um etwa **0,0955** waren schon vor dem geplanten Einstieg vorhanden. Phase 6V/6V2 bestätigte einen einschlägigen Micro-Lower mit `known_at` **27.09., 22:45 UTC**. Der Abstand zur gezeichneten Entry-Region betrug ungefähr **1,91–2,05 %** (je nach Referenzpreis und Zielgrenze). Die genaue TP-Grenze muss der Original-Pool-ID entnommen werden.
5. **Lücke im Upper-Bereich:** Im Screenshot wurde ein Abstand von ungefähr **0,7 %** zwischen ausgewählten Upper-Pool-Grenzen markiert. **Dieser Wert ist nicht als geometrisch freie 0,7-%-Lücke bestätigt.** Phase 6V fand für PD10 am früheren Higher High als größte benachbarte Upper-Lücke ungefähr **0,45 %**. Abstand zweier ausgewählter Linien, vollständiger von allen ACTIVE-Pools unbedeckter Preisraum und Distanz bis zur nächsten relevanten oberen Struktur bleiben getrennte Größen.

## 3. Vorgesehene Orderregeln – genau dieser Referenzfall

**Signal-/Ordervorbereitung:** Frühestens nach dem vollständig geschlossenen 15m-Bar um **00:15 UTC**, auf Grundlage ausschließlich vorher bekannter Kerzen, EMAs und originaler ACTIVE-LMV2-Pools. Eine frühere Platzierung wäre nur nach separater Prüfung einer entsprechend früheren, kausalen As-of-Zeit möglich.

**Short-Limit-Entry:**

- `entry_limit = bottom` (Unterkante) **des eindeutig zu identifizierenden Upper-Pools**.
- Visuell eingezeichnete Entry-Region: etwa **0,0975–0,0976**; keine bestätigte exakte originale Poolunterkante.
- **Kein Erfordernis**, vor Limit-Platzierung auf ein nachträgliches Abweisungssignal zu warten.
- Ein tatsächlicher Fill ist aus 1m-OHLC-Daten allein nicht garantiert; Orderbuch, Tick-Reihenfolge, Liquidität und Gebühren sind separat zu beachten.

**Stop-Loss:**

- `SL = selected_upper_pool.top × (1 + 0.002)` für den Short (0,2 % oberhalb **der Oberkante des ausgewählten Pools**).
- Nicht 0,2 % über dem Entry und nicht pauschal über dem gesamten höchsten Cluster.
- Exakter numerischer SL erst nach Zuordnung der richtigen Original-Pool-ID und ihrer damaligen Oberkante.

**Take-Profit:**

- Ziel an der konkret ausgewählten, **zum Entscheidungszeitpunkt bekannten ACTIVE-Lower-Pool-Zone**, ungefähr **0,09555** laut Zeichnung.
- Für das definitive mechanische TP muss die verwendete Lower-Grenze festgelegt und mit Pool-ID/`known_at` belegt werden.
- Frühere oder näherliegende Lower-Pools dürfen nicht nachträglich ignoriert werden, nur um ein attraktiveres Ziel zu erhalten.

**Vorbereitende Plausibilitätsprüfung:** Vor Orderplatzierung die gesamte Upper-/Lower-Landschaft und **tatsächlich freie Preisintervalle in beide Richtungen** erfassen. Der Nutzer beschreibt die Kombination aus bearishem EMA-Kontext, vorangegangenem Higher High, einer höheren Upper-Struktur, dem geplanten Retest und bestehenden Lower-Zielen – nicht lediglich einen isolierten Upper-Touch.

## 4. Bereits codegestützt festgestellte Zeitfolge (Phase 6V2)

| Zeit (UTC) | Fakt | Bedeutung |
|---|---|---|
| 27.09., ca. 12:45 | Higher High bis ca. 0,09887 | Historischer Strukturkontext; EMA9/20/59 am damaligen Close **nicht** unter EMA200 |
| 27.09., 22:45 | Relevanter Lower-Pool um 0,0955 bekannt | Potenzielles unteres Ziel vor geplanter Order vorhanden |
| 28.09., 00:15 | 15m-Close **0,09665**; EMA9/20/59 unter EMA200 | Zeitpunkt der hypothetischen Kontextentscheidung; **noch kein Fill** bei 0,0975–0,0976 |
| 28.09., 00:20 | Erstes 1m-High ≥ **0,09750** | Erster dokumentierter Kontakt zur unteren visuellen Entry-Referenz |
| 28.09., 00:21 | Erstes 1m-High ≥ **0,09760** | Zweite visuelle Entry-Referenz erreicht |
| 28.09., 00:30 | 15m-Close **0,09777** | Spätere Referenz, **kein nachgewiesener Fill zum Close** |

Phase 6V2: Eine **vorher platzierte** Short-Limit-Order ist für den Kontaktzeitraum **`EXECUTABLE_UNDER_ASSUMPTIONS`**, nicht als real ausgeführt bewiesen. Eine erst **nach bestätigter Ablehnung** platzierte Order war für das untersuchte Fenster **`EXECUTION_NOT_VERIFIED`**.

## 5. Was für die endgültige Mechanisierung noch fehlt

- Eindeutige **Upper-Pool-ID und PD-Stufe**, Unter-/Oberkante, Strength, `known_at`, ACTIVE-Status um 00:15.
- Eindeutige **Lower-Pool-ID**, konkret verwendete TP-Grenze sowie alle dazwischenliegenden Poolintervalle.
- Numerischer SL nach `top × 1.002`; danach Chance-Risiko-Verhältnis mit dokumentierten Gebühren.
- Prüfung, **ab wann** beide maßgeblichen Zonen gemeinsam bekannt waren, insbesondere der behaupteten fünf Kerzen Vorlaufzeit.
- Verlässliche Limit-Fill-Modellierung und getrennte spätere Exit-Auswertung.
- Weitere **positive und negative** Vergleichsfälle, damit das schöne historische Beispiel nicht zu einer rückblickend optimierten Regel wird.

## 6. Status und Forschungsdisziplin

**Belegt:** Higher High als Vorgeschichte; bearish EMA9/20/59 vs. EMA200 **am 28.09. um 00:15**; zuvor bekannter Lower-Bereich; Kurskontakt mit der visuellen Entry-Zone **ab 00:20/00:21**.

**Nicht belegt:** exakt 0,7 % vollständig poolfreier Upper-Gap; genaue ausgewählte Upper-Pool-ID und damit exakter Stop; ein realer Limit-Fill; dass dieselbe Gesamtkonstellation statistisch profitabel ist.

**Kein** Live-Orderauftrag, keine Strategieaktivierung, keine V1/V2-Änderung. Dieser Fall wird als **Referenzbeispiel für ein im Voraus geplantes Short-Limit-Setup** dokumentiert, nicht als unabhängig validierter Gewinnertrade.

### Bestehende Forschungsnachweise

- `logs/historical_import/overnight_2026/phase6v_single_short_full_context/report.md`
- `logs/historical_import/overnight_2026/phase6v2_short_context_entry_timing/report.md`
- `logs/historical_import/overnight_2026/phase6v2_short_context_entry_timing/asof_0015_frozen.json`
- `logs/historical_import/overnight_2026/phase6v2_short_context_entry_timing/entry_window_1m.csv`