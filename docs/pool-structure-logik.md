    # Pool-Struktur und Marktlogik

    Diese Notiz faasst die Struktur zusammen, die wir im Chart lesen wollen. Ziel ist nicht nur, einzelne Pools zu sehen, sondern die gesamte Pool-Hierarchie ueber die Zeit zu verstehen.

    ## Grundidee

    Der Preis laeuft nicht zufaellig. Er bewegt sich durch eine Folge von alten und neuen Pools, die zusammen eine Landkarte bilden.

    - Die **Zeit** ist wichtig: Ein Pool, der frueher entstanden ist, kann spaeter als Support oder Resistance dienen.
    - Die **Rolle** ist wichtiger als die reine Groesse: Ein Pool kann gross oder klein sein und trotzdem ein Anker sein.
    - Die **Hierarchie** muss erhalten bleiben: Nicht alles darf zu einer einzigen breiten Zone verschmolzen werden.

    ## Ankerpool

    Ein **Ankerpool** ist ein alter, wichtiger Pool oder Cluster.

    - Er kann gross oder klein sein.
    - Er ist der Startpunkt einer Struktur.
    - Er bleibt oft als Support oder Resistance relevant.
    - Auch wenn spaeter neue Pools entstehen, wird der Anker nicht automatisch ersetzt.

    ## Bridge-Pool

    Ein **Bridge-Pool** ist ein kleiner Verbindungs-Pool.

    - Oft ist er nur sehr klein oder sogar nur eine kurze 1-Bar-Struktur.
    - Er verbindet zwei groessere Pools.
    - Er zeigt den Weg, den der Preis gerade nimmt.
    - Mehrere Bridge-Pools hintereinander bilden oft die Treppe.

    ## Support und Resistance

    Ein alter Pool kann spaeter als Support oder Resistance wirken.

    - Der Preis beruehrt oft nur die **Kante** des Pools.
    - Beim oberen Pool ist oft die **Unterkante** relevant.
    - Beim unteren Pool ist oft die **Oberkante** relevant.
    - Ein Touch an der Kante kann schon reichen, ohne dass der gesamte Pool durchbrochen wird.

    ## Entwicklung bei fallendem Preis

    Wenn der Preis faellt, bilden sich unter dem Preis neue blaue Pools.

    - Diese Pools bauen die **Down-Treppe**.
    - Wenn der Preis stark faellt und kaum Ruecksetzer macht, entstehen auf dem Weg nach unten nicht immer viele neue Gegenpools.
    - Alte obere Pools bleiben dann als Resistance oben stehen.

    ## Entwicklung bei steigendem Preis

    Wenn der Preis stark steigt, bilden sich ueber dem Preis neue rote Pools.

    - Diese Pools zeigen den Weg nach oben.
    - Wenn der Move stark und direkt ist, gibt es auf dem Weg nach oben oft keine neuen unteren Pools.
    - Nach einem Reversal werden die oberen Pools zu Resistance und unten entstehen neue Support-Strukturen.

    ## Wichtige Beobachtung

    Die Pool-Struktur ist **zeitlich und richtungsabhaengig**.

    - Alte Pools bleiben als Orientierung erhalten.
    - Spaeter gebildete Pools gehoeren nicht rueckwirkend zur alten Bewegung.
    - Der Preis nutzt oft erst einen Pool als Support oder Resistance, bevor er weiterlaeuft.
    - Die Landkarte muss daher als **Pool-Hierarchie** gelesen werden, nicht nur als Preislinie.

    ## Konsequenz fuer die Analyse

    Wir wollen den Preisverlauf so lesen:

    1. Welcher Pool ist der Anker?
    2. Welche Pools sind nur Bruecken?
    3. Welche Kante wurde beruehrt?
    4. Welche Pools sind alt und damit besonders stark?
    5. Welche neuen Pools zeigen erst spaeter die naechste Richtung?

    Erst wenn diese Struktur klar ist, koennen wir den Preisverlauf sauber verstehen.

    ## Kompakte Regeln fuer Code

    - Behalte die **Zeitfolge** der Pools bei.
    - Behandle **alte Pools** als Anker, wenn sie nicht gebrochen sind.
    - Behandle **kleine Verbindungs-Pools** als Bridge-Pools und nicht automatisch als eigene Hauptzone.
    - Wenn der Preis faellt, dann bilden die **Bridge-Pools unter dem Preis** die **Down-Treppe**.
    - Diese Bridge-Pools entstehen aus den **kleinen Ruecksetzern auf dem Weg nach unten**.
    - Wenn der Preis steigt, dann bilden die **Bridge-Pools ueber dem Preis** die **Up-Treppe**.
    - Wenn der Preis stark laeuft und nicht zuruecksetzt, entstehen auf der Gegenseite oft keine neuen Pools.
    - Wenn ein Pool nur an seiner Kante beruehrt wird, kann er trotzdem als Support oder Resistance gelten.
    - Verschmelze Pools nicht zu frueh, wenn dadurch die sichtbare Struktur im Chart verloren geht.

    ## Typisches Muster auf 4h

    Ein sehr haeufiges Muster ist:

    1. Der Preis faellt.
    2. Unter dem Preis bilden sich durch kleine Ruecksetzer **Bridge-Pools**.
    3. Diese Bridge-Pools bauen die **Down-Treppe**.
    4. Der Preis faellt bis kurz vor oder in den Bereich eines **alten Main-Ankerpools**.
    5. Dort stoppt der Abverkauf oder laeuft in eine Seitwaertsphase.
    6. Danach dreht der Preis wieder hoch.
    7. Erst spaeter bilden sich ueber dem Preis neue rote Pools, die den Rueckweg nach oben markieren.

    Das ist genau das Muster, das wir wiedererkennen wollen:

    - erst die Down-Treppe
    - dann der Test des alten Ankers
    - dann der Rebound
    - und spaeter die neuen Pools ueber dem Preis

    Wichtig ist dabei:

    - Der Main-Ankerpool bleibt waehrend der Abwaertsphase oft die zentrale Orientierung.
    - Die Bridge-Pools sind nur die kleinen Zwischenstufen auf dem Weg nach unten.
    - Die roten Pools ueber dem Preis entstehen oft **erst nach** dem Tief oder nach der Seitwaertsphase und zeigen dann den neuen Weg.


3/06 12
5/06 00.00

7/06  12

## Entry-Logik fuer Shorts

- Der **Pool-Analyzer bleibt unveraendert**.
- Zuerst **15m**. Dort muss der Pool sichtbar sein, der Abverkauf muss schon laufen, und bis zur naechsten oberen Unterkante muessen etwa **1,2 Prozent** frei sein.
- Der Short sitzt an diesem 15m-Pool, auf der Kerze die ihn bildet oder eine Kerze spaeter.
- Danach bestaetigen **30m, 1h und 4h** denselben Downtrend. Finden sie ihn nicht, faellt der Short weg.
- Auf 30m sollte der Preis den Pullback zeigen und die schnellen EMAs sollten unter dem mittleren EMA liegen oder sauber abdrehen.
- Auf 1h sollte der Downtrend klar sein, idealerweise mit EMA59 deutlich unter EMA200.
- Die **TP-Zone** kommt aus dem naechsten relevanten unteren Pool.
- Auf **15m** muss bis zur Unterkante des naechsten oberen Pools eine Luecke von etwa **1,2 Prozent** offen sein. Erst dort beginnt der naechste Pool.
- Passt die Bestaetigung auf 30m, 1h oder 4h nicht, gibt es keinen Short.

## Praktische Reihenfolge

1. Auf 15m den lokalen oberen Pool mit etwa 1,2 Prozent Lücke nach oben suchen.
2. Pruefen, ob der 15m-Abverkauf schon sichtbar ist und der Preis am Pool liegt.
3. Downtrend auf 30m, 1h und 4h bestaetigen.
4. Short auf der Pool-Kerze oder eine Kerze spaeter, immer an diesem Pool.
5. Naechsten unteren Pool als TP nutzen.


Die wichtigste Datei morgen ist:
bot/e1r_live_scanner/logs/signals.jsonl
und für die Betriebsprüfung:
bot/e1r_live_scanner/logs/runner_live.log


| Coin | PnL | DD | Eindruck |
|---|---:|---:|---|
| ZEC | +97,40 % | 6,99 % | profitabel, aber unruhiger |
| ACE | +34,96 % | 11,05 % | deutlich schwächeres Risiko/Ertrag |
| TUT | +89,62 % | 23,07 % | extrem aggressiver Verlauf |