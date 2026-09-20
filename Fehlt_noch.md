Wichtige Dinge aus dem Plan, die noch fehlen
1. Microprice- und stabiler Reclaim

Das ist wahrscheinlich der wichtigste noch fehlende Baustein vor dem Classifier.

Wir messen bisher gut, was mit der Wall passiert. Zusätzlich müssen wir messen:

dreht der Microprice zurück?
kehrt der Preis wirklich auf die Verteidigerseite zurück?
wie lange bleibt er dort?
war es nur ein kurzer Rücksprung oder ein stabiler Reclaim?
wie viele Preislevel wurden beim Angriff konsumiert?
verändert oder erweitert sich der Spread?

Denn eine Wall kann stark refillen, ohne dass der Preis tatsächlich dreht. Das allein wäre noch kein Long- oder Short-Signal.

2. Normalisierte Absorption und Vacuum Score

Unsere Absorptionswerte sind momentan ausdrücklich NOT_CALIBRATED.

Der Plan enthält dafür:

normalisierte Impact Efficiency,
Absorption Ratio,
Vacuum Score.

Das ist wichtig, weil beispielsweise dieselbe Preisbewegung auf BTC und DOGE etwas völlig anderes bedeuten kann.

Verglichen werden muss immer mit der normalen Situation dieses Coins:

aktuelle Preiswirkung
gegen
historisch normale Preiswirkung bei ähnlicher
Volatilität, Tiefe, Spread und Tageszeit

Erst dann können wir sinnvoll sagen:

ungewöhnlich starke Absorption,
normaler Markt,
Liquiditätsvakuum.
3. Wall-Bewegung innerhalb des Bandes

Das steht im Plan nur teilweise, sollte aber später ausdrücklich ergänzt werden.

Wir sollten nicht nur die Gesamtmenge im Band betrachten, sondern auch:

wandert die Wall zum Preis hin?
weicht sie vor dem Preis zurück?
wird die Front abgebaut, während dahinter neue Liquidität entsteht?
verschiebt sich der Schwerpunkt der Liquidität?
bleibt nur die Gesamtmenge gleich, obwohl die ursprüngliche Wall verschwindet?

Beispiel:

Wall bei 79.780 verschwindet
neue Menge erscheint bei 79.781

Die Bandmenge wirkt stabil, aber die Verteidigung ist möglicherweise zurückgewichen. Dafür brauchen wir eine Art wall_band_centroid beziehungsweise wall_migration_ticks.

Das könnte für die Unterscheidung zwischen echter Verteidigung und kontrolliertem Zurückweichen sehr nützlich sein.

4. Genaue Definition: Wann gilt eine Wall als gehalten oder gebrochen?

Der Plan beschreibt die Zustände, aber vor der historischen Skalierung müssen die Outcomes exakt festgelegt werden.

Zum Beispiel:

HELD: Preis erreicht Wall, bleibt darunter und reclaimt anschließend die Verteidigerseite.
BROKEN: Preis handelt über der Wall und bleibt dort eine Mindestdauer.
FAKE_BREAK_RECLAIM: Preis durchbricht kurz und kehrt zurück.
PULLED: Wall verschwindet überwiegend ohne entsprechende Trades.
CENSORED: Datenende, Gap oder Beobachtungsfenster zu kurz.
INCONCLUSIVE: keine eindeutige Entwicklung.

Wir sollten außerdem getrennte Horizonte auswerten:

5 s / 15 s / 30 s / 60 s / 5 min / 15 min / 30 min

Eine Wall kann 30 Sekunden halten und nach fünf Minuten trotzdem brechen. Ohne diese zeitliche Trennung würden wir „hält“ und „hält kurzfristig“ vermischen.

5. Mehrere Decision-Snapshots statt nur eines Ergebnisses

Das ist besonders wichtig für einen später handelbaren Trigger.

Wir sollten pro Episode fragen:

Was wussten wir beim ersten Touch?
Was wussten wir nach einer Sekunde?
Was wussten wir nach drei, fünf, zehn und 15 Sekunden?
Wann war die erste belastbare Klassifikation möglich?
Welcher ausführbare Einstiegspreis war zu diesem Zeitpunkt noch erreichbar?

Sonst könnten wir zwar einen Reclaim korrekt erkennen, aber erst dann, wenn die profitable Bewegung schon fast vorbei ist.

6. OB1000-/OB-Full-Coverage-Vertrag

Das ist nach unserer letzten Frage eine sinnvolle Ergänzung zum Plan.

Pro Episode sollte gespeichert werden:

verwendete Quelle: OB1000 oder OB Full,
war die Wall im gesamten Pre-Touch-Fenster sichtbar?
Abstand der Wall zum Midprice in Ticks/Basispunkten,
niedrigste verfügbare Book-Tiefe,
ist die Wall erst durch Annäherung in OB1000 sichtbar geworden?
gab es Sequenzlücken oder Epoch-Wechsel?

Dann gilt:

Wall vollständig innerhalb OB1000 beobachtet
→ Episode zulässig

Wall-Herkunft wegen Tiefenbegrenzung unklar
→ Coverage ungültig oder OB Full erforderlich

Damit können wir ältere OB1000-Daten nutzen, ohne sie mit echten Full-OB-Daten gleichzusetzen.

7. Ein echter Preisaktions-Baseline-Test

Die bisherige Ablation vergleicht hauptsächlich:

Base,
Base + OI,
Base + Liquidationen.

Es fehlt noch ein sehr wichtiger Vergleich:

Variante	Informationen
P	nur Zone, Preis, Volatilität und Reclaim
T	P + Public Trades
B	T + Full-L2-Wall-Flow und QDH
O	B + OI
L	B + Liquidationen
ALL	alles zusammen

Damit beantworten wir die entscheidende Frage:

Bringen Full OB und QDH wirklich zusätzlichen Nutzen – oder hätte einfache Preisaktion denselben Ausgang bereits vorhergesagt?

Ohne diesen Vergleich könnten wir einen komplizierten Wall-Classifier bauen, obwohl der Mehrwert vielleicht nur vom Reclaim kommt.

8. Wiederholte Berührungen zusammenfassen

Dieser Punkt steht bereits im Plan, muss aber technisch streng umgesetzt werden.

Wenn der Preis dieselbe Wall innerhalb kurzer Zeit zehnmal berührt, dürfen wir daraus nicht zehn unabhängige Erfolge machen. Das ist häufig nur ein zusammenhängender Angriff.

Wir brauchen deshalb:

attack_cluster_id,
ersten und letzten Touch,
Anzahl der Retests,
Zeit zwischen den Retests,
kumulierte Hits/Refills/Pulls,
Wall-Generationswechsel innerhalb des Clusters.
Was später kommt – aber nicht jetzt

Diese Bestandteile bleiben sinnvoll, sollten jedoch erst nach dem allgemeinen Multi-Episode-Builder folgen:

OI als Shadow-Kontext,
Liquidationen und Forced-Flow-Anteil,
LIQUIDATION_EXHAUSTION_POINT,
Flow Type getrennt vom Wall State,
Feature-Ablation,
coinweise Kalibrierung,
Walk-forward mit Purging und Embargo,
handelbarer Backtest mit Gebühren, Slippage und Latenz,
Cross-Exchange ganz am Ende.
Meine empfohlene aktualisierte Reihenfolge
Laufenden Episode-1-Integrationsnachweis abschließen.
Microprice, Preis-Reclaim und Reclaim-Dauer ergänzen.
Normalisierte Impact Efficiency, Absorption Ratio und Vacuum Score bauen.
Wall-Migration innerhalb des Bandes messen.
Outcomes HELD/BROKEN/FAKE_BREAK/PULLED/CENSORED verbindlich definieren.
Generischen Builder entwickeln, der alle Zone-Visits automatisch erzeugt.
OB1000-/Full-OB-Coverage pro Episode prüfen.
Wiederholte Touches zu Angriffsclustern zusammenfassen.
BTC historisch outcome-blind skalieren.
Preisaktions-Baseline gegen Trades und OB/QDH vergleichen.
Danach DOGE mit coinweiser Normalisierung.
OI und Liquidationen erst als Shadow-Features testen.
Wall-State-Classifier kalibrieren.
Erst danach Signal und handelbaren Backtest bauen.

Der wichtigste zusätzliche Punkt, der im ursprünglichen Plan nicht deutlich genug abgesichert war, ist der Preisaktions-Baseline-Test. Nur dadurch können wir später beweisen, dass unsere Full-OB-Daten tatsächlich einen messbaren Vorteil bringen





##########################################################################

Dein Beispiel ist sehr wichtig:

Erste Ask-Wall bricht
→ Preis steigt etwas
→ neue beziehungsweise nächste Ask-Wall erscheint höher
→ Käufer greifen weiter an
→ Preisfortschritt wird schwächer
→ Käufer verlieren Kraft
→ Preis reclaimt die gesamte Zone nach unten

Dann wäre der Bruch der ersten Wall kein echter freier Breakout gewesen. Möglicherweise hat sich die Verteidigung nur höher verlagert.

Was wirklich passiert sein könnte
1. Verteidigung wurde stufenweise höher gesetzt

Die erste Ask-Wall wird teilweise ausgeführt oder gepullt. Kurz darüber erscheint erneut große Ask-Liquidität.

Der Verkäufer lässt den Preis etwas höher kommen, absorbiert dort weiter und bringt ihn anschließend zurück.

Das wäre eine Art:

STEP_UP_DEFENSE
oder
MIGRATING_ABSORPTION
2. Hinter der ersten Wall lag bereits eine zweite Wall

Die erste Wall war nur die vordere Verteidigung. Nachdem sie gebrochen wurde, trifft der Preis auf eine bereits vorher vorhandene größere Wall.

Dann sollten wir unterscheiden:

erste Wall verschoben
gegen
zweite Wall war schon vorher vorhanden
3. Die erste Wall wurde lediglich abgezogen

Wenn die Wall ohne entsprechende Trades verschwindet, wurde sie nicht wirklich durch Käufer gebrochen.

Dann gilt:

Wall verschwindet
+ kaum ausgeführtes Kaufvolumen
= Pull, nicht echter Durchbruch

Der Preis kann dadurch kurz in ein Liquiditätsvakuum springen und an der nächsten echten Wall scheitern.

4. Echter Breakout

Ein echter Breakout würde anders aussehen:

erste Wall überwiegend ausgeführt,
keine vergleichbare neue Ask-Liquidität direkt darüber,
Book wird in Angriffsrichtung dünner,
mehrere Preislevel werden konsumiert,
Käufer bleiben aggressiv,
Microprice bleibt oberhalb,
Preisfortschritt bleibt effizient,
kein schneller Reclaim.
Das müssen wir über viele Episoden messen

Nach dem laufenden Prompt sollten wir deshalb ausdrücklich die Wall-Migration und gestaffelte Verteidigung ergänzen:

Wann verschwand die erste Wall?
Wie viel davon wurde ausgeführt und wie viel gepullt?
Wann erschien die nächste große Ask-Wall?
War sie bereits vor dem Bruch vorhanden?
Wie viele Ticks liegt sie höher?
Wie ähnlich ist ihre Größe?
Verschiebt sich der Schwerpunkt der Ask-Liquidität nach oben?
Folgt der Preis dieser Verschiebung?
Wie oft wiederholt sich das?
An welcher Wall beginnt der endgültige Reclaim?

So könnten wir später Muster unterscheiden:

Muster	Bedeutung
Wall konsumiert, keine neue Wall	möglicher echter Breakout
Wall gepullt, Preis springt zur nächsten Wall	mögliches Vakuum
Neue Wall erscheint schrittweise höher	wandernde Verteidigung
Mehrere vorher vorhandene Walls	gestaffelte Verteidigung
Wall refilled am gleichen Preis	direkte Absorption
Preis überschreitet Walls, Flow bleibt stark	Breakout-Fortsetzung
Preis folgt Walls, Flow schwächt sich ab	möglicher finaler Reclaim

Eine wichtige Grenze bleibt: Mit Full L2 sehen wir keine Order-IDs. Wir können daher nicht beweisen, dass derselbe Verkäufer seine Order höher verschoben hat. Wir können aber sehr genau einen wall_migration_proxy erkennen:

Alte Wall verschwindet, neue gleichseitige Liquidität erscheint zeitnah höher, Mengen ähneln sich und der Preis folgt ihr.

Und ja: Damit lernen wir tatsächlich langsam die „Sprache“ des Orderbuchs. Nicht mehr:

„Kerze geschlossen, also Einstieg.“

Sondern:

„Wie wurde die Wall entfernt, wo erschien die nächste Liquidität, wie reagierten die Angreifer und wie effizient konnte sich der Preis noch bewegen?“

Genau diese Abläufe müssen wir anschließend über viele Episoden vergleichen. Dann sehen wir, ob wandernde Ask-Verteidigung plus fallende Käuferpersistenz regelmäßig vor einem Reclaim erscheint oder ob das nur in einzelnen Beispielen gut aussieht.



SELLER_CONTROL:
hohe Sell-Aggression + fallender Preis + kleiner Gegenlauf

SELL_ABSORBED:
hohe Sell-Aggression + Preis fällt nicht

BUYER_CONTROL:
Buy-Aggression dominiert + Preis steigt

Für den Einstieg könnte später gelten:

SELLER_CONTROL → Short erlaubt
SELL_ABSORBED → warten
BUYER_CONTROL → kein Short

Orderbuch, nächste Bid-Wall, POC und EMA brauchen wir danach hauptsächlich für Ziel und Trade-Management. Die erste Entscheidung „Short oder nicht?“ könnte tatsächlich schon weitgehend aus Reclaim plus Public-Trade-Aggressivität plus Preisreaktion entstehen.


PID=1420118
RUN_DIR=/home/telgenbuescher/projects/orderbook_analyse_btc30m_v1/obfull_research_engine/runs/btc_30m_builder_full_20260907_v1

date
ps -p "$PID" -o pid,etime,%cpu,%mem,rss,stat
du -sh "$RUN_DIR"
find "$RUN_DIR" -type f | wc -l