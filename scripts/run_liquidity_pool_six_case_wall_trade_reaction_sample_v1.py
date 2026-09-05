#!/usr/bin/env python3
"""LIQUIDITY_POOL_SIX_CASE_WALL_TRADE_REACTION_SAMPLE_V1

Small causal reaction sample (3 ASK FROM_BELOW + 3 BID FROM_ABOVE).
Research only. No strategy, PnL, backtest, or live mutation.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path
from typing import Any

OA_ROOT = Path(__file__).resolve().parents[1]
if str(OA_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(OA_ROOT / "src"))

from orderbook_analyse.liquidity_pool_six_case_wall_trade_reaction_sample_v1.audit_case import (
    audit_cluster_case,
)
from orderbook_analyse.liquidity_pool_six_case_wall_trade_reaction_sample_v1.selection import (
    select_six_cases,
)

V2 = OA_ROOT / "results" / "liquidity_pool_arrival_wall_monitor_v2"
RAW_ROOT = OA_ROOT / "data" / "orderbook_raw_shadow" / "ob200_v3"
OUT_DEFAULT = OA_ROOT / "results" / "liquidity_pool_six_case_wall_trade_reaction_sample_v1"


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in keys})


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", default=str(OUT_DEFAULT))
    ap.add_argument("--raw-root", default=str(RAW_ROOT))
    ap.add_argument("--v2-dir", default=str(V2))
    args = ap.parse_args(argv)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()

    clusters = list(
        csv.DictReader((Path(args.v2_dir) / "market_arrival_clusters.csv").open(encoding="utf-8"))
    )
    arrivals = list(csv.DictReader((Path(args.v2_dir) / "pool_arrivals_v2.csv").open(encoding="utf-8")))
    by_cluster: dict[str, list[dict[str, Any]]] = {}
    for a in arrivals:
        by_cluster.setdefault(a.get("market_arrival_cluster_id") or "", []).append(a)

    sel = select_six_cases(clusters)
    (out / "selection_manifest.json").write_text(json.dumps(sel, indent=2), encoding="utf-8")
    print(f"selected {[c['case_id']+':'+c['cluster_start_ts']+':'+c['side'] for c in sel['cases']]}", flush=True)

    summaries = []
    timelines = []
    prefixes = []
    queries = []
    causality_fail = False

    for case in sel["cases"]:
        print(f"audit {case['case_id']} {case['side']} {case['cluster_start_ts']}...", flush=True)
        res = audit_cluster_case(
            case=case,
            raw_root=Path(args.raw_root),
            arrivals_by_cluster=by_cluster,
        )
        summaries.append(res["summary"])
        timelines.extend(res["timeline"])
        prefixes.append(res["prefix"])
        queries.append(res["query"])
        if res["prefix"].get("critical_lookahead"):
            causality_fail = True
            print("CAUSALITY_FAILURE", res["prefix"], flush=True)
            break

    write_csv(out / "six_case_summary.csv", summaries)
    write_csv(out / "case_timelines.csv", timelines)
    write_csv(out / "prefix_parity.csv", prefixes)

    if causality_fail:
        (out / "REPORT.md").write_text("# CAUSALITY_FAILURE\n\nAbort.\n", encoding="utf-8")
        return 2

    # Known-case consistency vs committed single-case MIXED_WALL_REACTION
    known = next(
        (s for s in summaries if s["cluster_start_ts"] == "2026-08-26T02:27:36Z"), None
    )
    known_note = ""
    if known:
        mapped_ok = known["evidence_class"] in (
            "POOL_REJECTION_MIXED_WALL_REACTION",
            "POOL_REJECTION_WITH_ABSORPTION_EVIDENCE",
        )
        known_note = (
            f"Known 02:27:36 → {known['evidence_class']} "
            f"(committed Einzelfall evidence MIXED_WALL_REACTION; "
            f"mapped_family_ok={mapped_ok}; reaction_ts={known['reaction_first_available_ts']})"
        )

    manual_lines = [
        "# MANUAL_SIX_CASE_REVIEW",
        "",
        "Chart: Liquidity Location ON, Orderbook Walls ON, Trade Bubbles optional.",
        "Windows are UTC −10 / +10 minutes around cluster_start_ts.",
        "",
    ]
    for s in summaries:
        manual_lines += [
            f"## {s['case_id']} — {s['side']} {s['cluster_start_ts']}",
            "",
            f"- UTC timestamp: `{s['cluster_start_ts']}`",
            f"- Chart window: `{s['chart_window_start']}` → `{s['chart_window_end']}`",
            f"- Cluster: `{s['market_arrival_cluster_id']}`",
            f"- Pool component: [{s['component_lower_edge']}, {s['component_upper_edge']}]",
            f"- Entry-Edge: `{s['entry_edge']}` (ASK=lower / BID=upper)",
            f"- Start-Wall: `{s['wall_at_start_price']}` notional≈{s['wall_at_start_notional']} rank={s['wall_at_start_rank']} first_seen=`{s['wall_at_start_first_seen_class']}`",
            f"- Later wall: `{s['later_wall_price']}` first=`{s['later_wall_first_seen_ts']}`",
            f"- Evidence: `{s['evidence_class']}` | specific=`{s['specific_wall_reaction']}` | pool=`{s['pool_level_reaction']}`",
            "- Visual checks: approach into entry-edge; wall persistence vs cancel/move; aggressor bubbles at wall; reclaim vs acceptance beyond component.",
            "",
        ]
    (out / "MANUAL_SIX_CASE_REVIEW.md").write_text("\n".join(manual_lines) + "\n", encoding="utf-8")

    elapsed = time.perf_counter() - t0
    report = f"""# REPORT — LIQUIDITY_POOL_SIX_CASE_WALL_TRADE_REACTION_SAMPLE_V1

## Verdict

LIQUIDITY_POOL_SIX_CASE_WALL_TRADE_REACTION_SAMPLE_V1_COMPLETE

## Live safety

Read-only. No CH writes. No collector/dashboard changes. No commit/push.

## Runtime

{elapsed:.1f}s

## Queries / windows

- cases={len(summaries)}
- trade_queries={sum(q['trades_table_query'] for q in queries)}
- raw windows loaded={len(queries)} (pre 30s + up to 300s post start)

## Selection

See `selection_manifest.json`. Forced known ASK `2026-08-26T02:27:36Z` + deterministic start-feature sort.

## Cases

{chr(10).join(f"- {s['case_id']}: {s['side']} {s['cluster_start_ts']} wall={s['wall_at_start_price']} → {s['evidence_class']}" for s in summaries)}

## Known-case note

{known_note}

## Prefix

all EXACT_PREFIX_PARITY = {all(p['prefix_parity']=='EXACT_PREFIX_PARITY' for p in prefixes)}

## No trading edge / PnL

This sample does not claim edge, expectancy, or strategy readiness.
"""
    (out / "REPORT.md").write_text(report, encoding="utf-8")
    print(json.dumps({"elapsed_s": round(elapsed, 1), "cases": len(summaries), "known": known_note}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
