# ABSCHLUSSBERICHT — AGGRESSOR_EFFICIENCY_FLIP_DISCOVERY_V1 F0

## 1. Verdict

**B. AGGRESSOR_EFFICIENCY_FLIP_F0_PASSED_WITH_DATA_LIMITATIONS**

Dual-Impact, `strong_same_side_impact_veto`, Long/Short-Spiegelung, Prefix-Parität, Feature-/Outcome-Trennung und DOGE-F0-Lauf sind technisch grün. Einschränkung: Preiswirkung ausschließlich über Public-Trade-Preis (kein Mid/BBO/OB in diesem Fenster).

## 2. Executive Summary

Separates Research-Paket `orderbook_analyse.aggressor_efficiency_flip` implementiert das bestätigte Drei-Fenster-Modell (Flow+contemporaneous → Post-Flow → Counter-Search). F0 läuft nur unter Profil `unfitted_f0_diagnostic` und erzeugt ausschließlich `DIAGNOSTIC_CANDIDATE` (keine Trades, keine Edge-/Profit-Claims).

DOGEUSDT `2026-08-29T08:00:00Z`–`15:30:00Z` (Ende exklusiv): 41 523 Trades → 5 diagnostische Kandidaten; Prefix-Parität OK; bekannter Chartbereich ~11:55/12:20 wird **nicht** zum Kandidaten — stoppt kausal an Counter-Timeout bzw. Structure-Break-Timeout (Schwellen **nicht** nachgezogen).

## 3. Live-Sicherheit

- Nur neue Research-Module / Tests / CLI / Ergebnisordner
- ClickHouse: SELECT-only (`public_trades_canonical`, optional `open_interest_5s`)
- Keine Dashboard-/API-/Backtester-/Scanner-/Worker-Änderung
- Keine CH-Writes/DDL, keine Collector-/Prozess-/Lock-Eingriffe, kein Git-Commit
- Bestehender Dirty Tree unberührt gelassen

## 4. Branch / HEAD / Dirty Tree

| Repo | Branch | HEAD |
|------|--------|------|
| `orderbook_analyse` (Implementierung) | `feature/strategy-lab-phase1` | `3f2f18f7720b5bdd802543e80e3291dd9d3aaa0f` |
| `spread_recovery_hedge_short_dev` (Ergebnisse) | `feature/dashboard-research-charts` | `2a3c379e2fd5c0d7fe8b6999468092de962d7ef9` |

Beide Repos hatten vorab Dirty Trees; nur AEF-F0-Artefakte hinzugefügt/geändert.

## 5. Geänderte und neue Dateien

**Neu (orderbook_analyse):**

- `src/orderbook_analyse/aggressor_efficiency_flip/` — Paket (`contracts`, `buckets`, `impact`, `compression`, `initiative`, `structure`, `acceptance`, `episodes`, `outcomes`, `integrity`, `runner`, `cli`, …)
- `scripts/run_aggressor_efficiency_flip_discovery.py`
- `tests/test_aggressor_efficiency_flip_f0.py`

**Neu (Ergebnisse):**

- `results/aggressor_efficiency_flip_f0_doge_20260829/*` (dieser Ordner)

## 6. Wiederverwendete bestehende Komponenten

- Side-Semantik analog `oi_liq_impact_l2`: LONG-Compression = Sell-Aggressor, SHORT = Buy
- ClickHouse-Client: `orderbook_analyse.orderbook_v2.ch_client`
- Research-CLI-/Ergebnisordner-Muster wie andere Discovery-Scripts
- Keine ungeprüfte Fremdfunktion mit abweichender Fenstersemantik übernommen

## 7. Dual-Impact-Implementierung

1. Flow `[t0,t1)` (5s) + contemporaneous Impact im selben Fenster  
2. Post-Flow `[t1,t2)` (5s) — Compression erst nach `t2`  
3. Counter-Search `[t2,t3)` (max 180s) — eigenes 5s+5s Dual-Impact für Gegenseite  

Hard-Veto: **`strong_same_side_impact_veto`** (nicht `adverse_impact_veto`).  
Post-Flow-only bestätigt **nie** Compression.

## 8. Bucket-/Preisvertrag

- Intervalle `[start,end)`; Trade → genau ein 1s-Bucket  
- Sortierung `(trade_ts, trade_id)` nur für stabile Verarbeitung; **keine** Mikrostrukturbehauptung aus Tie-Reihenfolge  
- High/Low/Notional auf Timestamp-Gruppen; leere Sekunden: Coverage-Flags / Carry-Forward nur kausal rückwärts  
- Endfenster unvollständig → kein vorzeitiges Event (`as_of` / Horizon)

## 9. Compression- und Initiative-Semantik

Compression (LONG): Sell-dominant, schwacher contemporaneous Down, kein starkes Post-Down-Follow-through, kein Same-Side-Veto.  
Initiative: späterer Buy-Burst mit eigenem Dual-Impact. SHORT gespiegelt.  
Scores ordinal/rank-basiert, versioniert, NaN/Inf-sicher.

## 10. State Machine

Zustände u. a.: `NEUTRAL` → `AGGRESSOR_BURST` → `IMPACT_COMPRESSION_PENDING` → `IMPACT_COMPRESSION_CONFIRMED` → `COUNTER_SIDE_WATCH` → `COUNTER_INITIATIVE_PENDING` → `EFFICIENCY_FLIP` → `STRUCTURE_CONFIRM_PENDING` → `ACCEPTANCE_PENDING` → `DIAGNOSTIC_CANDIDATE` | `INVALIDATED` | `TIMEOUT`.

Jeder Wechsel: event/decision timestamp, reason, closed windows, DQ.

## 11. UNFITTED-Profil und diagnostische Parameter

Profil: `unfitted_f0_diagnostic` (siehe `resolved_config.json`).

| Parameter | Wert |
|-----------|------|
| flow / post / counter_search | 5s / 5s / 180s |
| min_dominant_share | 0.60 |
| min_notional_usdt | 10 000 |
| min_notional_rank | 0.70 |
| strong_same_side_impact_bps | 8.0 |
| weak_contemporaneous_max_bps | 3.0 |
| strong_post_followthrough_bps | 8.0 |
| counter_min_notional_usdt | 10 000 |
| counter_min_directional_impact_bps | 3.0 |
| structure_lookback_seconds | 60 |
| acceptance_hold_seconds | 5 |
| cooldown_seconds | 60 |
| require_structure / acceptance | true / true |
| status_label | DIAGNOSTIC_CANDIDATE |
| unfitted | true |

Keine Schwellenfitung auf DOGE-Outcome oder 11:55/12:20.

## 12. Prefix-Parität

`prefix_parity.json`: **ok=true**, 0 Errors, 32 Cutoffs (Flow/Post/Counter/Structure/Acceptance/Final-Stufen aus ersten Episoden + Fraktions-Grid). Spätere Daten ändern bereits entschiedene Features/Events nicht.

## 13. Testresultate

```
PYTHONPATH=src .venv/bin/python -m pytest tests/test_aggressor_efficiency_flip_f0.py -q
...............  15 passed
```

Abgedeckt u. a.: Side-Mapping, Buckets, gleiche ms, Dedup, Dual-Impact A–D + Spiegel, Counter nach t2, Timeout, Structure/Acceptance/Entry-Kausalität, Prefix-Parität, OI/OB missing, unvollständiges Endfenster. Details: `test_summary.txt`.

## 14. DOGE-Datenpreflight

Quelle: `orderbook_analysis.public_trades_canonical`

| Check | Ergebnis |
|-------|----------|
| rows / uniq trade_id | 41 523 / 41 523 |
| duplicate_surplus | 0 |
| bad_side | 0 |
| sides | Buy, Sell |
| min/max ts | 08:00:00.256 … 15:29:55.118 UTC |
| OI 5s labels geladen | 5400 |

Fail-closed bei Side-/Duplikatproblemen; Fenster vollständig für Trades.

## 15. F0-Funnel

| Stufe | n |
|-------|---|
| 1s-Buckets (Fenster) | 5010 |
| Raw Bursts (Long+Short) | 5926 |
| Compression evaluiert | 5707 |
| Compression zulässig | 141 |
| Same-Side-Veto | 9 |
| Delayed-Continuation-Veto | 1 |
| Counter-Events geprüft | 2614 |
| Counter bestätigt / Efficiency Flip | 24 |
| Structure-Confirm pending | 24 |
| Acceptance pending | 15 |
| Diagnostic Candidates | **5** |
| Timeout (terminal) | 134 |
| Invalidated | 10 |

## 16. Beispiel-Timelines (kausal)

**A) LONG Candidate `94647f626a46e3e7420e6876`**

- 10:06:15–20 Sell-Flow → 10:06:25 Compression confirmed  
- 10:06:55–10:07:05 Buy-Initiative → Efficiency Flip  
- 10:07:16 Structure Break → 10:07:24 Acceptance → Candidate  
- `final_decision_ts` 10:07:24Z; `diagnostic_earliest_entry_ts` 10:07:25Z (> final)

**B) Visuelles DOGE-Fenster ~11:55 / 12:20 (nicht nachgezogen)**

- LONG Compression `11:55:55` → confirmed `11:56:05` → **TIMEOUT** `11:59:05` `no_counter_within_search`  
- LONG `12:05:45` → ebenfalls Counter-Timeout  
- SHORT `12:19:05` → Flip `12:20:25` → **TIMEOUT** `12:22:15` `no_structure_break`  
- Mehrere LONG-Compressions ~12:20 → Counter-Timeouts  

→ Messmaschine validiert; Chartbild wird nicht erzwungen.

## 17. OI-/OB-/BBO-Verfügbarkeit

| Quelle | Status |
|--------|--------|
| Public trades | verfügbar (Primärpreis) |
| OI 5s | verfügbar; Labels an Kandidaten (5s-Floor, keine Zukunft) |
| OB / BBO / Mid / Micro / Pools | **nicht** verfügbar → Flags false; Events bleiben |

## 18. Diagnostic Outcomes (ohne Profit-Claim)

5 Outcome-Zeilen nach `diagnostic_earliest_entry_ts`; Horizonte 30s…60m; MFE/MAE; unvollständige Horizonte = leer/None.  
Kein TP/SL-PnL, keine Gebühren, keine Optimierung. Werte sind deskriptiv, keine Edge-Behauptung.

## 19. Einschränkungen

- Impact über Trade-Last/High/Low, nicht Mid/BBO  
- UNFITTED-Schwellen sind diagnostisch, nicht kalibriert  
- Structure/Acceptance Research-V1 (lokal, kausal), keine Exchange-Struktur  
- `trade_id`-Tie-Sort nur Verarbeitungsstabilität  
- Viele Compressions → wenige Candidates (Counter/Structure/Acceptance-Filter streng)

## 20. Offene Fragen

- Rank-Lookback vs. absolute Notional-Gates für F1-Universe  
- Ob Counter-Suche 3m / Acceptance-Hold an andere Symbole transferieren  
- OB1s/BBO-Anbindung sobald verfügbar (Impact-Qualität)  
- Episode-Merging feiner für eng gestaffelte Bursts

## 21. Empfehlung für F1

1. F0-Messmaschine unverändert als Contract-Basis behalten  
2. Multi-Symbol-Smoke **ohne** Schwellenfit auf Einzelcharts  
3. Optional BBO/Mid parallel als Impact-Quelle A/B (nicht still ersetzen)  
4. Ordinal-Score-Verteilungen und Gate-Funnel berichten; erst danach bewusste Kalibrierung außerhalb F0  
5. Kein Live-/Scanner-Anschluss bis Prefix-Parität und Dual-Impact auf ≥N Symbolen grün
