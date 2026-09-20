Masterplan: Erweiterung der OB-Forschungsengine um stabile Absorptions- und Breakout-Signale
1. Ziel

Die bestehende Strategie bleibt erhalten:

30m-/1h-/4h-Market-Profile-Kante
→ relevante Preiszone
→ erster kausaler Zone-Touch
→ relevante Full-OB-Wall
→ Orderflow-Analyse
→ Absorption, Reclaim oder Durchbruch
→ Long / Short / kein Signal

Die Engine soll nicht mehr allein erkennen, dass eine Wall kurzfristig hält. Sie soll unterscheiden:

Nachhaltige Absorption: Wall hält und Angreifer verliert Stärke.
Toxische Scheinabsorption: Wall hält kurz, aber persistenter Flow zerstört sie später.
Echter Breakout: Queue wird netto abgebaut und Preis expandiert.
Unklare Situation: Keine ausreichende Bestätigung, daher kein Signal.
Phase 0 – aktuellen Episode-1-Prompt abschließen
Voraussetzung

Der aktuell laufende Prompt muss zuerst erfolgreich beendet werden.

Er muss beweisen:

Zone wurde vor dem Touch kausal berechnet.
ZONE_FIRST_TOUCH wurde aus Rohdaten erkannt.
WALL_FIRST_TOUCH wurde separat erkannt.
Detection wurde unabhängig berechnet.
FIRST_TOUCH_ISO und DETECTION sind keine Berechnungsinputs mehr.
Keine Look-ahead-Verletzung.
Zwei unabhängige End-to-End-Läufe liefern dasselbe Ergebnis.
Full-OB-Parität bleibt 4178/4178.
Abbruchbedingung

Wenn der aktuelle Prompt beispielsweise liefert:

EPISODE1_DETECTION_RULE_NOT_IMPLEMENTED

oder:

LOOK_AHEAD_VIOLATION

dann wird zuerst dieser Blocker behoben. QDH und neue Signallogik werden noch nicht eingebaut.

Phase 1 – einheitliche Event-Timeline erzeugen
Ziel

Alle relevanten Rohdaten müssen in einer kausalen Event-Timeline zusammenlaufen.

Eingänge
Full-OB-Snapshots
Full-OB-Deltas
Public Trades
Zone-Events
Wall-Events
Best Bid/Ask
Midprice
Replay-Epochs und Resets
Exchange-Event-Time
Collector-Receive-Time, falls vorhanden
event_available_at
Reihenfolge
ZONE_AVAILABLE
ZONE_FIRST_TOUCH
WALL_VISIBLE
WALL_OBSERVATION_AT_ZONE_TOUCH
WALL_FIRST_TOUCH
BOOK_DEPLETION
WALL_REFILL
WALL_PULL
AGGRESSOR_TRADE
DETECTION
Neue zentrale Datenstruktur

Vorgeschlagen:

WallFlowEvent

Pflichtfelder:

episode_id
symbol
zone_id
wall_id
wall_side
wall_price
event_type
exchange_event_time
collector_received_at
event_available_at
book_sequence
replay_epoch
price
size_base
notional_usdt
aggressor_side
source_record_id
attribution_confidence
look_ahead
Harte Regeln
Events aus unterschiedlichen Replay-Epochs dürfen nicht zusammengeführt werden.
Fehlende Sequenzen erzeugen einen Coverage-Blocker.
Exchange-Zeit darf nicht als Receive-Zeit ausgegeben werden.
Es dürfen nur Events mit event_available_at <= decision_time verwendet werden.
Outcome-Daten dürfen nicht in die Feature-Berechnung gelangen.
Phase 2 – Wall-Flow-Attribution
Ziel

Für jede relevante Wall feststellen, wodurch sich ihre Größe verändert:

aggressiver Fill,
Pull/Cancel,
Refill,
unbekannte beziehungsweise nicht eindeutig attribuierbare Veränderung.
Vorgeschlagenes neues Modul
obfull_research_engine/wall_flow_attribution.py

Die genaue Position wird an die vorhandene Paketstruktur angepasst.

Berechnung pro Wallpreis

Für aufeinanderfolgende Book-Zustände:

$$ \Delta Q_t=Q_t-Q_{t-1} $$

Zuordnungen:

HIT:
passendes aggressives Handelsvolumen trifft die Wall

PULL:
Wall-Größe sinkt ohne ausreichend passendes Handelsvolumen

REFILL:
Wall-Größe steigt nach oder während eines Angriffs

UNKNOWN:
Veränderung kann zeitlich nicht eindeutig attribuiert werden
Wichtig: keine Doppelzählung

Aggressives Handelsvolumen und Book-Depletion dürfen nicht doppelt als Abbau gerechnet werden.

Vereinfachtes Modell:

$$ \text{beobachteter Queue-Abbau} = \text{attribuierte Fills} + \text{residuale Pulls} $$

Dabei:

$$ Pull_t= \max( BookDecrease_t-AttributedFill_t,\ 0 ) $$

Bei uneindeutiger zeitlicher Zuordnung:

attribution_confidence = LOW

Diese Events dürfen nicht still als sichere Cancellations interpretiert werden.

Preisband statt nur Einzelpreis

Neben dem exakten Wallpreis wird ein verteidigtes Band verwendet:

wall_price ± definierte Tick-Anzahl

Dadurch bleibt die Messung stabil, wenn passive Liquidität innerhalb weniger Ticks verschoben wird.

Auszugeben sind beide Werte:

exact_price_queue
defended_band_queue
Phase 3 – Angriffsgeschwindigkeit und Flow-Persistenz
Ziel

Nicht nur messen, wie viel aggressives Volumen gekommen ist, sondern ob der Angriff weiterläuft oder bereits ausläuft.

Vorgeschlagenes Modul
obfull_research_engine/aggressor_flow.py
Metriken
3.1 Hit Rate
$$ HitRate_t= \frac{\text{aggressives Notional gegen die Wall}} {\Delta t} $$

Getrennt nach:

Anzahl aggressiver Trades
Base-Volumen
USDT-Notional
Anzahl getroffener Preislevel
3.2 Event-Time-Pacing
$$ Pacing_t= \operatorname{median}(t_i-t_{i-1}) $$

Je kleiner der Abstand zwischen gleichgerichteten Trades, desto schneller der Angriff.

3.3 Kurz-/Langfrist-Intensität
$$ PersistenceRatio_t= \frac{EWMA_{\text{fast}}(HitRate)} {EWMA_{\text{slow}}(HitRate)+\varepsilon} $$

Interpretation:

> 1: Angriff beschleunigt
≈ 1: Angriff bleibt stabil
< 1: Angriff verliert Intensität

Die endgültigen Halbwertszeiten werden aus den Daten bestimmt. Keine Schwellen nachträglich an Episode 1 anpassen.

3.4 Same-Side-Persistence

Zusätzlich erfassen:

Länge gleichgerichteter Trade-Runs
Anteil gleichgerichteter Trades
aggressives Buy-/Sell-Imbalance
Zeit seit dem letzten Angriff
Intensitätsänderung nach dem Wall-Touch
Phase 4 – Queue Depletion Hazard
Ziel

Schätzen, wie schnell die verteidigende Queue unter dem aktuellen Flow netto zerstört wird.

Vorgeschlagenes Modul
obfull_research_engine/queue_depletion_hazard.py
Grundgrößen

Für das verteidigte Preisband:

$$ Q_t=\text{aktuell sichtbare Queue} $$ $$ H_t=\text{Rate der attribuierten aggressiven Hits} $$ $$ C_t=\text{Rate der attribuierten Pulls} $$ $$ R_t=\text{Rate der Refills} $$
Netto-Depletion
$$ D_t=H_t+C_t-R_t $$
Queue Depletion Hazard
$$ \boxed{ QDH_t= \frac{\max(D_t,0)} {Q_t+\varepsilon} } $$

Einheit:

erwarteter Anteil der Queue, der pro Sekunde abgebaut wird
Geschätzter Queue-Runway
$$ T_{\text{break}}= \frac{1} {QDH_t+\varepsilon} $$

Beispiel:

QDH = 0.10/s

bedeutet näherungsweise, dass bei unverändertem Flow 10 % der Queue pro Sekunde verloren gehen.

Persistenzbereinigte QDH

Der aktuelle Abbau wird mit der Fortsetzungswahrscheinlichkeit des Angriffs kombiniert:

$$ QDH^{toxic}_t= QDH_t\cdot PersistenceFactor_t $$

Ein möglicher Forschungswert:

$$ PersistenceFactor_t= \operatorname{clip} \left( \frac{EWMA_{fast}(HitRate)} {EWMA_{slow}(HitRate)+\varepsilon}, p_{\min}, p_{\max} \right) $$

Hohe QDH_toxic bedeutet:

schneller Nettoabbau,
kleine verbleibende Queue,
weiterlaufender oder beschleunigender Aggressor.
Phase 5 – Preisreaktion und Absorptionseffizienz
Ziel

Prüfen, ob das aggressive Volumen einen proportionalen Preisfortschritt erzeugt.

Vorgeschlagenes Modul
obfull_research_engine/price_response.py
Price Impact Efficiency
$$ ImpactEfficiency_t= \frac{ \text{Preisbewegung in Angriffsrichtung in bps} }{ \text{aggressives Hit-Notional in Mio. USDT}+\varepsilon } $$

Interpretation:

Hohes Volumen und geringe Preiswirkung: mögliche Absorption.
Kleines Volumen und starke Preiswirkung: dünne Liquidität oder Vakuum.
Hohes Volumen und hohe Preiswirkung: erfolgreicher aggressiver Durchbruch.
Zusätzlich berechnen
Midprice-Reaktion
Microprice-Reaktion
Best-Bid-/Best-Ask-Verschiebung
Spread-Ausweitung
Anzahl konsumierter Preislevel
Reclaim der Zone
Rückkehr des Microprice auf die Verteidigerseite
maximale Bewegung gegen und in erwartete Richtung

Die zukünftige MFE/MAE darf nur als Outcome verwendet werden, nicht als Feature.

Phase 6 – Zustandsklassifikation
Vorgeschlagenes Modul
obfull_research_engine/wall_state_classifier.py

Zunächst keine feste Tradingentscheidung, sondern vier Forschungszustände.

SUSTAINABLE_ABSORPTION

Erwartete Merkmale:

Wall wurde tatsächlich getroffen
Hit-Notional ist relevant
Preisfortschritt bleibt gering
Refills kompensieren Hits und Pulls
QDH fällt
Aggressor-Persistenz fällt
Microprice oder OFI dreht zurück
Wall beziehungsweise Zone wird reclaimed
TOXIC_ABSORPTION
Preis stoppt zunächst
aggressiver Flow bleibt aber persistent
QDH bleibt hoch oder steigt
Refills kompensieren den Abbau nicht
Liquidität wird hinter der Front gepullt
kein stabiler Microprice-Reclaim
WALL_BREAK_CONTINUATION
hohe QDH
beschleunigender aggressiver Flow
mehrere Preislevel werden konsumiert
Wall verschwindet überwiegend durch Ausführung
Preis expandiert in Angriffsrichtung
INCONCLUSIVE
unzureichende Daten
schlechte Attribution
Reset oder Sequenzlücke
widersprüchliche Metriken
zu kleines Volumen
kein klarer Reclaim oder Breakout

INCONCLUSIVE ist wichtig: Das System muss ausdrücklich kein Signal ausgeben können.

Phase 7 – Signalregel V2

Erst nach der deskriptiven Validierung wird eine Signalregel aktiviert.

Long an einer unteren Market-Profile-Kante
vorher bekannte untere MP-Zone
+ echter Zone-Touch
+ relevante Bid-Wall
+ aggressiver Sell-Angriff
+ geringe Price Impact Efficiency
+ fallende QDH
+ fallende Sell-Persistenz
+ Microprice-/OFI-Reclaim
= Long-Kandidat
Short an einer oberen Market-Profile-Kante
vorher bekannte obere MP-Zone
+ echter Zone-Touch
+ relevante Ask-Wall
+ aggressiver Buy-Angriff
+ geringe Price Impact Efficiency
+ fallende QDH
+ fallende Buy-Persistenz
+ Microprice-/OFI-Reclaim
= Short-Kandidat
Breakout
relevante Wall
+ hohe beziehungsweise steigende QDH
+ persistenter aggressiver Flow
+ mehrere Level werden konsumiert
+ kein Reclaim
+ Preisexpansion
= Breakout-Kandidat
Kein Signal

Bei einem der folgenden Punkte:

Zone war vorher nicht verfügbar.
Wall war vor dem angeblichen Touch nicht sichtbar.
Replay-Epoch-Wechsel im Analysefenster.
Sequenzlücke.
zu niedrige Attribution Confidence.
widersprüchliche Richtung von Preis, Microprice und Flow.
unzureichendes Hit-Notional.
QDH und Flow-Persistenz ergeben keine eindeutige Aussage.
Phase 8 – OI, Liquidationen, AVR und Footprint

Diese Daten werden zunächst als Kontext und Validierung verwendet, nicht als zwingender Bestandteil der QDH.

OI
Preis steigt und OI steigt: neue Positionen, Fortsetzungsrisiko.
Preis steigt und OI fällt: Short-Covering beziehungsweise Erschöpfung möglich.
Preis fällt und OI steigt: neue Shorts beziehungsweise Fortsetzungsrisiko.
Preis fällt und OI fällt: Long-Liquidation beziehungsweise Erschöpfung möglich.
Liquidationen

Prüfen:

Ist der Angriff durch Liquidationen getrieben?
Endet der Flow nach dem Liquidations-Cluster?
Liegt hinter der Wall ein weiteres Liquiditätsziel?
AVR und Footprint

Verwendung als Bestätigung:

BUY_CONTROL
SELL_CONTROL
BUY_ABSORPTION
SELL_ABSORPTION
VACUUM_UP
VACUUM_DOWN

Wichtig: AVR darf nicht dieselben Eingangsdaten mehrfach als scheinbar unabhängige Bestätigung zählen. QDH, Delta und AVR sind teilweise korreliert


Phase 9 – Episode-1-Proof

Alle neuen Berechnungen werden zuerst ausschließlich für Episode 1 ausgeführt.

Pflichtausgaben

Zeitreihe ab Zone-Touch:

timestamp
wall_size
band_size
aggressive_hit_notional
hit_rate
trade_pacing
pull_rate
refill_rate
persistence_ratio
QDH
QDH_toxic
midprice
microprice
impact_efficiency
wall_state
attribution_confidence
Bericht

Der Report muss beantworten:

Wann begann der echte Angriff?
Welche Seite griff an?
Wie schnell wurde gehandelt?
Wie viel Wall-Volumen wurde ausgeführt?
Wie viel wurde gepullt?
Wie viel wurde refilled?
Wie entwickelte sich QDH?
Wurde der Angriff stärker oder schwächer?
War die Reaktion echte oder toxische Absorption?
Welche Information wäre zum frühesten kausalen Detection-Zeitpunkt vorhanden gewesen?
Keine Schwellenoptimierung

Episode 1 dient zum Nachweis der Mechanik, nicht zur Auswahl profitabler Grenzwerte.

Phase 10 – historische Skalierung

Erst nach erfolgreichem Episode-1-Proof:

alle gültigen BTC-Episoden,
danach DOGE,
anschließend weitere Coins,
getrennt nach 30m-, 1h- und 4h-Profilen.
Gruppierungen

Ergebnisse getrennt auswerten nach:

obere/untere Profilkante
Profil-Zeitrahmen
Reversal/Breakout
Wall-Größen-Perzentil
Volatilitätsregime
Spread
Tageszeit
OI-Regime
Liquidationskontext
QDH-Bucket
Aggressor-Persistenz
Attribution Confidence

Mehrere Berührungen aus demselben Flow-Cluster dürfen nicht als unabhängige Episoden gezählt werden.

Phase 11 – statistische Validierung
Methodik
chronologisches Walk-forward
keine zufällige Vermischung von Vergangenheit und Zukunft
Purging überlappender Outcome-Fenster
Embargo zwischen Train und Test
Day- und Session-Holdouts
getrennte Coin-Validierung
Schwellen ausschließlich auf Trainingsdaten
finale Bewertung auf unangetasteten Testdaten
Handelbare Outcomes

Nicht nur Midprice-MFE/MAE:

$$ PnL= Exit_{\text{executable}} - Entry_{\text{executable}} - Fees - Slippage - LatencyCost $$

Auswerten:

Trefferquote
Profit Factor
Expectancy
MFE/MAE
maximale adverse Bewegung
Zeit bis Profit
Zeit bis Wall-Break
Signal-Halbwertszeit
Ergebnis nach Gebühren und Slippage
Phase 12 – spätere optionale Erweiterungen

Diese Punkte sind wertvoll, aber blockieren die erste Version nicht.

Cross-Exchange-Filter

Zusätzliche Binance-Futures-/Spot-Daten zur Erkennung:

führender Markt,
Bybit-Lag,
Cross-Venue-Fair-Value,
lokaler Scheinabsorption.
Echtes L3

Nur falls später verfügbar:

Order-IDs
genaue FIFO-Queue-Position
Order-Lifetime
Teilnehmer- beziehungsweise Order-Refill-Erkennung

Mit unserem Full L2 bleibt die Bezeichnung:

aggregate_queue_survival_proxy

Nicht:

exact_queue_position
Vorgeschlagene neue Module

Die endgültigen Pfade müssen an die aktuelle Repository-Struktur angepasst werden.

obfull_research_engine/
├── wall_flow_attribution.py
├── aggressor_flow.py
├── queue_depletion_hazard.py
├── price_response.py
├── wall_state_classifier.py
├── signal_v2.py
├── schemas.py
├── coverage_gates.py
└── reports/
    └── episode_wall_flow_report.py

Vorhandene Module sollen wiederverwendet werden für:

Full-OB-Replay
100-ms-States
Wall-Erkennung
Refill-Erkennung
Episode-Erkennung
Outcomes
Persisted Readback
Oracle und Paritätsprüfung

Keine zweite parallele Replay-Engine bauen.

Pflicht-Tests
Unit-Tests
Aggressiver Fill reduziert die Queue.
Pull ohne Trade wird als Pull erkannt.
Refill erhöht die Queue.
Fill und Refill im gleichen Intervall werden nicht doppelt gezählt.
Ask-Wall akzeptiert nur passenden Buy-Angriff.
Bid-Wall akzeptiert nur passenden Sell-Angriff.
Trade an anderem Preis greift die Wall nicht an.
Event vor Wall-Sichtbarkeit wird ignoriert.
Event nach Detection wird nicht als Feature verwendet.
Replay-Epoch-Wechsel blockiert die Berechnung.
Sequenzlücke setzt Coverage auf ungültig.
Fehlende Receive-Time wird ehrlich gekennzeichnet.
QDH steigt bei mehr Hits.
QDH steigt bei mehr Pulls.
QDH fällt bei mehr Refills.
QDH steigt bei kleinerer Rest-Queue.
Flow-Persistenz steigt bei beschleunigenden Angriffen.
Kein Look-ahead aus Outcomes.
Produktion und unabhängiger Oracle stimmen überein.
Zwei frische Prozesse liefern identische Ergebnisse.
Regression
bestehende Full-OB-Parität unverändert
Episode-1-Touch unverändert unabhängig ableitbar
Wall-Touch bleibt separat
keine Veränderung laufender Collector-Prozesse
keine Änderung vorhandener Rohdaten
keine CH-Writes während des Forschungsdurchlaufs
Erfolgsreihenfolge
1. Aktuellen Touch-/Detection-Prompt abschließen
2. Ergebnis und Restblocker prüfen
3. Wall-Flow-Attribution für Episode 1
4. Aggressor-Persistenz
5. Queue Depletion Hazard
6. Preisreaktion und Microprice-Reclaim
7. Zustandsklassifikation
8. Episode-1-Proof
9. Historische Skalierung
10. Signal V2
11. Handelbarer Backtest
12. Cross-Exchange später ergänzen
Wichtigstes Architekturprinzip

Die Engine soll nicht behaupten:

Großes aggressives Volumen ohne sofortige Preisbewegung
= Absorption

Sondern prüfen:

Wall wird getroffen
+ Netto-Depletion fällt
+ Refills überleben weitere Treffer
+ Aggressor verliert Persistenz
+ Preis/Microprice reclaimt
= nachhaltige Absorption

Damit bleibt unser ursprünglicher Ansatz erhalten, wird aber um das entscheidende fehlende Element ergänzt: Überlebt die Wall den erwarteten zukünftigen aggressiven Flow – oder hält sie lediglich für einen kurzen Moment?



############################################### Ergänzung ###############################################

Ergänzung 1 – klare Trennung der Datenrollen

Unter „Ziel“ ergänzen:

Verbindliche Datenrollen:

1. Full L2:
   Liefert sichtbare Queue, Queue-Veränderungen, Pulls, Refills und Depth.

2. Public Trades:
   Liefert tatsächliche Ausführungen, Aggressorseite, Tradepreis,
   Tradegröße, Geschwindigkeit und Impact Efficiency.
   Public Trades sind für die Wall-Flow-Attribution verpflichtend.

3. Open Interest:
   Liefert ausschließlich verzögerten Flow-/Positionskontext.
   OI ist kein physischer Bestandteil des Queue-Abbaus und darf den
   Subsekunden-QDH-Trigger nicht rückwirkend verändern.

4. Liquidationen:
   Klassifizieren einen Teil des bereits beobachteten aggressiven Flows
   als forced. Liquidationsvolumen darf niemals zusätzlich zum
   Public-Trade-Volumen addiert werden.

5. AVR/Footprint:
   Sind abgeleitete Darstellungen der Public Trades und dürfen nicht als
   statistisch unabhängige Bestätigung behandelt werden.
Ergänzung 2 – QDH-Formel gegen Doppelzählung absichern

In Phase 2 und Phase 4 ergänzen beziehungsweise präzisieren:

Hits dürfen nur einmal gezählt werden.

Wenn BookDecrease bereits ausgeführte Trades enthält, gilt:

AttributedFill_t =
    aus Public Trades der Wall zugeordnete Ausführungsmenge

ResidualPull_t =
    max(BookDecrease_t - AttributedFill_t, 0)

Refill_t =
    positive Queue-Zunahme beziehungsweise inferierte Replenishment-Menge

NetDepletion_t =
    AttributedFill_t + ResidualPull_t - Refill_t

Dann ausdrücklich umbenennen:

$$ QDH_{\text{base},t} = \frac{ \max\left(EWMA(NetDepletion_t/\Delta t),0\right) }{ Q_t+\varepsilon } $$

Die bisherige QDH_toxic-Formel erweitern:

$$ \boxed{ QDH_{\text{toxic},t} = QDH_{\text{base},t} \cdot M_{\text{persistence},t} \cdot M_{\text{OI},t} \cdot M_{\text{Liq},t} } $$

Harte Regel:

QDH_base misst physischen Nettoabbau.

M_persistence, M_OI und M_Liq dürfen nur die erwartete Fortsetzungsgefahr
skalieren. Sie dürfen kein Volumen erneut zum Queue-Abbau addieren.
Ergänzung 3 – FootprintClusterEvent und normalisierte Preiswirkung

Nach Phase 5 ergänzen:

Phase 5A – kausaler Footprint-Cluster am Wall-Band
Der Footprint wird ausschließlich aus den kanonischen Public Trades
gebildet, die bereits für die Wall-Flow-Attribution verwendet werden.

Er ist kein zusätzlicher Flow, sondern eine abgeleitete Analyse derselben
Trade-IDs.
Preisband
$$ B(w,k)= [w-k\cdot tick,\;w+k\cdot tick] $$
Aggressives Wall-Volumen
$$ A_d(B,t_0,t_1)= \sum_i q_i \mathbf{1} [ side_i=d,\;p_i\in B,\;t_0<T_i\le t_1 ] $$
Impact Efficiency
$$ ImpactEfficiency_t= \frac{ \text{Preisfortschritt in Angriffsrichtung in bps} }{ \text{aggressives Wall-Notional in Mio. USDT}+\varepsilon } $$
Normalisierte Impact Efficiency
$$ IE^{norm}_t= \frac{IE_t} { \operatorname{median}_{past} ( IE\mid Depth,\ Spread,\ Volatilität,\ Session )+\varepsilon } $$
Absorption Ratio
$$ AbsorptionRatio_t= \operatorname{clip}(1-IE^{norm}_t,0,1) $$
Vacuum Score
$$ VacuumScore_t= z(PriceProgress_t) -z(AggressorVolume_t) +z(PullRate_t) $$

Interpretation:

Hohe Absorption Ratio:
Viel aggressives Volumen erzeugt ungewöhnlich wenig Preisfortschritt.

Hoher Vacuum Score:
Große Preisbewegung entsteht bei wenig aggressivem Volumen und
gleichzeitigem Pulling passiver Liquidität.
Neue Datenstruktur
FootprintClusterEvent:
    cluster_id
    episode_id
    wall_id
    wall_side
    wall_price
    band_low
    band_high
    attack_direction

    exchange_start
    exchange_end
    max_input_available_at
    computed_at
    replay_epoch

    trade_count
    unique_trade_count
    trade_ids_hash

    buy_qty
    sell_qty
    buy_notional
    sell_notional
    signed_delta_notional

    attributed_hit_qty
    attributed_hit_notional
    opposite_flow_qty

    interarrival_p50_ms
    interarrival_p90_ms
    attack_rate_qty_per_s
    attack_rate_notional_per_s

    microprice_before
    microprice_after
    progress_ticks
    progress_bps
    impact_efficiency
    normalized_impact_efficiency
    absorption_ratio
    vacuum_score

    attribution_confidence
    coverage_ok
    look_ahead

Doppelzählungsregel:

qdh_hits = sum(trade.qty for trade in attributed_trades)
footprint = calculate_metrics(attributed_trades)

# Verboten:
qdh_hits += footprint.attributed_hit_qty
Ergänzung 4 – Phase 8 vollständig ersetzen

Die bisherige Phase 8 ist noch zu allgemein. Ersetze sie durch:

Phase 8 – optionale Flow-Type-Enrichment-Schicht
Grundsatz
OI und Liquidationen werden zunächst ausschließlich als Shadow-Features
berechnet.

Sie verändern weder Episode-1-Detection noch WallState noch Trading-Signal,
bis ihr zusätzlicher Informationsgewinn out-of-sample bewiesen wurde.
8.1 OI
Harte OI-Regeln
Historisches 5-Minuten-OI:
- kein Subsekunden-QDH-Multiplikator
- kein rückwirkendes Auffüllen
- keine Interpolation
- nur Regime-/Episodenkontext

Kausal archiviertes WebSocket-OI:
- erst ab event_available_at verwendbar
- nur für zukünftige Entscheidungen
- Source Age immer persistieren

Bei historischem 5-Minuten-OI:

M_OI = 1.0

für den eigentlichen Wall-Trigger.

OI-Innovation
$$ g^{OI}_k= \frac{ \log(OI_k+\varepsilon)-\log(OI_{k-1}+\varepsilon) }{ available\_at_k-available\_at_{k-1} } $$

Robuste Normalisierung:

$$ z^{OI}_k= \frac{ g^{OI}_k-\operatorname{median}_{past}(g^{OI}) }{ 1.4826\cdot MAD_{past}(g^{OI})+\varepsilon } $$

Optional multikollinearitätsbereinigt:

$$ z^{OI,\perp}_k= z^{OI}_k- \widehat E[ z^{OI}_k \mid SignedFlow,\ Return,\ Volatility,\ Depth ] $$

Nur dieses Residuum sollte später als eigenständiges OI-Feature getestet werden.

8.2 Liquidationen
Forced-Flow-Anteil
$$ \phi_d(t)= \operatorname{clip} \left( \frac{L_d^{fast}(t)} {A_d^{fast}(t)+\varepsilon}, 0,1 \right) $$

Dabei gilt:

A_d = gesamter aggressiver Public-Trade-Flow
L_d = als Liquidation gemeldeter Flow derselben Richtung

L_d ist eine Teilmenge beziehungsweise ein Flow-Type-Proxy von A_d.
Liquidationsbeschleunigung
$$ \rho^{Liq}_d(t)= \frac{ L_d^{fast}(t)+\varepsilon }{ L_d^{slow}(t)+\varepsilon } $$

Interpretation:

rho > 1: Forced Flow beschleunigt
rho ≈ 1: Forced Flow bleibt stabil
rho < 1: Forced Flow verliert Intensität
Liquidations-Multiplikator
$$ M_{\text{Liq},t} = \operatorname{clip} \left[ \exp \left( \beta_\phi\phi_d+ \beta_a\phi_d\max(\log\rho^{Liq}_d,0) \right), 1, M_{\max} \right] $$

Die Parameter dürfen nicht anhand von Episode 1 festgelegt werden.

8.3 Liquidation Exhaustion Point

Neuer separater Zustand:

LIQUIDATION_EXHAUSTION_POINT

Kausal erfüllt, wenn:

LEP = (
    forced_flow_share >= X
    and liquidation_peak_is_significant
    and liquidation_fast / liquidation_slow <= decay_threshold
    and qdh_drop >= qdh_drop_min
    and absorption_ratio >= absorption_min
    and reclaim_ticks >= reclaim_min
    and reclaim_dwell_ms >= dwell_min
    and coverage_ok
)

Dabei ist \(X\) kein vorab erfundener Prozentwert. Er wird später ausschließlich auf Trainingsdaten bestimmt.

8.4 Separater Flow-Type-State

Nicht mit dem Wall-State vermischen:

Wall State:
- SUSTAINABLE_ABSORPTION
- TOXIC_ABSORPTION
- WALL_BREAK_CONTINUATION
- INCONCLUSIVE

Flow Type:
- ORGANIC_BUILDING
- ORGANIC_CLOSING
- FORCED_ACCELERATING
- FORCED_DECELERATING
- UNKNOWN

So bleibt klar:

Was macht die Wall?

ist eine andere Frage als:

Welche Art von Flow greift die Wall an?
Ergänzung 5 – dynamische Classifier-Regeln

In Phase 6 ergänzen:

Forced-Flow-Anpassung

Während einer beschleunigenden Liquidationskaskade:

Reversal schwerer bestätigen.
Breakout leichter beziehungsweise früher erkennen.

Mathematisch:

$$ Threshold_{rev}(\phi,\rho)= Threshold_{rev,0} +a\phi +b\phi\max(\log\rho,0) $$

Strengere QDH-Anforderung:

$$ QDH_{rev,max}(\phi,\rho)= QDH_{rev,0} \exp[-c\phi\max(\log\rho,0)] $$

Breakout-Schwelle:

$$ QDH_{break,min}(\phi,\rho)= QDH_{break,0} \exp[-c_b\phi\max(\log\rho,0)] $$

Harte Regel:

Ein hoher Forced-Flow-Anteil allein ist niemals ein Reversal-Signal.

Reversal wird erst erlaubt, wenn:
- Liquidationsintensität fällt,
- QDH deutlich fällt,
- Absorption Ratio hoch bleibt,
- und ein kausaler Reclaim mit Mindestdauer vorliegt.
Ergänzung 6 – Episode-1-Ausgabe erweitern

Zu den Pflichtausgaben von Phase 9 hinzufügen:

net_depletion
qdh_base
qdh_persistence_multiplier
qdh_toxic_base_only

footprint_cluster_id
unique_trade_count
trade_ids_hash
absorption_ratio
normalized_impact_efficiency
vacuum_score

oi_source
oi_available_at
oi_source_age_ms
oi_innovation
M_OI
oi_trigger_eligible

liquidation_qty_same_direction
forced_flow_share
liquidation_fast
liquidation_slow
liquidation_acceleration
M_Liq
LEP_state

wall_state
flow_type
max_input_available_at
coverage_ok
look_ahead

Für den ersten Episode‑1-QDH-Proof:

M_OI = 1
M_Liq = 1

Die beiden Werte dürfen zwar als Shadow-Spalten berechnet werden, aber das Base-Ergebnis nicht verändern.

Ergänzung 7 – echte Ablation vor Signalaktivierung

Zwischen Phase 10 und Phase 11 einfügen:

Phase 10A – Feature-Ablation

Vier getrennte Varianten:

A: Full L2 + Public Trades + QDH + Price Response
B: A + OI
C: A + Liquidationen
D: A + OI + Liquidationen

Bewertung:

$$ \Delta LogLoss= LogLoss_{base}-LogLoss_{extended} $$ $$ \Delta Brier= Brier_{base}-Brier_{extended} $$

Zusätzlich:

Calibration Error
Wall-Break-False-Positive-Rate
Reversal-False-Positive-Rate
Profit Factor nach Kosten
Slippage
Signal-Halbwertszeit
Stabilität über Walk-forward-Folds
Bootstrap-Konfidenzintervall nach Tagen

Aufnahmeregel:

keep_feature = (
    delta_logloss_oos > 0
    and ci95_delta_logloss_lower > 0
    and sign_stable_across_walkforward_folds
    and net_pnl_after_costs_improves
    and latency_budget_not_violated
)

Wenn OI oder Liquidationen diese Prüfung nicht bestehen, bleiben sie nur im Forschungsreport und werden nicht Teil des Signals.

Ergänzung 8 – Multikollinearitätsregeln

Unter „Harte Regeln“ ergänzen:

Verbotene Doppelzählungen:

1. Public-Trade-Hits + Footprint-Hits
   → dieselben Trade-IDs, nur einmal zählen.

2. Aggressives Volumen + Liquidationsvolumen
   → Liquidationen sind Bestandteil des aggressiven Flows.

3. Delta + CVD + Buy/Sell-Imbalance + Aggressor Ratio
   → nicht als vier unabhängige Signale behandeln.

4. Hits + Pulls + Refills + QDH als gleichgewichtete Features
   → QDH ist bereits deren Zusammenfassung.

5. OI Delta + OI Prozent + OI-Z-Score + OI-Slope
   → eine kanonische OI-Innovation verwenden.

6. QDH + AVR Absorption + Footprint Absorption
   → nur zusätzliche Residualinformation verwenden, keine Stimmenmehrheit.

7. Mehrere OB-Imbalances über zahlreiche Tiefen
   → lokales Wall-Band plus Microprice bevorzugen.
Aus dem Trigger streichen
VPIN
24h-Volumen
24h-Preisänderung
Funding Rate im Sekundentrigger
geschätzte Liquidation Heatmaps als Ausführungsbeweis
OI-Interpolation
5-Minuten-OI als Wall-Touch-Trigger
statische L2-Snapshot-Imbalance ohne Eventverlauf
mehrere nahezu identische Velocity-Fenster
Candle-Footprint zusätzlich zu denselben Raw Trades
Ergänzung 9 – neue Module

Zur vorgeschlagenen Modulstruktur hinzufügen:

obfull_research_engine/
├── footprint_cluster.py
├── flow_type_enrichment.py
├── oi_context.py
├── liquidation_flow.py
├── liquidation_exhaustion.py
└── feature_ablation.py

Dabei:

footprint_cluster.py

ist Teil der notwendigen Public-Trade-Verarbeitung.

Die übrigen Module werden erst nach dem QDH-Base-Proof aktiviert.

Ergänzung 10 – zusätzliche Pflicht-Tests

Zu den bisherigen Tests hinzufügen:

Dieselbe Trade-ID darf nicht zweimal gezählt werden.
Footprint-Hit und QDH-Hit referenzieren dieselbe kanonische Trade-Menge.
Liquidationsvolumen wird nicht zum aggressiven Volumen addiert.
Liquidationsseite wird korrekt in Forced-Flow-Richtung übersetzt.
Liquidationsmeldung darf nicht rückwirkend frühere Features verändern.
Liquidations-Bankruptcy-Price wird nicht als exakter Tradepreis behandelt.
Historisches 5-Minuten-OI ergibt im Wall-Trigger M_OI = 1.
OI wird nicht zwischen Updates interpoliert.
Neues OI wirkt erst ab event_available_at.
Veraltetes OI erhält korrektes source_age.
Steigendes OI ohne Richtungsbestätigung erzeugt kein Building-Signal.
Forced-Flow-Anteil bleibt auf \([0,1]\) begrenzt.
Beschleunigende Liquidationen erhöhen Fortsetzungsgefahr.
Fallende Liquidationen allein erzeugen noch kein LEP.
LEP verlangt zusätzlich QDH-Drop und Reclaim.
OI-/Liquidations-Shadow-Features verändern Episode‑1-Base-Ergebnis nicht.
Ablation kann OI und Liquidationen vollständig deaktivieren.
Deaktivierte optionale Datenquellen verändern das Base-Signal nicht.
Aktualisierte Reihenfolge
1. Aktuellen Zone-/Touch-/Detection-Prompt abschließen
2. Restblocker beseitigen
3. Einheitliche Event-Timeline
4. Public-Trade-/Wall-Flow-Attribution
5. FootprintClusterEvent
6. QDH_base
7. Aggressor-Persistenz
8. Impact Efficiency / Absorption Ratio / Vacuum Score
9. Episode-1-QDH-Base-Proof
10. OI und Liquidationen als Shadow-Features
11. Flow Type und LEP
12. Historische Skalierung
13. Feature-Ablation
14. WallStateClassifier kalibrieren
15. Signal V2
16. Handelbarer Backtest
17. Cross-Exchange später

Damit ist der Plan vollständig. Die entscheidende Änderung gegenüber der bisherigen Version lautet:

Zuerst beweisen Full L2 und Public Trades allein den QDH-Base-Mechanismus. OI und Liquidationen dürfen das Signal erst verändern, nachdem ihr zusätzlicher Out-of-Sample-Informationsgewinn bewiesen wurde.