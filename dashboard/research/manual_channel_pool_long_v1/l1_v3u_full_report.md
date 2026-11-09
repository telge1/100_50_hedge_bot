# L1 V3U — Dashboard sync, drawing delete, INDEPENDENT_SUPPORT backtest

## Operations (V3U)

| Step | Result |
|---|---|
| Pre-run backup | `l1_v3u_drawings_backup_pre_run.json` — 22 drawings, SHA256 `bf064b5bee1c4073931134c03408142fd6b129cff5a8cc6d13a08c4af69cd2e0` |
| Dashboard service | **No systemd unit**; restarted prior `python app.py` on **127.0.0.1:3000** (was PID 1002012) via SIGTERM + nohup |
| Post-restart health | PID **1237376** (uvicorn/app), `/api/health` OK |
| Drawing health **before** delete | HTTP 200, `drift=false`, ram=22, file=22, canonical SHA `75389489163ec4b6…` |
| Delete `d-ac60572bfc69` | API hit + `POST …/drawings/delete` → `deleted_id=d-ac60572bfc69`, persist OK |
| Drawing health **after** delete | HTTP 200, `drift=false`, ram=21, file=21, canonical SHA `6b50e7f525eec9e5…` |
| Post-delete file SHA (raw JSON) | `d05d218646aced84ffc0adbb8ca66a6a8f92048190e72dc9cea3140270fb594c` |
| Backup integrity | All 21 remaining drawings **byte-identical** to pre-run backup except removed ID |

## Backtest run

- Run-ID: `20261108T125450Z`
- Runner: `run_l1_v3u_full_backtest.py` → `v3l._run(INDEPENDENT_SUPPORT)`
- Canonical: `research_charts/user_data/drawings.json`
- Snapshot: `l1_v3u_drawings_snapshot_d05d218646aced84.json`
- Mode / window: `INDEPENDENT_SUPPORT` / `DRAWINGS_AUTO_WINDOW` (DOGEUSDT 5m)
- Drawings: **21** (13 manual support candidates; 8 `p17ag-*` overlays in file, not used as support)
- Simulation: 2026-03-29 16:00:00 UTC .. 2026-10-19 13:05:00 UTC (`DATA_END_REACHED`)

## Stats (gross %, not compounded, no fees)

| Variant | Trades | Wins | SL | Hit rate % | Σ wins % | Σ losses % | Gross % | Pool TP | Channel | Open | Support lines used |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **A** | 7 | 3 | 4 | 42.86 | 29.5831 | -4.8000 | **+24.7831** | 3 | 0 | 0 | 4 |
| **B** | 6 | 3 | 3 | 50.00 | 29.5831 | -3.6000 | **+25.9831** | 3 | 0 | 0 | 4 |
| **C** | 3 | 2 | 1 | 66.67 | 3.2372 | -1.2000 | **+2.0372** | 0 | 2 | 0 | 3 |

## vs L1 V3S

| Kennzahl | A (V3S→V3U) | B | C |
|---|---:|---:|---:|
| Trades | 13 → **7** | 11 → **6** | 3 → **3** |
| Wins | 3 → **3** | 3 → **3** | 2 → **2** |
| SL | 10 → **4** | 8 → **3** | 1 → **1** |
| Gross % | +17.5831 → **+24.7831** | +19.9831 → **+25.9831** | +2.0372 → **+2.0372** |

**Entfallene Trades (6):** ausschließlich Stop-Loss-Entries auf Support `d-ac60572bfc69` (Sep 2026). **Keine neuen Trades.** Gewinner und übrige Verlierer unverändert (gleiche Entry/TP/SL/Exit).

**Zeichnungs-Delta vs V3S-Snapshot:** `d-ac60572bfc69` entfernt; zusätzlich 8 `p17ag-*` Markierungen im Kanon (kein Backtest-Effekt). Kein Entry auf `d-ac60572bfc69` in V3U.

**Obere Kanäle (Upper-IDs in Trades):** `d-0e5e7004dccd`, `d-e1c6e07765fe`, `d-2f6effea5ff3` — unverändert ggü. V3S-Restset.

## Artefakte

- `l1_v3u_run_manifest.json`
- `l1_v3u_chart_review_all_trades.csv`
- `l1_v3u_variant_{a,b,c}_{winners,stop_losses}.csv`
- `l1_v3u_drawings_snapshot_d05d218646aced84.json`
- `l1_v3u_drawings_backup_pre_run.json`, `l1_v3u_drawings_backup_20261108T125450Z.json`
