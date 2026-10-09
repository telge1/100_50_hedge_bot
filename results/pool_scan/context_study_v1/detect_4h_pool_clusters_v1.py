"""Detect 4h liquidity pool clusters by separation gap.

The goal is to group nearby pools into one cluster and only start a new
cluster once the next pool is clearly separated. This study keeps the
existing pool engine unchanged and only post-processes the pools it already
produces.
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT_ROWS = ROOT / "pool_clusters_4h_v1.csv"
OUT_METHOD = ROOT / "pool_clusters_4h_method_v1.json"

LOAD_START = datetime(2026, 6, 1, tzinfo=timezone.utc)
LOAD_END = datetime(2026, 7, 10, tzinfo=timezone.utc)
SYMBOLS = ("XRPUSDT", "ADAUSDT", "DOGEUSDT")
CLUSTER_GAP_PCT = 0.5
CLUSTER_MAX_SPAN_PCT = 3.0


def load_symbol(symbol: str):
    repo = Path(__file__).resolve().parents[3]
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    from pool_pattern.market import ensure_paths, load_4h
    from pool_scan.clock import available_at, break_known_at

    ensure_paths()
    bars, pools = load_4h(symbol, LOAD_START, LOAD_END)
    rows = []
    for pool in pools:
        if pool.side not in ("lower", "upper"):
            continue
        known = available_at(pool)
        if known is None:
            continue
        rows.append({
            "pool_id": pool.pool_id,
            "side": pool.side,
            "bottom": float(pool.bottom_price),
            "top": float(pool.top_price),
            "known": known,
            "source": pool.source_timestamp,
            "break_at": break_known_at(pool),
        })
    return bars, rows


def cluster_side(pools: list[dict], side: str) -> list[dict]:
    rows = [pool for pool in pools if pool["side"] == side]
    rows.sort(key=lambda pool: (pool["bottom"], pool["top"], pool["known"]))
    clusters: list[dict] = []
    current: list[dict] = []
    cluster_top = None
    cluster_start_bottom = None

    def flush() -> None:
        nonlocal current, cluster_top, cluster_start_bottom
        if not current:
            return
        ordered = sorted(current, key=lambda pool: pool["known"])
        bottom_min = min(pool["bottom"] for pool in current)
        top_max = max(pool["top"] for pool in current)
        clusters.append({
            "side": side,
            "start_known": min(pool["known"] for pool in current),
            "end_known": max(pool["known"] for pool in current),
            "pool_count": len(current),
            "bottom_min": bottom_min,
            "top_max": top_max,
            "span_pct": round((top_max - bottom_min) / bottom_min * 100.0, 4) if bottom_min > 0 else "",
            "pools": ordered,
        })
        current = []
        cluster_top = None
        cluster_start_bottom = None

    for pool in rows:
        if not current:
            current = [pool]
            cluster_top = pool["top"]
            cluster_start_bottom = pool["bottom"]
            continue
        gap_pct = (pool["bottom"] - cluster_top) / cluster_top * 100.0 if cluster_top else None
        prospective_top = max(cluster_top, pool["top"])
        prospective_span = (prospective_top - cluster_start_bottom) / cluster_start_bottom * 100.0 if cluster_start_bottom else None
        if gap_pct is not None and gap_pct <= CLUSTER_GAP_PCT and prospective_span is not None and prospective_span <= CLUSTER_MAX_SPAN_PCT:
            current.append(pool)
            cluster_top = prospective_top
        else:
            flush()
            current = [pool]
            cluster_top = pool["top"]
            cluster_start_bottom = pool["bottom"]
    flush()
    for index, cluster in enumerate(clusters):
        cluster["cluster_index"] = index
        cluster["next_gap_pct"] = ""
        if index + 1 < len(clusters):
            nxt = clusters[index + 1]
            cluster["next_gap_pct"] = round((nxt["bottom_min"] - cluster["top_max"]) / cluster["top_max"] * 100.0, 4)
    return clusters


def main() -> None:
    for path in (OUT_ROWS, OUT_METHOD):
        if path.exists():
            raise SystemExit(f"refuse overwrite {path}")

    rows = []
    summary = {}
    for symbol in SYMBOLS:
        _, pools = load_symbol(symbol)
        for side in ("lower", "upper"):
            clusters = cluster_side(pools, side)
            for cluster in clusters:
                row = {
                    "symbol": symbol,
                    "timeframe": "4h",
                    "side": side,
                    "cluster_index": cluster["cluster_index"],
                    "start_known": cluster["start_known"].isoformat(),
                    "end_known": cluster["end_known"].isoformat(),
                    "pool_count": cluster["pool_count"],
                    "bottom_min": cluster["bottom_min"],
                    "top_max": cluster["top_max"],
                    "span_pct": cluster["span_pct"],
                    "next_gap_pct": cluster["next_gap_pct"],
                    "pool_ids": "|".join(pool["pool_id"] for pool in cluster["pools"]),
                }
                rows.append(row)
                summary.setdefault(symbol, {}).setdefault(side, 0)
                summary[symbol][side] += 1
    rows.sort(key=lambda row: (row["symbol"], row["side"], row["start_known"]))
    with OUT_ROWS.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    OUT_METHOD.write_text(json.dumps({
        "script": Path(__file__).name,
        "timeframe": "4h",
        "cluster_gap_pct": CLUSTER_GAP_PCT,
        "cluster_max_span_pct": CLUSTER_MAX_SPAN_PCT,
        "rule": "Pools of the same side are merged while the next pool bottom is within the current cluster top plus the bridge gap.",
        "summary": summary,
    }, indent=2) + "\n")
    print("clusters", summary)


if __name__ == "__main__":
    main()
