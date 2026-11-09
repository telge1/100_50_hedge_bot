# L1 V3AB — Backtest mit aktuellen Zeichnungen

Run: `20261109T102115Z`
Kanonisch: `/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard/research_charts/user_data/drawings.json`
Snapshot SHA256: `b0718d82a94e8ad12d27d570c0cd7ce97cf8088a2db21c4c92830cd80fc600cb`
V3Y-Frozen-SHA (Vergleich Zeichnungen): `d05d218646aced84…`
Drawing health preflight: **drift=false** (RAM reload ≡ file)

Strategie: identisch V3Y (`INDEPENDENT_SUPPORT`, C1, Pool/Kanal-TP, SL −1,20 %, Supportbruch 0,20 %).
Alle Trades: **RETROSPECTIVE_DRAWING**. Keine Übernahme in Produktion.

## Zeichnungs-Δ vs. V3U/V3Y-Frozen-Snapshot

- Entfernt (21): `d-036ba7cefe2e, d-0e5e7004dccd, d-1f70c9798935, d-248d74050cc8, d-289deb31eb84, d-2f6effea5ff3, d-3bd7446b7a5b, d-5c8ffbfb8a06, d-7f4519d5ce5b, d-988236deb817, d-9b6e62dc884c, d-a6d9a05da2f1, d-e1c6e07765fe, p17ag-entry-01-20260707T111000Z, p17ag-entry-02-20260708T092500Z, p17ag-entry-03-20260717T020000Z, p17ag-entry-04-20260729T194500Z, p17ag-pos-01-20260707T111000Z, p17ag-pos-02-20260708T092500Z, p17ag-pos-03-20260717T020000Z, p17ag-pos-04-20260729T194500Z`
- Neu (5): `d-1731fb4bb889, d-8521dc37f7b9, d-c0791ea0c65e, d-e6e9b83d2be1, d-ea081434c5d3`
- Unveränderte IDs: **0**

Inventar: `l1_v3ab_drawing_inventory.json` · Immutable: `l1_v3ab_drawings_snapshot_*.json`

## Vergleich V3Y (alte Zeichnungen) → V3AB (neue Zeichnungen)

| Var | Trades V3Y→V3AB (Y) | Σ% V3Y Y | Σ% V3AB Y | Σ% Baseline (ohne Support-Exit) | SUPPORT_BREAK | neue Entries Y |
|---|---:|---:|---:|---:|---:|---:|
| A | 11→4 | 37.518 | 0.2258 | -3.6 | 2 | 1 |
| B | 9→4 | 39.7269 | 0.2258 | -3.6 | 2 | 1 |
| C | 4→0 | 3.1944 | 0 | 0 | 0 | 0 |

## Kennzahlen je Variante (V3AB)

| Var | Trades BL / Y | Gewinne BL / Y | Verluste BL / Y | Ø% BL / Y | längste Loss BL / Y |
|---|---:|---:|---:|---:|---|
| A | 3/4 | 0/1 | 3/3 | -1.2/0.0565 | 3/3 |
| B | 3/4 | 0/1 | 3/3 | -1.2/0.0565 | 3/3 |
| C | 0/0 | 0/0 | 0/0 | 0/0 | 0/0 |

Exit-Mix (V3AB Support-Exit): siehe `l1_v3ab_summary.csv` → `exit_counts_*`.

Vollständige Trades: `l1_v3ab_trades_all.csv`

## Reproduktion

```bash
cd support_break_exit_v3ab
python3 run_v3ab.py
```

**STOP** — keine Änderung an drawings.json, V3Y-Artefakten oder Hauptstrategie.