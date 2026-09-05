#!/usr/bin/env python3
"""Stage B V2: aggregate zone-depth labels on Stage-A A7 candidates."""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

OA = Path("/home/telgenbuescher/projects/orderbook_analyse")
sys.path.insert(0, str(OA / "src"))

from orderbook_analyse.canonical_pool_wall_trade_reaction_v1.selection_rule_v1 import (  # noqa: E402
    ONE_LINE_DE,
    RULE_ID,
)
from orderbook_analyse.canonical_pool_wall_trade_reaction_v1.stage_b_zone_depth import (  # noqa: E402
    DROP_HELD_MAX,
    DROP_MATERIAL,
    POST_DECISION_S,
    STAGE_B_VERSION,
    TRADE_COVER_EATEN,
    TRADE_COVER_PULLED_MAX,
    run_stage_b_zone_depth,
)

RAW_ROOT = OA / "data/orderbook_raw_shadow/ob200_v3"
IN_DEFAULT = OA / "results/canonical_pool_selection_stage_a_v1/stage_a_candidates.csv"
OUT_DEFAULT = OA / "results/canonical_pool_selection_stage_b_zone_v2_contact"


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
    ap.add_argument("--decision-s", type=int, default=POST_DECISION_S)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()

    cands = pd.read_csv(args.candidates)
    if "a7_pass" in cands.columns:
        cands = cands[cands["a7_pass"] == True]  # noqa: E712
    cands = cands.sort_values("first_touch_ts")
    if args.limit and args.limit > 0:
        cands = cands.head(args.limit)

    print(f"rule={RULE_ID} version={STAGE_B_VERSION} n={len(cands)}", flush=True)
    print(ONE_LINE_DE, flush=True)

    summaries, timelines = run_stage_b_zone_depth(
        cands, raw_root=Path(args.raw_root), decision_s=args.decision_s
    )
    write_csv(out / "stage_b_summary.csv", summaries)
    write_csv(out / "stage_b_timelines.csv", timelines)

    zone_counts = Counter(s.get("zone_label") for s in summaries)
    reason_counts = Counter(s.get("label_reason") for s in summaries)
    summary = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rule_id": RULE_ID,
        "stage_b_version": STAGE_B_VERSION,
        "decision_window_s": args.decision_s,
        "thresholds": {
            "DROP_HELD_MAX": DROP_HELD_MAX,
            "DROP_MATERIAL": DROP_MATERIAL,
            "TRADE_COVER_EATEN": TRADE_COVER_EATEN,
            "TRADE_COVER_PULLED_MAX": TRADE_COVER_PULLED_MAX,
        },
        "n_candidates": int(len(cands)),
        "zone_label_counts": dict(zone_counts),
        "label_reason_counts": dict(reason_counts),
        "elapsed_s": round(time.perf_counter() - t0, 1),
        "candidates_file": str(args.candidates),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")

    lines = [
        f"# Stage B zone-depth — `{STAGE_B_VERSION}`",
        "",
        f"Generated: `{summary['generated_at']}`",
        f"Rule: `{RULE_ID}`",
        "",
        f"> {ONE_LINE_DE}",
        "",
        "## Method",
        "",
        "- Input: Stage-A A7 pass (raw zone fill)",
        "- Observe **aggregate** resting notional/qty/levels inside `[lower,upper]`",
        f"- Decision window: **{args.decision_s}s** post-touch + `public_trades_canonical`",
        "- Labels: HELD / EATEN / PULLED / UNKNOWN — no entry/PnL",
        "",
        "## Counts",
        "",
        f"- Candidates: **{summary['n_candidates']}**",
        "",
    ]
    for k, v in sorted(zone_counts.items(), key=lambda kv: (-kv[1], str(kv[0]))):
        lines.append(f"- `{k}`: **{v}**")
    lines += ["", "### reasons", ""]
    for k, v in sorted(reason_counts.items(), key=lambda kv: (-kv[1], str(kv[0]))):
        lines.append(f"- `{k}`: **{v}**")
    lines += ["", "## Files", "", "- `stage_b_summary.csv`", "- `stage_b_timelines.csv`", ""]
    (out / "REPORT.md").write_text("\n".join(lines))
    print(json.dumps(summary, indent=2), flush=True)
    print(f"DONE → {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
