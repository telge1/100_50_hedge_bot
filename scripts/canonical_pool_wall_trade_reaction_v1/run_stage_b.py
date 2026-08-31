#!/usr/bin/env python3
"""Stage B: six-case OB tracker on Stage-A (A7 raw zone depth) candidates.

Input must be `stage_a_candidates.csv` from run_stage_a.py — not the old
1s wall_in_pool Strong shortlist.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

OA = Path("/home/telgenbuescher/projects/orderbook_analyse")
sys.path.insert(0, str(OA / "src"))

from orderbook_analyse.canonical_pool_wall_trade_reaction_v1.selection_rule_v1 import (  # noqa: E402
    ONE_LINE_DE,
    RULE_ID,
)
from orderbook_analyse.canonical_pool_wall_trade_reaction_v1.stage_b import (  # noqa: E402
    EVIDENCE_TO_ZONE,
    run_stage_b_on_candidates,
)
import pandas as pd  # noqa: E402

RAW_ROOT = OA / "data/orderbook_raw_shadow/ob200_v3"
IN_DEFAULT = OA / "results/canonical_pool_selection_stage_a_v1/stage_a_candidates.csv"
OUT_DEFAULT = OA / "results/canonical_pool_selection_stage_b_v1"


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates", default=str(IN_DEFAULT))
    ap.add_argument("--out-dir", default=str(OUT_DEFAULT))
    ap.add_argument("--raw-root", default=str(RAW_ROOT))
    ap.add_argument("--limit", type=int, default=0, help="0=all")
    args = ap.parse_args()

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()

    cands = pd.read_csv(args.candidates)
    if "a7_pass" in cands.columns:
        before = len(cands)
        cands = cands[cands["a7_pass"] == True]  # noqa: E712
        if len(cands) < before:
            print(f"dropped {before - len(cands)} rows without a7_pass", flush=True)
    cands["touch_dt"] = pd.to_datetime(cands["first_touch_ts"], utc=True)
    cands = cands.sort_values("first_touch_ts")
    if args.limit and args.limit > 0:
        cands = cands.head(args.limit)

    print(f"rule={RULE_ID} candidates={len(cands)} (Stage-A A7 raw zone)", flush=True)
    print(ONE_LINE_DE, flush=True)

    summaries, timelines, prefixes = run_stage_b_on_candidates(
        cands, raw_root=Path(args.raw_root)
    )

    write_csv(out / "stage_b_summary.csv", summaries)
    write_csv(out / "stage_b_timelines.csv", timelines)
    write_csv(out / "stage_b_prefix_parity.csv", prefixes)

    zone_counts = Counter(s.get("zone_label") for s in summaries)
    evidence_counts = Counter(s.get("evidence_class") for s in summaries)
    n_wall = sum(1 for s in summaries if s.get("zone_wall_discovered"))

    summary = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rule_id": RULE_ID,
        "n_candidates": int(len(cands)),
        "n_audited": len(summaries),
        "n_zone_wall_discovered": n_wall,
        "zone_label_counts": dict(zone_counts),
        "evidence_class_counts": dict(evidence_counts),
        "evidence_to_zone_map": EVIDENCE_TO_ZONE,
        "elapsed_s": round(time.perf_counter() - t0, 1),
        "candidates_file": str(args.candidates),
        "candidates_gate": "stage_a_a7_raw_ob200_zone_depth",
        "reused_auditor": "liquidity_pool_six_case_wall_trade_reaction_sample_v1.audit_case",
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")

    lines = [
        "# Stage B — CLEAR_POOL_SELECTION_RULE_V1",
        "",
        f"Generated: `{summary['generated_at']}`",
        f"Rule: `{RULE_ID}`",
        "",
        f"> {ONE_LINE_DE}",
        "",
        "## Setup",
        "",
        "- Candidates: **Stage A A7 pass** (`stage_a_candidates.csv`, raw zone depth)",
        "- Not input: old 1s `wall_in_pool` Strong shortlist",
        "- Tracker: reused `audit_cluster_case` (Raw OB200 + `public_trades_canonical`)",
        "- Wall anchor = strongest level inside A7-confirmed zone at touch",
        "- No entry / PnL",
        "",
        "## Counts",
        "",
        f"- Candidates: **{summary['n_candidates']}**",
        f"- Zone wall found at touch: **{n_wall}**",
        "",
        "### zone_label",
        "",
    ]
    for k, v in sorted(zone_counts.items(), key=lambda kv: (-kv[1], str(kv[0]))):
        lines.append(f"- `{k}`: **{v}**")
    lines += ["", "### evidence_class (six-case)", ""]
    for k, v in sorted(evidence_counts.items(), key=lambda kv: (-kv[1], str(kv[0]))):
        lines.append(f"- `{k}`: **{v}** → `{EVIDENCE_TO_ZONE.get(k, '?')}`")
    lines += [
        "",
        "## Files",
        "",
        "- `stage_b_summary.csv`",
        "- `stage_b_timelines.csv`",
        "- `summary.json`",
        "",
    ]
    (out / "REPORT.md").write_text("\n".join(lines))
    print(json.dumps(summary, indent=2), flush=True)
    print(f"DONE → {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
