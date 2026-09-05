Die wichtigsten noch fehlenden Werte
1. Wall Fate: Was passiert mit dem Pool beim Angriff?

Das ist wahrscheinlich der zweitwichtigste Baustein nach der Preiseffizienz.

Ein sichtbarer Ask-Pool kann:

ausgeführt und absorbiert werden,
nach Ausführungen refillen,
vor dem Kontakt entfernt werden,
nach oben verschoben werden,
vollständig konsumiert werden,
kurz brechen und anschließend reclaimt werden.
Verhalten der Ask-Wall	Bedeutung
Wall bleibt + refilled	echte Defense, Short interessant
Wall wird durch Trades reduziert	Konsum, Breakout wahrscheinlicher
Wall verschwindet ohne Trades	Pulling/Spoof-Verdacht, kein echter Widerstand
Wall wandert nach oben	Verkäufer weichen zurück, bullish
Preis bricht und hält darüber	Acceptance, Long-Fortsetzung
Preis bricht und fällt zurück	Failed Break, Short interessant

Momentan behandelt die Strategie offenbar zu viele rote Pools gleich. Das müssen wir ändern.

2. Executed Liquidity versus Cancelled Liquidity

Ein Pool von beispielsweise 10 Mio. USDT verschwindet. Aber warum?

Wurde er durch aggressive Käufer gehandelt?
Oder wurde die Order kurz vor dem Kontakt storniert?

Das sind gegensätzliche Informationen:

Executed: Käufer mussten echtes Kapital einsetzen.
Cancelled: Der Widerstand war möglicherweise nie bereit, den Angriff aufzunehmen.

Für einen bullishen Ausbruch wäre besonders stark:

Ask-Liquidität wird zurückgezogen, während aggressive Käufer erscheinen und der Preis sofort in die entstandene Lücke steigt.

Für einen Short:

Ask-Liquidität bleibt liegen, wird tatsächlich gehandelt, refilled und der Preis schafft trotzdem keinen Fortschritt.

Dafür brauchen wir per-Level-L2-Daten in ausreichender zeitlicher Auflösung. Historisch haben wir sie in den Raw-OB200-Fenstern; im laufenden ClickHouse-Feature-Feed möglicherweise nicht vollständig.

3. Order Flow Imbalance

Nicht nur Buy- gegen Sell-Notional, sondern auch die Veränderung an Bid und Ask:

$$ OFI = \text{Bid-Zunahme} -\text{Bid-Abnahme} -\text{Ask-Zunahme} +\text{Ask-Abnahme} $$

Praktisch beantwortet das:

Werden Bids aufgebaut?
Werden Bids entfernt?
Werden Asks aufgebaut?
Werden Asks entfernt?

Ein bullish relevantes Muster wäre:

Bids steigen oder rücken nach,
Asks werden reduziert oder zurückgezogen,
Preis hält trotzdem oberhalb der letzten Breakkante.

Das zeigt Druck häufig früher als ein Candle-Breakout.

4. Microprice und Queue Imbalance

Der Midprice liegt einfach zwischen Best Bid und Best Ask. Der Microprice berücksichtigt zusätzlich, auf welcher Seite am Top-of-Book mehr Liquidität liegt.

Wenn der Microprice bereits über dem Midprice liegt, obwohl der Last Price noch seitwärts läuft, besteht kurzfristiger Aufwärtsdruck.

Nützlich wäre:

$$ \text{Queue Imbalance} = \frac{Q_{bid}-Q_{ask}}{Q_{bid}+Q_{ask}} $$

Nicht nur auf Level 1, sondern beispielsweise über:

Top 5 Levels,
Top 10 Levels,
5 bp Preisabstand,
10 bp Preisabstand.

Das ist eher ein Timing-Feature für Sekunden bis wenige Minuten und kein eigenständiges 15m-Signal.

5. Liquidity Vacuum

In deinem DOGE-Beispiel war genau das relevant: Unterhalb beziehungsweise zwischen Preis und Ziel lagen zeitweise nur dünne Strukturen, während die nächsten bedeutenden Pools gestaffelt oberhalb lagen.

Wir sollten messen:

kumulierte Liquidität bis 5/10/20 bp oberhalb,
kumulierte Liquidität bis 5/10/20 bp unterhalb,
Abstand zum nächsten Major Pool,
Anzahl und Größe der Zwischenpools,
größte liquiditätsarme Preisstrecke.

Ein Vacuum bedeutet:

Sobald die lokale Kante bricht, gibt es wenig passive Liquidität, die den Preis bis zum nächsten Pool aufhalten kann.

Dabei muss man zwischen zwei Dingen unterscheiden:

viele Ask-Pools direkt über dem Preis können Widerstand sein,
dieselben Pools können nach beginnendem Konsum zu gestaffelten Zielen einer Liquidations- beziehungsweise Stop-Kaskade werden.

Der Efficiency Flip entscheidet, welche Interpretation gerade gilt.

6. Angriffsgeschwindigkeit und Wiederholungsrate

Ein Pool, der zehnmal langsam getestet wird, ist anders als ein Pool, der innerhalb von drei Sekunden mit hohem Notional angegriffen wird.

Mögliche Features:

Notional pro Sekunde während des Angriffs,
Trades pro Sekunde,
Anzahl der Tests,
Zeit zwischen den Tests,
Preisfortschritt je Angriff,
verbleibende Wall-Größe nach jedem Test,
Refill-Geschwindigkeit.

Besonders informativ:

Wiederholte Angriffe mit sinkendem Preisfortschritt = Angreifer erschöpfen sich.

Oder umgekehrt:

Jeder neue Angriff benötigt weniger Volumen und erzeugt mehr Fortschritt = Wall wird schwächer.

Damit könnten wir erkennen, ob ein Pool bald bricht, bevor er tatsächlich gebrochen wurde.

7. Trapped Traders und Failed Auction

Nach großem aggressivem Flow können wir prüfen, ob diese Trader anschließend im Verlust liegen.

Beispiel für gefangene Verkäufer:

starkes Sell-Notional bei 0.0843,
Preis fällt kaum,
Preis steigt anschließend über den gewichteten Preis dieser Verkäufe,
die Verkäufer müssen möglicherweise eindecken,
Buy-Impact beschleunigt sich.

Dafür könnten wir einen ungefähren Aggressor VWAP berechnen:

VWAP der aggressiven Verkäufer im Absorptionsfenster,
Abstand des aktuellen Preises zu diesem VWAP,
Anteil des Sell-Notionals, das nun „unter Wasser“ liegt.

Das verbindet Absorption direkt mit dem möglichen Short-Squeeze.

Gespiegelt gilt das für gefangene Käufer am oberen Pool.

8. OI-Effizienz statt OI nur als Richtung

OI nutzen wir bisher überwiegend als „steigt oder fällt“. Wertvoller wäre die Frage:

Wie viel Preisbewegung erzeugte die Veränderung des OI?

Beispiele:

Preis/OI	Mögliche Interpretation
viel neues OI, kaum Preisanstieg	neue Longs möglicherweise absorbiert/gefangen
viel neues OI, starker Anstieg	gesunde Long-Initiative
Preis steigt stark, OI fällt	Short-Squeeze
Squeeze beginnt, danach OI steigt	neue Käufer übernehmen
Preis fällt, OI steigt stark	neue Shorts treiben Bewegung
OI steigt, Preis schafft kein neues Tief	Shorts möglicherweise absorbiert

Damit erhalten wir eine zweite Effizienzmetrik:

$$ \text{OI Efficiency} = \frac{\text{Preisbewegung}}{|\Delta OI|} $$
9. Liquidationswirkung

Eine große Liquidation ist nicht automatisch ein Einstiegssignal. Entscheidend ist wieder ihre Wirkung.

Bullishes Beispiel:

große Long-Liquidationen beziehungsweise aggressive Verkäufe,
OI fällt deutlich,
Preis macht nur ein kleines neues Tief,
Bid-Pool hält,
Preis reclaimt das Liquidationslevel.

Das wäre ein LIQUIDATION_IMPACT_COMPRESSION_RECLAIM.

Bearish gespiegelt:

große Short-Liquidationen,
starke Käufe,
aber kein weiterer Preisanstieg,
Ask-Wall refilled,
Rückfall unter das Liquidationslevel.

Das kann exakt die Endphase eines oberen Liquidity Sweeps markieren.

10. Acceptance nach dem Break

Das ist weniger spektakulär, aber zwingend notwendig. Viele Strategien verwechseln einen kurzen Wick mit einem echten Ausbruch.

Messbar wären:

Zeit oberhalb der gebrochenen Poolkante,
Anzahl geschlossener 1s/5s-Buckets oberhalb,
gehandeltes Volumen oberhalb,
maximale Rückfalltiefe,
Retest-Erfolg,
neue Bid-Liquidität oberhalb des früheren Ask-Pools.

Ein echter bullish Break sollte nicht nur kurz über dem Pool handeln. Er sollte dort neue zweiseitige Auktion beziehungsweise Bid-Unterstützung entwickeln.

Meine Priorisierung

Wir sollten nicht alles gleichzeitig bauen. Für unseren konkreten Ansatz wäre diese Reihenfolge am sinnvollsten:

Aggressor Impact Efficiency
Wall Fate: defended, refilled, consumed, pulled
Break Acceptance/Reclaim
Trapped Aggressor VWAP
OI- und Liquidationseffizienz
Order Flow/Queue Imbalance
Liquidity Vacuum und Pool Route
Attack Velocity und Wall Weakening
Das vollständige Entscheidungsmodell

Damit könnten wir beim Kontakt mit einem oberen Pool tatsächlich unterscheiden:

Wahrscheinlicher Breakout
große Sell-Aggression konnte den Preis vorher nicht drücken,
anschließend effiziente Käufer,
Ask-Wall wird konsumiert oder zurückgezogen,
Refill wird schwächer,
Preisfortschritt je Buy-Notional steigt,
Break oberhalb,
Bid-Liquidität bildet sich hinter dem Preis,
Acceptance hält.
Wahrscheinliche Rejection
große Buy-Aggression erzeugt kaum Up-Movement,
Ask-Wall bleibt bestehen oder refilled,
mehrere Angriffe erzeugen immer weniger Fortschritt,
Käufer liegen anschließend unter ihrem Aggressor-VWAP,
OI steigt, Preis aber nicht,
Preis fällt wieder unter die Poolkante,
bearish Acceptance.

Das wäre wirklich die Richtung, von der Fabio spricht: nicht den Ausbruch vorhersagen, weil eine Linie erreicht wurde, sondern während des Angriffs messen, welche Seite trotz großen Kapitaleinsatzes verliert.

Der vermutlich stärkste kombinierte Kern für unsere Strategie wäre:

$$ \text{Aggressor Efficiency} + \text{Wall Fate} + \text{Trapped Traders} + \text{Acceptance} $$

Pools sagen uns dann nur noch, wo wir beobachten müssen. Diese vier Bausteine sagen uns, was dort tatsächlich passiert und welche Richtung gehandelt werden darf.


###############################################################################

Mit den historischen Raw-OB200-Daten können wir zusätzlich erforschen:

Wall hält oder wird konsumiert
Refill
Pool Route
Liquidity Vacuum
Queue Imbalance

Nur die ganz genaue Trennung zwischen ausgeführt und storniert braucht eine Verbesserung der laufenden per-Level-L2-Speicherung. Das ist aber kein Grund, die Strategie zu blockieren.

Mein Vorschlag: nur drei Ausbaustufen


Nicht erraten, ob die Linie bricht – beobachten, welche Seite trotz Kapitaleinsatz nicht mehr weiterkommt.
Pools zeigen den Ort des Kampfes. Public Trades zeigen den Angreifer. Das Orderbook zeigt, ob die Verteidigung hält. Der Preis zeigt, wer gewonnen hat.

Wir bauen damit keine Glaskugel. Wir bauen einen Übersetzer für die Sprache des Marktes.



#####################################################################################################

| Klasse | Bedingungen                                                       | Behandlung             |
| ------ | ----------------------------------------------------------------- | ---------------------- |
| A+     | Major Pool plus mindestens zwei unabhängige strukturelle Faktoren | vollständig beobachten |
| A      | Major Pool plus ein struktureller Faktor                          | beobachten             |
| B      | Major Pool ohne Konfluenz oder starkes Strukturlevel ohne Pool    | nur protokollieren     |
| C      | gewöhnlicher Pool mitten in der Range                             | ignorieren             |


Was ich für unsere Strategie nehmen würde

Unsere primären Beobachtungsorte sollten zunächst sein:

Major Orderbook Pool
Value Area High/Low beziehungsweise äußere Market-Profile-Kante
Range High/Low oder bestätigtes Swing High/Low
EMA20/59/200 nur als Kontextverstärker

Damit bleibt das System fokussiert.

Ich würde nicht verlangen, dass immer alles zusammenliegt. Das könnte zu wenige Signale erzeugen. Für V1 wären zwei Beobachtungswege sinnvoll:




Signal erklären → Wall im Chart validieren → Trendfilter definieren → konsumiert/gezogen/verteidigt unterscheiden → Pool-to-Pool-Exit mit Bestätigung testen.