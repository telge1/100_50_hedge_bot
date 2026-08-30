#!/usr/bin/env python3
"""Thin CLI: Liquidity Location chart pools via liquidity_pool_signal.

All pool logic lives in orderbook_analyse.liquidity_pool_signal.
This script only parses args, calls the package, and writes outputs.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

OA_ROOT = Path("/home/telgenbuescher/projects/orderbook_analyse")
if str(OA_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(OA_ROOT / "src"))

from orderbook_analyse.liquidity_pool_signal import (  # noqa: E402
    chart_lookback_start,
    chart_pool_engine,
    export_snapshot,
    get_engine_function,
    parity_pair,
)


def _utc(ts: str | datetime) -> datetime:
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc)
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(timezone.utc)


def _iso_z(dt: datetime) -> str:
    return _utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in keys})


def main(argv: list[str] | None = None) -> int:
    eng = get_engine_function()
    assert eng is chart_pool_engine()
    assert eng.__module__ == "indicators.liquidity_location.engine"

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--timeframe", default="5m")
    p.add_argument("--window-start", default="2026-08-25T20:00:00Z")
    p.add_argument("--window-end", default="2026-08-26T12:30:00Z")
    p.add_argument(
        "--out-dir",
        default=str(OA_ROOT / "results" / "liquidity_location_chart_engine_direct_reuse_v1"),
    )
    args = p.parse_args(argv)

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    snapshots = [
        "2026-08-25T21:00:00Z",
        "2026-08-25T23:00:00Z",
        "2026-08-26T02:30:00Z",
        "2026-08-26T04:48:00Z",
        "2026-08-26T08:30:00Z",
        "2026-08-26T11:00:00Z",
    ]
    ws = _utc(args.window_start)
    we = _utc(args.window_end)

    meta = {
        "symbol": args.symbol,
        "timeframe": args.timeframe,
        "window_start": _iso_z(ws),
        "window_end": _iso_z(we),
        "engine": f"{eng.__module__}.{eng.__name__}",
        "cli_is_chart_engine": eng is chart_pool_engine(),
        "vp_ui_does_not_affect_lld": True,
        "snapshots": [],
    }

    all_rows: list[dict[str, Any]] = []
    snap_rows: list[dict[str, Any]] = []
    nearest_rows: list[dict[str, Any]] = []

    for s in snapshots:
        as_of = _utc(s)
        snap = export_snapshot(
            symbol=args.symbol,
            timeframe=args.timeframe,
            window_start=ws,
            as_of=as_of,
        )
        meta["snapshots"].append(
            {
                "as_of": snap["as_of"],
                "market_price": snap["market_price"],
                "n_ask_active": snap["n_ask_active"],
                "n_bid_active": snap["n_bid_active"],
                "market_pool_location": snap.get("market_pool_location"),
            }
        )
        for r in snap["active_pools"]:
            row = dict(r)
            row["snapshot_as_of"] = snap["as_of"]
            all_rows.append(row)
            snap_rows.append(
                {
                    "as_of": snap["as_of"],
                    "pool_id": r["pool_id"],
                    "side": r["side"],
                    "source_timeframe": r["source_timeframe"],
                    "lower_edge": r["lower_edge"],
                    "upper_edge": r["upper_edge"],
                    "created_ts": r["created_ts"],
                    "available_at": r["available_at"],
                    "invalidated_ts": r["invalidated_ts"],
                    "chart_color": r["chart_color"],
                    "strength": r["strength"],
                    "distance_to_market_bps_diagnostic": r["distance_to_market_bps_diagnostic"],
                    "market_price": snap["market_price"],
                }
            )
        n = snap["nearest"]
        nearest_rows.append(
            {
                "as_of": snap["as_of"],
                "market_price": snap["market_price"],
                "n_ask_active": snap["n_ask_active"],
                "n_bid_active": snap["n_bid_active"],
                "market_inside_pool": n["market_inside_pool"],
                "market_pool_location": n.get("market_pool_location"),
                "inside_pool_ids": json.dumps(n.get("inside_pool_ids") or []),
                "nearest_ask": json.dumps(n.get("nearest_ask_pool_above_market")),
                "nearest_bid": json.dumps(n.get("nearest_bid_pool_below_market")),
            }
        )

    parity_targets = [snapshots[0], snapshots[3]]
    parity_docs: dict[str, Any] = {}
    chart_norm_all: dict[str, Any] = {}
    cli_norm_all: dict[str, Any] = {}
    for s in parity_targets:
        as_of = _utc(s)
        pr = parity_pair(
            symbol=args.symbol,
            timeframe=args.timeframe,
            start=chart_lookback_start(as_of, args.timeframe),
            end=as_of,
        )
        parity_docs[s] = {
            "chart_payload_sha256": pr["chart_payload_sha256"],
            "cli_payload_sha256": pr["cli_payload_sha256"],
            "parity_pass": pr["parity_pass"],
            "engine_identity": pr["engine_identity"],
        }
        chart_norm_all[s] = pr["chart_payload_normalized"]
        cli_norm_all[s] = pr["cli_payload_normalized"]

    _write_csv(out / "all_chart_pools.csv", all_rows)
    _write_csv(out / "pool_snapshots.csv", snap_rows)
    _write_csv(out / "nearest_pools.csv", nearest_rows)
    (out / "chart_payload_normalized.json").write_text(
        json.dumps(chart_norm_all, indent=2, default=str), encoding="utf-8"
    )
    (out / "cli_payload_normalized.json").write_text(
        json.dumps(cli_norm_all, indent=2, default=str), encoding="utf-8"
    )
    all_pass = all(v["parity_pass"] for v in parity_docs.values())
    (out / "parity_fingerprint.json").write_text(
        json.dumps(
            {
                "parity_pass": all_pass,
                "by_snapshot": parity_docs,
                "invariant": {
                    "cli_pool_engine_function": f"{eng.__module__}.{eng.__name__}",
                    "chart_backend_pool_engine_function": f"{eng.__module__}.{eng.__name__}",
                    "identical_object": eng is chart_pool_engine(),
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(json.dumps({"out_dir": str(out), "parity_pass": all_pass, "meta": meta}, indent=2))
    return 0 if all_pass else 2


if __name__ == "__main__":
    raise SystemExit(main())
