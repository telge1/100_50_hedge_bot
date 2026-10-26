# DOGEUSDT – Forschungsnotiz: Liquidity Depletion & Opposing Pool Structure

**Referenzfall:** 10. Juni 2026, 12:45 UTC  
**Strategie:** Original-LMV2 Short V2 (Nearest-Lower), 15m Entry mit 4h-Trendfilter und Floor-Guard  
**Status:** Forschungshypothese, keine freigegebene neue Handelsregel

## Ausgangsbeobachtung

Der Preis befand sich zuvor in einer Abwärtsbewegung und hatte zahlreiche untere Liquiditätspools erreicht oder durchlaufen.

Danach zeigte die untere Poolstruktur teilweise ausgedünnte Bereiche und größere räumliche Lücken. Zwar entstanden neue Lower-Pools, aber die zuvor sichtbare Struktur hatte sich verändert.

Oberhalb des Preises befanden sich bereits seit längerer Zeit Upper-Pools, darunter gelb hervorgehobene Strong-Pools.

Der Preis hatte begonnen, sich nach oben zu erholen, obwohl der 4h-Abwärtstrend weiterhin gültig war.

Unsere Strategie eröffnete trotzdem einen Short.

**Referenztrade:**
- Entry: 0,08394 um 12:45 UTC
- Stop-Loss: 0,08487441
- Take-Profit: 0,08299
- Exit: Stop-Loss um 13:45 UTC
- Nettoverlust: −12,24 USDT bei 1.000 USDT Trade-Notional

## Zentrale Hypothese

Ein Short könnte besonders riskant sein, wenn mehrere Eigenschaften zusammen auftreten:

1. Der Preis hat zuvor viele untere Poolbereiche erreicht.
2. Die ursprüngliche Lower-Pool-Struktur wurde teilweise inaktiv oder räumlich ausgedünnt.
3. Der Preis reagiert aufwärts und entfernt sich vom vorherigen Tief.
4. Oberhalb liegen ältere, kausal bekannte, möglicherweise noch nicht berührte starke Upper-Pools.
5. Der geplante Short-Entry liegt unterhalb dieser Upper-Strukturen; der Stop-Loss liegt auf dem Weg zu ihnen oder innerhalb einer übergeordneten Pool-Zone.

**Arbeitstitel:** Liquidity Depletion & Opposing Pool Structure.

Die zeitliche Abfolge ist wichtiger als die reine Anzahl der Pools in einem einzelnen Snapshot.

## Bestätigte technische Erkenntnisse

- Gelb entspricht der LMV2-Klasse Strong mit Strength ab 4,0 bis unter 6,0.
- Orange entspricht Very Strong ab Strength 6,0.
- Die Farbe basiert auf `strength_class`; visuelle Überlagerungen können die Darstellung beeinflussen.
- Der Entry-Upper-Pool des Referenztrades hatte in PD10 Strength 4,56, war also Strong.
- In PD1/PD5 war die Einstufung derselben Pool-ID bei anderer Poolgeometrie ungefähr Weak.
- Der PD10-Entry-Upper-Pool wurde nach dem Einstieg um 13:31 UTC vom Preis erreicht.
- Der Kurs erreichte um etwa 13:44 UTC weitere obere Poolbereiche und anschließend den SL.
- V2 verwendet die Poolgeometrie, berücksichtigt bislang aber keine Strength-, Alter-, Berührungs- oder Gesamtkontextlogik.
- PD1, PD5 und PD10 zeigen teilweise unterschiedliche Poolüberlappungen und Strukturbreiten.

## Verbindliche V2-TP-Regel

Zuerst den nächsten gültigen ACTIVE-Lower-Pool unter dem Signalpreis bestimmen.

Wenn dessen Oberkante weniger als 0,8 % vom Signal-Close entfernt ist, wird der gesamte Trade blockiert.

Kein Überspringen näherer Pools, um ein größeres TP zu erzwingen.

Die eingefrorene V1 bleibt unverändert; V2 ist eine getrennte Forschungsvariante.

## Noch nicht bewiesen

- Dass Lower-Pool-Berührungen den Rückgang tatsächlich erschöpfen.
- Dass eine Pool-Lücke die folgende Aufwärtsbewegung verursacht.
- Dass alte oder starke Upper-Pools generell vom Preis bevorzugt angelaufen werden.
- Dass die Merkmalskombination zuverlässig Verlusttrades vorhersagt.
- Dass die beobachteten Chart-Pools in jedem einzelnen Fall exakt mit den vom Backtester verwendeten Pools übereinstimmen.
- Dass ein neuer Filter außerhalb der untersuchten Juni-Daten profitabel wäre.

## Verbindlicher Forschungsplan

**Phase 1:** Exakte Identität zwischen historischen Research-Charts, Original-LMV2-ClickHouse-SSOT und Backtester anhand von Pool-ID, Bounds, Strength, ACTIVE-Zustand, Publication-Zeit und Snapshot nachweisen.

**Phase 2:** Zeitliche Poolentwicklung vor einem Signal rekonstruieren: vorhandene Lower-/Upper-Pools, Berührungen, neue und inaktive Pools, strukturelle Lücken, Strength, Alter und vorherige Kursbewegung.

**Phase 3:** Gewinner und Verlierer mit identischen Messgrößen untersuchen. Besonders Verlustserien und erfolgreiche Gegenbeispiele berücksichtigen.

**Phase 4:** Intelligente Mustererkennung anhand ausschließlich zum Signalzeitpunkt verfügbarer Merkmale erforschen.

**Phase 5:** Einen etwaigen neuen Short-Blocker als separate Strategievariante testen und auf bisher nicht zur Entwicklung verwendeten Monaten prüfen.

**Grundprinzip:** Erst beweisen, dass die Daten identisch und zeitlich korrekt sind. Dann den Marktkontext messen. Erst danach optimieren. Keine Veränderung der bisherigen Frozen-V1 oder V2 ohne ausdrückliche Freigabe.










. Die Reihenfolge, die ich festlegen würde
1. Phase 6Q – Pool-Parität: Für den 10. Juni zunächst PD1, PD5 und PD10 vollständig zwischen ClickHouse, Backtester und Chart vergleichen.
2. Phase 6R – Kontextanalyse-Engine: Den historischen Zustandsverlauf der Pools automatisiert rekonstruieren, zuerst für unseren bekannten Verlustfall und einige Gegenbeispiele.
3. Phase 6S – Musterforschung: Dieselben Merkmale für einen größeren Datensatz berechnen, Gewinner und Verlierer vergleichen und Hypothesen prüfen.
4. Phase 6T – Unabhängige Validierung: Erst später neue, bisher nicht zur Regelentwicklung verwendete Monate heranziehen und eine gesonderte Strategievariante testen.