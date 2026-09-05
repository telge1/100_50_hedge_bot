# BTCUSDT Profile Fight — Explanatory Research Audit

**Anchor:** 2026-08-31T19:00:00Z  
**Core window:** 2026-08-31T18:30:00Z–19:30:00Z  
**Extended:** bis 21:30 UTC (soweit Coverage)  
**Source run:** `/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/results/btc_ob_fight_cases/20260831T190000Z/run_017`  
**Verdict:** `BTC_OB_FIGHT_EXPLANATORY_AUDIT_COMPLETE`  
**Runtime:** 36.59s | **Peak RSS:** 463312 KB

> Research-only. `rules_frozen=false`, `trade_verdict_evaluated=false`, `direction=null`.

---

## 1. Liquidation semantics (proved)

- Side field `liquidated_position_side` = **liquidated position side**, not taker aggressor.
- `LIQUIDATED_SHORT` → position side Short → closure requires **FORCED BUY** (aggressive Buy vs Ask).
- `LIQUIDATED_LONG` → **FORCED SELL**.
- `bankruptcy_price`: reference price from feed, **not proven exact execution print**.
- Row = one WS liquidation message; dedup via `event_key`. Core: 60 rows = 60 unique keys → **UNIQUE**.
- **No shared ID** with `public_trades_canonical` → only `HEURISTIC_TEMPORAL_PRICE_ASSOCIATION`.

## 2. Liquidation timeline

- Core events: **60** (59 SHORT, remainder LONG).
- First short liq: `2026-08-31T18:41:12.500000Z`
- Short notional: **490,939 USD** | base: 6.1820 BTC
- Distribution: 83.6% quote before peak | 15.3% peak→reclaim | 1.0% after reclaim

## 3. Public trades (dedup trade_id, taker aggressor)

| Window | Delta | Price chg |
|--------|-------|-----------|
| 19:00–19:10 | +2.76 Mio. USD | +25.88 bps |
| 19:00–19:30 | -0.87 Mio. USD | +9.07 bps |
| 19:10–19:30 (direct) | -3.63 Mio. USD | -16.77 bps |

Same-timestamp ordering: `trade_ts, trade_id` — exchange order not proven.

## 4. Liquidation ↔ trade association

- ±100ms: 59/59 liq events with temporal Buy match; overlapping buy notional 1,662,882 USD (2.0% of taker buy); **NOT_DIRECTLY_IDENTIFIED** / HEURISTIC_TEMPORAL_PRICE_ASSOCIATION
- ±250ms: 59/59 liq events with temporal Buy match; overlapping buy notional 3,474,919 USD (4.2% of taker buy); **NOT_DIRECTLY_IDENTIFIED** / HEURISTIC_TEMPORAL_PRICE_ASSOCIATION
- ±500ms: 59/59 liq events with temporal Buy match; overlapping buy notional 9,564,117 USD (11.7% of taker buy); **NOT_DIRECTLY_IDENTIFIED** / HEURISTIC_TEMPORAL_PRICE_ASSOCIATION
- ±1000ms: 59/59 liq events with temporal Buy match; overlapping buy notional 55,357,816 USD (67.5% of taker buy); **NOT_DIRECTLY_IDENTIFIED** / HEURISTIC_TEMPORAL_PRICE_ASSOCIATION

## 5. Open interest

- Core OI: 52549.44 → 52663.77 (Δ +114.33, +0.218%)
- Attack-window OI delta (outer cross → peak): **-4.73** → `SHORT_COVERING_OR_SHORT_LIQUIDATION_COMPONENT` während des Ausbruchs; Gesamt +114.33 entsteht überwiegend **nach** Peak/Reclaim.
- Interpretation matrix applied per phase in `oi_phase_summary.csv`.

## 6. Market structure

- First peak: **79280.8** @ `2026-08-31T19:10:42.6Z`
- Canonical reclaim: `2026-08-31T19:10:58.515Z` @ 79136.0
- Extended retest high: **79166.7** @ `2026-08-31T20:20:44.76Z` → **LOWER_HIGH**
- Within standard 30m window: **False** — NOT_AVAILABLE_TO_STANDARD_30M_DECISION_WINDOW

## 7. Orderbook (run_017)

- Trade-associated Ask decreases (PROFILE_EDGE_ZONE): **60**
- Nearby Ask / Bid / Unknown increases: **542 / 126 / 0**
- Edge-zone coverage mostly `EDGE_REGION_OUTSIDE_BOOK_RANGE` / partial → **absorption not provable from OB alone**.

## 8. Hypothesis matrix

| Hypothesis | Status |
|------------|--------|
| `PURE_NEW_BUYER_BREAKOUT` | CONTRADICTED |
| `SHORT_LIQUIDATION_DOMINANT_UP_MOVE` | PARTIALLY_SUPPORTED |
| `MIXED_SHORT_SQUEEZE_AND_NEW_LONGS` | SUPPORTED |
| `PASSIVE_SELLER_ABSORPTION` | INCONCLUSIVE |
| `BUYER_EXHAUSTION_WITHOUT_PROVEN_ABSORPTION` | PARTIALLY_SUPPORTED |
| `FAILED_BREAKOUT_AFTER_RECLAIM` | PARTIALLY_SUPPORTED |
| `FAILED_RETEST_LOWER_HIGH` | SUPPORTED |
| `SUSTAINED_BREAKOUT_ACCEPTANCE` | CONTRADICTED |

## 9. Decision-time snapshots

### A_outer_edge_cross
- ts: `2026-08-31T19:08:13.573Z`
- allowable: `WAIT`

### B_price_peak
- ts: `2026-08-31T19:10:42.6Z`
- allowable: `WAIT`

### C_reclaim
- ts: `2026-08-31T19:10:58.515Z`
- allowable: `PARTIAL — reclaim is necessary but not sufficient for failed-breakout short`

### D_retest
- ts: `2026-08-31T20:20:44.76Z`
- allowable: `PARTIAL if lower-high confirmed at decision time`

### E_post_resolution
- ts: `None`
- allowable: `WAIT`

---

## Pflichtantworten (18)

1. **Sind Short-Liquidationen erzwungene Käufe?** Ja — `LIQUIDATED_SHORT` = Short-Position zwangsweise via aggressivem Buy geschlossen (Collector-bewiesen).
2. **59 Positionen oder Events?** **59 Short-Liquidation-Events** (dedupliziert per `event_key`), nicht bewiesen 59 eindeutige Positionen.
3. **Wann?** Erste Short-Liq `2026-08-31T18:41:12.500000Z`; Konzentration vor Peak (84% Notional). Details: `liquidation_events.csv`.
4. **Notional?** Short ~490,939 USD gesamt im Kernfenster.
5. **Anteil am positiven Delta?** Heuristisch ±500ms: ~11.7% des Taker-Buy-Volumens temporal assoziierbar — **nicht kausal bewiesen**.
6. **Direkte Trade-Identifikation?** **Nein** — keine gemeinsame ID.
7. **Trades vor/während/nach Peak?** 19:00–19:10 Δ +2.76 Mio. USD; 19:10–19:30 Δ -3.63 Mio. USD (stark negativ post-peak).
8. **OI beim Ausbruch?** Attack-window Δ **-4.73** (fällt während Ausbruch); Gesamt +114.33 (+0.218%) über Kernfenster — Anstieg überwiegend später.
9. **Short-Squeeze vs neue Longs?** **Gemischt** — dominante Short-Liq + fallendes OI im Ausbruch (Squeeze/Covering); späteres OI-Wachstum deutet auf Long-Nachzug.
10. **Ask-Absorption?** **INCONCLUSIVE** — Nearby Ask increases vorhanden, aber Edge-Coverage schwach.
11. **Käufererschöpfung?** **PARTIALLY_SUPPORTED** — Delta bricht post-peak ein ohne bewiesene Absorption.
12. **Reclaim?** `2026-08-31T19:10:58.515Z` @ 79136.0.
13. **Retest-Docht?** `2026-08-31T20:20:44.76Z` @ 79166.7.
14. **Higher High?** **Nein** — LOWER_HIGH.
15. **Lower High?** **Ja** (79166.7 vs peak 79280.8).
16. **Innerhalb Standardfenster?** **Nein** — `NOT_AVAILABLE_TO_STANDARD_30M_DECISION_WINDOW`.
17. **Erstmals Failed-Breakout-Short begründbar?** Snapshot C (Reclaim) — **PARTIAL**; volle Bestätigung erst Snapshot D (extended retest, hindsight).
18. **Unsicher bleibt:** Direkte Liquidation→Trade-Zuordnung; OB-Absorption an der Kante; physische OI-Einheit; ob Retest-Hindsight für 30m-Decision relevant ist.

---

**Abschluss:** `BTC_OB_FIGHT_EXPLANATORY_AUDIT_COMPLETE`
