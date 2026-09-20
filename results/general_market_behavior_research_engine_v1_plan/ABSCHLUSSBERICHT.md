# ABSCHLUSSBERICHT — GENERAL_MARKET_BEHAVIOR_RESEARCH_ENGINE_V1 (PLAN/AUDIT ONLY)

**Verdict:** `GENERAL_MARKET_BEHAVIOR_RESEARCH_ENGINE_V1_PLAN_READY_WITH_GAPS`

**Audit time (approx):** 2026-09-06T10:15Z  
**Mode:** Plan/Audit only — no collector restart, no systemd changes, no CH writes/DDL, no commit/push, no implementation started.

---

## 1. Verdict
Plan is ready enough to design the engine and run a bounded BTC Full-OB COMPLETE-hour pilot later, **but** shared Full-OB calendar history is short, many hours are GAP, and research CH modalities lag live through 2026-08-31. Hence **READY_WITH_GAPS**, not blocked on replay (replay works for COMPLETE).

## 2. Branch / HEAD / Dirty
| Repo | Branch | HEAD | Dirty |
|------|--------|------|-------|
| spread_recovery_hedge_short_dev | `feature/btc-doge-research-db` | `9ab70ee` | Yes (pre-existing dashboard/research/results; **preserved**) |
| orderbook_analyse | `feature/strategy-lab-phase1` | `1019974` | Yes (raw archive/collector/FR/cache bridge; **preserved**) |

No branch switch, no clean, no commit.

## 3. Live-Sicherheit
No collector restart; Full-OB archive not touched; no systemd edits; CH read-only queries only; no dashboard/trading/forecast changes; no large full-history replay (one COMPLETE hour probe only).

## 4. Datenquellen
See `source_inventory.csv`. Highlights:
- Full-OB continuous RAW: `…/full_ob_v1/{BTC,DOGE}/` from 2026-09-05T17Z.
- Longer book history: OB200 FS Aug24–Sep6; CH OB200 snapshots only through Aug31.
- Trades/OI/Liq/Candles: live CH dense into Sep6 (with PT early-Sep6 gap).
- Research tables: generally through Aug31; `research_market_1s` pilot-only.

## 5. Gemeinsame Coverage
See `coverage_matrix.csv`.
- **Best multi-day joint (depth200):** ~2026-08-24 → 2026-08-31 (OB200 + trades + OI + liq + candles); **no** continuous Full-OB.
- **Best Full-OB FULL join candidates:** COMPLETE hours e.g. BTC `2026-09-05T17Z` with live trades/OI/liq (research CH lag → use live FQNs).
- **GAP:** many Full-OB hours; public trades `2026-09-06T00–06Z`.

## 6. Full-OB-Replayfähigkeit
**Yes for COMPLETE segments.** Probe BTC `20260905T170000Z`:
- `replay_segment` → `ok=True`, 0 errors, 7 checkpoints, 6102 applied deltas, hashes OK.
- Extra integrity walk: crossed_checks=0, neg_size_checks=0, final bid 80025.8 / ask 80025.9.
- GAP segments must not be labeled FULL; closed COMPLETE segments are independently replayable.

## 7. Zeit-/Join-Vertrag
Documented in `temporal_join_contract.md`: event-time UTC primary; 1s grid; OI forward-fill with max age; no future fill; trades/liq sum in bucket; book hold last ready state; epoch reset on resync.

## 8. Empfohlene Basisauflösung
**1 second continuous state** + on-demand event/100ms overlays for episodes. Balances microstructure vs 6-core cost; matches existing research 1s patterns. Not default level-per-row CH.

## 9. Feature-Gruppen
Book Shape, Book Flow (+ attribution), Footprint, Price Response, OI/Liq, Regime, Data Quality — `feature_catalog.md`.

## 10. Messbar versus Proxy
- **Exact/measurable:** mid/spread/depth bands, raw Δ liquidity, taker volumes, liq events, returns, vol regimes, coverage flags.
- **Proxy/heuristic:** buyer/seller “control”, absorption, OI long/short intent, cancel vs execute (except EXECUTED_LIKELY with trade overlap), squeeze narratives.

## 11. Marktverhaltenszustände
Pattern library P01–P18 in `behavior_pattern_catalog.md`; UNCLEAR/WAIT required.

## 12. Outcomes und Horizonte
5s–30m; return/MFE/MAE/vol/direction/path/time-to-threshold — `outcome_contract.md`. Neutral from train-only noise/spread/fees.

## 13. Leakage-Schutz
`leakage_prevention_contract.md`: causal cut, chrono splits, purge/embargo, no shuffle, overlap discount for DOGE, prefix parity.

## 14. Musterfindung
Stufe1 descriptive → Stufe2 rules/trees → Stufe3 simple baselines only after N allows. No DL in V1.

## 15. Vorhersagevalidierung
Vs class base rates, balanced acc, P/R, Brier/calibration, return-by-prob, day/regime stability, coverage of clear signals, MFE/MAE, fee-aware only if trade layer added. Pattern ≠ profit.

## 16. Wiederverwendete Module
OA FullBookState + continuous replay; OB Fight coverage/walls/consumption/refill; research source readers; trade buckets; liquidation contract; market_aggregation as template — `existing_module_reuse_matrix.csv`.

## 17. Neue Module
`research/general_market_behavior_v1/*` + CLI modes; Track F vs Track D200 labeling; general price-response & episode layer beyond fight edges.

## 18. Speicher-/Rechenbedarf
Aggregates-only ~0.1–0.5 GB/symbol/day derived; Full-OB worker 2–4 GB RAM; avoid full level arrays in CH — `resource_estimate.md`.

## 19. Pilotplan
P1 BTC COMPLETE hour 2026-09-05T17Z → join live modalities → 1s states → spot checks → GAP refusal → then OB200 multi-day — `pilot_validation_plan.md`.

## 20. Offene Risiken
Short Full-OB history; GAP-heavy manifests; research lag; PT Sep6 gap; attribution timing; false confidence from proxies; small N for 30m prediction; collector stop history — `open_questions.md`.

## 21. Kleinster nächster Implementierungsschritt
**Offline read-only builder for one BTC COMPLETE hour:** coverage gate → replay → 1s aggregate state Parquet + quality flags + 5 feature asserts + report. No CH DDL, no collector touch.

## 22. Dateien und Result-Pfade
`results/general_market_behavior_research_engine_v1_plan/` — all required artifacts listed below.

## 23. Bestätigung
Kein Live-Eingriff, kein CH-Write, kein Commit/Push in diesem Auftrag.

---

## Pflichtfragen (14)

1. **Deterministic Full-OB replay?** Yes for COMPLETE (probe ok).  
2. **Shared coverage?** Aug24–31 depth200+modalities; Full-OB only from Sep5 17Z with many GAP hours; research CH lags.  
3. **Canonical price?** Mid from reconstructed book (Track F or D200); candles secondary; avoid ticker_samples. Trades last price optional cross-check.  
4. **Base resolution?** 1s (+ event/100ms overlays).  
5. **Forward-fill?** OI (capped age); book last ready within epoch; **not** trades/liq volumes; **not** across gaps.  
6. **Exact vs proxy?** See §10.  
7. **Consumption vs cancel?** Not reliably exact; use EXECUTED_LIKELY / CANCEL_LIKELY / MIXED_OR_UNKNOWN.  
8. **Price response?** return_bps / aggressive quote vol; / EXECUTED_LIKELY; / removed liquidity; quadrant analysis — causal windows vs separate outcome windows.  
9. **Overlap/leakage?** Chrono splits, embargo ≥ horizon, min spacing, prefix parity.  
10. **Realistic states?** Vacuums, wall pull/consume/refill, aggression-without-progress, liq clusters, regimes — strong; “true control”/OI intent — proxy only.  
11. **Horizons?** 5s–30m as listed; longer needs more calendar data.  
12. **Data volume?** Exploration: days–2 weeks OB200 + few Full-OB COMPLETE hours. Validation for predictive claims: multiple weeks Full or careful D200 with embargo — **currently thin for strong forecast claims**.  
13. **Reuse OB Fight?** Coverage gates, wall tracks, consumption/refill/nearby, price_response_facts patterns — adapt off fight-edge-only framing.  
14. **Derived tables later?** state_1s, book_flow_1s, episodes, outcomes, predictions — `proposed_schema.sql` design only.  
15. **Resources?** See `resource_estimate.md`.  
16. **Smallest next step?** §21.  
17. **Unproven?** GAP semantics frequency, attribution ms, OB200≈Full near touch, research rematerialization schedule.  
18. **False pattern risks?** Labeling GAP as FULL; cancel/execute confusion; OI stories; overlapping samples; short Full-OB N; future leakage via rematerialization.

---

## Artifact checklist
- ABSCHLUSSBERICHT.md (this file)
- source_inventory.csv
- coverage_matrix.csv
- existing_module_reuse_matrix.csv
- semantic_contract.md
- temporal_join_contract.md
- feature_catalog.md
- behavior_pattern_catalog.md
- outcome_contract.md
- leakage_prevention_contract.md
- proposed_architecture.md
- proposed_schema.sql
- implementation_phases.md
- pilot_validation_plan.md
- resource_estimate.md
- open_questions.md
