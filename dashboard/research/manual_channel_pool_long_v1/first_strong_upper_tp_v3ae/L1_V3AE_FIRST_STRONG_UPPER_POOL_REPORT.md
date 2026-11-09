# L1 V3AE — FIRST_STRONG_UPPER_POOL (isoliert)

Run: `20261109T105536Z`
Drawings SHA256: `b0718d82a94e8ad12d27d570c0cd7ce97cf8088a2db21c4c92830cd80fc600cb`
Snapshot: `/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard/research/manual_channel_pool_long_v1/support_break_exit_v3ab/l1_v3ab_drawings_snapshot_b0718d82a94e8ad1.json`
Supportbruch: unverändert V3Y (Δ 0.2 %)

## 1. Getestete Schwellenvarianten (V3W TP2-Grid)

- `TP2a_w008_d3_o3` — width≥0.08 %, depth≥3, origins≥3
- `TP2b_w012_d4_o4` — width≥0.12 %, depth≥4, origins≥4
- `TP2c_w015_d5_o4` — width≥0.15 %, depth≥5, origins≥4
- Baseline: `BASELINE_V3AB_TP` (`pick_tp` / max D2 wie V3AB)

## 2–5. Kennzahlen je Szenario × Variante A/B/C

| Szenario | Var | Trades | TP | SUPPORT_BREAK | SL | CHANNEL | Σ% brutto | vs BL Σ% |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| BASELINE_V3AB_TP | A | 4 | 0 | 2 | 2 | 0 | 0.2258 |  |
| BASELINE_V3AB_TP | B | 4 | 0 | 2 | 2 | 0 | 0.2258 |  |
| BASELINE_V3AB_TP | C | 0 | 0 | 0 | 0 | 0 | 0.0000 |  |
| FIRST_STRONG_UPPER_POOL/TP2a_w008_d3_o3 | A | 7 | 4 | 2 | 1 | 0 | 1.3260 | +1.1002 |
| FIRST_STRONG_UPPER_POOL/TP2a_w008_d3_o3 | B | 7 | 4 | 2 | 1 | 0 | 1.3260 | +1.1002 |
| FIRST_STRONG_UPPER_POOL/TP2a_w008_d3_o3 | C | 0 | 0 | 0 | 0 | 0 | 0.0000 | +0.0000 |
| FIRST_STRONG_UPPER_POOL/TP2b_w012_d4_o4 | A | 6 | 3 | 1 | 2 | 0 | 2.9004 | +2.6746 |
| FIRST_STRONG_UPPER_POOL/TP2b_w012_d4_o4 | B | 6 | 3 | 1 | 2 | 0 | 2.9004 | +2.6746 |
| FIRST_STRONG_UPPER_POOL/TP2b_w012_d4_o4 | C | 0 | 0 | 0 | 0 | 0 | 0.0000 | +0.0000 |
| FIRST_STRONG_UPPER_POOL/TP2c_w015_d5_o4 | A | 5 | 2 | 1 | 2 | 0 | 1.7417 | +1.5159 |
| FIRST_STRONG_UPPER_POOL/TP2c_w015_d5_o4 | B | 5 | 2 | 1 | 2 | 0 | 1.7417 | +1.5159 |
| FIRST_STRONG_UPPER_POOL/TP2c_w015_d5_o4 | C | 0 | 0 | 0 | 0 | 0 | 0.0000 | +0.0000 |

## 6. Entry-Parität vs. Baseline

- FIRST_STRONG_UPPER_POOL/TP2a_w008_d3_o3 / A: identische Entries **nein**
- FIRST_STRONG_UPPER_POOL/TP2a_w008_d3_o3 / B: identische Entries **nein**
- FIRST_STRONG_UPPER_POOL/TP2a_w008_d3_o3 / C: identische Entries **ja**
- FIRST_STRONG_UPPER_POOL/TP2b_w012_d4_o4 / A: identische Entries **nein**
- FIRST_STRONG_UPPER_POOL/TP2b_w012_d4_o4 / B: identische Entries **nein**
- FIRST_STRONG_UPPER_POOL/TP2b_w012_d4_o4 / C: identische Entries **ja**
- FIRST_STRONG_UPPER_POOL/TP2c_w015_d5_o4 / A: identische Entries **nein**
- FIRST_STRONG_UPPER_POOL/TP2c_w015_d5_o4 / B: identische Entries **nein**
- FIRST_STRONG_UPPER_POOL/TP2c_w015_d5_o4 / C: identische Entries **ja**

## 7–9. Trade-Effekte (Variant B)

### FIRST_STRONG_UPPER_POOL/TP2a_w008_d3_o3
- TP-Treffer: 4 · Supportbruch: 2 · SL: 1
- Begrenzte Gewinner (gleicher Entry, niedrigeres PnL): 1
- Verbesserte Trades vs Baseline (gleicher Entry): 1
- Neue Entry-Slots (nur FS): `te-d-8521dc37f7b9-0008, te-d-c0791ea0c65e-0007, te-d-ea081434c5d3-0005`
- Entfallene vs BL: `—`

### FIRST_STRONG_UPPER_POOL/TP2b_w012_d4_o4
- TP-Treffer: 3 · Supportbruch: 1 · SL: 2
- Begrenzte Gewinner (gleicher Entry, niedrigeres PnL): 1
- Verbesserte Trades vs Baseline (gleicher Entry): 1
- Neue Entry-Slots (nur FS): `te-d-8521dc37f7b9-0008, te-d-c0791ea0c65e-0007`
- Entfallene vs BL: `—`

### FIRST_STRONG_UPPER_POOL/TP2c_w015_d5_o4
- TP-Treffer: 2 · Supportbruch: 1 · SL: 2
- Begrenzte Gewinner (gleicher Entry, niedrigeres PnL): 1
- Verbesserte Trades vs Baseline (gleicher Entry): 0
- Neue Entry-Slots (nur FS): `te-d-c0791ea0c65e-0007`
- Entfallene vs BL: `—`


## 10. Referenztrade `te-d-c0791ea0c65e-0003`

Objektive Regel wählt **nicht** die violette Konzentration (~0.08929): die ersten gültigen „strong“-Zonen liegen bei ~0.08536–0.08604 (V3AD-Audit, Zone #0–#1).

- **BASELINE_V3AB_TP**: TP **0.0949600000** · Zone **–** (depth , origins , dist %) · Exit **SUPPORT_BREAK_EXIT** @ 2026-08-27 13:50:00 UTC · PnL **2.8854%** (Baseline-V3AB-Ref: SUPPORT_BREAK +2.89 % @ 0.08736)
- **FIRST_STRONG_UPPER_POOL/TP2a_w008_d3_o3**: TP **0.0853600000** · Zone **0.08536–0.08546** (depth 4, origins 4, dist 0.5299729124955905%) · Exit **TP** @ 2026-08-26 21:00:00 UTC · PnL **0.5300%** (Baseline-V3AB-Ref: SUPPORT_BREAK +2.89 % @ 0.08736)
- **FIRST_STRONG_UPPER_POOL/TP2b_w012_d4_o4**: TP **0.0860400000** · Zone **0.08604–0.086276** (depth 5, origins 5, dist 1.3308208691555836%) · Exit **TP** @ 2026-08-26 21:15:00 UTC · PnL **1.3308%** (Baseline-V3AB-Ref: SUPPORT_BREAK +2.89 % @ 0.08736)
- **FIRST_STRONG_UPPER_POOL/TP2c_w015_d5_o4**: TP **0.0860400000** · Zone **0.08604–0.086276** (depth 5, origins 5, dist 1.3308208691555836%) · Exit **TP** @ 2026-08-26 21:15:00 UTC · PnL **1.3308%** (Baseline-V3AB-Ref: SUPPORT_BREAK +2.89 % @ 0.08736)

Vollständige Trades: `l1_v3ae_trades_all.csv`

**STOP** — keine Übernahme in V3AB/V3Y/Produktion.