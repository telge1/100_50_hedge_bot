<timestamp>Thursday, Oct 8, 2026, 1:19 PM (UTC+3)</timestamp>
<user_query>
# FORENSIC REVIEW — Original Live Micro LLD V2 Engine

## Auftrag: Vollständige Analyse vor Multi-Timeframe-Persistenzarchitektur

Wir wollen langfristig eine einzige kanonische Live-Micro-LLD-V2-Pool-Quelle entwickeln, aus der sowohl unsere zukünftigen Scanner als auch die Research-UI exakt dieselben Pool-Zustände beziehen.

**Oberstes Ziel: Scanner und UI müssen zwingend 1:1 dieselben originalen V2-Pools verwenden.**

Dafür müssen wir zuerst vollständig verstehen, wie unsere bereits vorhandene V2-Engine tatsächlich funktioniert.

Dies ist ausdrücklich noch KEIN Implementierungsauftrag.

---

# 0. STRIKTE SICHERHEITSREGELN

**READ-ONLY — KEINE ÄNDERUNGEN.**

Verboten:

- Kein Code ändern.
- Keine neuen Dateien im Repository erstellen.
- Keine Pool-Engine ändern.
- Keine PD10-Parameter ändern.
- Keine Scanner- oder Strategie-Logik ändern.
- Keine ClickHouse-Tabellen erstellen oder verändern.
- Keine historischen Daten überschreiben.
- Keine Warmup- oder Backfill-Jobs starten.
- Keine laufenden Prozesse stoppen oder restarten.
- Keine Änderungen an Short/Long/Collector/Dashboard.
- Kein Commit.
- Keine produktiven Daten verändern.

Erlaubt:

- Originalen Source-Code lesen.
- Funktionsaufrufe und Datenflüsse verfolgen.
- Bestehende Konfigurationen analysieren.
- Vorhandene Tests lesen.
- ClickHouse-Schema und vorhandene Metadaten ausschließlich read-only untersuchen.
- Kleine isolierte, nicht persistierende Analysen durchführen, sofern sie keine produktiven Dienste belasten.

Bei Unsicherheiten ausdrücklich `NOT_VERIFIED` melden.

Keine Annahmen als bestätigte Fakten darstellen.

---

# 1. ORIGINALEN V2-CODE VOLLSTÄNDIG ANALYSIEREN

Primärer Einstiegspunkt:

`trading_research_platform/indicators/live_micro_lld_v2/engine.py`

Untersuche auch sämtliche direkt oder indirekt verwendeten:

- Helper
- Modelle und Dataclasses
- Konfigurationen
- Pool-Konverter
- Chart-Adapter
- Candle-Adapter
- State-Funktionen
- Tests

Erstelle zuerst einen vollständigen Call-Graph der tatsächlichen Pool-Berechnung.

Nicht nur Funktionsnamen sammeln.

Für jede relevante Funktion erklären:

1. Welche Daten bekommt sie?
2. Welche Berechnung führt sie aus?
3. Welche Zustände verändert sie?
4. Welche Daten gibt sie zurück?
5. Von welchen anderen Funktionen hängt sie ab?

Alle Aussagen mit konkreten Dateipfaden, Funktionsnamen und Zeilennummern belegen.

## 1.1 Exakte Pool-Entstehung

Untersuche Schritt für Schritt:

- Welche Candle-Daten benötigt die Engine?
- Welche Rolle spielt das Volumen?
- Wie werden Swing Highs und Swing Lows erkannt?
- Wie erfolgt die Pivot-Bestätigung?
- Wie entsteht ein neuer Pool?
- Wann ist ein Pool erstmals bekannt?
- Wie wird `known_at` bestimmt?
- Wie wird die `pool_id` erzeugt?
- Wie werden obere und untere Preisgrenze bestimmt?
- Können mehrere Pools zusammengefasst werden?
- Können bestehende Pools erweitert werden?
- Werden Pools später erneut bewertet?

Unterscheide unbedingt:

- Pivot-Kerze
- Bestätigungskerze
- Pool-Entstehung
- `known_at`
- erste erlaubte Sichtbarkeit
- erste erlaubte Scanner-Verwendung

**Keine eigenen Regeln ergänzen. Ausschließlich den Originalcode beschreiben.**

---

# 2. VOLLSTÄNDIGEN POOL-LIFECYCLE REKONSTRUIEREN

Wir müssen verstehen, was mit einem Pool nach seiner Entstehung passiert.

Analysiere:

- Pool-Erstellung
- Pool-Aktivierung
- Pool-Verstärkung
- Strength-Veränderung
- Bounds-Veränderung
- mögliche Pool-Zusammenführung
- Break-Erkennung
- Inaktivierung
- historische Aufbewahrung
- eventuelle Lösch- oder Pruning-Mechanismen

Für jedes Pool-Feld feststellen:

**A. Immutable:** Nach Entstehung unveränderlich.

**B. Mutable:** Kann sich später verändern.

**C. Derived:** Wird aus anderen Daten berechnet.

**D. Historical:** Muss zur exakten historischen Rekonstruktion versioniert werden.

Erstelle dafür eine vollständige Feld-Matrix.

Besonders untersuchen:

- `pool_id`
- `known_at`
- `break_at`
- `start_timestamp`
- `end_timestamp`
- `strength`
- `strength_class`
- obere/untere Bounds
- Aktivitätsstatus
- Seiten-/Richtungsinformation
- alle weiteren tatsächlich vorhandenen Felder

Keine nicht existierenden Felder unterstellen.

## Zentrale Frage

Wenn ein Pool am Zeitpunkt T einen bestimmten Zustand hat und später verändert wird:

**Kann die originale Engine den Zustand von T vollständig und exakt rekonstruieren, ohne spätere Informationen zu verwenden?**

---

# 3. MULTI-TIMEFRAME-FORENSIK

Unsere endgültige Architektur muss folgende sechs Timeframes unterstützen:

- 1m
- 5m
- 15m
- 30m
- 1h
- 4h

Für jeden TF getrennt prüfen:

1. Unterstützt die originale Engine diesen TF tatsächlich?
2. Welche Candle-Quelle wird verwendet?
3. Welche Timeframe-Parameter gelten?
4. Wird PD10 identisch angewendet?
5. Ist die Engine TF-unabhängig parametrisiert?
6. Werden Pools pro TF unabhängig erzeugt?
7. Gibt es TF-übergreifende Abhängigkeiten?
8. Wie werden Pool-IDs über TFs eindeutig?
9. Welche Close-Timestamp-Semantik wird verwendet?
10. Wie wird die letzte vollständig geschlossene Candle bestimmt?
11. Wie werden Candle-Gaps behandelt?
12. Wie wird mit unvollständigen Candles umgegangen?

Nicht annehmen, dass die bestehende 15m-Logik automatisch für alle TFs korrekt ist.

## 3.1 Native Candles vs 1m-Aggregation

Bitte prüfen, ob die Engine derzeit:

- native TF-Candles konsumiert,
- aus 1m aggregierte Candles konsumiert,
- oder mehrere Datenwege unterstützt.

Bewerte die Möglichkeit einer gemeinsamen 1m-Datenbasis nur konzeptionell.

Keine Umstellung vorschlagen, bevor nachgewiesen ist, dass daraus exakt dieselben Candle-Werte und Pool-Ergebnisse entstehen.

---

# 4. WARMUP UND HISTORISCHE POOL-VOLLSTÄNDIGKEIT

Hier besonders sorgfältig arbeiten.

Wir unterscheiden vier verschiedene Dinge:

**A. Rechen-Warmup**

Wie viele vergangene Candles braucht die Engine, um überhaupt gültige Pools zu berechnen?

**B. Historischer Pool-Horizont**

Wie weit muss die Berechnung zurückreichen, damit ältere, noch relevante Pools nicht fehlen?

**C. Initialer historischer Aufbau**

Wie werden alle benötigten Pools vom gewählten historischen Startpunkt bis zur aktuell letzten geschlossenen Candle aufgebaut?

**D. Laufende Live-Fortschreibung**

Wie wird der Zustand anschließend mit jeder neuen geschlossenen Candle fortgeführt?

Für alle sechs TFs untersuchen:

- minimaler Warmup in Bars
- Warmup in Stunden/Tagen
- Lookback-Abhängigkeiten
- historische Pool-Lebensdauer
- mögliche unbegrenzt aktive Pools
- Verhalten bei kurzen Ladefenstern
- Unterschiede zwischen kurzen und langen Prefix-Läufen
- erforderliche Datenkontinuität

Bisher wurde für eine PD10-Konfiguration ein Warmup von 366 Bars beobachtet.

**Bitte diesen Wert unabhängig aus dem Originalcode verifizieren und nicht ungeprüft als universellen Warmup übernehmen.**

Insbesondere feststellen, ob eine endliche Mindesthistorie die vollständige aktive Pool-Menge garantiert oder ob ältere Pools erhalten bleiben können.

---

# 5. PREFIX-SAFETY UND HISTORISCHE KAUSALITÄT

Prüfe die tatsächlichen Implementierungen von:

- `known_at`
- `break_at`
- `pools_as_of`
- `active_at_as_of`
- `pools_to_zones`
- vorhandener Prefix-/Replay-Logik

Beantworte:

1. Können Pools vor `known_at` erscheinen?
2. Werden Pivot-Pools rückwirkend erzeugt?
3. Können Strength-Werte zukünftige Candles enthalten?
4. Kann ein späterer Break den früheren Zustand verändern?
5. Können Preisgrenzen nachträglich angepasst werden?
6. Ist ein Prefix-Lauf bis T exakt gleich einem korrekt auf T rekonstruierten längeren Lauf?
7. Gilt dies für sämtliche Pool-Felder oder nur für die sichtbaren Chart-Segmente?

Wichtig:

Vorhandene Tests sind Belege, aber kein automatischer Beweis für sämtliche Symbole, TFs und Datenfenster.

Bitte klar unterscheiden zwischen:

- durch Code nachgewiesen
- durch vorhandene Tests nachgewiesen
- nur teilweise bestätigt
- noch nicht verifiziert

---

# 6. INKREMENTELLE ENGINE-FORTSCHREIBUNG

Das ist einer der wichtigsten Teile der Analyse.

Unsere spätere Architektur soll nicht ständig die komplette Historie neu berechnen müssen.

Bitte feststellen:

**Unterstützt die originale V2-Engine bereits eine echte inkrementelle State-Fortschreibung?**

Prüfen:

- Gibt es persistierbare Engine-State-Objekte?
- Gibt es eine Update-Funktion für neue Candles?
- Kann die Engine mit exakt einer neuen abgeschlossenen Candle fortfahren?
- Welche historischen Candles müssen im Speicher bleiben?
- Welche offenen Pivot-Kandidaten müssen erhalten bleiben?
- Welche aktiven Pools müssen erhalten bleiben?
- Welche Strength-Akkumulatoren werden benötigt?
- Welche Volumenhistorie wird benötigt?
- Welche Break-Zustände werden benötigt?
- Welche Pool-ID-Zähler oder Identitätsinformationen werden benötigt?

Falls die Engine bislang ausschließlich vollständige Batch-/Prefix-Läufe unterstützt:

Dies ausdrücklich feststellen.

**Keine neue inkrementelle Logik entwerfen, bevor wir verstanden haben, ob und wie sie sich exakt äquivalent zur bestehenden Original-Engine implementieren ließe.**

---

# 7. CHECKPOINT- UND RESUME-FÄHIGKEIT

Wir wollen später nach einem Prozessneustart exakt fortfahren können.

Untersuche, ob sich der vollständige Engine-Zustand serialisieren lässt.

Welche Informationen müssten in einem Checkpoint enthalten sein?

Insbesondere:

- Engine-Version
- Parameter-Version
- Symbol
- TF
- letzter abgeschlossener Candle-Close
- letzte verarbeitete Candle
- aktive Pools
- historische Pool-Zustände, soweit benötigt
- offene Pivot-Kandidaten
- Volumen-/Lookback-Fenster
- Strength-relevante Zustände
- Break-relevante Zustände
- Pool-ID-Generierungszustand
- weitere interne Abhängigkeiten

Prüfe, ob ein Checkpoint mit anschließender Live-Fortschreibung exakt dasselbe Ergebnis erzeugen könnte wie ein vollständiger Prefix-Lauf.

Unterscheide:

- logisch gleiche Pool-Mengen
- gleiche Pool-IDs
- gleiche Bounds
- gleiche Strengths
- gleiche Break-Zeiten
- kanonisch feldgleiche Zustände
- echte bitweise Identität

Eine bitgenaue Gleichheit darf nur behauptet werden, wenn Datentypen, Float-Arithmetik, Serialisierung und deterministische Reihenfolge dies ermöglichen.

---

# 8. EVENT-SOURCING-EIGNUNG DER ORIGINALEN ENGINE

Bitte prüfen, ob wir den Pool-Lifecycle als unveränderliche Ereignisse speichern könnten.

Mögliche Ereignistypen nur dann berücksichtigen, wenn die Engine entsprechende reale Zustandsübergänge besitzt:

- Pool Created
- Pool Strength Changed
- Pool Bounds Changed
- Pool Broken
- weitere tatsächlich vorhandene Änderungen

Wir wollen keine künstlichen Ereignisse erfinden, die von der Original-Engine nicht eindeutig ableitbar sind.

Für jede mögliche Zustandsänderung prüfen:

1. Ist sie im Originalcode eindeutig erkennbar?
2. Hat sie einen eindeutigen kausalen Zeitpunkt?
3. Ist ihre Reihenfolge deterministisch?
4. Gibt es eindeutige Pool-IDs?
5. Sind alle benötigten neuen Werte verfügbar?
6. Kann der Zustand dadurch verlustfrei rekonstruiert werden?

**Zentrale Forschungsfrage:**

Reichen unveränderliche Pool-Ereignisse plus periodische Engine-Checkpoints aus, um jeden historischen Prefix-Zustand vollständig und kausal korrekt zu rekonstruieren?

Oder benötigen wir vollständige Zustands-Snapshots?

Bitte anhand des Originalcodes beantworten, nicht anhand allgemeiner Event-Sourcing-Theorie.

---

# 9. CLICKHOUSE-ARCHITEKTUR BEWERTEN

Noch nichts implementieren.

Vergleiche drei Modelle:

### Modell A — Vollständige Snapshots

Für jeden TF-Decision-Close wird der komplette kanonische Pool-State gespeichert.

### Modell B — Immutable Events + Checkpoints

Wir speichern sämtliche Pool-Zustandsänderungen und regelmäßige vollständige Engine-Checkpoints.

### Modell C — Hybrid

Wir speichern:

- originale Pool-Ereignisse
- vollständige Checkpoints
- ausgewählte vorberechnete As-Of-Zustände
- versionierte Veröffentlichungs-Metadaten

Für alle drei Modelle bewerten:

- exakte historische Rekonstruktion
- Live-Fortschreibung
- Wiederanlauf
- Datenkonsistenz
- Datenvolumen
- Schreiblast
- Leseperformance
- GO-TO-Geschwindigkeit
- manuelles Chart-Scrollen
- Multi-TF-Zugriff
- Scanner-/UI-Parität
- Debugging
- Komplexität
- Fehlertoleranz

Prüfe auch, welche vorhandenen ClickHouse-Tabellen und Datenquellen wiederverwendbar wären.

Keine Tabellen erstellen.

---

# 10. ZWINGENDE SCANNER-/UI-1:1-PARITÄT

Unser wichtigstes Architekturziel:

Scanner und UI dürfen niemals verschiedene Pool-Zustände als denselben historischen Zustand behandeln.

Bitte konzeptionell untersuchen, wie wir dies technisch garantieren können.

Vorgesehene Eigenschaften:

- genau eine autoritative V2-Berechnung pro definiertem Symbol/TF/Decision-Close/Version
- eindeutige State-ID
- Engine- und Parameter-Version
- Datenquellen-/Datenrevisionskennung
- kanonischer Pool-State-Hash
- validierte Veröffentlichung vollständiger Zustände
- gemeinsamer Pool-State-Reader für Scanner und UI

Prüfen:

1. Welche dieser Mechanismen sind notwendig?
2. Welche sind optional?
3. Welche passen zur bestehenden Codearchitektur?
4. Wie verhindern wir unfertige States?
5. Wie verhindern wir doppelte Events?
6. Wie verhindern wir fehlende Candles?
7. Wie unterscheiden wir einen tatsächlich leeren Pool-State von fehlenden Daten?
8. Wie gehen wir mit verspäteten oder korrigierten Candle-Daten um?
9. Wie verhindern wir, dass ein späterer Backfill historische Scanner-Zustände stillschweigend verändert?

Scanner-spezifische Pool-Filter gehören ausschließlich in die Scanner-Entscheidungslogik.

Sie dürfen die kanonischen Original-Pools niemals verändern.

Die UI muss sowohl die vollständigen Original-Pools als auch später die vom Scanner tatsächlich verwendeten Pool-IDs darstellen können.

---

# 11. MULTI-TF-STATE-SYNCHRONITÄT

Unsere Scanner sollen später alle sechs Timeframes verwenden können.

Analysiere, wie wir pro Scannerentscheidung einen kausal korrekten Multi-TF-State bestimmen.

Beispiel:

Ein Signal entsteht zu Zeitpunkt T.

Dann müssen für 1m, 5m, 15m, 30m, 1h und 4h jeweils die letzten tatsächlich verfügbaren, vollständig veröffentlichten TF-Zustände ausgewählt werden.

Keine zukünftige Candle.

Keine unvollständige Forming-Candle.

Keine willkürliche Vermischung alter und neuer Pool-State-Versionen.

Prüfe das Konzept eines Multi-TF-State-Vektors:

- Scanner-Decision-Timestamp
- State-ID pro TF
- tatsächlicher TF-Decision-Close
- Engine-/Parameter-Version
- Pool-State-Hash

Dabei die tatsächliche Datenverfügbarkeit berücksichtigen, nicht nur theoretische Candle-Close-Zeitpunkte.

Noch keine Implementierung.

---

# 12. HISTORISCHER AUFBAU UND LIVE-CUTOVER

Bitte konzeptionell prüfen, wie ein späterer einmaliger historischer Aufbau bis zum Live-Betrieb ablaufen könnte.

Untersuche:

1. Historischen Startpunkt bestimmen.
2. Notwendige Eingangsdaten vollständig prüfen.
3. Originale V2-Engine kausal über die Historie ausführen.
4. Pool-Zustände bzw. Pool-Ereignisse speichern.
5. Checkpoints erzeugen.
6. Historische Vollständigkeit validieren.
7. Bis zur aktuellen letzten geschlossenen Candle aufholen.
8. Ohne Datenlücken und ohne Doppelverarbeitung in den Live-Betrieb wechseln.
9. Nach einem Neustart exakt fortsetzen.

Wichtig:

Nicht behaupten, dass ein einziger historischer Aufbau automatisch genügt, solange Resume-Parität und historische Coverage nicht nachgewiesen sind.

Keine Warmup-Läufe starten.

---

# 13. ABSCHLIESSENDE ARCHITEKTURENTSCHEIDUNG

Erst nachdem alle vorherigen Punkte untersucht wurden, eine Architektur-Empfehlung abgeben.

Nicht im Voraus auf Event Sourcing, vollständige Snapshots oder Hybrid festlegen.

Die Empfehlung muss sich aus den tatsächlichen Eigenschaften der originalen V2-Engine ergeben.

Bewerte insbesondere:

- kleinstmögliche Veränderung an der bestehenden Codebasis
- Erhalt der originalen Pool-Logik
- deterministische Pool-Zustände
- vollständige historische Kausalität
- 1:1-Scanner-/UI-Parität
- sechs Timeframes
- Restart-Stabilität
- ClickHouse-Performance
- langfristige Wartbarkeit

Wenn für eine belastbare Entscheidung noch Informationen fehlen, ausdrücklich benennen.

---

# 14. ABSCHLUSSBERICHT

Bitte einen strukturierten, technisch belegten Bericht liefern.

## A. Originale Engine

ORIGINAL_ENGINE_ENTRYPOINT = ...

POOL_CREATION_LOGIC = ...

POOL_ID_SEMANTICS = ...

KNOWN_AT_SEMANTICS = ...

BREAK_AT_SEMANTICS = ...

STRENGTH_UPDATE_LOGIC = ...

BOUNDS_MUTABILITY = ...

POOL_LIFECYCLE = ...

## B. Timeframes

TF_SUPPORT_1M = YES/NO/NOT_VERIFIED

TF_SUPPORT_5M = YES/NO/NOT_VERIFIED

TF_SUPPORT_15M = YES/NO/NOT_VERIFIED

TF_SUPPORT_30M = YES/NO/NOT_VERIFIED

TF_SUPPORT_1H = YES/NO/NOT_VERIFIED

TF_SUPPORT_4H = YES/NO/NOT_VERIFIED

TF_INDEPENDENCE = YES/NO/CONDITIONAL

CANDLE_SOURCE_PER_TF = ...

TF_PARAMETER_MATRIX = ...

## C. Historie

WARMUP_REQUIREMENTS_PER_TF = ...

HISTORICAL_POOL_HORIZON = ...

PREFIX_CAUSALITY = PASS/FAIL/NOT_VERIFIED

HISTORICAL_ASOF_PARITY = PASS/FAIL/NOT_VERIFIED

## D. Persistenz

ENGINE_SUPPORTS_INCREMENTAL_UPDATES = YES/NO

ENGINE_STATE_SERIALIZABLE = YES/NO/CONDITIONAL

EXACT_RESUME_POSSIBLE = YES/NO/NOT_VERIFIED

POOL_EVENTS_LOSSLESS = YES/NO/NOT_VERIFIED

EVENT_PLUS_CHECKPOINT_RECONSTRUCTION = YES/NO/NOT_VERIFIED

FULL_SNAPSHOTS_REQUIRED = YES/NO/NOT_VERIFIED

## E. Architektur

RECOMMENDED_PERSISTENCE_MODEL = ...

RECOMMENDED_CANONICAL_STATE_MODEL = ...

RECOMMENDED_MULTI_TF_DESIGN = ...

RECOMMENDED_SCANNER_UI_READER = ...

LIVE_CUTOVER_REQUIREMENTS = ...

OPEN_TECHNICAL_RISKS = ...

SAFE_TO_BEGIN_IMPLEMENTATION = YES/NO

## F. Konkrete Empfehlung

Zum Schluss bitte:

1. Die tatsächlich beste Architektur für unsere vorhandene V2-Engine nennen.
2. Begründen, warum andere Varianten weniger geeignet sind.
3. Offene Risiken priorisieren.
4. Einen minimalen schrittweisen Implementierungsplan vorschlagen.
5. Exakt benennen, welche Nachweise vor dem ersten produktiven V2-Scannerbetrieb erforderlich sind.

**Keine Implementierung durchführen.**

Alle wesentlichen Aussagen mit konkreten Originaldateien, Funktionen und Zeilennummern belegen.

Wenn eine Eigenschaft nicht eindeutig nachweisbar ist, `NOT_VERIFIED` melden.

Keine theoretischen Annahmen als tatsächliches Engine-Verhalten ausgeben.

---

# ABSOLUTE PRIORITÄT

Wir dürfen die originale Live-Micro-LLD-V2-Pool-Logik nicht verändern oder durch eine ähnliche Eigenimplementierung ersetzen.

Die spätere ClickHouse-Architektur muss die Ergebnisse der originalen Engine exakt bewahren.

**Unser Ziel ist nicht, neue Pools zu berechnen, sondern die originalen V2-Pools historisch vollständig, kausal korrekt, reproduzierbar und für Scanner sowie UI zwingend identisch verfügbar zu machen.**
</user_query>