# CH Break/Reclaim Microstructure Audit

**Primary Decision:** `DATA_INSUFFICIENT`

Research only. No live gate, no trading rule, no trend-scanner changes.

## Scope

- Events: **2** (exactly CH-covered rows from coverage audit)
- Feature rows: 28
- Causal cutoffs: all features use data with timestamp ≤ observation time T
- Outcome labels may use future information (explicitly separated)

## 1. Coverage / data quality

- DATA_VALID: **2**
- DATA_WARNING: **0**
- DATA_INVALID: **0**

Main statistics use DATA_VALID only.

## 2. Outcome distribution

- `BREAK_ACCEPTED`: 2

### Taxonomy mapping

- `BREAKDOWN/BREAKOUT_CONFIRMED`, `BEARISH_ACCEPTANCE`, `RECLAIM_THEN_BREAK_CONTINUATION` → `BREAK_ACCEPTED`
- Reclaim/failed-break with ≤15m → `RECLAIM_FAST`, else `RECLAIM_SLOW`
- `UNRESOLVED_WITHIN_MAX_WINDOW` → `HOLD_NO_BREAK`
- `EVENT_DATA_INVALID` / unmapped → `EXCLUDED`

## 3. Strongest features (BREAK_ACCEPTED vs RECLAIM_FAST)

| feature | timepoint | AUC | orientation | median_A | median_B |
|---|---|---:|---|---:|---:|

## 4. EARLIEST_USEFUL_TIME (top features)


## 5. BREAK_ACCEPTED vs RECLAIM_FAST — core

No feature reached stable separation with n≥3 per class.

## 6. Symbol / direction strata (top feature)


## 7. Deep-dive events

- `ev_1h_protected_low_0.6052_20260726T050000` | APTUSDT | bearish | BREAK_ACCEPTED | level=0.6052
- `ev_1h_protected_low_0.6008_20260725T170000` | APTUSDT | bearish | BREAK_ACCEPTED | level=0.6008

See `deep_dive_timelines.csv` for compact relative timelines.

## 8. Primary questions

### Q1 — Saubere OB+Trade-Coverage?

Von 2 Events: VALID=2, WARNING=0, INVALID=0. Grenzwertig / unzureichend für robuste Claims.

### Q2 — Welche Features unterscheiden BREAK_ACCEPTED vs RECLAIM/HOLD?

Keine robuste Einzel-Feature-Trennung in der VALID-Stichprobe.

### Q3 — Was ist erst nach Bestätigung sichtbar (zu spät)?

Viele Features ohne Signal; wo Signal existiert, siehe EARLIEST_USEFUL_TIME-Tabelle.

### Q4 — Kausales Signal früh genug für Hedge-Bot Block/Freigabe?

Primary decision `DATA_INSUFFICIENT` beantwortet das vorläufig. Noch keine Thresholds / Live-Logik — nur Evidence für einen späteren Gate-Design-Schritt.

## 9. Einschränkungen

- Kleine Stichprobe; Symbol/Richtung unbalanciert möglich
- C3 `break_available_at` ist Scanner-Close; Trade-through wurde zusätzlich abgeleitet
- Wall pull vs trade-consumption nur als Proxy (depth Δ vs aggressor flow)
- Kein ML; AUC ist univariater Rank-Score
- RECLAIM_FAST-Schwelle 15m ist a-priori, nicht optimiert

Artifacts: `results/ch_break_reclaim_microstructure_audit_20260808_smoke`

