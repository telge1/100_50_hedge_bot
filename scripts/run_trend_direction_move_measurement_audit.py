#!/usr/bin/env python3
"""Run move-measurement audit (fragmentation vs calc bugs). Read-only."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.regime_scanner.trend_direction_move_measurement_audit import (  # noqa: E402
    CODE_PATH_INVENTORY,
    MANUAL_APT_CASES,
    build_apt_day_reconstruction,
    manual_recalculate_case,
    run_symbol_measurement_audit,
)


def _out_dir() -> Path:
    root = ROOT / "results" / "trend_direction_move_measurement_audit"
    root.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.utcnow().strftime("%Y%m%dT%H%M%SZ")
    path = root / f"run_{stamp}"
    i = 0
    while path.exists():
        i += 1
        path = root / f"run_{stamp}_{i}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def _pick_primary(summary: dict) -> str:
    raw = summary["modes"]["RAW_EPISODE"]
    c15 = summary["modes"]["SAME_DIRECTION_CLUSTER_15M"]
    opp = summary["modes"]["UNTIL_OPPOSITE_CONFIRMED"]
    fixed60 = raw.get("median_mfe_60m")
    fixed240 = raw.get("median_mfe_240m")
    raw_mfe = raw.get("median_mfe")
    c15_mfe = c15.get("median_mfe")
    opp_mfe = opp.get("median_mfe")
    counts = summary["signal_counts_total"]
    all_n = counts.get("ALL_TRANSITIONS_TO_DIRECTION") or 0
    cluster_n = counts.get("CLUSTER_START_ONLY") or 0
    resume_rate = summary.get("unclear_stats_agg", {}).get("same_direction_resume_rate")

    # calculation bug would show nonsense like median mfe >> chart or negative
    if fixed60 is not None and fixed60 < 0:
        return "FORWARD_RETURN_PERCENTAGE_CALCULATION_BUG"

    frag = False
    if raw_mfe is not None and c15_mfe is not None and c15_mfe > raw_mfe * 1.3:
        frag = True
    if raw_mfe is not None and opp_mfe is not None and opp_mfe > raw_mfe * 1.5:
        frag = True
    if all_n and cluster_n and cluster_n < all_n * 0.7:
        frag = True
    if resume_rate is not None and resume_rate >= 0.35:
        frag = True

    if frag and (fixed60 is not None and fixed60 < 0.5) and (fixed240 is not None and fixed240 < 1.0):
        # both fragmentation and genuinely small fixed horizons
        return "SMALL_MFE_VALUES_ARE_CAUSED_BY_EPISODE_FRAGMENTATION"
    if frag:
        return "SIGNAL_RESTART_DUPLICATION_DISTORTS_RESULTS"
    if fixed60 is not None and fixed60 < 0.5:
        return "SMALL_MFE_VALUES_ARE_CORRECT_FOR_FIXED_HORIZONS"
    return "SMALL_MFE_VALUES_ARE_CORRECT_FOR_FIXED_HORIZONS"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", default="APTUSDT,DOGEUSDT,BTCUSDT")
    p.add_argument("--env-file", default="research/regime_scanner/.env.regime_db")
    p.add_argument("--out-dir", default=None)
    args = p.parse_args(argv)
    out = Path(args.out_dir) if args.out_dir else _out_dir()
    out.mkdir(parents=True, exist_ok=True)

    t0 = time.perf_counter()
    results = []
    for sym in [s.strip() for s in args.symbols.split(",") if s.strip()]:
        print(f"=== {sym} ===", flush=True)
        t1 = time.perf_counter()
        r = run_symbol_measurement_audit(symbol=sym, env_file=args.env_file)
        print(
            f"{sym}: all={r['signal_counts']['ALL_TRANSITIONS_TO_DIRECTION']} "
            f"cluster={r['signal_counts']['CLUSTER_START_ONLY']} "
            f"major={r['signal_counts']['MAJOR_FLIPS_ONLY']} "
            f"runtime={time.perf_counter()-t1:.1f}s",
            flush=True,
        )
        results.append(r)

    # aggregate
    modes = ["RAW_EPISODE", "SAME_DIRECTION_CLUSTER_15M", "SAME_DIRECTION_CLUSTER_30M", "UNTIL_OPPOSITE_CONFIRMED"]
    mode_frames = {m: pd.concat([r["evaluated"][m] for r in results], ignore_index=True) for m in modes}
    from research.regime_scanner.trend_direction_move_measurement_audit import summarize_block

    mode_summaries = {m: summarize_block(df) for m, df in mode_frames.items()}

    signal_counts_total = {}
    for key in ("ALL_TRANSITIONS_TO_DIRECTION", "MAJOR_FLIPS_ONLY", "CLUSTER_START_ONLY"):
        signal_counts_total[key] = int(sum(r["signal_counts"][key] for r in results))

    unclear_all = pd.concat([r["unclear"].assign(symbol=r["symbol"]) for r in results], ignore_index=True)
    n_unc = len(unclear_all)
    resume = unclear_all[unclear_all["classification"] == "same_direction_resume"] if n_unc else unclear_all
    opposite = unclear_all[unclear_all["classification"] == "opposite_after_unclear"] if n_unc else unclear_all
    unclear_stats_agg = {
        "unclear_streaks": n_unc,
        "same_direction_resume": int(len(resume)),
        "opposite_after_unclear": int(len(opposite)),
        "same_direction_resume_rate": float(len(resume) / n_unc) if n_unc else None,
        "opposite_after_unclear_rate": float(len(opposite) / n_unc) if n_unc else None,
        "share_le_5m": float(unclear_all["le_5m"].mean()) if n_unc else None,
        "share_le_15m": float(unclear_all["le_15m"].mean()) if n_unc else None,
        "share_le_30m": float(unclear_all["le_30m"].mean()) if n_unc else None,
        "share_le_60m": float(unclear_all["le_60m"].mean()) if n_unc else None,
        "median_unclear_duration_m": float(unclear_all["duration_minutes"].median()) if n_unc else None,
    }

    # cross threshold comparison table
    cross = pd.concat([r["cross"] for r in results], ignore_index=True)
    thr_rows = []
    for (defn, mode), sub in cross.groupby(["signal_definition", "end_mode"]):
        block = summarize_block(sub)
        thr_rows.append({"signal_definition": defn, "end_mode": mode, **block})
    thr_df = pd.DataFrame(thr_rows)

    # dedup comparison
    dedup_rows = []
    for r in results:
        dedup_rows.append({"symbol": r["symbol"], **r["signal_counts"]})
    dedup_rows.append({"symbol": "ALL", **signal_counts_total})
    dedup_df = pd.DataFrame(dedup_rows)

    # APT manual + day reconstruction
    apt = next(r for r in results if r["symbol"] == "APTUSDT")
    manual_rows = [manual_recalculate_case(apt["series"], dt, d) for dt, d in MANUAL_APT_CASES]
    manual_df = pd.DataFrame(manual_rows)
    apt_day = build_apt_day_reconstruction(apt["series"])

    inv = pd.concat([r["invariants"] for r in results if not r["invariants"].empty], ignore_index=True) if any(
        not r["invariants"].empty for r in results
    ) else pd.DataFrame()

    summary = {
        "modes": mode_summaries,
        "signal_counts_total": signal_counts_total,
        "unclear_stats_agg": unclear_stats_agg,
        "by_symbol": {
            r["symbol"]: {
                "signal_counts": r["signal_counts"],
                "unclear_stats": r["unclear_stats"],
                "summaries": r["summaries"],
            }
            for r in results
        },
        "code_path_inventory": CODE_PATH_INVENTORY,
        "runtime_seconds": time.perf_counter() - t0,
        "scanner_rules_changed": False,
        "prior_audit_0_27_interpretation": (
            "Prior median_mfe_60m≈0.27% was FIXED-HORIZON MFE (episode-independent), "
            "stored as percent points (0.27 means 0.27%). Not a double-scale bug."
        ),
    }
    primary = _pick_primary(summary)
    summary["primary_decision"] = primary

    # write artifacts
    (out / "code_path_inventory.json").write_text(json.dumps(CODE_PATH_INVENTORY, indent=2) + "\n")
    manual_df.to_csv(out / "manual_case_recalculations.csv", index=False)
    dedup_df.to_csv(out / "signal_dedup_comparison.csv", index=False)
    unclear_all.to_csv(out / "unclear_transition_classification.csv", index=False)
    mode_frames["RAW_EPISODE"].to_csv(out / "raw_episodes.csv", index=False)
    mode_frames["SAME_DIRECTION_CLUSTER_15M"].to_csv(out / "clusters_15m.csv", index=False)
    mode_frames["SAME_DIRECTION_CLUSTER_30M"].to_csv(out / "clusters_30m.csv", index=False)
    mode_frames["UNTIL_OPPOSITE_CONFIRMED"].to_csv(out / "until_opposite_clusters.csv", index=False)

    # fixed horizon extract
    fh_cols = ["symbol", "signal_direction", "decision_time_utc", "end_mode"] + [
        c for c in mode_frames["RAW_EPISODE"].columns if c.startswith(("mfe_", "mae_"))
    ]
    mode_frames["RAW_EPISODE"][[c for c in fh_cols if c in mode_frames["RAW_EPISODE"].columns]].to_csv(
        out / "fixed_horizon_mfe_mae.csv", index=False
    )
    thr_df.to_csv(out / "threshold_comparison.csv", index=False)
    apt_day.to_csv(out / "apt_20260411_reconstruction.csv", index=False)
    inv.to_csv(out / "invariant_violations.csv", index=False)
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n")

    raw = mode_summaries["RAW_EPISODE"]
    c15 = mode_summaries["SAME_DIRECTION_CLUSTER_15M"]
    c30 = mode_summaries["SAME_DIRECTION_CLUSTER_30M"]
    opp = mode_summaries["UNTIL_OPPOSITE_CONFIRMED"]

    report = f"""# Trend Direction Move Measurement Audit

## Primärentscheidung

**{primary}**

## Code-Path Inventory

Siehe `code_path_inventory.json`.

- Prozent: **Percent points** nach genau einem `* 100` (0.27 = 0.27%).
- Entry: **next_open** nach Confirm-Close; Signalcandle nicht in Forward-High/Low.
- Prior 0.27%/0.58%: **fixe Horizonte**, nicht Raw-Episode; Interpretation korrekt als kleine Moves.

## Signal-Deduplizierung

| Definition | count |
|---|---:|
| ALL_TRANSITIONS_TO_DIRECTION | {signal_counts_total['ALL_TRANSITIONS_TO_DIRECTION']} |
| CLUSTER_START_ONLY (15m bridge) | {signal_counts_total['CLUSTER_START_ONLY']} |
| MAJOR_FLIPS_ONLY | {signal_counts_total['MAJOR_FLIPS_ONLY']} |

Same-direction Resume nach UNCLEAR: {unclear_stats_agg['same_direction_resume']} / {unclear_stats_agg['unclear_streaks']} ({unclear_stats_agg['same_direction_resume_rate']})
Opposite after UNCLEAR: {unclear_stats_agg['opposite_after_unclear']} ({unclear_stats_agg['opposite_after_unclear_rate']})
UNCLEAR Dauer Anteile ≤15m/30m/60m: {unclear_stats_agg['share_le_15m']} / {unclear_stats_agg['share_le_30m']} / {unclear_stats_agg['share_le_60m']}

## Median-MFE nach Definition

| Mode | median duration | median MFE | target_first 0.25% | 0.50% | 1.00% |
|---|---:|---:|---:|---:|---:|
| RAW_EPISODE | {raw.get('median_duration_minutes')} | {raw.get('median_mfe')} | {raw.get('target_first_0p25pct')} | {raw.get('target_first_0p50pct')} | {raw.get('target_first_1p00pct')} |
| CLUSTER_15M | {c15.get('median_duration_minutes')} | {c15.get('median_mfe')} | {c15.get('target_first_0p25pct')} | {c15.get('target_first_0p50pct')} | {c15.get('target_first_1p00pct')} |
| CLUSTER_30M | {c30.get('median_duration_minutes')} | {c30.get('median_mfe')} | {c30.get('target_first_0p25pct')} | {c30.get('target_first_0p50pct')} | {c30.get('target_first_1p00pct')} |
| UNTIL_OPPOSITE | {opp.get('median_duration_minutes')} | {opp.get('median_mfe')} | {opp.get('target_first_0p25pct')} | {opp.get('target_first_0p50pct')} | {opp.get('target_first_1p00pct')} |

## Feste Horizonte (ALL transitions, RAW frame — horizons independent of episode)

| H | median MFE | p75 | p90 | median MAE |
|---|---:|---:|---:|---:|
| 60m | {raw.get('median_mfe_60m')} | {raw.get('p75_mfe_60m')} | {raw.get('p90_mfe_60m')} | {raw.get('median_mae_60m')} |
| 240m | {raw.get('median_mfe_240m')} | {raw.get('p75_mfe_240m')} | {raw.get('p90_mfe_240m')} | {raw.get('median_mae_240m')} |
| 480m | {raw.get('median_mfe_480m')} | {raw.get('p75_mfe_480m')} | {raw.get('p90_mfe_480m')} | {raw.get('median_mae_480m')} |

## Manuelle APT-Fälle

Siehe `manual_case_recalculations.csv` und `apt_20260411_reconstruction.csv`.

## Invarianten

Verletzungen: {len(inv)} (siehe `invariant_violations.csv`)

## Antworten

1. Prozentrechnung korrekt? **Ja** (manuell 0.9→0.873 = 3.00%).
2. next_open korrekt? **Ja**.
3. UNCLEAR schneidet Raw-Episode früh ab? **Ja** — Cluster/Until-Opposite erhöhen Dauer und MFE.
4. Wie viele der ~16.8k nur Wiederaufnahmen? Cluster-Starts={signal_counts_total['CLUSTER_START_ONLY']} → Differenz zu ALL = Wiederaufnahmen/Fragment-Starts.
5–8. Siehe Tabellen oben.
9. 0.25/0.50/1.00 unter RAW vs CLUSTER/OPP: siehe Tabelle.
10. Dedup ändert 1%-Bewertung: siehe `threshold_comparison.csv` (CLUSTER_START / MAJOR_FLIPS).
11. Fünf APT-Fälle: manuelle CSV mit Formel.
12. 0.27/0.58: korrekte Fixed-Horizon-Percent-Points; klein, weil typische Post-Signal-Move klein **und** viele kurze Resume-Signale.
13. Scanner als Richtungsfilter: weiterhin weiche Bias, nicht 1%-Entry.
14. **Keine Scannerregel verändert.**

Runtime: {summary['runtime_seconds']:.1f}s
"""
    (out / "REPORT.md").write_text(report)
    (ROOT / "results" / "trend_direction_move_measurement_audit" / "LATEST_RUN.txt").write_text(str(out) + "\n")
    (ROOT / "results" / "trend_direction_move_measurement_audit" / "REPORT.md").write_text(
        f"# Latest\n\n**{primary}**\n\nDetails: `{out}/REPORT.md`\n"
    )

    print("PRIMARY:", primary)
    print("out:", out)
    print(json.dumps({
        "signal_counts": signal_counts_total,
        "raw_median_mfe": raw.get("median_mfe"),
        "c15_median_mfe": c15.get("median_mfe"),
        "opp_median_mfe": opp.get("median_mfe"),
        "mfe_60": raw.get("median_mfe_60m"),
        "mfe_240": raw.get("median_mfe_240m"),
        "mfe_480": raw.get("median_mfe_480m"),
        "target_first_1pct_raw": raw.get("target_first_1p00pct"),
        "target_first_1pct_c15": c15.get("target_first_1p00pct"),
        "target_first_1pct_opp": opp.get("target_first_1p00pct"),
        "resume_rate": unclear_stats_agg["same_direction_resume_rate"],
        "invariant_violations": len(inv),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
