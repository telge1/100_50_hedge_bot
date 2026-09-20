# LIQUIDITY_DESTINATION_BIAS — Phase 2 Abschlussbericht

Verdict:

`LIQUIDITY_DESTINATION_BASELINES_V1_DISTANCE_ONLY_STRONG_CAUTION`

Die Baseline-Evaluation ist methodisch vollständig und reproduzierbar. Auf dem
eingefrorenen Testfenster dominiert `B1_NEAREST_TARGET` so klar gegenüber
Majority- und Hash-Kontrollen, dass spätere kausale Features zwingend gegen
diese Distanz-Baseline und nicht gegen Zufall bewertet werden müssen.

Dieses Verdict beweist keine Trading-Edge, keine Profitabilität und keinen
aktuellen Long-/Short-Bias.

## Git und Scope

- Branch: `feature/btc-doge-research-db`
- HEAD: `9ab70ee955ff5a47e2c42ecaa6d727df581c2d34`
- Vorbestehend dirty/untracked: u.a. `dashboard/app.py` (M) und frühere
  Research-/Result-Artefakte — nicht angefasst
- Neu: `research/liquidity_destination_bias/baselines/`,
  `scripts/evaluate_liquidity_destination_baselines.py`,
  `tests/research/test_liquidity_destination_baselines.py`,
  `results/liquidity_destination_bias_phase_2_baselines/`
- Builder, Episode-Contract und Phase-1D-CSVs unverändert
- Kein Commit/Push

## Freeze vor Outcome-Auswertung

Vor der Metrikberechnung eingefroren:

```text
dataset_fingerprint_sha256=
9f7d2bd2fd1aff9315b268c70f818020ba91f3c2f5992f3dcd15aec550ad77e8
BTCUSDT/episodes.csv=
ca3ba554e59653fbdbe03124d1558aa3f4f767c274c532c9c611d92abf4cf1a4
DOGEUSDT/episodes.csv=
2d1217b46d490bed10377add4318563e461eefb6a509a943631e9d5e8d0b08af
evaluation_contract_sha256=
185691763f9b11f0602c832c99a6b5210642684d10a3f61360236dab42f85a0c
```

Phase-1D-Manifest-Hashes wurden verifiziert. Ambiguous-Policy: aus primären
Drei-Klassen-Metriken ausschließen und getrennt berichten (Ist-Wert: 0).

## Zeitliche Splits

| Split | Gesamt | BTC | DOGE | UPPER | LOWER | NEITHER |
|---|---:|---:|---:|---:|---:|---:|
| TRAIN `2026-08-25`–`29` | 1074 | 553 | 521 | 490 | 509 | 75 |
| VALIDATION `2026-08-29` | 209 | 106 | 103 | 103 | 86 | 20 |
| TEST `2026-08-30`–Ende | 436 | 220 | 216 | 183 | 221 | 32 |

Keine zufällige Aufteilung. Kein Episode-Leak zwischen Splits.

TRAIN-Majority (B0/B4): durchgängig `LOWER_FIRST`.

## TEST-Metriken (primär, drei Klassen)

COMBINED Test (n=436):

| Baseline | Accuracy | Balanced Acc | Macro F1 | Wilson Acc |
|---|---:|---:|---:|---|
| B0 Train Majority | 0.5069 | 0.3333 | 0.2243 | 0.460–0.554 |
| B1 Nearest Target | **0.7913** | **0.5677** | **0.5463** | **0.751–0.827** |
| B2 Inverse Nearest | 0.1353 | 0.0990 | 0.0936 | 0.106–0.171 |
| B3 Hash Control | 0.3326 | 0.3536 | 0.3020 | 0.290–0.378 |
| B4 Symbol Majority | 0.5069 | 0.3333 | 0.2243 | 0.460–0.554 |

Nearest Target je Symbol (TEST):

- BTC: Acc 0.7864, BalAcc 0.5759, MacroF1 0.5486
- DOGE: Acc 0.7963, BalAcc 0.5592, MacroF1 0.5435

Hinweise: Majority und Nearest warnen `missing_predicted_class:NEITHER...`
(Zero-Division → 0.0). Accuracy allein ist kein Beweis; Balanced Accuracy und
F1 bleiben für Nearest deutlich über den Kontrollen, aber weit unter perfekt.

## Sekundäre Directional Accuracy (nur UPPER/LOWER)

TEST COMBINED (n=404, 32 NEITHER ausgeschlossen):

| Baseline | Directional Acc | Balanced Directional Acc |
|---|---:|---:|
| B0/B4 Majority | 0.5470 | 0.5000 |
| B1 Nearest | **0.8540** | **0.8515** |
| B2 Inverse | 0.1460 | 0.1485 |
| B3 Hash | 0.3267 | 0.3273 |

BTC Nearest directional: 0.8650; DOGE: 0.8431.

Diese Metrik ist sekundär und darf nicht als primäres Ergebnis gelesen werden.

## Tägliche Stabilität (TEST)

Nur zwei Testtage:

- B1 COMBINED `2026-08-30`: Acc 0.7976 (bester)
- B1 COMBINED `2026-08-31`: Acc 0.7831 (schlechtester)

Wegen nur sieben Gesamttagen und zwei Testtagen keine starke statistische
Unabhängigkeit behaupten. Kein episodischer Bootstrap als Verdict-Grundlage.

## Distanzdiagnostik (deskriptiv, keine Schwellenwahl)

Über alle 1719 Primärepisoden:

- Näheres Ziel gewann: 79,06 %
- Ferneres Ziel gewann: 13,50 %
- NEITHER: 7,39 %
- Exakte Distanzgleichstände: 1
- Obere näher: 847; untere näher: 871
- Median Distanzverhältnis (oben/unten): UPPER_FIRST 0.125; LOWER_FIRST 7.129;
  NEITHER 0.794

Nearest-korrekt-Konzentration: stärkster Tag `2026-08-28` (254), stärkste
Stunde `13` (85). Das Signal ist nicht auf ein Symbol beschränkt (BTC und DOGE
ähnlich), kann aber tages-/stundenweise korreliert sein.

Inverse-Nearest kollabiert erwartungsgemäß und bestätigt, dass die gemessene
Struktur ein Distanz-Nähe-Effekt ist und keine symmetrische Klassen-Artefakt-
Baseline.

## Beste Baseline (rein deskriptiv)

Deskriptiv am stärksten auf TEST: `B1_NEAREST_TARGET`.  
Das ist **keine** Freigabe als Live-Signal und **keine** Feature-Auswahl.

## Tests

- 14/14 neue Phase-2-Tests bestanden
- 38/38 bestehende Builder-/Multiday-Tests bestanden
- 9/9 LLD-/Kausalitätstests bestanden
- Evaluation zweimal identisch (Core-Artefakt-Hashes gleich)
- CLI gibt `RESEARCH_ONLY_NO_TRADE` aus; keine DB-Verbindung

## Einschränkungen

- Nur Distanz-/Majority-/Hash-Baselines; keine Orderflow-/OB-/OI-/Liq-/Profile-
  Features und kein ML
- Nur sieben UTC-Tage; Marktphasen-Korrelation möglich
- Nearest sagt praktisch nie NEITHER vorher
- Kein `--now`, kein Dashboard, keine Tradingentscheidung
- Validation wurde nicht zur nachträglichen Baseline-Auswahl missbraucht

## Sicherheitsbestätigung

```text
ORDERFLOW_FEATURES=false
ML=false
CURRENT_BIAS_EVALUATION=false
EDGE_OR_PROFITABILITY_CLAIM=false
BUILDER_CHANGED=false
CONTRACT_CHANGED=false
PHASE1D_DATA_CHANGED=false
DATABASE_WRITES_EXECUTED=false
LIVE_PROCESSES_CHANGED=false
DASHBOARD_CHANGED=false
COLLECTOR_CHANGED=false
STRATEGY_OR_TRADING_CHANGED=false
COMMIT_CREATED=false
PUSH_EXECUTED=false
PHASE_3_STARTED=false
```

**STOP.** Keine Phase 3 und keine aktuellen Bias-Bewertungen beginnen.
