#!/usr/bin/env python3
"""Visual spot-check atlas for Stage-B ZONE_HELD / ZONE_PULLED cases.

Outcome-blind for entry/PnL: shows mid vs pool band + tracked wall notional
+ aggressor notional around touch. Writes PNG panels + HTML index + shortlist CSV.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

OA = Path("/home/telgenbuescher/projects/orderbook_analyse")
STAGE_A = OA / "results/canonical_pool_selection_stage_a_v1/stage_a_candidates.csv"
STAGE_B = OA / "results/canonical_pool_selection_stage_b_v1/stage_b_summary.csv"
TIMELINES = OA / "results/canonical_pool_selection_stage_b_v1/stage_b_timelines.csv"
OUT_DEFAULT = OA / "results/canonical_pool_selection_stage_b_spotcheck_v1"
SEED = 42
N_HELD = 12
N_PULLED = 12


def _utc_iso(ts) -> str:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def stratified_sample(df: pd.DataFrame, n: int, seed: int) -> pd.DataFrame:
    """Round-robin across timeframe×side cells until n rows."""
    cells = []
    for (tf, side), g in df.groupby(["timeframe", "side"], sort=True):
        cells.append(g.sample(frac=1.0, random_state=seed).reset_index(drop=True))
    if not cells:
        return df.head(0)
    out = []
    i = 0
    while len(out) < n and any(len(c) > 0 for c in cells):
        c = cells[i % len(cells)]
        if len(c) > 0:
            out.append(c.iloc[0].to_dict())
            cells[i % len(cells)] = c.iloc[1:].reset_index(drop=True)
        i += 1
        if i > n * 20:
            break
    return pd.DataFrame(out)


def plot_case(row: dict, tl: pd.DataFrame, out_png: Path) -> dict:
    tl = tl.sort_values("second_ms").copy()
    if tl.empty:
        return {"ok": False, "reason": "empty_timeline"}

    ts = pd.to_datetime(tl["second"], utc=True)
    mid = tl["mid"].astype(float)
    wall = tl["wall_at_start_notional"].astype(float)
    buy = tl["aggressive_buy_notional"].astype(float).fillna(0)
    sell = tl["aggressive_sell_notional"].astype(float).fillna(0)
    side = str(row["side"]).upper()
    lo = float(row["component_lower_edge"])
    hi = float(row["component_upper_edge"])
    touch = pd.Timestamp(row["cluster_start_ts"])
    if touch.tzinfo is None:
        touch = touch.tz_localize("UTC")

    # Into-zone aggressor: BID pool attacked by sells; ASK by buys
    into = sell if side == "BID" else buy
    away = buy if side == "BID" else sell

    fig, axes = plt.subplots(3, 1, figsize=(10, 8), sharex=True, gridspec_kw={"height_ratios": [2.2, 1.2, 1.2]})
    ax0, ax1, ax2 = axes

    ax0.plot(ts, mid, color="#1f2937", lw=1.1, label="mid")
    ax0.axhspan(lo, hi, color="#93c5fd", alpha=0.35, label="pool zone")
    ax0.axvline(touch, color="#dc2626", ls="--", lw=1.0, label="touch")
    ax0.set_ylabel("price")
    ax0.set_title(
        f"{row['case_id']}  {row['zone_label']}  {row['timeframe']} {side}  "
        f"P={row.get('member_pool_count') or row.get('maximum_P')}  "
        f"{row['pool_id']}"
    )
    ax0.legend(loc="upper right", fontsize=8, frameon=False)

    ax1.plot(ts, wall, color="#7c3aed", lw=1.2, label="tracked wall notional")
    ax1.axvline(touch, color="#dc2626", ls="--", lw=1.0)
    ax1.set_ylabel("wall $")
    ax1.legend(loc="upper right", fontsize=8, frameon=False)

    ax2.fill_between(ts, into.cumsum(), color="#ea580c", alpha=0.55, label="cum aggressor into zone")
    ax2.fill_between(ts, away.cumsum(), color="#64748b", alpha=0.35, label="cum aggressor away")
    ax2.axvline(touch, color="#dc2626", ls="--", lw=1.0)
    ax2.set_ylabel("cum $")
    ax2.legend(loc="upper left", fontsize=8, frameon=False)
    ax2.set_xlabel("UTC")

    # quick optics metrics
    post = tl[tl["phase"] == "POST"]
    pre = tl[tl["phase"] == "PRE"]
    wall0 = float(wall.iloc[0]) if len(wall) else math.nan
    wall_touch = float(wall[ts >= touch].iloc[0]) if (ts >= touch).any() else math.nan
    wall_end = float(wall.iloc[-1]) if len(wall) else math.nan
    into_post = float(into[tl["phase"] == "POST"].sum()) if len(post) else 0.0
    mid_touch = float(mid[ts >= touch].iloc[0]) if (ts >= touch).any() else math.nan
    mid_end = float(mid.iloc[-1]) if len(mid) else math.nan
    # rejection heuristic: mid leaves zone back toward approach side
    front = hi if side == "BID" else lo
    back = lo if side == "BID" else hi
    if side == "BID":
        rejected = mid_end > front if mid_end == mid_end else False
        passed = mid_end < back if mid_end == mid_end else False
    else:
        rejected = mid_end < front if mid_end == mid_end else False
        passed = mid_end > back if mid_end == mid_end else False

    fig.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, dpi=110)
    plt.close(fig)

    return {
        "ok": True,
        "png": str(out_png.name),
        "wall0": wall0,
        "wall_touch": wall_touch,
        "wall_end": wall_end,
        "wall_drop_frac": (wall_touch - wall_end) / wall_touch if wall_touch and wall_touch > 0 else None,
        "into_post": into_post,
        "mid_touch": mid_touch,
        "mid_end": mid_end,
        "price_hint": "REJECT_HINT" if rejected else ("PASS_HINT" if passed else "INSIDE_OR_AMBIG"),
        "n_pre": int(len(pre)),
        "n_post": int(len(post)),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default=str(OUT_DEFAULT))
    ap.add_argument("--n-held", type=int, default=N_HELD)
    ap.add_argument("--n-pulled", type=int, default=N_PULLED)
    ap.add_argument("--seed", type=int, default=SEED)
    args = ap.parse_args()
    out = Path(args.out_dir)
    png_dir = out / "png"
    out.mkdir(parents=True, exist_ok=True)
    png_dir.mkdir(parents=True, exist_ok=True)

    summary = pd.read_csv(STAGE_B)
    a7 = pd.read_csv(STAGE_A)[
        ["pool_id", "a7_zone_level_count", "a7_zone_notional", "a7_zone_qty", "maximum_P", "lower", "upper"]
    ]
    summary = summary.merge(a7, on="pool_id", how="left")
    timelines = pd.read_csv(TIMELINES)

    held = summary[summary["zone_label"] == "ZONE_HELD"]
    pulled = summary[summary["zone_label"] == "ZONE_PULLED"]
    sample = pd.concat(
        [
            stratified_sample(held, args.n_held, args.seed).assign(spot_bucket="HELD"),
            stratified_sample(pulled, args.n_pulled, args.seed + 1).assign(spot_bucket="PULLED"),
        ],
        ignore_index=True,
    )

    rows = []
    for i, row in enumerate(sample.to_dict(orient="records"), start=1):
        cid = row["case_id"]
        print(f"spot {i}/{len(sample)} {cid} {row['zone_label']}…", flush=True)
        tl = timelines[timelines["case_id"] == cid]
        png = png_dir / f"{cid}_{row['zone_label']}.png"
        meta = plot_case(row, tl, png)
        rec = {
            "spot_idx": i,
            "case_id": cid,
            "spot_bucket": row["spot_bucket"],
            "zone_label": row["zone_label"],
            "evidence_class": row.get("evidence_class"),
            "pool_id": row["pool_id"],
            "timeframe": row["timeframe"],
            "side": row["side"],
            "cluster_start_ts": row["cluster_start_ts"],
            "lower": row.get("lower") or row.get("component_lower_edge"),
            "upper": row.get("upper") or row.get("component_upper_edge"),
            "maximum_P": row.get("maximum_P") or row.get("member_pool_count"),
            "a7_zone_level_count": row.get("a7_zone_level_count"),
            "a7_zone_notional": row.get("a7_zone_notional"),
            "reaction_1s_prior": row.get("reaction_1s_prior"),
            "png": meta.get("png"),
            **{k: meta.get(k) for k in ("ok", "wall_drop_frac", "into_post", "price_hint", "wall_touch", "wall_end", "mid_touch", "mid_end")},
            # research-charts style goto hint (manual)
            "goto_hint": _utc_iso(row["cluster_start_ts"]),
        }
        rows.append(rec)

    spot = pd.DataFrame(rows)
    spot.to_csv(out / "spotcheck_shortlist.csv", index=False)

    # HTML gallery
    cards = []
    for r in rows:
        cards.append(
            f"""
            <figure class="card">
              <figcaption>
                <b>{r['case_id']}</b> · {r['zone_label']} · {r['timeframe']} {r['side']}<br/>
                evidence={r['evidence_class']}<br/>
                price_hint={r.get('price_hint')} · into_post=${r.get('into_post'):,.0f}<br/>
                wall_drop={r.get('wall_drop_frac')}<br/>
                touch={r['goto_hint']} · {r['pool_id']}
              </figcaption>
              <img src="png/{r['png']}" loading="lazy"/>
            </figure>
            """
        )
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"/>
<title>Stage-B spot-check HELD vs PULLED</title>
<style>
body{{font-family:ui-sans-serif,system-ui,sans-serif;margin:24px;background:#0b1220;color:#e5e7eb}}
h1,h2{{font-weight:600}}
.meta{{opacity:.8;margin-bottom:20px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(460px,1fr));gap:18px}}
.card{{margin:0;background:#111827;border:1px solid #1f2937;border-radius:10px;overflow:hidden}}
.card img{{width:100%;display:block;background:#fff}}
figcaption{{padding:10px 12px;font-size:13px;line-height:1.35}}
.held{{outline:2px solid #16a34a33}}
.pulled{{outline:2px solid #ea580c33}}
</style></head><body>
<h1>Stage-B spot-check · CLEAR_POOL_SELECTION_RULE_V1</h1>
<p class="meta">n={len(rows)} · seed={args.seed} · generated {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}
<br/>Top: mid + pool zone · Mid: tracked wall notional · Bottom: cum aggressor into/away
<br/>No entry / PnL — visual label sanity only.</p>
<div class="grid">
{''.join(cards)}
</div>
</body></html>
"""
    (out / "index.html").write_text(html)

    summary_obj = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "n_total": int(len(rows)),
        "n_held": int((spot["spot_bucket"] == "HELD").sum()),
        "n_pulled": int((spot["spot_bucket"] == "PULLED").sum()),
        "price_hint_by_label": {
            lab: spot[spot["zone_label"] == lab]["price_hint"].value_counts().to_dict()
            for lab in ("ZONE_HELD", "ZONE_PULLED")
        },
        "median_into_post_by_label": {
            lab: float(spot[spot["zone_label"] == lab]["into_post"].median())
            for lab in ("ZONE_HELD", "ZONE_PULLED")
        },
        "out_dir": str(out),
        "index_html": str(out / "index.html"),
    }
    (out / "summary.json").write_text(json.dumps(summary_obj, indent=2) + "\n")
    print(json.dumps(summary_obj, indent=2), flush=True)
    print(f"DONE → {out / 'index.html'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
