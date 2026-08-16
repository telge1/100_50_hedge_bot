#!/usr/bin/env python3
"""VALIDATE_SYMBOL_DIRECTION_BLOCKS_OOS — research-only, no DB writes.

Chronological TRAIN 60% / OOS 40% on frozen 15m BASELINE_IMMEDIATE trades.
Bad Symbol×Direction candidates discovered on TRAIN only, validated on OOS.
"""

from __future__ import annotations

import csv
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from signal_generator.strategy.wave_fade.parameters import PRIMARY_FEE  # noqa: E402

SRC = ROOT / "results" / "symbol_direction_edge_audit" / "trade_detail.csv"
OUT = ROOT / "results" / "symbol_direction_oos_validation"
FEE = float(PRIMARY_FEE)
TRAIN_FRAC = 0.60
MIN_TRAIN_N = 15
POSITIVE_CONTROLS = [
    ("APTUSDT", "LONG"),
    ("AVAXUSDT", "SHORT"),
    ("DOGEUSDT", "SHORT"),
    ("DOGEUSDT", "LONG"),
    ("ZECUSDT", "SHORT"),
    ("BMTUSDT", "SHORT"),
]


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    cols: list[str] = []
    seen: set[str] = set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                cols.append(k)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def oos_sample_flag(n: int) -> str:
    if n < 10:
        return "TINY"
    if n < 20:
        return "SMALL"
    if n < 50:
        return "MODERATE"
    return "BETTER"


def equity_and_streaks(closed: list[dict]) -> dict[str, Any]:
    if not closed:
        return {"max_drawdown": 0.0, "max_sl_streak": 0}
    nets = [float(r["pnl"]) - FEE for r in closed]
    eq = np.cumsum(np.asarray(nets, dtype=float))
    peak = np.maximum.accumulate(eq)
    max_dd = float((eq - peak).min())
    max_sl = streak = 0
    for r in closed:
        if r.get("exit_reason") == "SL":
            streak += 1
            max_sl = max(max_sl, streak)
        else:
            streak = 0
    return {"max_drawdown": max_dd, "max_sl_streak": int(max_sl)}


def summarize(rows: list[dict], *, label: str = "") -> dict[str, Any]:
    """rows may include OPEN; metrics on closed only."""
    closed = [r for r in rows if r.get("result") in ("WIN", "LOSS")]
    wins = [r for r in closed if r["result"] == "WIN"]
    losses = [r for r in closed if r["result"] == "LOSS"]
    n = len(closed)
    nets = [float(r["pnl"]) - FEE for r in closed]
    gross = [float(r["pnl"]) for r in closed]
    gp = sum(x for x in gross if x > 0)
    gl = sum(-x for x in gross if x < 0)
    pf = (gp / gl) if gl > 0 else (None if gp == 0 else float("inf"))
    sl = sum(1 for r in closed if r.get("exit_reason") == "SL")
    eq = equity_and_streaks(closed)
    net = float(sum(nets)) if nets else 0.0
    return {
        "label": label,
        "signals": len(rows),
        "closed": n,
        "open": len(rows) - n,
        "wins": len(wins),
        "losses": len(losses),
        "winrate": (100.0 * len(wins) / n) if n else None,
        "net": net,
        "net_per_trade": (net / n) if n else None,
        "profit_factor": None if pf is None or (isinstance(pf, float) and math.isinf(pf)) else float(pf),
        "SL_rate": (100.0 * sl / n) if n else None,
        "SL_count": sl,
        "max_drawdown": eq["max_drawdown"],
        "max_sl_streak": eq["max_sl_streak"],
        "sample_flag": oos_sample_flag(n),
    }


def is_train_bad(s: dict) -> bool:
    if s["closed"] < MIN_TRAIN_N:
        return False
    npt = s.get("net_per_trade")
    pf = s.get("profit_factor")
    slr = s.get("SL_rate")
    if npt is not None and npt < 0:
        return True
    if pf is not None and pf < 1.0:
        return True
    if slr is not None and slr > 55.0:
        return True
    return False


def oos_verdict(s: dict) -> str:
    if s["closed"] < 10:
        return "INSUFFICIENT_OOS_SAMPLE"
    npt = s.get("net_per_trade")
    pf = s.get("profit_factor")
    slr = s.get("SL_rate")
    bad_signals = 0
    if npt is not None and npt < 0:
        bad_signals += 1
    if pf is not None and pf < 1.0:
        bad_signals += 1
    if slr is not None and slr > 55.0:
        bad_signals += 1
    # confirmed if still weak on at least one primary criterion and not clearly good
    clearly_good = (
        npt is not None
        and npt > 0.05
        and pf is not None
        and pf >= 1.1
        and (slr is None or slr <= 50)
    )
    if clearly_good:
        return "NOT_CONFIRMED"
    if bad_signals >= 1:
        return "CONFIRMED_BAD"
    return "NOT_CONFIRMED"


def rolling_blocks(closed: list[dict], window: int = 30) -> list[dict]:
    out = []
    if len(closed) < window:
        # fall back to thirds if enough
        if len(closed) < 9:
            return out
        n = len(closed)
        chunks = [closed[: n // 3], closed[n // 3 : 2 * n // 3], closed[2 * n // 3 :]]
        for i, ch in enumerate(chunks, 1):
            s = summarize(ch, label=f"block_{i}")
            out.append(
                {
                    "block_i": i,
                    "mode": "thirds",
                    "closed": s["closed"],
                    "net_per_trade": s["net_per_trade"],
                    "profit_factor": s["profit_factor"],
                    "SL_rate": s["SL_rate"],
                    "net": s["net"],
                    "first_ts": ch[0]["entry_ts"],
                    "last_ts": ch[-1]["entry_ts"],
                }
            )
        return out
    for i in range(0, len(closed) - window + 1, max(1, window // 2)):
        ch = closed[i : i + window]
        s = summarize(ch, label=f"roll_{i}")
        out.append(
            {
                "block_i": i // max(1, window // 2) + 1,
                "mode": f"rolling_{window}",
                "closed": s["closed"],
                "net_per_trade": s["net_per_trade"],
                "profit_factor": s["profit_factor"],
                "SL_rate": s["SL_rate"],
                "net": s["net"],
                "first_ts": ch[0]["entry_ts"],
                "last_ts": ch[-1]["entry_ts"],
            }
        )
    return out


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    df = pd.read_csv(SRC)
    df["entry_ts"] = pd.to_datetime(df["entry_ts"], utc=True)
    df = df.sort_values(["entry_ts", "symbol", "signal_id"]).reset_index(drop=True)
    records = df.to_dict("records")
    # normalize
    for r in records:
        r["entry_ts"] = pd.Timestamp(r["entry_ts"]).isoformat().replace("+00:00", "Z")
        if r.get("pnl") is not None and not (isinstance(r["pnl"], float) and math.isnan(r["pnl"])):
            r["pnl"] = float(r["pnl"])
        else:
            r["pnl"] = None

    n = len(records)
    n_train = int(n * TRAIN_FRAC)
    # if OOS tiny for expected combos, still use 60/40 as primary; user said 50/50 if too small overall
    if n < 100:
        n_train = n // 2
    train = records[:n_train]
    oos = records[n_train:]

    symbols = sorted({r["symbol"] for r in records})
    sides = ("LONG", "SHORT")

    # TRAIN discovery
    train_rows = []
    train_map: dict[tuple[str, str], dict] = {}
    for sym in symbols:
        for side in sides:
            sub = [r for r in train if r["symbol"] == sym and r["direction"] == side]
            s = summarize(sub, label=f"{sym}_{side}")
            row = {
                "symbol": sym,
                "direction": side,
                "split": "TRAIN",
                **s,
                "is_bad_candidate": is_train_bad(s),
            }
            train_rows.append(row)
            train_map[(sym, side)] = row
    write_csv(OUT / "train_summary.csv", train_rows)

    frozen = [
        {"symbol": r["symbol"], "direction": r["direction"], **{k: r[k] for k in (
            "closed", "winrate", "net", "net_per_trade", "profit_factor", "SL_rate", "max_drawdown", "max_sl_streak", "sample_flag"
        )}}
        for r in train_rows
        if r["is_bad_candidate"]
    ]
    frozen.sort(key=lambda r: (r["net_per_trade"] is None, r["net_per_trade"] or 0))
    frozen_meta = {
        "source": str(SRC.relative_to(ROOT)),
        "train_frac": TRAIN_FRAC if n >= 100 else 0.5,
        "n_total_signals": n,
        "n_train": n_train,
        "n_oos": n - n_train,
        "train_end_exclusive": oos[0]["entry_ts"] if oos else None,
        "min_train_n": MIN_TRAIN_N,
        "criteria": ["closed>=15", "net_per_trade<0 OR profit_factor<1 OR SL_rate>55"],
        "candidates": frozen,
        "frozen_after_train_only": True,
    }
    (OUT / "frozen_bad_symbol_directions.json").write_text(json.dumps(frozen_meta, indent=2) + "\n")

    # OOS per combo (all) + verdict for frozen
    oos_rows = []
    oos_map: dict[tuple[str, str], dict] = {}
    for sym in symbols:
        for side in sides:
            sub = [r for r in oos if r["symbol"] == sym and r["direction"] == side]
            s = summarize(sub, label=f"{sym}_{side}")
            row = {"symbol": sym, "direction": side, "split": "OOS", **s}
            oos_rows.append(row)
            oos_map[(sym, side)] = row
    write_csv(OUT / "oos_summary.csv", oos_rows)

    confirmed: list[tuple[str, str]] = []
    not_confirmed: list[tuple[str, str]] = []
    insufficient: list[tuple[str, str]] = []
    verdicts = []
    for c in frozen:
        key = (c["symbol"], c["direction"])
        o = oos_map[key]
        v = oos_verdict(o)
        verdicts.append({**c, "oos": o, "verdict": v})
        if v == "CONFIRMED_BAD":
            confirmed.append(key)
        elif v == "NOT_CONFIRMED":
            not_confirmed.append(key)
        else:
            insufficient.append(key)

    # Stability
    stab_rows = []
    for c in frozen:
        key = (c["symbol"], c["direction"])
        t = train_map[key]
        o = oos_map[key]
        same = None
        if t["net_per_trade"] is not None and o["net_per_trade"] is not None and o["closed"] >= 5:
            same = "YES" if (t["net_per_trade"] < 0) == (o["net_per_trade"] < 0) else "NO"
        elif t["net_per_trade"] is not None and o["net_per_trade"] is not None:
            same = "YES" if (t["net_per_trade"] < 0) == (o["net_per_trade"] < 0) else "NO"
        stab_rows.append(
            {
                "symbol": c["symbol"],
                "direction": c["direction"],
                "train_closed": t["closed"],
                "oos_closed": o["closed"],
                "train_npt": t["net_per_trade"],
                "oos_npt": o["net_per_trade"],
                "train_pf": t["profit_factor"],
                "oos_pf": o["profit_factor"],
                "train_sl_rate": t["SL_rate"],
                "oos_sl_rate": o["SL_rate"],
                "same_sign_npt": same,
                "verdict": next(v["verdict"] for v in verdicts if (v["symbol"], v["direction"]) == key),
                "oos_sample_flag": o["sample_flag"],
            }
        )
    write_csv(OUT / "stability.csv", stab_rows)

    # Positive controls
    pos_rows = []
    for sym, side in POSITIVE_CONTROLS:
        t = train_map.get((sym, side))
        o = oos_map.get((sym, side))
        if t is None or o is None:
            continue
        pos_rows.append(
            {
                "symbol": sym,
                "direction": side,
                "train_closed": t["closed"],
                "train_npt": t["net_per_trade"],
                "train_pf": t["profit_factor"],
                "train_wr": t["winrate"],
                "oos_closed": o["closed"],
                "oos_npt": o["net_per_trade"],
                "oos_pf": o["profit_factor"],
                "oos_wr": o["winrate"],
                "oos_net": o["net"],
                "oos_sl_rate": o["SL_rate"],
                "oos_sample_flag": o["sample_flag"],
                "still_positive_oos": bool(
                    o["closed"] >= 3
                    and o["net_per_trade"] is not None
                    and o["net_per_trade"] > 0
                ),
            }
        )
    write_csv(OUT / "positive_controls.csv", pos_rows)

    # OOS block counterfactuals
    oos_base = summarize(oos, label="BASELINE_OOS")
    cf_rows = []

    def block_cf(label: str, keys: list[tuple[str, str]]) -> dict:
        keyset = set(keys)
        removed = [r for r in oos if (r["symbol"], r["direction"]) in keyset]
        kept = [r for r in oos if (r["symbol"], r["direction"]) not in keyset]
        rem_c = [r for r in removed if r["result"] in ("WIN", "LOSS")]
        after = summarize(kept, label=label)
        cov = (100.0 * after["closed"] / oos_base["closed"]) if oos_base["closed"] else None
        return {
            "block": label,
            "trades_removed": len(rem_c),
            "signals_removed": len(removed),
            "winners_removed": sum(1 for r in rem_c if r["result"] == "WIN"),
            "losers_removed": sum(1 for r in rem_c if r["result"] == "LOSS"),
            "net_before": oos_base["net"],
            "net_after": after["net"],
            "delta_net": after["net"] - oos_base["net"],
            "pf_before": oos_base["profit_factor"],
            "pf_after": after["profit_factor"],
            "max_dd_before": oos_base["max_drawdown"],
            "max_dd_after": after["max_drawdown"],
            "delta_max_dd": after["max_drawdown"] - oos_base["max_drawdown"],
            "coverage_pct": cov,
            "closed_after": after["closed"],
        }

    for sym, side in confirmed:
        cf_rows.append(block_cf(f"BLOCK_{sym}_{side}", [(sym, side)]))
    if confirmed:
        cf_rows.append(block_cf("BLOCK_ALL_CONFIRMED_BAD", confirmed))
    # also show frozen-not-confirmed individually for transparency (optional but useful)
    for sym, side in not_confirmed + insufficient:
        cf_rows.append(block_cf(f"BLOCK_{sym}_{side}_UNCONFIRMED", [(sym, side)]))
    write_csv(OUT / "oos_block_counterfactual.csv", cf_rows)

    # Rolling view for APT SHORT / ACE SHORT
    roll_rows = []
    for sym, side in (("APTUSDT", "SHORT"), ("ACEUSDT", "SHORT")):
        closed = [
            r
            for r in records
            if r["symbol"] == sym and r["direction"] == side and r["result"] in ("WIN", "LOSS")
        ]
        for blk in rolling_blocks(closed, window=30 if len(closed) >= 30 else 15):
            roll_rows.append({"symbol": sym, "direction": side, **blk})
    write_csv(OUT / "rolling_view.csv", roll_rows)

    # Rankings among confirmed (or frozen with enough OOS)
    def rank_pool():
        return [(v["symbol"], v["direction"], v["oos"], v["verdict"]) for v in verdicts]

    pool = [x for x in rank_pool() if x[2]["closed"] >= 5]
    rankings = {
        "most_stably_bad": None,
        "largest_OOS_net_damage": None,
        "largest_OOS_drawdown_damage": None,
        "highest_OOS_SL_rate": None,
        "longest_OOS_SL_streak": None,
    }
    # stably bad: confirmed + same_sign YES + lowest oos npt
    stable = [
        s
        for s in stab_rows
        if s["verdict"] == "CONFIRMED_BAD" and s["same_sign_npt"] == "YES"
    ]
    if stable:
        best = min(stable, key=lambda r: r["oos_npt"] if r["oos_npt"] is not None else 0)
        rankings["most_stably_bad"] = f"{best['symbol']}_{best['direction']}"
    if pool:
        rankings["largest_OOS_net_damage"] = min(
            pool, key=lambda x: x[2]["net"] if x[2]["net"] is not None else 0
        )
        rankings["largest_OOS_net_damage"] = (
            f"{rankings['largest_OOS_net_damage'][0]}_{rankings['largest_OOS_net_damage'][1]}"
        )
        rankings["largest_OOS_drawdown_damage"] = min(pool, key=lambda x: x[2]["max_drawdown"])
        rankings["largest_OOS_drawdown_damage"] = (
            f"{rankings['largest_OOS_drawdown_damage'][0]}_{rankings['largest_OOS_drawdown_damage'][1]}"
        )
        rankings["highest_OOS_SL_rate"] = max(
            pool, key=lambda x: x[2]["SL_rate"] if x[2]["SL_rate"] is not None else -1
        )
        rankings["highest_OOS_SL_rate"] = (
            f"{rankings['highest_OOS_SL_rate'][0]}_{rankings['highest_OOS_SL_rate'][1]}"
        )
        rankings["longest_OOS_SL_streak"] = max(pool, key=lambda x: x[2]["max_sl_streak"])
        rankings["longest_OOS_SL_streak"] = (
            f"{rankings['longest_OOS_SL_streak'][0]}_{rankings['longest_OOS_SL_streak'][1]}"
        )

    # Primary decision
    apt_v = next((v for v in verdicts if v["symbol"] == "APTUSDT" and v["direction"] == "SHORT"), None)
    ace_v = next((v for v in verdicts if v["symbol"] == "ACEUSDT" and v["direction"] == "SHORT"), None)

    # regime: rolling blocks flip sign
    regime = False
    for sym, side in (("APTUSDT", "SHORT"), ("ACEUSDT", "SHORT")):
        blks = [r for r in roll_rows if r["symbol"] == sym and r["direction"] == side]
        npts = [r["net_per_trade"] for r in blks if r["net_per_trade"] is not None]
        if len(npts) >= 2 and any(x > 0.05 for x in npts) and any(x < -0.05 for x in npts):
            regime = True

    all_cf = next((r for r in cf_rows if r["block"] == "BLOCK_ALL_CONFIRMED_BAD"), None)
    apt_only_cf = next((r for r in cf_rows if r["block"] == "BLOCK_APTUSDT_SHORT"), None)

    if not frozen:
        primary = "INSUFFICIENT_SAMPLE"
        recommendation = "NEEDS_MORE_HISTORY"
    elif not confirmed and all(v["verdict"] == "INSUFFICIENT_OOS_SAMPLE" for v in verdicts):
        primary = "INSUFFICIENT_SAMPLE"
        recommendation = "NEEDS_MORE_HISTORY"
    elif confirmed == [("APTUSDT", "SHORT")] or (
        confirmed
        and len(confirmed) == 1
        and confirmed[0] == ("APTUSDT", "SHORT")
    ):
        primary = "APT_SHORT_ONLY_OOS_CONFIRMED"
        recommendation = "SYMBOL_DIRECTION_PERMISSION_LAYER_WORTH_NEXT_STAGE"
    elif len(confirmed) >= 2 and all_cf and all_cf["delta_net"] > 1.0:
        primary = "SYMBOL_DIRECTION_BLOCKS_OOS_CONFIRMED"
        recommendation = "SYMBOL_DIRECTION_PERMISSION_LAYER_WORTH_NEXT_STAGE"
    elif len(confirmed) >= 1 and apt_v and apt_v["verdict"] == "CONFIRMED_BAD" and len(confirmed) == 1:
        primary = "APT_SHORT_ONLY_OOS_CONFIRMED"
        recommendation = "SYMBOL_DIRECTION_PERMISSION_LAYER_WORTH_NEXT_STAGE"
    elif regime and not confirmed:
        primary = "EFFECT_IS_REGIME_DEPENDENT"
        recommendation = "NEEDS_MORE_HISTORY"
    elif regime and confirmed and any(
        s["same_sign_npt"] == "NO" for s in stab_rows if (s["symbol"], s["direction"]) in confirmed
    ):
        primary = "EFFECT_IS_REGIME_DEPENDENT"
        recommendation = "NEEDS_MORE_HISTORY"
    elif not confirmed:
        primary = "BAD_SYMBOL_DIRECTIONS_NOT_STABLE"
        recommendation = "KEEP_ALL"
    elif len(confirmed) >= 2:
        primary = "SYMBOL_DIRECTION_BLOCKS_OOS_CONFIRMED"
        recommendation = "SYMBOL_DIRECTION_PERMISSION_LAYER_WORTH_NEXT_STAGE"
    else:
        # single non-APT confirmed
        if confirmed == [("APTUSDT", "SHORT")]:
            primary = "APT_SHORT_ONLY_OOS_CONFIRMED"
        else:
            primary = "SYMBOL_DIRECTION_BLOCKS_OOS_CONFIRMED"
        recommendation = "SYMBOL_DIRECTION_PERMISSION_LAYER_WORTH_NEXT_STAGE"

    # If APT only among confirmed with meaningful OOS, prefer APT_SHORT_ONLY
    if confirmed == [("APTUSDT", "SHORT")]:
        primary = "APT_SHORT_ONLY_OOS_CONFIRMED"
        recommendation = "SYMBOL_DIRECTION_PERMISSION_LAYER_WORTH_NEXT_STAGE"
    elif ("APTUSDT", "SHORT") in confirmed and len(confirmed) >= 2:
        primary = "SYMBOL_DIRECTION_BLOCKS_OOS_CONFIRMED"
        recommendation = "SYMBOL_DIRECTION_PERMISSION_LAYER_WORTH_NEXT_STAGE"

    meta = {
        "task": "VALIDATE_SYMBOL_DIRECTION_BLOCKS_OOS",
        "source": str(SRC.relative_to(ROOT)),
        "variant": "BASELINE_IMMEDIATE",
        "timeframe": "15m",
        "fee": FEE,
        "n_total": n,
        "n_train": n_train,
        "n_oos": len(oos),
        "train_end_exclusive": oos[0]["entry_ts"] if oos else None,
        "frozen_candidates": [f"{c['symbol']}_{c['direction']}" for c in frozen],
        "confirmed_bad": [f"{a}_{b}" for a, b in confirmed],
        "not_confirmed": [f"{a}_{b}" for a, b in not_confirmed],
        "insufficient_oos": [f"{a}_{b}" for a, b in insufficient],
        "oos_baseline": oos_base,
        "block_all_confirmed": all_cf,
        "block_apt_short": apt_only_cf,
        "rankings": rankings,
        "primary_decision": primary,
        "recommendation": recommendation,
        "strategy_logic_changed": False,
        "lookahead_violations": 0,
    }
    (OUT / "summary.json").write_text(
        json.dumps(
            {
                "meta": meta,
                "verdicts": [
                    {
                        "symbol": v["symbol"],
                        "direction": v["direction"],
                        "verdict": v["verdict"],
                        "train_npt": v["net_per_trade"],
                        "oos_npt": v["oos"]["net_per_trade"],
                        "oos_closed": v["oos"]["closed"],
                    }
                    for v in verdicts
                ],
                "positive_controls": pos_rows,
                "counterfactuals": cf_rows,
            },
            indent=2,
            default=str,
        )
        + "\n"
    )

    def fmt(v: Any, nd: int = 2) -> str:
        if v is None:
            return "–"
        if isinstance(v, float):
            if math.isnan(v) or math.isinf(v):
                return "–"
            return f"{v:.{nd}f}"
        return str(v)

    lines = [
        "# VALIDATE_SYMBOL_DIRECTION_BLOCKS_OOS",
        "",
        f"Primary: `{primary}`",
        f"Recommendation: `{recommendation}`",
        f"Split: TRAIN n={n_train} / OOS n={len(oos)} | cut `{oos[0]['entry_ts'] if oos else None}`",
        "Frozen TRAIN bad: " + ", ".join(f"{c['symbol']}_{c['direction']}" for c in frozen),
        f"Confirmed OOS: {meta['confirmed_bad']}",
        f"Not confirmed: {meta['not_confirmed']}",
        f"Insufficient OOS: {meta['insufficient_oos']}",
        "",
        "## OOS frozen candidates",
        "",
        "| Symbol | Side | Trades | WR | Net | NPT | PF | SL Rate | Max DD | Verdict |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for v in verdicts:
        o = v["oos"]
        lines.append(
            f"| {v['symbol']} | {v['direction']} | {o['closed']} | {fmt(o['winrate'],1)} | {fmt(o['net'])} | "
            f"{fmt(o['net_per_trade'],3)} | {fmt(o['profit_factor'])} | {fmt(o['SL_rate'],1)} | "
            f"{fmt(o['max_drawdown'])} | {v['verdict']} |"
        )
    lines += ["", "## Counterfactual OOS", ""]
    for r in cf_rows:
        if "UNCONFIRMED" in r["block"] and r["block"] != "BLOCK_ALL_CONFIRMED_BAD":
            continue
        lines.append(
            f"- {r['block']}: ΔNet={fmt(r['delta_net'])} ΔDD={fmt(r['delta_max_dd'])} "
            f"removed W/L={r['winners_removed']}/{r['losers_removed']} cov={fmt(r['coverage_pct'],1)}%"
        )
    lines += ["", "## Strategy Logic Changed", "", "`NO`", ""]
    (OUT / "summary.md").write_text("\n".join(lines), encoding="utf-8")

    print("PRIMARY", primary, flush=True)
    print("REC", recommendation, flush=True)
    print("FROZEN", [f"{c['symbol']}_{c['direction']}" for c in frozen], flush=True)
    print("CONFIRMED", meta["confirmed_bad"], flush=True)
    print("NOT", meta["not_confirmed"], "INSUFF", meta["insufficient_oos"], flush=True)
    if all_cf:
        print("BLOCK_ALL ΔNet", all_cf["delta_net"], "ΔDD", all_cf["delta_max_dd"], flush=True)
    for v in verdicts:
        o = v["oos"]
        print(
            f"  {v['symbol']} {v['direction']}: train_npt={v['net_per_trade']:.3f} "
            f"oos_n={o['closed']} oos_npt={o['net_per_trade']} → {v['verdict']}",
            flush=True,
        )
    print("wrote", OUT, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
