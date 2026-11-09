# L1 V3K — Diagnose: September-Support `d-1f70c9798935`

**Modus:** READ-ONLY (bestehende Pilot-/V3J-Logik, keine Strategieänderung)  
**Datum:** 2026-11-08  
**Symbol:** DOGEUSDT 5m  
**Bezugslauf:** L1 V3J `DRAWINGS_AUTO_WINDOW`, SHA256 `0380057b4660d3090d51eaeae22ea609aaae382aa9af699912be93c73c4a8b3f`

---

## 1. Identifizierte Zeichnung

Die gesuchte **aufsteigende** diagonale Unterstützung ab ~16.09.2026 / 0,078 ist:

| Feld | Wert |
|---|---|
| **drawing_id** | `d-1f70c9798935` |
| **Typ** | `trend` |
| **Farbe** | `#f5f0f0` |
| **Anker 1** | **2026-09-16 12:00:00 UTC** @ **0.0779466405** |
| **Anker 2** | **2026-10-02 04:00:00 UTC** @ **0.1049478456** |
| **extend_right** | `false` (kein `metadata.extend_right` in JSON) |
| **Geometrisch gültig** | `BETWEEN_ANCHORS` auf **[2026-09-16 12:00 UTC … 2026-10-02 04:00 UTC]**; danach `AFTER_SECOND_ANCHOR` → inaktiv |
| **Im aktuellen Chart** | Ja (`visible: true`) |

Hinweis: Zweiter naheliegender Trend `d-ac60572bfc69` (Start ~15.09.) ist **fallend** (Preis steigt von 0,0766 → 0,0894) und kein aufsteigender Support.

---

## 2. Snapshot-Vergleich (V3J)

| Prüfung | Ergebnis |
|---|---|
| In `l1_v3j_drawings_snapshot_0380057b4660d309.json`? | **Ja**, identischer Eintrag |
| Seit V3J neu hinzugefügt? | **Nein** |
| Anker verändert? | **Nein** (byte-identisch) |
| `extend_right` verändert? | **Nein** |
| SHA256 aktuell vs. Snapshot | **Gleich:** `0380057b4660d3090d51eaeae22ea609aaae382aa9af699912be93c73c4a8b3f` |
| Zeichnungsanzahl | **14** (unverändert) |

**Folgerung:** Fehlende Trades nach 02.09. hängen **nicht** daran, dass die Linie im V3J-Lauf noch fehlte.

---

## 3. Dynamische Support-Erkennung

`support_line_selection.py` liefert **`LINE_SELECTION_DYNAMIC`**; die operative Entscheidung erfolgt in `run_l1_pilot.assess_long_touch_eligibility()`.

| Kriterium | Bewertung |
|---|---|
| Diagonale Trendlinie | Ja |
| Geometrisch aktiv am Touch | Ja (innerhalb Ankersegment) |
| Support von oben (meiste Touches) | Ja (**26/30** mit gültigem Approach) |
| Upper-Main nur Exit, kein Trigger | Korrekt (nicht als Long-Trigger gezählt) |
| Unterhalb fester Oberkanal-Referenz `d-22f8d9f720ce` | **Nein** → **REJECTED** |
| Anker-/extend_right-Fehler | Nein |

**Auswahlentscheidung:** **`REJECTED`**  
**Dominanter Ablehnungsgrund:** **`TREND_NOT_BELOW_MAIN_UPPER_AT_TOUCH`** (26/30 Touch-Phasen)

Zusatzablehnungen: `TREND_RESISTANCE_FROM_BELOW` (1), `TREND_APPROACH_NOT_FROM_ABOVE` (3).

`certify_lower_parallel_trends` (Forschungsrolle): **`NOT_ALWAYS_BELOW_MAIN_UPPER`**.

---

## 4. Historische Touch-Diagnose

Zeitraum: verfügbare 5m-Daten im V3J-Fenster, Linie aktiv auf **4513** Bars.

| Metrik | Anzahl |
|---|---:|
| Geometrisch gültige 5m-Kerzen (Linie aktiv) | 4513 |
| Preisannäherungen von oben (gültiger Approach) | 26 |
| Touches in 0,20%-Zone (`build_touch_events`) | 30 Touch-Phasen |
| Gültige Touch-Phasen gesamt | 30 |
| C1-Bestätigungen (reine Touch-Logik) | 12 |
| C1-Kandidaten im Pilot-Pipeline (`entry_utc`) | **0** |
| Mögliche Entries nach C1 (Pipeline) | **0** |

Alle Touches inkl. Upper-Main-Projektion: siehe Tabelle unten und `l1_v3k_september_support_touches.csv`.

### Touch-Tabelle (vollständig)

| Touch UTC | Linienpreis | Candle Low | Abstand % | C1 UTC | Entry UTC | Status | Eligibility | Ablehnung | Upper-Main @ Touch |
|---|---|---|---|---|---|---|---|---|---|
| 2026-09-16 17:40:00 UTC | 0.0783535735 | 0.0783900000 | 0.0465 |  |  | NO_C1 | REJECTED | TREND_NOT_BELOW_MAIN_UPPER_AT_TOUCH | 0.0634484437 (active=False) |
| 2026-09-16 17:55:00 UTC | 0.0783715265 | 0.0784700000 | 0.1256 | 2026-09-16 18:00:00 UTC | 2026-09-16 18:05:00 UTC | C1_OK | REJECTED | TREND_NOT_BELOW_MAIN_UPPER_AT_TOUCH | 0.0634344031 (active=False) |
| 2026-09-16 18:00:00 UTC | 0.0783775108 | 0.0783700000 | 0.0096 | 2026-09-16 18:05:00 UTC | 2026-09-16 18:10:00 UTC | C1_OK | REJECTED | TREND_NOT_BELOW_MAIN_UPPER_AT_TOUCH | 0.0634297229 (active=False) |
| 2026-09-20 08:40:00 UTC | 0.0846011928 | 0.0845500000 | 0.0605 |  |  | NO_C1 | REJECTED | TREND_NOT_BELOW_MAIN_UPPER_AT_TOUCH | 0.0585622992 (active=False) |
| 2026-09-20 09:00:00 UTC | 0.0846251301 | 0.0847000000 | 0.0885 |  |  | NO_C1 | REJECTED | TREND_NOT_BELOW_MAIN_UPPER_AT_TOUCH | 0.0585435784 (active=False) |
| 2026-09-20 10:00:00 UTC | 0.0846969418 | 0.0848500000 | 0.1807 |  |  | NO_C1 | REJECTED | TREND_NOT_BELOW_MAIN_UPPER_AT_TOUCH | 0.0584874158 (active=False) |
| 2026-09-20 12:00:00 UTC | 0.0848405652 | 0.0848800000 | 0.0465 | 2026-09-20 12:05:00 UTC | 2026-09-20 12:10:00 UTC | C1_OK | REJECTED | TREND_NOT_BELOW_MAIN_UPPER_AT_TOUCH | 0.0583750906 (active=False) |
| 2026-09-20 12:20:00 UTC | 0.0848645024 | 0.0850000000 | 0.1597 |  |  | NO_C1 | REJECTED | TREND_NOT_BELOW_MAIN_UPPER_AT_TOUCH | 0.0583563698 (active=False) |
| 2026-09-20 14:00:00 UTC | 0.0849841886 | 0.0848900000 | 0.1108 |  |  | NO_C1 | REJECTED | TREND_RESISTANCE_FROM_BELOW | 0.0582627655 (active=False) |
| 2026-09-20 15:00:00 UTC | 0.0850560004 | 0.0850500000 | 0.0071 | 2026-09-20 15:05:00 UTC | 2026-09-20 15:10:00 UTC | C1_OK | REJECTED | TREND_APPROACH_NOT_FROM_ABOVE | 0.0582066029 (active=False) |
| 2026-09-24 09:30:00 UTC | 0.0915549606 | 0.0914600000 | 0.1037 | 2026-09-24 09:35:00 UTC | 2026-09-24 09:40:00 UTC | C1_OK | REJECTED | TREND_NOT_BELOW_MAIN_UPPER_AT_TOUCH | 0.0531238894 (active=False) |
| 2026-09-24 09:35:00 UTC | 0.0915609449 | 0.0913700000 | 0.2085 |  |  | NO_C1 | REJECTED | TREND_NOT_BELOW_MAIN_UPPER_AT_TOUCH | 0.0531192092 (active=False) |
| 2026-09-24 09:50:00 UTC | 0.0915788979 | 0.0916900000 | 0.1213 | 2026-09-24 09:55:00 UTC | 2026-09-24 10:00:00 UTC | C1_OK | REJECTED | TREND_NOT_BELOW_MAIN_UPPER_AT_TOUCH | 0.0531051685 (active=False) |
| 2026-09-24 10:30:00 UTC | 0.0916267723 | 0.0917600000 | 0.1454 | 2026-09-24 10:35:00 UTC | 2026-09-24 10:40:00 UTC | C1_OK | REJECTED | TREND_NOT_BELOW_MAIN_UPPER_AT_TOUCH | 0.0530677268 (active=False) |
| 2026-09-24 10:35:00 UTC | 0.0916327567 | 0.0917900000 | 0.1716 | 2026-09-24 10:40:00 UTC | 2026-09-24 10:45:00 UTC | C1_OK | REJECTED | TREND_NOT_BELOW_MAIN_UPPER_AT_TOUCH | 0.0530630466 (active=False) |
| 2026-09-26 20:40:00 UTC | 0.0958038205 | 0.0953900000 | 0.4319 |  |  | NO_C1 | REJECTED | TREND_NOT_BELOW_MAIN_UPPER_AT_TOUCH | 0.0498009367 (active=False) |
| 2026-09-26 21:00:00 UTC | 0.0958277577 | 0.0957400000 | 0.0916 |  |  | NO_C1 | REJECTED | TREND_APPROACH_NOT_FROM_ABOVE | 0.0497822158 (active=False) |
| 2026-09-27 00:45:00 UTC | 0.0960970517 | 0.0962500000 | 0.1592 |  |  | NO_C1 | REJECTED | TREND_NOT_BELOW_MAIN_UPPER_AT_TOUCH | 0.0495716062 (active=False) |
| 2026-09-27 01:50:00 UTC | 0.0961748477 | 0.0963600000 | 0.1925 |  |  | NO_C1 | REJECTED | TREND_NOT_BELOW_MAIN_UPPER_AT_TOUCH | 0.0495107634 (active=False) |
| 2026-09-27 02:40:00 UTC | 0.0962346908 | 0.0964100000 | 0.1822 |  |  | NO_C1 | REJECTED | TREND_NOT_BELOW_MAIN_UPPER_AT_TOUCH | 0.0494639612 (active=False) |
| 2026-09-27 06:10:00 UTC | 0.0964860318 | 0.0966000000 | 0.1181 |  |  | NO_C1 | REJECTED | TREND_NOT_BELOW_MAIN_UPPER_AT_TOUCH | 0.0492673922 (active=False) |
| 2026-09-27 06:30:00 UTC | 0.0965099690 | 0.0966100000 | 0.1036 | 2026-09-27 06:35:00 UTC | 2026-09-27 06:40:00 UTC | C1_OK | REJECTED | TREND_NOT_BELOW_MAIN_UPPER_AT_TOUCH | 0.0492486713 (active=False) |
| 2026-09-27 13:00:00 UTC | 0.0969767452 | 0.0970100000 | 0.0343 |  |  | NO_C1 | REJECTED | TREND_NOT_BELOW_MAIN_UPPER_AT_TOUCH | 0.0488836145 (active=False) |
| 2026-09-27 13:05:00 UTC | 0.0969827295 | 0.0969800000 | 0.0028 |  |  | NO_C1 | REJECTED | TREND_NOT_BELOW_MAIN_UPPER_AT_TOUCH | 0.0488789343 (active=False) |
| 2026-09-27 13:15:00 UTC | 0.0969946981 | 0.0969100000 | 0.0873 | 2026-09-27 13:20:00 UTC | 2026-09-27 13:25:00 UTC | C1_OK | REJECTED | TREND_APPROACH_NOT_FROM_ABOVE | 0.0488695739 (active=False) |
| 2026-09-27 13:20:00 UTC | 0.0970006824 | 0.0971100000 | 0.1127 | 2026-09-27 13:25:00 UTC | 2026-09-27 13:30:00 UTC | C1_OK | REJECTED | TREND_NOT_BELOW_MAIN_UPPER_AT_TOUCH | 0.0488648937 (active=False) |
| 2026-09-27 13:40:00 UTC | 0.0970246197 | 0.0971100000 | 0.0880 | 2026-09-27 13:45:00 UTC | 2026-09-27 13:50:00 UTC | C1_OK | REJECTED | TREND_NOT_BELOW_MAIN_UPPER_AT_TOUCH | 0.0488461728 (active=False) |
| 2026-09-27 13:45:00 UTC | 0.0970306040 | 0.0970000000 | 0.0315 |  |  | NO_C1 | REJECTED | TREND_NOT_BELOW_MAIN_UPPER_AT_TOUCH | 0.0488414926 (active=False) |
| 2026-09-27 14:20:00 UTC | 0.0970724941 | 0.0971400000 | 0.0695 |  |  | NO_C1 | REJECTED | TREND_NOT_BELOW_MAIN_UPPER_AT_TOUCH | 0.0488087311 (active=False) |
| 2026-09-27 18:55:00 UTC | 0.0974016312 | 0.0975600000 | 0.1626 |  |  | NO_C1 | REJECTED | TREND_NOT_BELOW_MAIN_UPPER_AT_TOUCH | 0.0485513193 (active=False) |

**Beispiel erster C1-Touch (16.09.):** Linienpreis **0,07837**, Upper-Main-Projektion **0,06343** → Support liegt **preislich über** der fest codierten August-Obergrenze; daher `long_trigger_allowed = no` und **kein** `entry_utc` in V3J-Pending.

---

## 5. Trading-Gates A / B / C

Es wurden **keine** Kandidaten mit `entry_utc` für diese Linie erzeugt (Vorprüfung scheitert). Varianten-Gates **nicht erreicht**.

| Gate | A | B | C |
|---|---|---|---|
| Support-Eligibility | **REJECTED** | **REJECTED** | **REJECTED** |
| Restliche Gates | — | — | — |

Erster Gate-Hit (alle Varianten gleich): **`TREND_NOT_BELOW_MAIN_UPPER_AT_TOUCH`**.

---

## 6. Obere Kanalbegrenzung `d-22f8d9f720ce`

| Frage | Antwort |
|---|---|
| Geometrisch aktiv am 16.09.2026? | **Nein** (`AFTER_SECOND_ANCHOR`; Segment endet **2026-09-03 22:55 UTC**) |
| Projizierter Preis am 16.09. ~13:00 UTC | ca. **0,06377** (mathematische Linienfortsetzung) |
| Als TP für September-Entries genutzt? | **Nein** – kein Entry |
| Verhindert Long-Entries? | **Ja** – Pflichtvergleich in `assess_long_touch_eligibility` auch bei inaktiver Geometrie |
| Neue obere Trendlinien als TP-Referenz? | `channel_upper_at()` kann andere aktive Trends oberhalb Entry wählen; für diese Linie irrelevant, da vorher abgelehnt |

---

## 7. Eindeutige Erklärung

1. Die September-Linie **`d-1f70c9798935` war im V3J-Lauf vorhanden** und erzeugte **30 Touch-Phasen** / **12 C1-Signale** (Touch-Engine).
2. Alle Long-Touches wurden in der **Support-Eligibility** verworfen, weil der Linienpreis **nicht unter** der fest verdrahteten Referenz **`d-22f8d9f720ce`** liegt (Weiterprojektion ~0,048–0,063 vs. Support ~0,078–0,097).
3. Dadurch **keine** `entry_utc`-Kandidaten → **keine** Ausführung in A/B/C → **kein** Trade nach 02.09. aus dieser Linie.
4. D2-Nähe, Staffelung, globales Ein-Long und TP-Logik spielen **keine Rolle**, weil die Pipeline vorher stoppt.

**Primärursache:** Feste August-Oberkanal-Referenz + Pflicht „Support strikt unter Upper-Main“ auf der September-Linie (mathematisch nicht erfüllt).

---

## 8. Antworten (§9)

| # | Frage | Antwort |
|---|---|---|
| 1 | Linie beim letzten V3J-Lauf vorhanden? | **Ja** (Snapshot-identisch) |
| 2 | Als Unterstützung erkannt? | **Als Trend ja**, als **Long-Trigger nein** (`REJECTED`) |
| 3 | Historische Touches von oben? | **Ja**, 30 Phasen / 26 Approach |
| 4 | Bestätigte C1-Kandidaten (Pipeline)? | **Nein** (0 mit `entry_utc`; 12 nur rein touch-logisch) |
| 5 | Welches Gate? | **`TREND_NOT_BELOW_MAIN_UPPER_AT_TOUCH`** (vor D2/B/C) |
| 6 | Alte August-Obergrenze verantwortlich? | **Ja** (Referenz `d-22f8d9f720ce`, inkl. extrapolierter Preis nach Segmentende) |

---

**Artefakte dieser Diagnose:** `l1_v3k_september_support_diagnostic.md`, `l1_v3k_september_support_touches.csv`

**STOP** — nur Diagnose, keine Implementierung.
