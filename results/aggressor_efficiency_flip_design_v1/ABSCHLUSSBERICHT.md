# ABSCHLUSSBERICHT — AGGRESSOR_EFFICIENCY_FLIP_DISCOVERY_V1

**Design-ID:** aggressor_efficiency_flip_design_v1  
**Datum (UTC):** 2026-08-29  
**Basis:** `results/aggressor_efficiency_data_audit_v1` (DATA_READY_WITH_LIMITATIONS)  
**Scope:** Read-only Design- & Feasibility-Analyse — keine Implementierung, keine Schwellen, kein PnL.

---

## 1. Verdict

**B. AGGRESSOR_EFFICIENCY_FLIP_DESIGN_READY_WITH_OPEN_CHOICES**

Kausale Event-Semantik, Wiederverwendung bestehender Impact-/Episode-Muster, Discovery-Schema, Test- und Chunking-Plan sind eindeutig beschreibbar. Vor Code sind wenige **explizit benannte** methodische Defaults zu bestätigen (unten §20); sie blockieren das Design nicht.

---

## 2. Executive Summary

- Bestehende Bausteine liefern **Side-Mapping**, **impact_per_notional / first–last compression**, **Episode-FSM**, **kausale 1s Trade-Bubbles** und **Acceptance/Reclaim-Vokabular** — aber **keinen** fertigen Trade-only Efficiency-Flip.
- V1 soll **separat** auf `public_trades_canonical` + Trade-Preis laufen; OB/Pools/OI nur Flags/Labels.
- **Flow `[t0,t1)`** und **Impact `(t1,t2]`** strikt trennen (Variante B).
- Flip = zeitlich getrennte **Compression → Gegen-Initiative**, dann **Structure + Acceptance**; erst dann Candidate.
- Flip-Score: **kategoriale Gates + ordinaler Score**, keine fragile Effektivitäts-Division.
- DOGE 11:55 / 12:20 nur Semantikbeispiel — **keine** Schwellenbasis.

---

## 3. Live-Sicherheitsbestätigung

Keine Prozesse gestartet/gestoppt/neu gestartet. Keine CH-Writes/DDL. Keine bestehenden Dateien geändert. Kein Git-Commit. Keine Dashboard/API/Job/Lock/Manifest-Änderungen.

Beobachteter Ist-Zustand (nur Liste): `clickhouse-server`, Live-Collector, OI/Liq-Collector, Dashboard `app.py` liefen bereits.

Neue Artefakte ausschließlich unter `results/aggressor_efficiency_flip_design_v1/`.

---

## 4. Bestehende relevante Module und Artefakte

Siehe `existing_component_inventory.csv`. Kernfunde:

| Bereich | Wichtigste Funde |
|---|---|
| Impact compression | `oi_liq_impact_l2` (1m + event_chain); `ob200_v3.../pipeline.slice_impact` + `classify_compression`; `public_trade_audit.classify` |
| Aggressor notional | `contracts.AGGRESSIVE_NOTIONAL_COLUMN_BY_DIRECTION`; trade_bubbles buy/sell |
| Trade load/aggregate | `dashboard/.../trade_bubbles.py`, `public_trades_profile.py` |
| Absorption (anders) | regime_scanner orderflow_absorption*; execution_wall_detector (L2) |
| Structure/accept | break/reclaim audits; Market_Tools.md Vision |
| Data constraints | prior aggressor_efficiency_data_audit_v1 |

---

## 5. Wiederverwendbar versus neu erforderlich

**Direkt wiederverwendbar**

- Direction↔aggressor mapping from `oi_liq_impact_l2.contracts`
- Causal 1s aggregation / `known_at` pattern from `trade_bubbles.aggregate`
- CH read-only access patterns from `public_trades_profile` / bubbles loader
- `safe_div`, bps-per-1M-USDT scaling idea from v3 pipeline
- Episode timeout/abort vocabulary from `event_chain.py`

**Nur konzeptionell**

- first5/last5 compression *inside* a post-touch window (wall-anchored) → AEF uses post-flow windows without wall
- 1m `impact_compression_observed` → too coarse alone
- Candle orderflow absorption patterns → different grain
- Acceptance/reclaim audits → structure stage inspiration

**Ungeeignet als V1-Pflicht**

- execution_wall / L2 wall absorption / OB200 wall chains (OB missing on DOGE ref day)
- Any definition that requires mid/micro for the core gate

**Neu erforderlich**

- Trade-only burst detector (5s + merge)
- Post-flow impact efficiency + compression scores with robust norms
- Counter-side flip linker + ordinal flip score
- Causal micro-high/low + acceptance without future pivots
- Discovery writer + prefix-parity tests for AEF contract

---

## 6. Exakte Long-/Short-Event-Semantik

**LONG (vollständig)**

1. Sell-dominant aggressor burst (closed flow).  
2. Post-flow impact shows little downside → Sell impact compression.  
3. Later Buy-dominant burst with stronger upside impact → Buy efficiency.  
4. Break of frozen local micro-high / range high.  
5. Acceptance above level.  
6. Candidate; entry ≥ next closed 1s after `final_decision_ts`.

**SHORT:** mirror (Buy compression → Sell efficiency → micro-low break → accept below).

**Nicht Entry:** Volumen allein, Compression allein, Flip allein.

---

## 7. Vorgeschlagener Zustandsautomat

Dokument: `causal_state_machine.md`.

Zustände: `NEUTRAL → AGGRESSOR_BURST → IMPACT_COMPRESSION → COUNTER_SIDE_WATCH → EFFICIENCY_FLIP → STRUCTURE_CONFIRM_PENDING → ACCEPTANCE_PENDING → CANDIDATE_CONFIRMED`, plus `INVALIDATED` / `TIMEOUT`.

Decision timestamps = frühester Zeitpunkt, zu dem **alle** genutzten Fenster geschlossen sind.

---

## 8. Flow- und Impact-Fenster

Dokument: `feature_window_contract.md`.

**Empfehlung: Variante B (post-flow impact)** mit festem `H` (Implementierung wie D).  
Variante A nur diagnostisch. 15m-Mischung verboten.

---

## 9. Impact-Effizienz-Definition

- `sell_directional_impact_bps = max(0, -raw_bps)` nach Sell-Flow  
- `buy_directional_impact_bps = max(0, +raw_bps)` nach Buy-Flow  
- Nenner: Notional / rolling median (Floor); Score via MAD/Perzentil  
- Compression = niedrige adverse Effizienz bei hohem Notional-Score  
- Keine NaN/Inf: Floors, Winsorize, categorical abort bei invalid

---

## 10. Efficiency-Flip-Definition

Compression-Episode und Flip-Episode **zeitlich getrennt** (`flip_start ≥ compression_decision_ts`), Delay-Band.

**V1 Flip-Score:** zweistufige kategoriale Gates + ordinaler Punkt-Score (Rank-/Score-Differenz), **kein** `eff_buy/eff_sell`.

---

## 11. Trade-Preis-Limitationen

- V1 Preis: bucket last/first trade; VWAP side für trapped proxy  
- Mindesthorizont Gates: ≥5s+5s  
- Ohne Mid keine Micro/OFI/Wall-Fate-Claims  
- Später Paritätsaudit an OB1s-Tagen  

---

## 12. Trapped-Aggressor-VWAP

Kausal aus Side-Trades im Compression-Flow berechenbar.  
**V1:** Analyse-/Confirm-Label, **nicht** Pflichtgate für Flip.

---

## 13. Strukturbruch und Acceptance

Kausale frozen levels at `flip_decision_ts`; Break/Acceptance nur mit geschlossenen Buckets; Pivot-Timestamp = Confirm-Zeit.  
Kurze Wicks ohne Hold ≠ Acceptance.

---

## 14. OI-Klassifikation

5s/5m closed; vier Quadranten + MIXED/FLAT/MISSING; **kein** Entry-Gate; Missing behält Episode.

---

## 15. Vorgeschlagenes Event-Schema

Siehe `proposed_event_schema.csv`.

- PK/Dedupe: `(symbol, direction, compression_decision_ts, flip_decision_ts, feature_version)`  
- Versionierung: `feature_version`, `causal_contract_version`  
- Cooldown + merge rules in state machine doc  

---

## 16. Outcome-Plan

Siehe `planned_outcomes.csv`.  
Alle Outcomes **nach** `earliest_entry_ts`; Feature/Outcome strikt getrennt; keine TP/SL-Strategie.

---

## 17. Discovery-/OOS-Plan

Siehe `validation_matrix.csv` (F0–F5).

Anti-Leak:

- F0 Zeiten nicht für Cuts  
- Day lists frozen before run  
- Per-symbol reporting vs BTC dominance  
- F4 time holdout  
- F5 nested ablation only  

---

## 18. Kausalitätstests

Später verpflichtend:

Prefix-Parität; No-Future-Data; Bucket-Close; Feature/Outcome-Separation; Buy/Sell-Spiegel; Timestamp-Tie (`trade_id`); Duplicate-trade_id; Empty-second; zero-notional; NaN/Inf; Chunk-/Day-Boundary; Episode-Dedupe; Determinismus; Symbol-Norm; OI-Missing; OB-Missing (episodes still emitted).

---

## 19. Performance- und Chunking-Plan

- CH read-only, `symbol` + day/`trade_ts` pruning  
- Hybrid: CH fetch day chunk → local sort `(trade_ts, trade_id)` → aggregate → checkpoint JSONL/CSV small  
- No raw full dumps; row/byte limits; resume by symbol-day  
- No new persistent CH agg table unless F3 proves CPU-bound  

---

## 20. Offene Entscheidungen

Vor Implementierung **bestätigen** (Defaults unten bereits empfohlen):

| # | Choice | Empfohlener Default |
|---|---|---|
| 1 | Impact window A/B/C/D | **B / fixed-H (D)** |
| 2 | Primary burst grain | **5s (+ adjacent merge)** |
| 3 | Entry clock | **D1 next closed 1s** after final_decision |
| 4 | Flip score | **categorical + ordinal** (no eff ratio) |
| 5 | Acceptance grain | **N×1s hold**, 1m close as optional confirm |
| 6 | Trapped VWAP | **label only in V1** |
| 7 | Compression intra-burst first/last5 | optional diagnostic; **not** required if B impact used |

---

## 21. Exakte Empfehlung für den nächsten Implementierungsschritt

1. Neues **separates** Research-Paket (z. B. unter `orderbook_analyse` oder `research/`), **ohne** bestehende Strategien/YAML zu ändern.  
2. Implementiere nur **F0 scaffold**: day-chunk loader → 1s aggregates → 5s bursts → post-flow impact scores → episode FSM stubs → write schema rows + prefix-parity unit tests.  
3. Keine Schwellenoptimierung; Platzhalter-Gates als config constants marked `UNFITTED`.  
4. Kein Dashboard, kein Backtester, keine CH-Tabelle.  
5. Nach grünem Prefix-Paritätstest: F1 day list freeze, dann erst Zählen von Episode-Raten (deskriptiv).

---

## Interpretationsgrenze

Dieser Bericht behauptet **nicht**, dass der DOGE-Long profitabel war oder ein „Game Changer“ vorliegt. Er definiert nur, wie die Hypothese später **kausal und separat** prüfbar wäre.
