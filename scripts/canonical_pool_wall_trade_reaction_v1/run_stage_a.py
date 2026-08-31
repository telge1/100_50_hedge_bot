#!/usr/bin/env python3
"""Stage A: CLEAR_POOL_SELECTION_RULE_V1 candidate filter (raw zone depth as A7 SoT).

Does NOT use 1s wall_in_pool as the book-fill gate.
"""

from __future__ import annotations

import argparse
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
    STAGE_A_A7_MIN_ZONE_LEVELS,
    STAGE_A_BOOK_FILL_SOT,
    STAGE_A_MIN_P,
    STAGE_A_TIMEFRAMES,
)
from orderbook_analyse.canonical_pool_wall_trade_reaction_v1.stage_a import (  # noqa: E402
    A7_MIN_ZONE_LEVELS,
    select_stage_a_candidates,
)

RAW_ROOT = OA / "data/orderbook_raw_shadow/ob200_v3"
EP_DEFAULT = OA / "results/canonical_pool_wall_trade_reaction_v1/episode_reactions.csv"
OUT_DEFAULT = OA / "results/canonical_pool_selection_stage_a_v1"
RAW_START = "2026-08-24T00:00:00Z"
RAW_END = "2026-08-28T16:26:23Z"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--episodes", default=str(EP_DEFAULT))
    ap.add_argument("--out-dir", default=str(OUT_DEFAULT))
    ap.add_argument("--raw-root", default=str(RAW_ROOT))
    ap.add_argument("--raw-start", default=RAW_START)
    ap.add_argument("--raw-end", default=RAW_END)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()

    eps = pd.read_csv(args.episodes)
    print(f"rule={RULE_ID} episodes={len(eps)}", flush=True)
    print(ONE_LINE_DE, flush=True)
    print(
        f"A7 SoT={STAGE_A_BOOK_FILL_SOT} min_levels={STAGE_A_A7_MIN_ZONE_LEVELS} "
        f"TF={STAGE_A_TIMEFRAMES} P≥{STAGE_A_MIN_P}",
        flush=True,
    )

    passes, rejects = select_stage_a_candidates(
        eps,
        raw_root=Path(args.raw_root),
        raw_start=args.raw_start,
        raw_end=args.raw_end,
        limit=args.limit,
    )

    passes.to_csv(out / "stage_a_candidates.csv", index=False)
    rejects.to_csv(out / "stage_a_rejects.csv", index=False)

    fail_reasons = Counter(rejects["a7_fail_reason"].fillna("unknown")) if len(rejects) else Counter()
    proxy_yes_among_pass = (
        int((passes["wall_in_pool_1s_proxy"] == "YES").sum()) if len(passes) else 0
    )
    proxy_no_among_pass = (
        int((passes["wall_in_pool_1s_proxy"] != "YES").sum()) if len(passes) else 0
    )

    summary = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rule_id": RULE_ID,
        "book_fill_sot": STAGE_A_BOOK_FILL_SOT,
        "a7_min_zone_levels": A7_MIN_ZONE_LEVELS,
        "raw_window": {"start": args.raw_start, "end": args.raw_end},
        "n_a1_a6_scanned": int(len(passes) + len(rejects)),
        "n_a7_pass": int(len(passes)),
        "n_a7_reject": int(len(rejects)),
        "a7_fail_reason_counts": dict(fail_reasons),
        "among_a7_pass_1s_wall_proxy_YES": proxy_yes_among_pass,
        "among_a7_pass_1s_wall_proxy_NO": proxy_no_among_pass,
        "median_a7_zone_level_count": float(passes["a7_zone_level_count"].median())
        if len(passes)
        else None,
        "median_a7_zone_notional": float(passes["a7_zone_notional"].median()) if len(passes) else None,
        "elapsed_s": round(time.perf_counter() - t0, 1),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")

    lines = [
        "# Stage A — CLEAR_POOL_SELECTION_RULE_V1",
        "",
        f"Generated: `{summary['generated_at']}`",
        f"Rule: `{RULE_ID}`",
        "",
        f"> {ONE_LINE_DE}",
        "",
        "## Gate",
        "",
        f"- A1–A6: BTCUSDT · TF ∈ {list(STAGE_A_TIMEFRAMES)} · P≥{STAGE_A_MIN_P} · mid inside zone",
        f"- A7 SoT: **{STAGE_A_BOOK_FILL_SOT}** (≥{A7_MIN_ZONE_LEVELS} levels + qty>0 in `[lower,upper]`)",
        "- **Not used as A7:** 1s `wall_in_pool` dominant-wall proxy",
        "",
        "## Counts",
        "",
        f"- A1–A6 scanned in raw window: **{summary['n_a1_a6_scanned']}**",
        f"- A7 pass (candidates): **{summary['n_a7_pass']}**",
        f"- A7 reject: **{summary['n_a7_reject']}**",
        f"- Among pass, 1s proxy YES: **{proxy_yes_among_pass}** / NO: **{proxy_no_among_pass}**",
        f"- Median zone levels at touch: **{summary['median_a7_zone_level_count']}**",
        f"- Median zone notional at touch: **{summary['median_a7_zone_notional']}**",
        "",
        "### A7 reject reasons",
        "",
    ]
    for k, v in sorted(fail_reasons.items(), key=lambda kv: (-kv[1], str(kv[0]))):
        lines.append(f"- `{k}`: **{v}**")
    lines += [
        "",
        "## Files",
        "",
        "- `stage_a_candidates.csv` — feed these to Stage B",
        "- `stage_a_rejects.csv`",
        "- `summary.json`",
        "",
    ]
    (out / "REPORT.md").write_text("\n".join(lines))
    print(json.dumps(summary, indent=2), flush=True)
    print(f"DONE → {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
