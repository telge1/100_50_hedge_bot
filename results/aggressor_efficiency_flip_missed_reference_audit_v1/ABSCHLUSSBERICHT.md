# ABSCHLUSSBERICHT — AGGRESSOR_EFFICIENCY_FLIP Missed-Reference Audit V1

**Scope:** READ-ONLY. Keine Implementierungs-, Schwellen- oder Live-Änderungen.  
**Referenz:** DOGEUSDT 2026-08-29, F0-Fenster 08:00–15:30Z; Fokus 11:30–12:40Z.  
**Basis:** F0-Artefakte + SELECT auf `public_trades_canonical` + unveränderte Contract-Funktionen (nur Aufruf).

---

## 1. Verdict

**H. MISSED_REFERENCE_AUDIT_COMBINED_REVISION_REQUIRED**

Der verfehlte visuelle Long ist **kein** Prefix-/Integritätsfehler und **kein** reines Bucket-Artefakt. Er entsteht materiell aus der Kombination von:

1. festem **3m-Counter-Timeout**, das ~22 min vor der ersten kausal bestätigbaren Buy-Initiative endet, und  
2. fehlendem Modell einer **länger lebenden Sell-Absorptions-Phase** (mehrere Compressions 11:30–12:25),  

während ein **blindes** Verlängern des Counter-Fensters auf 15–30 min im gesamten F0-Fenster unverbundene Phasen koppelt (Attach-Rate 13 % → 45–48 %, Mehrfachzuordnung derselben Initiative steigt).

---

## 2. Executive Summary

- LONG `11:55:55Z`: Sell-Compression **korrekt** (Sell≈212.7k USDT, contemporaneous/post Down ≈0, Case A).  
- Counter-Suche `[11:56:05, 11:59:05)`: 13 Buy-Versuche, **kein** bestätigtes Initiative-Event; max Buy-Notional ≈2.7k ≪ 10k. Timeout `no_counter_within_search` ist unter dem F0-Contract **korrekt**.  
- Erste F0-konforme Buy-Initiative im Aufwärtscluster: `12:18:00` → confirmed `12:18:10` (Buy≈11.7k, +4.7 bps). Abstand zu Compression-Confirm: **1325 s (~22.1 min)**.  
- Stärkster Buy-Impuls: `12:20:00` → confirmed `12:20:10` (Buy≈1.03 M USDT, +29.5 bps) — vom Scaffold als Initiative **erkannt**, aber nicht an die 11:55-Episode gebunden.  
- SHORT `12:19:05`: Buy-Compression + Sell-Initiative `12:20:15` → Flip → Timeout `no_structure_break` — lokal **semantisch konsistent**, aber Kurzimpuls im größeren bullishen Kontrollwechsel; **kein** Long/Short-Spiegelungsbug.  
- 5s-Offset 0–4: Compression und Buy-Impuls bleiben sichtbar → **kein** materielles Boundary-Artefakt als Primärursache.  
- Fünf bestehende Diagnostic Candidates: semantisch plausibel; sie unterscheiden sich vom Missed-Long durch erfolgreiche Counter+Structure+Acceptance **innerhalb** der F0-Fenster.

---

## 3. Live-Sicherheit

- Keine Code-/Schwellenänderung am AEF-Paket  
- Keine Dashboard-/API-/Backtester-/Scanner-Änderung  
- ClickHouse nur SELECT; keine Writes/DDL  
- Keine Prozess-Eingriffe, kein Git-Commit  
- Neue Artefakte nur unter `results/aggressor_efficiency_flip_missed_reference_audit_v1/`

---

## 4. Branch / HEAD / Dirty Tree

| Repo | Branch | HEAD |
|------|--------|------|
| `orderbook_analyse` | `feature/strategy-lab-phase1` | `3f2f18f7720b5bdd802543e80e3291dd9d3aaa0f` |
| `spread_recovery_hedge_short_dev` | `feature/dashboard-research-charts` | `2a3c379e2fd5c0d7fe8b6999468092de962d7ef9` |

Dirty Trees unberührt; nur neuer Ergebnisordner.

---

## 5. Unveränderte F0-Parameter

Aus `resolved_config.json` (keine Änderung):

| Parameter | Wert |
|-----------|------|
| profile | `unfitted_f0_diagnostic` |
| flow / post / counter_search | 5s / 5s / **180s** |
| min_notional_usdt / counter_min | 10 000 / 10 000 |
| weak_contemporaneous_max_bps | 3.0 |
| counter_min_directional_impact_bps | 3.0 |
| strong_same_side_impact_bps | 8.0 |
| require_structure / acceptance | true / true |

---

## 6. Vollständige Referenztimeline

Fenster: `2026-08-29T11:30:00Z`–`12:40:00Z` (intern nur geschlossene Buckets).

| Datei | Inhalt |
|-------|--------|
| `reference_1s_timeline.csv` | 4200 Zeilen: 1s Notional/Side/Preis/FSM-Annotation |
| `reference_5s_timeline.csv` | 403 Zeilen: Dual-Impact, Compression-/Initiative-Klassifikation Offset 0 |

Keine Raw-Trades exportiert.

---

## 7. Long-Episode 11:55

Quelle: `long_1155_episode.csv` + F0 `compression_events` / `counter_initiative_events` / `state_transitions`.

| Feld | Wert |
|------|------|
| episode_id | `c61994f3385529a07bc120ef` |
| compression_start / end | `11:55:55` / `11:56:00` |
| compression_confirmed_ts | `11:56:05` |
| sell_notional / buy_notional | ≈212 674 / ≈0 |
| dominant_share | 1.0 |
| contemporaneous Down / same-side | 0 / 0 |
| post same-side follow-through | 0 |
| semantic_case | `A_possible_absorption` |
| notional_rank / ordinal score | 1.0 / 2.33 |
| aggressor_vwap | 0.08445 (alle Trades gleicher Preis) |
| counter_search | `[11:56:05, 11:59:05)` |
| counter attempts | 13 |
| confirmed initiatives | **0** |
| max buy notional in search | ≈2719.6 |
| reject reasons | `not_counter_dominant` (7), `below_counter_min` (6) |
| timeout | `11:59:05` `no_counter_within_search` |
| Preis während Suche | 0.08445 → 0.08442 (−3.6 bps) |
| Preis bis 12:25 | → 0.08470 (+29.6 bps vs Compression-Ende) |
| VWAP-Reclaim in 3m-Suche | nein |
| Hard-Invalidierung vor Timeout | nein (Timeout, nicht INVALIDATED) |
| neues Tief bis 12:25 | lokal 0.08440 (leicht unter Compression-Ende) |

**Antworten Auftrag 2:**

1. Ja — die 3m-Suche endet **vor** der relevanten Käuferinitiative (`12:18`/`12:20`).  
2. Innerhalb 3m gab es Buy-Bursts, aber nur an Gates `below_counter_min` / `WRONG_SIDE` — **kein** Near-Miss an Impact-Gates mit großem Notional.  
3. 11:55 ist Teil einer Serie wiederholter Sell-Compressions (siehe §10), nicht isoliert im Tageskontext.  
4. Die Hypothese wurde **nicht** hard-invalidiert; sie **lief ab**.  
5. Leichtes neues Tief (~0.08440), keine nachhaltige Down-Expansion.  
6. Aggressor-VWAP in der 3m-Suche nicht nachhaltig zurückerobert.

---

## 8. Short-Episode 12:19

Quelle: `short_1219_episode.csv`.

| Feld | Wert |
|------|------|
| direction | SHORT (Buy-Compression) |
| compression | `12:19:05`–`12:19:10`, confirm `12:19:15` |
| buy_notional | ≈19 580 (Sell 0) |
| contemporaneous up / same-side | ≈2.36 / ≈1.18 bps |
| post | 0 |
| Warum „ineffizient“ | schwaches Up trotz Buy-Dominanz → Contract: Buy-Compression |
| Sell-Initiative | `12:20:15`–confirm `12:20:25`, Sell≈17 941, impact≈5.89 bps |
| structure_level | 0.08469 |
| structure break | **nein** |
| timeout | `12:22:15` `no_structure_break` |
| Preis +10m nach Flip | ≈0.08472 (flach/leicht) |

**Antworten Auftrag 3:**

- Short-Flip **semantisch korrekt**, aber **unbestätigt** (kein bearish Structure Break) — Timeout gerechtfertigt.  
- Ja: kurzfristiger Gegenimpuls innerhalb eines größeren bullishen Kontrollwechsels (Mega-Buy `12:20:00`).  
- Sell-Counter (~18k) ist klein gegen den Buy-Impuls (~1.03 M) derselben Minute — Übergewichtung lokal im SHORT-Pfad, aber Contract spegelt Compression→Counter, nicht Global-Context.  
- Bucket-Grenzen trennen Buy-Flow nicht materiell (Offset-Sensitivität §12).  
- **Keine** Long/Short-Spiegelungsabweichung: dieselbe `12:19:05`-Buy-Window ist als LONG-Initiative wegen `first→last` Up≈1.18 bps `< 3.0` abgelehnt (`no_contemporaneous_impact`), als SHORT-Compression erlaubt — konsistent mit getrennten Gates (weak-max vs min-impact), nicht Spiegelbruch.  
- Trade-Price: Short-Compression **possibly_bounce_influenced** (Impact ~1 Tick); Sell-Counter und Mega-Buy dagegen klar gerichtet.

---

## 9. Tatsächliche Buy-Initiative

Quelle: `buy_initiatives_1200_1230.csv` (nur Merkmale bis Decision Timestamp).

| Rang | flow_start | confirmed_ts | buy_notional | up_bps | Status |
|------|------------|--------------|--------------|--------|--------|
| 1 | `12:20:00` | `12:20:10` | **1 027 171** | **30.70** | `BUY_INITIATIVE_CONFIRMED` |
| 2 | `12:18:00` | `12:18:10` | 11 712 | 4.73 | `BUY_INITIATIVE_CONFIRMED` |

Weitere Buy-Fenster ≥10k ohne Confirm: u. a. `12:19:05`, `12:15:30`, `12:21:30` → `no_contemporaneous_impact`.

**Frühester kausal erkennbarer Beginn** der Aufwärtsinitiative (F0-Gates): **`12:18:00`**, bekannt ab **`12:18:10`**.  
Visuell dominanter Impuls: **`12:20:00`**, bekannt ab **`12:20:10`**.

**Verbindbare vorherige Sell-Compression:** kausal am nächsten und semantisch stärkste Vorstufe ist `11:55:55` (und optional die Folge `12:05:45`), aber nur mit Suchhorizont **≥ ~1325 s** bzw. Regime-Bindung — nicht mit 180 s.

**Structure+Acceptance nach frühester Initiative (`12:18`):** Break confirm `12:19:06`, Acceptance `12:20:08` → `diagnostic_earliest_entry_ts` frühestens `12:20:09`. Das liegt **im/am Beginn** des Mega-Buy — „rechtzeitig“ relativ zum Chartimpuls ist zweifelhaft; **kein Entry-/Profit-Claim**.

Der visuelle große grüne Bubble scheitert **nicht** am Initiative-Gate (er wird erkannt), sondern an der **fehlenden Bindung** an die 11:55-Compression.

---

## 10. Wiederholte Compression-Episoden

Quelle: `repeated_compressions.csv` — **12** erlaubte LONG-Compressions `11:30`–`12:25`.

Hervorhebung:

- `11:55:55` Sell≈213k, Impact 0 — Peak-Absorption  
- `12:05:45` Sell≈40k, schwaches Down — Folgecompression  
- Cluster `12:20`–`12:21` nach dem Buy-Impuls (teils Post-Up/Reclaim-Kontext)

**Vorschlag (nur konzeptionell):** Phase `SELL_ABSORPTION_REGIME` / `REPEATED_SELL_COMPRESSION`, wenn wiederholt Sell-dominant + geringe Down-Effizienz + keine nachhaltige Down-Expansion + Tiefs halten/steigen. Gespiegelt: `BUY_ABSORPTION_REGIME`.

Im Referenzfenster sind die Bedingungen **deskriptiv plausibel** bis zum Buy-Impuls `12:18`/`12:20`; Hard-Invalidierung der 11:55-Episode unter F0 fehlte (nur Timeout).

**Noch nicht implementieren.**

---

## 11. Counter-Suchfenster-Sensitivität

Quelle: `counter_search_sensitivity.csv` (deskriptiv, **keine** Return-Optimierung).

| Suche | 11:55 findet Buy-Init? | erste Initiative | F0 LONG attach-rate | Initiative-Reuse (multi) | Kopplungsrisiko |
|------|-------------------------|------------------|---------------------|---------------------------|-----------------|
| 3m (180) | nein | — | 13.0 % (9/69) | 2 | LOW |
| 5m | nein | — | 23.2 % | 5 | LOW |
| 10m | nein | — | 36.2 % | 7 | MEDIUM |
| 15m | nein | — | 44.9 % | 8 | HIGH |
| 20m | nein | — | 44.9 % | 8 | HIGH |
| 30m | **ja** | `12:18:00` / conf `12:18:10` | 47.8 % | 9 | HIGH |

Nur **30m** verbindet 11:55 mit der ersten Buy-Initiative. Gleichzeitig verdreifacht+/erhöht sich die Flip-Anbindung und Mehrfachzuordnung — Gefahr, unverbundene Marktphasen zu koppeln.

**Empfehlung (semantisch, nicht PnL):** Nicht blind 30m festsetzen. Stattdessen:

- Hypothese aktiv, solange Compression-Extrem / Absorptions-Regime **nicht invalidiert**  
- **harter Max-Timeout** (z. B. Obergrenze explorativ, nicht an DOGE gefittet)  
- optional Score-Decay  
- Regime-Merge wiederholter Compressions vor Counter-Bindung  

---

## 12. Bucket-Offset-Sensitivität

Quelle: `bucket_offset_sensitivity.csv`.

| Offset | LONG≈11:55 | Sell-Notional | Buy≈12:20 | Buy-Notional | F0 LONG comps | F0 Buy-Inits |
|--------|------------|---------------|-----------|--------------|---------------|--------------|
| 0 | ja `11:55:55` | 212 674 | ja `12:20:00` | 1 027 171 | 73 | 28 |
| 1–4 | ja (Shift 1–4s) | ≈212 674 | ja | ≈1.00–1.03 M | 68–73 | 24–29 |
| rolling 1s (nur Ref) | 5 Events | — | 9 Events | — | — | HIGH Dedup-Risiko |

**Fazit:** Offset-0 erzeugt **kein** materielles Boundary-Artefakt für den Missed-Long. Rollierende 5s erhöhen Spam/Dedup-Bedarf; Prefix-Parität bleibt prinzipiell möglich, erfordert aber striktes Episode-Merging.

---

## 13. Trade-Price-/BBO-Limitation

Quelle: `trade_price_robustness.csv`.

| Episode | Klassifikation | Beleg |
|---------|----------------|-------|
| LONG 11:55 Sell-Compression | **robust_against_bounce** | 77 Trades, **1** Preis, Return 0, Span 0 |
| SHORT 12:19 Buy-Compression | **possibly_bounce_influenced** | Impact ≈1 Tick (≈1.18 bps) |
| Buy `12:20:00` | klar gerichtet | +29.5 bps, 1042 Trades, 27 Preise |

Fehlendes Mid/BBO erklärt den Missed-Long **nicht** primär: die Sell-Compression ist preisflach über einen einzigen Trade-Preis; der Buy-Impuls ist stark trendig. Späterer Trade-vs-Mid-Paritätsaudit an Tagen mit OB1s bleibt sinnvoll, blockiert F0-Semantik hier aber nicht.

---

## 14. Structure-/Acceptance-Audit

Quelle: `structure_acceptance_audit.csv`.

**Long-Pfad nach `12:18`-Initiative:**

- Micro-High/Level (60s Lookback): 0.08470  
- Break confirm: `12:19:06`  
- Acceptance: `12:20:08`  
- `final_decision_ts`: `12:20:08`  
- `diagnostic_earliest_entry_ts`: `12:20:09`  

**Strongest Buy `12:20`:** Structure-Level 0.08496, **kein** Break in +120 s — Level wird während/durch den Impuls selbst „hochgezogen“; Bestätigungslogik greift hier schlecht für Intra-Burst-Extremes.

**Short 12:19:** Micro-Low 0.08469, kein Break → Timeout korrekt.

Bewertung: Structure/Acceptance für den **Short** passend (blockiert falsch-positiven Short). Für den **Missed-Long** nicht die Primärursache (Episode stirbt vorher am Counter-Timeout). Lookback/Level-Freeze kann bei Impuls-Fenstern zu spät/eng wirken — Design-Delta, nicht F0-Bugfix.

---

## 15. Semantikcheck der fünf Candidates

Quelle: `existing_candidates_semantic_audit.csv`.

Alle 5: `semantically_plausible=True`, Entry strikt nach Final, keine offensichtlichen Notional-/Veto-Widersprüche, Bounce-Flag `low_risk`.  
Sie besitzen Counter+Structure+Acceptance **innerhalb** der F0-Fenster — strukturell anders als der Missed-Long (Timeout ohne Counter).

Messmaschine produziert hier **keine** offenkundig falschen Kandidaten; das Referenzbeispiel fehlt aus Contract-/Zeit-/Regime-Gründen.

---

## 16. Exakte Ursache des False Negative

**Klassifikation: H. MULTIPLE_CAUSES** (Primärgewichte unten)

| Code | Rolle | Beleg |
|------|-------|-------|
| **B. COUNTER_SEARCH_TOO_SHORT** | Primär #1 | Timeout 11:59:05; erste Buy-Init 12:18:10; Δ=1325 s; erst 30m-Suche verbindet |
| **C. ABSORPTION_REGIME_MISSING** | Primär #2 | 12 LONG-Compressions 11:30–12:25; 11:55 Peak; Hypothese nur per Einzel-Episode + festem Timeout |
| D. BUCKET_BOUNDARY | nein | Offsets 0–4 finden Compression+Buy |
| E. INITIATIVE_GATE | nein als Primär | `12:20` Confirm OK; Bubble nicht „abgelehnt“ |
| F. STRUCTURE/ACCEPTANCE | sekundär | Short korrekt blockiert; Long stirbt vorher |
| G. TRADE_PRICE | nein als Primär | 11:55 robust; 12:20 klar |
| A. TRUE_MODEL_REJECTION | teilweise | Unter **strikt** 3m-Einzel-Episode ist Rejection contract-treu — aber Eventmodell ist für die visuelle Phase zu eng |

---

## 17. Empfehlung vor F1

**H. COMBINED_REVISION**

Konkret (Design only):

1. **`ADD_ABSORPTION_REGIME`** — wiederholte Sell-/Buy-Compressions kausal zu einer Phase mergen, solange Extrem/Tiefs nicht invalidieren.  
2. **`EXTEND_COUNTER_SEARCH` nur invalidierungsbewusst** — aktives Regime + harter Max-Timeout + Decay; **nicht** pauschal 30m.  
3. Optional später: Structure-Level-Freeze relativ zu Regime-Start statt nur Flip-Timestamp; BBO-Paritätsaudit parallel.

**Nicht empfohlen als alleinige F1-Vorstufe:** blindes 30m-Fenster oder Threshold-Tuning auf DOGE 11:55/12:20.

---

## 18. Design-Delta (ohne Implementierung)

```text
NEU: AbsorptionRegime
  - start = erste bestätigte Compression der Seite
  - refresh bei neuer Compression gleicher Seite ohne Invalidation
  - invalidation = nachhaltiges Same-Side-Extrem / Structure gegen Hypothese / Datenlücke
  - counter_search_clock startet ab Regime (oder last refresh), endet bei
      min(hard_max_timeout, invalidation)
  - Score decay mit Zeit seit last_compression_confirm
  - eine Gegenseiten-Initiative bindet das Regime (nicht jede Micro-Compression separat)
  - Dedup: eine Initiative darf nicht N Regimes gleichzeitig speisen ohne Prioritätsregel
```

Unfitted-Diagnostics bleiben; keine Outcome-Fit-Schwellen.

---

## 19. Offene Risiken

- Längere Suche ohne Regime → künstliche Flips / Phasen-Kopplung  
- Regime-Merge zu aggressiv → verspätete Invalidierung  
- Structure nach Impuls-Burst (Level-Chase)  
- Trade-Price vs Mid an anderen Tagen unklar  
- DOGE-Einzelfenster ist nicht universell

---

## 20. Nächster sicherer Schritt

1. Design-Review des `AbsorptionRegime` + invalidierungsbewusster Counter-Lifetime (Read-only Spec).  
2. Danach F0.1-Scaffold **nur** mit synthetischen Tests + Prefix-Parität — **ohne** DOGE-Threshold-Fit.  
3. Erst dann begrenzter Multi-Symbol-Smoke; parallel optional BBO-Paritätstag.  
4. Kein Scanner-/Live-Anschluss.

---

## Artefaktindex

| Datei | Auftrag |
|-------|---------|
| `reference_1s_timeline.csv` | 1 |
| `reference_5s_timeline.csv` | 1 |
| `long_1155_episode.csv` | 2 |
| `short_1219_episode.csv` | 3 |
| `buy_initiatives_1200_1230.csv` | 4 |
| `repeated_compressions.csv` | 5 |
| `counter_search_sensitivity.csv` | 6 |
| `bucket_offset_sensitivity.csv` | 7 |
| `trade_price_robustness.csv` | 8 |
| `structure_acceptance_audit.csv` | 9 |
| `existing_candidates_semantic_audit.csv` | 11 |
| `query_log.md` | CH SELECT-Log |
| `ABSCHLUSSBERICHT.md` | dieser Bericht |
