# Calibration Plan

Dieses Verzeichnis beschreibt, wie wir die Thresholds fuer jeden Coin automatisiert kalibrieren.

## 1. Ziel

Fuer jeden Coin sollen wir per Code automatisch lernen:

- welche Breakouts echt sind
- welche Breakouts Fakeouts sind
- welche EMA59-Touches handelbar sind
- welche Schwellenwerte fuer den Coin passen

## 2. Eingabedaten

Wir nutzen nur historische Daten, keine Live-Entscheidung:

- vollstaendige 5m-Candles
- EMA-Snapshots
- Full-OB am Touch-Zeitpunkt
- Public-Trades fuer Context / Confirm / Follow-through

## 3. Schrittfolge

### Phase A: Echte Breakouts finden

Wir suchen zuerst die klaren, starken Ausbrueche nur aus Candle-Daten.

Danach speichern wir pro Event:

- Symbol
- Timestamp
- Richtung
- Candle-Kontext
- EMA-Struktur
- Full-OB
- Public-Trade-Fenster

### Phase B: Fakeouts sammeln

Danach suchen wir die kleinen oder schwachen EMA59-Touches, die nicht durchziehen.

Auch diese Events speichern wir mit denselben Feldern.

### Phase C: Thresholds kalibrieren

Mit den gelabelten Daten suchen wir pro Coin die besten Grenzwerte fuer:

- Confirm-Delta
- Follow-through-Delta
- Orderbook-Ratio
- Fakeout-Grenzen

### Phase D: Gegen aktuelle YAML vergleichen

Die neuen Werte werden gegen die aktuelle Coin-Config verglichen.

Wichtig sind:

- Signalanzahl
- Breakout-Qualitaet
- Fakeout-Quote
- Long/Short-Stabilitaet
- Netto-Edge nach Fees

## 4. Startreihenfolge

1. Zuerst DOGE kalibrieren
2. Danach weitere Coins aufnehmen
3. Pro Coin eigene YAML speichern
4. Nur Thresholds uebernehmen, die im Walk-Forward-Test stabil bleiben

## 5. Erfolgskriterien

Die Kalibrierung ist gut, wenn:

- echte Breakouts sauber erkannt werden
- Fakeouts sauber herausgefiltert werden
- Long und Short getrennt stabil funktionieren
- die Werte im Out-of-Sample-Test nicht stark einbrechen

## 6. Output

Am Ende speichern wir pro Coin:

- die finalen Schwellenwerte
- die getesteten Kandidaten
- die Vergleichsergebnisse gegen die aktuelle YAML
- eine kurze Bewertung, ob die neue Config besser ist

## 7. DOGE — erstes Analysefenster (Coverage-Check)

Stand: 2026-09-21

### Full-OB Archiv (`full_ob_v1/DOGEUSDT`)

- erster Segment-Stundenstart: `2026-09-05T17:00:00Z`
- letzter Segment-Stundenstart: `2026-09-19T10:00:00Z`
- durchgehender Block: `2026-09-05T17:00:00Z` → `2026-09-19T11:00:00Z` (**330 Stunden**, keine Stundenluecke)
- Segmentdateien gesamt: 349 (davon 10 Stunden mit mehreren Dateien / Reconnects)

### Public Trades (ClickHouse)

- im gleichen Fenster: **330/330 Stunden** vorhanden, keine Luecke
- Trades vor OB-Start existieren (fuer EMA200-Warmup): seit mind. `2026-07-18`

### Empfohlenes Kalibrierfenster fuer DOGE

```text
scan_from = 2026-09-05T17:00:00Z
scan_to   = 2026-09-19T11:00:00Z
```

Hinweise:

- Candles/EMA koennen mit Warmup vor `scan_from` geladen werden (Trades reichen aus).
- Full-OB darf nur innerhalb dieses Fensters gesampelt werden.
- Stunden mit mehreren Segmentdateien sind vorhanden; Segmentwahl (`files[0]`) bleibt ein bekanntes Risiko und sollte bei der Kalibrierung markiert oder spaeter gehaertet werden.
- Einzelne `No mid`-Fehler innerhalb des Fensters bedeuten nicht „Stunde fehlt“, sondern Replay-/Segment-Probleme an einzelnen Timestamps.

## 8. Phase A Ergebnis — DOGE starke Candle-Breakouts

Stand: 2026-09-21

### Tool

```text
python -m ob_microstructure_breakout_bot.calibration.discover_strong_breakouts \
  --symbol DOGEUSDT \
  --scan-from 2026-09-05T17:00:00Z \
  --scan-to 2026-09-19T11:00:00Z
```

Candle-only Filter (keine YAML-Thresholds):

- Range >= 2× Median-ATR (24 Bars)
- Body/Range >= 0.55, Close nahe Extremum
- Bruch der lokalen 12-Bar High/Low
- Continuation ueber 6 Bars (~30m) in Breakout-Richtung
- Cluster-Dedup (6 Bars)

### Output

Datei:

`calibration/events/DOGEUSDT_strong_breakouts_phase_a.json`

Ergebnis im Analysefenster:

- **17** starke Breakouts
- **10 long / 7 short**
- **11** davon nahe EMA59 (<= 25 bps)
- Full-OB Enrichment: **16/17** ok (`No mid` nur bei `2026-09-13T22:00Z`)

Bekannter Chart-Anker:

- Research-Case ~`2026-09-18 13:15` erscheint hier als Impulse-Bar `2026-09-18T13:35Z` (long, near_ema59, starkes Confirm/FT)

### Naechster Schritt

Phase B: schwache / kippende EMA59-Touches (Fakeouts) im gleichen Fenster sammeln und mit denselben Feldern speichern.

## 9. Phase B Ergebnis — DOGE Fakeouts (Long + Short)

Stand: 2026-09-21

### Tool

```text
python -m ob_microstructure_breakout_bot.calibration.discover_fakeouts \
  --symbol DOGEUSDT \
  --scan-from 2026-09-05T17:00:00Z \
  --scan-to 2026-09-19T11:00:00Z
```

Regeln (keine YAML-Thresholds, Long/Short gespiegelt):

- erster EMA59-Touch im Cluster
- `from_below` → Long-Setup-Versuch
- `from_above` → Short-Setup-Versuch
- Failure ueber ~30m Hold: Close gegen EMA59 und/oder harte Continuation gegen Setup und/oder Full-Reject
- Phase-A Strong-Breakouts werden ausgeschlossen (6-Bar-Gap)

### Output

Datei:

`calibration/events/DOGEUSDT_fakeouts_phase_b.json`

Ergebnis im Analysefenster:

- **91** Fakeouts
- **46 long / 45 short** (nahezu ausgeglichen)
- Full-OB Enrichment: mehrheitlich ok; einzelne `No mid` / Segment-Ende-Fehler wie in Phase A

Beispiel aus dem bekannten Fake-Fenster:

- `2026-09-16T02:30Z` long fakeout (Δc=+241957, Δft=-105484) — Confirm noch positiv, Follow-through kippt

### Naechster Schritt

Phase C: mit Phase-A (strong) + Phase-B (fakeout) Events die Thresholds fuer Long und Short getrennt suchen und gegen `doge_usdt.yaml` vergleichen.

## 10. Phase C/D Ergebnis — Threshold-Suche vs `doge_usdt.yaml`

Stand: 2026-09-21

### Tool

```text
python -m ob_microstructure_breakout_bot.calibration.calibrate_thresholds \
  --symbol DOGEUSDT --write-suggested-yaml
```

- Input: Phase-A strong + Phase-B fakeouts
- Suche: Grid um Quantile von Confirm/FT/OB
- Bewertung getrennt fuer **long** und **short**
- Walk-forward: 70% train / 30% test (chronologisch)
- Rule-Engine nutzt weiterhin **eine** shared YAML (Short = Spiegel)

### Output

- `calibration/events/DOGEUSDT_calibration_phase_c.json`
- `calibration/events/DOGEUSDT_calibrated_suggestion.yaml` (Vorschlag, nicht aktiv)

### Vergleich (Full Sample, 108 Events)

| Seite | Baseline score | Calibrated score | Strong recall |
|-------|----------------|------------------|---------------|
| overall | 0.495 | 0.506 | 0.35 → 0.35 |
| long | 0.654 | 0.678 | 0.60 → 0.60 |
| short | 0.111 | 0.111 | **0.00 → 0.00** |

Beste Kalibrier-Kandidaten (Suggestion):

```text
fakeout_max_confirm_delta: 25000
tier1_min_confirm_delta: 100000
tier2_min_confirm_delta: 150000
tier1_min_bid_ask_ratio_5bps: 1.0
tier2_min_bid_ask_ratio_5bps: 1.5
fakeout_followthrough_flip_delta: -50000
```

vs aktuell in `config/doge_usdt.yaml`:

```text
50000 / 100000 / 200000 / 1.05 / 2.0 / -100000
```

### Entscheidung

**`doge_usdt.yaml` nicht ersetzen.**

Gruende:

1. Walk-forward Test-Score ist **nicht** klar besser (0.675 = 0.675)
2. Test-Fold enthaelt **keine** short strong Labels (alle 7 short strong liegen im Train)
3. **Short strong_recall = 0** unter Baseline und Calibrated — strukturell:
   - Phase-A Short-Dumps haben oft **kein ask-dominantes** 5bps-Buch
   - die Short-Regel verlangt aber ask-Dominanz / ask/bid-Ratio fuer Confirm
   - grosse negative Deltas allein reichen aktuell nicht

### Naechster Schritt

Short-Pfad der Rule-Engine fuer delta-led Breakouts haerten (OB-unabhaengiger oder anders gewichteter Short-Confirm), dann Phase C erneut — erst danach YAML aendern.

## 11. Long vs Short — getrennte Threshold-Analyse (DOGE)

Stand: 2026-09-21

Die aktuelle `doge_usdt.yaml` ist ein **Long-Satz**. Short spiegelt nur Vorzeichen / ask/bid.
Die gelabelten Events zeigen: **Short-Schwellen sind nicht dieselben.**

### Confirm-Delta (abs)

| | Long strong | Short strong | Long fake | Short fake |
|--|-------------|--------------|-----------|------------|
| n | 10 | 7 | 46 | 45 |
| p25 | ~162k | ~365k | ~37k | ~46k |
| p50 | ~349k | ~729k | ~111k | ~152k |

Short-Strong-Confirms sind deutlich **groesser** (betragsmaessig) als Long-Strong.

### Follow-through Flip

| | Long fake (neg. FT) | Short fake (pos. FT) |
|--|---------------------|----------------------|
| p25 | ~−187k | ~+172k |
| p50 | ~−134k | ~+219k |

Gleiche Groessenordnung, aber **entgegengesetztes Vorzeichen** — Short braucht einen eigenen positiven Flip-Wert, nicht nur `-(long_flip)`.

### Orderbook 5bps (Setup-Seite)

| | Long strong bid/ask | Short strong ask/bid |
|--|---------------------|----------------------|
| p50 | ~1.17 | ~0.69 |
| Side dominant | **90%** bid | nur **33%** ask |

Fazit OB:

- Long: Ratio-Gate (>= ~1.05 / 2.0) ist sinnvoll
- Short: Ask-Dominanz / ask/bid-Ratio ist **kein** stabiler Confirm-Filter fuer starke Dumps

### Quantil-Vorschlag (nur Analyse, noch nicht in YAML)

| Parameter | Long (aus Daten) | Short (aus Daten) |
|-----------|------------------|-------------------|
| fakeout_max_confirm | ~37k | ~46k |
| tier1_min_confirm | ~162k | ~365k |
| tier2_min_confirm | ~349k | ~729k |
| FT flip | ~−187k | ~+172k |
| OB ratio tier1/tier2 | ~1.08 / ~1.5+ | **nicht analog** — eher delta-led / anderes OB-Merkmal |

### Konsequenz

1. YAML spaeter in **long_*** / **short_*** Felder splitten (oder zwei Bloecke)
2. Short-Rule: starke negative Deltas ohne Ask-Dominanz zulassen
3. Phase C getrennt fuer Long und Short wiederholen
4. Aktuelle shared YAML vorerst lassen

## 12. Neue Kalibrier-YAML + Backtest

Stand: 2026-09-21

### Neue Datei

`config/doge_usdt_calibrated.yaml`

- getrennte `long:` / `short:` Bloecke
- Short: hoehere Confirm-Schwellen, positiver FT-Flip, `require_ob_support: false`
- Laden via Symbol `DOGEUSDT_CALIBRATED` oder `--config .../doge_usdt_calibrated.yaml`
- Generator: `python -m ob_microstructure_breakout_bot.calibration.export_calibrated_yaml`

Alte `doge_usdt.yaml` bleibt unveraendert (Legacy / Baseline).

### Labeled-Set (Phase A+B, 108 Events)

| | Legacy | Calibrated |
|--|--------|------------|
| overall score | 0.495 | **0.612** |
| long strong recall | 0.60 | 0.60 |
| short strong recall | **0.00** | **0.43** |

### Scanner-Backtest (DOGE Fenster 2026-09-05T17Z → 2026-09-19T11Z)

| | Legacy | Calibrated |
|--|--------|------------|
| Touches | 171 | 171 |
| breakout_confirmed | 20 | **13** |
| fakeout | 48 | 43 |
| chop | 49 | 62 |

Calibrated ist **strenger auf Long** (tier1 160k statt 100k) und filtert schwache Longs raus.
Short-Breakouts im Scan bleiben in beiden ~3; der Gewinn zeigt sich vor allem auf den gelabelten Strong-Shorts (delta-led).

Ergebnisdatei:

`calibration/events/DOGEUSDT_backtest_legacy_vs_calibrated.json`

