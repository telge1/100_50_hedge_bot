# LIQUIDITY_DESTINATION_BIAS — Phase 2B Abschlussbericht

**Verdict:**

```text
LIQUIDITY_DESTINATION_HISTORY_LOOKAHEAD_V1_CAUSAL_EXTENSION_READY
```

Phase 2B ist ein read-only Audit von Upstream-Kausalität, T0/Outcome-Trennung,
Public-Trade-1s-Semantik, Phase-2-Splits und History-Suffizienz. Es wurde keine
Historie erweitert, kein Feature gebaut und keine Bias-Prognose freigegeben.

## 1. Git-Status

| Repo | Branch | HEAD | Dirty |
|------|--------|------|-------|
| spread_recovery_hedge_short_dev | `feature/btc-doge-research-db` | `9ab70ee955ff5a47e2c42ecaa6d727df581c2d34` | ja (vorbestehend; unangetastet außer neue Results) |
| orderbook_analyse | `feature/strategy-lab-phase1` | `1019974694c27f01249718c99b192c8490038f49` | ja (vorbestehend; unangetastet) |

Keine `AGENTS.md` in beiden Roots gefunden.

Neu ausschließlich:

```text
results/liquidity_destination_bias_phase_2b_history_lookahead_audit/
```

## 2. Audit-Kurzfazit

| Audit | Resultat |
|-------|----------|
| A Upstream-LLD-Kausalität | **PASS** (Residual: HTF-Tip Invalidierung ~1m) |
| B T0-/Outcome-Trennung | **PASS** |
| C Public-Trade-1s | **PASS** mit Bucket-Regel dokumentiert |
| D Phase-2-Splits | **PASS**; 30./31.08. = consumed benchmark |
| E Sieben-Tage-Ursache | Operational (168h Cap + Research-Trades bis 31.08. + Start 25.08. 00:05Z) |
| F Feature-Verfügbarkeit | Kern SAFE; Profile/Full-OB BLOCKED; OI nur closed bucket |

## 3. Upstream-LLD (`available_at`, Persistenz)

- Pool entsteht in TRP `run_liquidity_location` (Candle i-1 → Confirm i).
- `available_at` = **Ende der Confirmation-Bar** = frühester erkennbarer/nutzbarer Zeitpunkt.
- Snapshot: OA `export_snapshot(as_of=T0)` = historische Rekonstruktion, keine Endzustands-Rückprojektion.
- 60s-Gate: `available_at <= T0-60s` (vergangene Lebensdauer, nicht zukünftige).
- Keine Nutzung von späterer Maximalgröße, Lebensdauer, Touch oder Auflösung für Zielwahl.
- Targets werden vor dem Preisweg eingefroren; Outcome ändert Geometrie nicht.

Residualrisiko: HTF-Kerzen-Tipp kann Invalidierung leicht vor wahrem Bar-Close auslösen (~1m bei 5m). Das ist frühe Deaktivierung, kein klassisches Ranking-Lookahead. Details: `upstream_causality_audit.md`.

## 4. T0-/Outcome und Public-Trade-1s

Bekannt bei T0: Preis (`bucket_start < T0`), Targets, Distanzen, Eligibility.  
Nur nach T0 / nur Label: Outcome, Touch-Zeiten, Pfad `[T0, horizon)`.

`active_until` wird fortlaufend nach bekanntem Outcome gesetzt; kein rückwirkendes Preferencing anhand zukünftiger Labels.

Public-Trade-Regeln:

- SoT: `btc_doge_research.research_public_trades` FINAL, Event-Time.
- Bucket `toStartOfSecond(event_time)`; Close = letzter Trade der Sekunde.
- **price_t0:** letzter Close mit `bucket_start < T0` (strikt).
- Feature-Empfehlung später: linksabgeschlossen / `event_time < T0`; für Availability-aware Pfade `available_at <= T0`.
- Verspätete Trades mit alter Event-Time können bei Re-Read Buckets revidieren (Datenrevision).

NEITHER erfordert geschlossenen Horizont; HORIZON_NOT_CLOSED ist Ausschluss, kein Outcome.

## 5. Phase-2-Splits und verbrauchter Test

Splits kalendarisch, kein Overlap, Train-only Majority, Hash-Control, keine Global-Normierung: **PASS**.

```text
2026-08-30 / 2026-08-31 = consumed_benchmark_test
```

Weiter nutzbar für reproduzierbare Baseline-Vergleiche; nicht als einziger finaler Holdout für spätere Orderflow-Modelle.

## 6. Warum nur sieben Tage?

Nicht „keine Rohdaten“. Ursache:

1. `MAX_BOUNDED_EXPAND_HOURS = 168`
2. Research-Trades enden **2026-08-31**
3. Konservativer Start **2026-08-25T00:05Z** (proven eligible ab 24.08. 22:53Z)

Vor dem 25.08. existieren Research-Trades ab **19.07.** und Candles ab **11.12.2025**.  
Nach dem 31.08. existieren Live-Trades/OI/OB200-Raw, aber **kein** Research-Trade-SoT (Sep+=0).

## 7. History-Gates (vor Prognose eingefroren)

| Gate | Minimum | Aktueller Stand |
|------|---------|-----------------|
| Technische Feature-Entwicklung | 7 Tage, 1000 Episoden, 2 Symbole, beide Touch-Klassen | **PASSED** (7 / 1719) |
| Bias-/Modellentwicklung | ≥30 Tage, ≥5000 Episoden, Regime-Vielfalt, keine kritischen Seams | **FAILED** |
| Finaler Forward-Test | ≥14 neue Tage, ≥1000 Episoden, unused outcomes | **NOT STARTED** |

Live-Bias-Prognose: **unzulässig**.

## 8. Empfohlene Variante

**Variante A** — ältere Historie kausal erweiterbar (Jul19→Aug24+), gleiche Semantik, kein Collector-Backfill für Pools (reine Neu-Berechnung). Erwartungsordnung ~10k Episoden über Jul19–Aug31 bei ähnlicher Rate (Schätzung). Nächster Prompt: Phase 2C Causal History Extension.

Variante B (Forward Rematerialisierung) parallel optional. Variante C (Lookahead-Block) nicht gewählt.

## 9. Sichere vs blockierte spätere Features

- **SAFE_CORE:** LLD-Distanzen/Pool-Geometrie  
- **SAFE_WITH_SOT_FREEZE:** Trade-Delta/Volumen/Aggressor (ein SoT freezen)  
- **SAFE_IN_OB200_SPAN / POST_SEAM:** OB200-Imbalance/Walls ab 24.08.  
- **SAFE_IF_CLOSED_BUCKET_ONLY:** OI (nie in-progress Bucket)  
- **BLOCKED:** Full-Session POC/VAH/VAL ohne kausales Profil; Full-OB Grid-History; continuous Full-OB  

## 10. Sicherheitsbestätigung

```text
CODE_CHANGED=false
TEST_CHANGED=false
CONTRACT_CHANGED=false
DATASET_CHANGED=false
DATABASE_WRITES_EXECUTED=false
BACKFILL_EXECUTED=false
LIVE_PROCESSES_CHANGED=false
DASHBOARD_CHANGED=false
COLLECTOR_CHANGED=false
COMMIT_CREATED=false
PUSH_EXECUTED=false
HISTORY_EXTENSION_STARTED=false
FEATURE_IMPLEMENTATION_STARTED=false

```

**STOP.** Keine History-Erweiterung und keine Feature-Implementierung in diesem Auftrag.
