# Preflight — Console None / Doppelausgabe (read-only)

**Branch:** `feature/btc-doge-research-db`  
**HEAD:** `2af0477dc4654d47603a2845a0ea1463b0bcfa12`  
**Dirty files:** 121  
**Fight-CLI aktiv:** nein  
**Bestehender Lauf (unverändert):** `results/btc_ob_fight_cases/20260830T163000Z/run_001/`

## 1. Warum Console `None` zeigt

`research_db_cli.py` schreibt nach Performance-Lean:

```python
summary["fight_facts"] = (fight_bundle or {}).get("manifest")          # flach
summary["sequence_validation"] = (sequence_bundle or {}).get("fight_sequence_summary")  # flach
```

`reporting.print_console_summary` liest aber noch:

```python
fm = ff.get("manifest") or {}                 # → {}
sq = sv.get("fight_sequence_summary") or {}  # → {}
```

Die Counts liegen bereits auf der obersten Ebene von `summary["fight_facts"]` /
`summary["sequence_validation"]` (belegt in `run_001/summary.json`:
`profile_state_episode_count=1`, `canonical_outside_count=1`, …).
Die Console greift auf die falschen verschachtelten Pfade → `None`.

`REPORT.md` / CSV / JSON erhalten die **vollen** Bundles über
`write_all_outputs(..., fight_facts=fight_bundle, sequence_validation=sequence_bundle)`
— daher korrekt befüllt.

## 2. Zwischenstand vor Sequenz?

Nein. Die finale Console läuft **nach** `build_sequence_validation` und
`write_all_outputs`. Es ist kein vorzeitiges Rendern — nur falsche Feldpfade
auf dem bereits geleanten Summary-Objekt.

## 3. Unterschiedliche Objekte / veraltete Feldpfade

Ja: Lean-Summary vs. Full-Bundle. Console und REPORT nutzen dieselben
Funktionen, aber REPORT bekommt Full-Bundles als Keyword-Args, Console nur
`summary`.

## 4–5. Doppelter Header / doppeltes TOTAL RUNTIME

`research_db_cli.run_research_db_analysis`:

1. `_print_eligibility_banner(...)` nach Eligibility (~Input/Coverage-Zeit)
2. `_print_eligibility_banner(...)` am Ende (Gesamtlaufzeit)
3. `print_console_summary(...)` mit erneutem Header `BTC OB FIGHT FACT ANALYSIS`

Erste Zeit = Lade-/Eligibility-Zeit; zweite = Full-Analysis-Runtime.

## 6. Leeres LEVEL EPISODE SUMMARY

Lean `level_events` enthalten nur
`level_id/label/price/first_touch_ts/cross_count/episode_count`
ohne `episodes` / `first_complete_above_episode`. Console sucht TPO_VAH-Episoden
in diesem Stub → leer. Full-Daten liegen in `level_episodes.csv` / REPORT.

## 7. `heavy_detail_csv=False`

Nur Output-Schreiben (Wall-/Edge-Detail-CSVs). **Keine** Auswirkung auf
Fight-/Sequence-Berechnung.

## 8. Edge consumption 1 vs. 604

- `manifest.edge_consumption_count = 1` → Exact frozen-edge events
  (`fight_facts.edge_consumption_events`, Tick = eingefrorene Kante).
- `coverage_aware_consumption_metrics.total_events = 604` → Summe über Scopes
  `TPO_EDGE_BIN` (50) + `VOLUME_EDGE_BIN` (51) + `PROFILE_EDGE_ZONE` (418) +
  `FIRST_OUTSIDE_BIN` (84) + `EXACT_LEVEL_TICK` (1).  
Unterschiedliche Metriken, nicht vermischen.
