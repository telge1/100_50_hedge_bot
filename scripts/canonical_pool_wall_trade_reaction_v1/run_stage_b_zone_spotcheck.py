#!/usr/bin/env python3
"""Visual spot-check for Stage-B zone-depth V2 (HELD / EATEN / PULLED)."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

OA = Path("/home/telgenbuescher/projects/orderbook_analyse")
STAGE_B = OA / "results/canonical_pool_selection_stage_b_zone_v2_contact/stage_b_summary.csv"
TIMELINES = OA / "results/canonical_pool_selection_stage_b_zone_v2_contact/stage_b_timelines.csv"
OUT_DEFAULT = OA / "results/canonical_pool_selection_stage_b_zone_v2_contact_spotcheck"
SEED = 42
PER_LABEL = 8


def stratified_sample(df: pd.DataFrame, n: int, seed: int) -> pd.DataFrame:
    cells = []
    for (_, _), g in df.groupby(["timeframe", "side"], sort=True):
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
        return {"ok": False}
    ts = pd.to_datetime(tl["second"], utc=True)
    mid = tl["mid"].astype(float)
    zone_n = tl["zone_notional"].astype(float)
    zone_l = tl["zone_level_count"].astype(float)
    side = str(row["side"]).upper()
    lo = float(row["component_lower_edge"])
    hi = float(row["component_upper_edge"])
    touch = pd.Timestamp(row["cluster_start_ts"])
    if touch.tzinfo is None:
        touch = touch.tz_localize("UTC")

    fig, axes = plt.subplots(
        3, 1, figsize=(10, 8), sharex=True, gridspec_kw={"height_ratios": [2.2, 1.3, 1.0]}
    )
    ax0, ax1, ax2 = axes
    ax0.plot(ts, mid, color="#1f2937", lw=1.1, label="mid")
    ax0.axhspan(lo, hi, color="#93c5fd", alpha=0.35, label="pool zone")
    ax0.axvline(touch, color="#dc2626", ls="--", lw=1.0, label="touch")
    ax0.set_ylabel("price")
    ax0.set_title(
        f"{row['case_id']}  {row['zone_label']}  {row['timeframe']} {side}  "
        f"P={row.get('member_pool_count')}  {row['pool_id']}"
    )
    ax0.legend(loc="upper right", fontsize=8, frameon=False)

    ax1.plot(ts, zone_n, color="#7c3aed", lw=1.2, label="zone notional (aggregate)")
    ax1.axvline(touch, color="#dc2626", ls="--", lw=1.0)
    ax1.set_ylabel("zone $")
    ax1.legend(loc="upper right", fontsize=8, frameon=False)

    ax2.plot(ts, zone_l, color="#0f766e", lw=1.1, label="zone level count")
    ax2.axvline(touch, color="#dc2626", ls="--", lw=1.0)
    ax2.set_ylabel("levels")
    ax2.set_xlabel("UTC")
    ax2.legend(loc="upper right", fontsize=8, frameon=False)

    fig.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, dpi=110)
    plt.close(fig)

    dec = tl[tl["phase"] == "DECISION"] if "phase" in tl.columns else tl[ts >= touch]
    return {
        "ok": True,
        "png": out_png.name,
        "zone_n0": float(row.get("zone_notional_at_touch") or 0),
        "zone_n_end": float(row.get("zone_notional_end") or 0),
        "drop_frac_end": row.get("zone_drop_frac_end"),
        "trade_cover": row.get("trade_cover_of_drop"),
        "label_reason": row.get("label_reason"),
        "mid_reclaimed": row.get("mid_reclaimed"),
        "mid_accepted_beyond": row.get("mid_accepted_beyond"),
        "n_dec": int(len(dec)),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default=str(OUT_DEFAULT))
    ap.add_argument("--per-label", type=int, default=PER_LABEL)
    ap.add_argument("--seed", type=int, default=SEED)
    args = ap.parse_args()
    out = Path(args.out_dir)
    png_dir = out / "png"
    out.mkdir(parents=True, exist_ok=True)

    summary = pd.read_csv(STAGE_B)
    timelines = pd.read_csv(TIMELINES)
    parts = []
    for lab, seed_off in (("ZONE_HELD", 0), ("ZONE_EATEN", 1), ("ZONE_PULLED", 2)):
        sub = summary[summary["zone_label"] == lab]
        if len(sub) == 0:
            continue
        parts.append(
            stratified_sample(sub, min(args.per_label, len(sub)), args.seed + seed_off).assign(
                spot_bucket=lab.replace("ZONE_", "")
            )
        )
    sample = pd.concat(parts, ignore_index=True)

    rows = []
    for i, row in enumerate(sample.to_dict(orient="records"), start=1):
        cid = row["case_id"]
        print(f"spot {i}/{len(sample)} {cid} {row['zone_label']}…", flush=True)
        tl = timelines[timelines["case_id"] == cid]
        png = png_dir / f"{cid}_{row['zone_label']}.png"
        meta = plot_case(row, tl, png)
        rows.append(
            {
                "spot_idx": i,
                "case_id": cid,
                "spot_bucket": row["spot_bucket"],
                "zone_label": row["zone_label"],
                "label_reason": row.get("label_reason"),
                "pool_id": row["pool_id"],
                "timeframe": row["timeframe"],
                "side": row["side"],
                "cluster_start_ts": row["cluster_start_ts"],
                "drop_frac_end": row.get("zone_drop_frac_end"),
                "trade_cover_of_drop": row.get("trade_cover_of_drop"),
                "trade_into_zone_notional": row.get("trade_into_zone_notional"),
                "mid_reclaimed": row.get("mid_reclaimed"),
                "mid_accepted_beyond": row.get("mid_accepted_beyond"),
                "png": meta.get("png"),
                **{k: meta.get(k) for k in ("ok", "zone_n0", "zone_n_end")},
            }
        )

    spot = pd.DataFrame(rows)
    spot.to_csv(out / "spotcheck_shortlist.csv", index=False)

    cards = []
    for r in rows:
        cards.append(
            f"""
            <figure class="card">
              <figcaption>
                <b>{r['case_id']}</b> · {r['zone_label']}<br/>
                {r['timeframe']} {r['side']} · reason={r['label_reason']}<br/>
                drop_end={r.get('drop_frac_end')} · trade_cover={r.get('trade_cover_of_drop')}<br/>
                reclaim={r.get('mid_reclaimed')} · beyond={r.get('mid_accepted_beyond')}<br/>
                {r['cluster_start_ts']} · {r['pool_id']}
              </figcaption>
              <img src="png/{r['png']}" loading="lazy"/>
            </figure>"""
        )
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"/>
<title>Stage-B zone-depth spot-check</title>
<style>
body{{font-family:ui-sans-serif,system-ui,sans-serif;margin:24px;background:#0b1220;color:#e5e7eb}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(460px,1fr));gap:18px}}
.card{{margin:0;background:#111827;border:1px solid #1f2937;border-radius:10px;overflow:hidden}}
.card img{{width:100%;display:block;background:#fff}}
figcaption{{padding:10px 12px;font-size:13px;line-height:1.35}}
.meta{{opacity:.85;margin-bottom:16px}}
</style></head><body>
<h1>Stage-B zone-depth V2 spot-check</h1>
<p class="meta">n={len(rows)} · aggregate zone notional/levels · no entry/PnL
<br/>Top mid+zone · Mid zone $ · Bottom zone levels</p>
<div class="grid">{''.join(cards)}</div>
</body></html>"""
    (out / "index.html").write_text(html)

    # Separation diagnostics
    full = summary.copy()
    diag = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "n_spot": int(len(rows)),
        "label_counts_full": full["zone_label"].value_counts().to_dict(),
        "median_drop_frac_end_by_label": full.groupby("zone_label")["zone_drop_frac_end"]
        .median()
        .to_dict(),
        "median_trade_cover_by_label": full.groupby("zone_label")["trade_cover_of_drop"]
        .median()
        .to_dict(),
        "beyond_rate_by_label": full.groupby("zone_label")["mid_accepted_beyond"].mean().to_dict(),
        "reclaim_rate_by_label": full.groupby("zone_label")["mid_reclaimed"].mean().to_dict(),
        "index_html": str(out / "index.html"),
    }
    (out / "summary.json").write_text(json.dumps(diag, indent=2) + "\n")
    print(json.dumps(diag, indent=2), flush=True)
    print(f"DONE → {out / 'index.html'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
