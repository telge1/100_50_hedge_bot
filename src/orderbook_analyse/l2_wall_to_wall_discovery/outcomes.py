"""Horizon outcomes, controls, optional OI/liq context."""

from __future__ import annotations

import random
from collections import defaultdict
from datetime import datetime
from typing import Any

import pandas as pd

from orderbook_analyse.l2_wall_to_wall_discovery import COST_BPS, HORIZONS_S, NOTIONAL_USDT
from orderbook_analyse.l2_wall_to_wall_discovery.models import (
    sample_at,
    samples_between,
    side_adjusted_return_bps,
)
from orderbook_analyse.ob200_v3_raw_discovery.reconstruct import SampleRow
from orderbook_analyse.ob200_v3_raw_discovery.v3.sources import load_liquidations, load_oi_5s
from orderbook_analyse.ob200_v3_raw_discovery.v3.pipeline import oi_asof


def compute_horizon_outcomes(
    entries: list[dict[str, Any]],
    samples_by: dict[str, list[SampleRow]],
    ts_by: dict[str, list[int]],
    *,
    data_end_ms: int,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for e in entries:
        entry_at = int(e["entry_at"])
        mid0 = float(e["entry_mid"])
        pos = e["position_side"]
        samples = samples_by.get(e["symbol"], [])
        tss = ts_by.get(e["symbol"], [])
        for h in HORIZONS_S:
            t1 = entry_at + h * 1000
            complete = t1 <= data_end_ms
            path = samples_between(samples, tss, entry_at, t1) if complete else []
            if not complete or not path:
                out.append(
                    {
                        "signal_id": e["signal_id"],
                        "module": e["module"],
                        "variant": e["variant"],
                        "symbol": e["symbol"],
                        "position_side": pos,
                        "horizon_s": h,
                        "outcome_complete": False,
                        "forward_return_bps": None,
                        "mfe_bps": None,
                        "mae_bps": None,
                        "gross_pnl_1000": None,
                    }
                )
                continue
            mids = [s.mid for s in path]
            fwd = side_adjusted_return_bps(mid0, mids[-1], pos)
            rets = [side_adjusted_return_bps(mid0, m, pos) for m in mids]
            rets = [r for r in rets if r is not None]
            mfe = max(rets) if rets else None
            mae = min(rets) if rets else None
            out.append(
                {
                    "signal_id": e["signal_id"],
                    "module": e["module"],
                    "variant": e["variant"],
                    "symbol": e["symbol"],
                    "position_side": pos,
                    "horizon_s": h,
                    "outcome_complete": True,
                    "forward_return_bps": fwd,
                    "mfe_bps": mfe,
                    "mae_bps": mae,
                    "gross_pnl_1000": None if fwd is None else NOTIONAL_USDT * fwd / 10000.0,
                }
            )
    return out


def cost_summary(outcomes: list[dict[str, Any]], exits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    # horizon costs
    by: dict[tuple, list[float]] = defaultdict(list)
    for r in outcomes:
        if not r.get("outcome_complete") or r.get("forward_return_bps") is None:
            continue
        if r["horizon_s"] not in (300, 900, 3600):
            continue
        key = (r["module"], r["position_side"], r["horizon_s"])
        by[key].append(float(r["forward_return_bps"]))
    for key, vals in sorted(by.items()):
        for c in COST_BPS:
            nets = [v - c for v in vals]
            rows.append(
                {
                    "source": "horizon",
                    "module": key[0],
                    "position_side": key[1],
                    "horizon_s": key[2],
                    "exit_variant": None,
                    "cost_bps": c,
                    "n": len(vals),
                    "mean_gross_bps": sum(vals) / len(vals),
                    "mean_net_bps": sum(nets) / len(nets),
                    "hit_rate_net_gt0": sum(1 for x in nets if x > 0) / len(nets),
                    "mean_net_pnl_1000": NOTIONAL_USDT * (sum(nets) / len(nets)) / 10000.0,
                }
            )
    # exit costs
    by_e: dict[tuple, list[float]] = defaultdict(list)
    for r in exits:
        if not r.get("completed") or r.get("exit_return_bps") is None:
            continue
        # need module from signal — skip if missing; join externally in runner often
        by_e[(r.get("module") or "UNKNOWN", r.get("position_side") or "?", r["exit_variant"])].append(
            float(r["exit_return_bps"])
        )
    for key, vals in sorted(by_e.items()):
        for c in COST_BPS:
            nets = [v - c for v in vals]
            rows.append(
                {
                    "source": "exit",
                    "module": key[0],
                    "position_side": key[1],
                    "horizon_s": None,
                    "exit_variant": key[2],
                    "cost_bps": c,
                    "n": len(vals),
                    "mean_gross_bps": sum(vals) / len(vals),
                    "mean_net_bps": sum(nets) / len(nets),
                    "hit_rate_net_gt0": sum(1 for x in nets if x > 0) / len(nets),
                    "mean_net_pnl_1000": NOTIONAL_USDT * (sum(nets) / len(nets)) / 10000.0,
                }
            )
    return rows


def match_controls(
    entries: list[dict[str, Any]],
    samples_by: dict[str, list[SampleRow]],
    *,
    seed: int = 42,
    per_event: int = 2,
    max_events: int = 400,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rng = random.Random(seed)
    prim = list(entries)
    if len(prim) > max_events:
        prim = rng.sample(prim, max_events)

    forbidden: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for e in entries:
        a = int(e["entry_at"]) - 30_000
        b = int(e["entry_at"]) + 120_000
        forbidden[e["symbol"]].append((a, b))

    def merge(iv):
        if not iv:
            return []
        iv = sorted(iv)
        out = [[iv[0][0], iv[0][1]]]
        for a, b in iv[1:]:
            if a <= out[-1][1] + 1:
                out[-1][1] = max(out[-1][1], b)
            else:
                out.append([a, b])
        return out

    merged = {s: merge(v) for s, v in forbidden.items()}

    def blocked(sym, ts):
        for a, b in merged.get(sym, []):
            if a <= ts <= b:
                return True
            if a > ts:
                break
        return False

    free: dict[str, list[SampleRow]] = {}
    for sym, samples in samples_by.items():
        free[sym] = [s for s in samples if not s.warmup and not blocked(sym, s.ts_ms)]

    controls, quality = [], []
    used: dict[str, set[int]] = defaultdict(set)
    cid = 0
    for e in prim:
        sym = e["symbol"]
        pool = [s for s in free.get(sym, []) if s.ts_ms not in used[sym]]
        hour = (int(e["entry_at"]) // 3_600_000) % 24
        hour_pool = [s for s in pool if ((s.ts_ms // 3_600_000) % 24) == hour]
        use = hour_pool if len(hour_pool) >= per_event else pool
        if not use:
            quality.append({"signal_id": e["signal_id"], "n_controls": 0, "match_quality": "NONE"})
            continue
        picks = rng.sample(use, k=min(per_event, len(use)))
        for s in picks:
            used[sym].add(s.ts_ms)
            cid += 1
            controls.append(
                {
                    "control_id": f"ctrl_w2w_{seed}_{cid}",
                    "matched_to_signal_id": e["signal_id"],
                    "symbol": sym,
                    "position_side": e["position_side"],
                    "module": e["module"],
                    "entry_at": s.ts_ms,
                    "entry_mid": s.mid,
                    "is_control": True,
                }
            )
        quality.append({"signal_id": e["signal_id"], "n_controls": len(picks), "match_quality": "OK"})
    return controls, quality


def event_vs_control(event_out: list[dict[str, Any]], ctrl_out: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for h in (300, 900, 3600):
        for module in ("WALL_HOLD_RECLAIM", "WALL_REMOVED_BREAK"):
            for pos in ("LONG", "SHORT"):
                for is_c, src in ((False, event_out), (True, ctrl_out)):
                    vals = [
                        float(r["forward_return_bps"])
                        for r in src
                        if r.get("outcome_complete")
                        and r.get("forward_return_bps") is not None
                        and r["horizon_s"] == h
                        and r.get("module") == module
                        and r.get("position_side") == pos
                    ]
                    if not vals:
                        continue
                    rows.append(
                        {
                            "horizon_s": h,
                            "module": module,
                            "position_side": pos,
                            "is_control": is_c,
                            "n": len(vals),
                            "mean_fwd_bps": sum(vals) / len(vals),
                            "median_fwd_bps": sorted(vals)[len(vals) // 2],
                        }
                    )
    return rows


def attach_oi_liq_context(
    entries: list[dict[str, Any]],
    *,
    start: datetime,
    end: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Optional context only — never filters entries."""
    from orderbook_analyse.orderbook_v2.ch_client import get_clickhouse_client, load_clickhouse_settings

    load_clickhouse_settings()
    client = get_clickhouse_client()
    symbols = sorted({e["symbol"] for e in entries})
    oi = {s: load_oi_5s(client, symbol=s, start=start, end=end) for s in symbols}
    liq = {s: load_liquidations(client, symbol=s, start=start, end=end) for s in symbols}

    ctx_rows = []
    for e in entries:
        sym = e["symbol"]
        from datetime import timezone as _tz

        t = datetime.fromtimestamp(int(e["entry_at"]) / 1000.0, tz=_tz.utc)
        o0 = oi_asof(oi[sym], datetime.fromtimestamp((int(e["entry_at"]) - 60_000) / 1000.0, tz=_tz.utc))
        o1 = oi_asof(oi[sym], t)
        oi_delta = None
        oi_regime = "DATA_UNAVAILABLE"
        if o0.get("oi") is not None and o1.get("oi") is not None:
            oi_delta = o1["oi"] - o0["oi"]
            if oi_delta < 0:
                oi_regime = "OI_FALLING"
            elif oi_delta > 0:
                oi_regime = "OI_RISING"
            else:
                oi_regime = "OI_NEUTRAL"
        lf = liq[sym]
        long_n = short_n = 0.0
        if not lf.empty:
            t0 = pd.Timestamp(t) - pd.Timedelta(seconds=60)
            t1 = pd.Timestamp(t)
            sub = lf[(lf["event_time"] >= t0) & (lf["event_time"] < t1)]
            long_n = float(sub.loc[sub["liquidated_position_side"] == "LIQUIDATED_LONG", "notional_estimate"].sum()) if len(sub) else 0.0
            short_n = float(sub.loc[sub["liquidated_position_side"] == "LIQUIDATED_SHORT", "notional_estimate"].sum()) if len(sub) else 0.0
        ctx_rows.append(
            {
                "signal_id": e["signal_id"],
                "oi_regime": oi_regime,
                "oi_delta_60s": oi_delta,
                "long_liq_notional_60s": long_n,
                "short_liq_notional_60s": short_n,
                "has_long_liqs": long_n > 0,
                "has_short_liqs": short_n > 0,
            }
        )

    # comparison placeholder filled in runner with outcomes join
    return ctx_rows, []
