#!/usr/bin/env python3
"""AUDIT_15M_DIRECTIONAL_POSITION_SIZING — research-only, no DB writes.

Counterfactual: same 15m Tier-A BASELINE_IMMEDIATE APT+DOGE trades as
results/baseline_timeframe_quality_audit/signal_detail.csv.
Only scales trade PnL by directional size. No signal/outcome changes.
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

SRC = ROOT / "results" / "baseline_timeframe_quality_audit" / "signal_detail.csv"
OUT = ROOT / "results" / "15m_directional_position_sizing_audit"
FEE = float(PRIMARY_FEE)

# Locked baseline expectations from COMPARE_BASELINE_SIGNAL_QUALITY_BY_TIMEFRAME (15m APT+DOGE)
EXPECT_CLOSED = 53
EXPECT_NET = 9.17
EXPECT_NPT = 0.173
EXPECT_LONG_NPT = 0.423
EXPECT_SHORT_NPT = -0.153
TOL = 0.02

VARIANTS = [
    ("BASELINE_EQUAL_SIZE", 1.00, 1.00),
    ("SHORT_75", 1.00, 0.75),
    ("SHORT_50", 1.00, 0.50),
    ("SHORT_25", 1.00, 0.25),
    ("LONG_ONLY", 1.00, 0.00),
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


def size_for(direction: str, long_sz: float, short_sz: float) -> float:
    return long_sz if direction == "LONG" else short_sz


def equity_stats(nets: list[float]) -> dict[str, Any]:
    if not nets:
        return {
            "final_cum_net": 0.0,
            "max_dd": 0.0,
            "max_dd_pct": None,
            "longest_losing_streak": 0,
            "worst_5_trade_seq": None,
            "worst_10_trade_seq": None,
        }
    eq = np.cumsum(np.asarray(nets, dtype=float))
    peak = np.maximum.accumulate(eq)
    dd = eq - peak
    max_dd = float(dd.min())  # negative or 0
    # dd % vs peak equity when peak > 0; else vs abs peak or None
    max_dd_pct = None
    if len(peak):
        # at trough index
        i = int(np.argmin(dd))
        pk = float(peak[i])
        if pk > 0:
            max_dd_pct = 100.0 * float(dd[i]) / pk
        elif abs(pk) > 1e-12:
            max_dd_pct = 100.0 * float(dd[i]) / abs(pk)

    streak = longest = 0
    for x in nets:
        if x < 0:
            streak += 1
            longest = max(longest, streak)
        else:
            streak = 0

    def worst_k(k: int) -> float | None:
        if len(nets) < k:
            return None
        window = [sum(nets[i : i + k]) for i in range(len(nets) - k + 1)]
        return float(min(window))

    return {
        "final_cum_net": float(eq[-1]),
        "max_dd": max_dd,
        "max_dd_pct": max_dd_pct,
        "longest_losing_streak": int(longest),
        "worst_5_trade_seq": worst_k(5),
        "worst_10_trade_seq": worst_k(10),
    }


def summarize_trades(rows: list[dict], *, label: str, long_sz: float, short_sz: float) -> dict[str, Any]:
    """rows: closed trades with scaled_net already applied for included trades."""
    wins = [r for r in rows if r["result"] == "WIN"]
    losses = [r for r in rows if r["result"] == "LOSS"]
    longs = [r for r in rows if r["direction"] == "LONG"]
    shorts = [r for r in rows if r["direction"] == "SHORT"]
    nets = [float(r["scaled_net"]) for r in rows]
    gross = [float(r["scaled_gross"]) for r in rows]
    gp = sum(x for x in gross if x > 0)
    gl = sum(-x for x in gross if x < 0)
    pf = (gp / gl) if gl > 0 else (None if gp == 0 else float("inf"))
    long_net = sum(float(r["scaled_net"]) for r in longs)
    short_net = sum(float(r["scaled_net"]) for r in shorts)
    total_net = float(sum(nets))
    abs_contrib = abs(long_net) + abs(short_net)
    eq = equity_stats(nets)
    return {
        "variant": label,
        "long_size": long_sz,
        "short_size": short_sz,
        "total_trades": len(rows),
        "long_trades": len(longs),
        "short_trades": len(shorts),
        "wins": len(wins),
        "losses": len(losses),
        "winrate": (100.0 * len(wins) / len(rows)) if rows else None,
        "gross_profit": float(gp),
        "gross_loss": float(-gl),
        "net": total_net,
        "net_per_trade": (total_net / len(rows)) if rows else None,
        "profit_factor": None if pf is None or (isinstance(pf, float) and math.isinf(pf)) else float(pf),
        "mean_trade_pnl": float(np.mean(nets)) if nets else None,
        "median_trade_pnl": float(np.median(nets)) if nets else None,
        "long_net": float(long_net),
        "short_net": float(short_net),
        "long_contribution_pct": (100.0 * long_net / total_net) if abs(total_net) > 1e-12 else None,
        "short_contribution_pct": (100.0 * short_net / total_net) if abs(total_net) > 1e-12 else None,
        "long_abs_share_pct": (100.0 * abs(long_net) / abs_contrib) if abs_contrib > 1e-12 else None,
        "short_abs_share_pct": (100.0 * abs(short_net) / abs_contrib) if abs_contrib > 1e-12 else None,
        **eq,
        "max_drawdown": eq["max_dd"],
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    raw = pd.read_csv(SRC)
    df = raw[(raw["timeframe"] == "15m") & (raw["symbol"].isin(["APTUSDT", "DOGEUSDT"]))].copy()
    df["entry_ts"] = pd.to_datetime(df["entry_ts"], utc=True)
    df = df.sort_values(["entry_ts", "symbol", "signal_id"]).reset_index(drop=True)

    all_sigs = df.to_dict("records")
    closed = df[df["result"].isin(["WIN", "LOSS"])].copy()
    closed_recs = closed.to_dict("records")

    # Baseline guard (equal size)
    base_nets = []
    for r in closed_recs:
        gross = float(r["pnl"])
        base_nets.append(gross - FEE)
    base_net = float(sum(base_nets))
    base_npt = base_net / len(base_nets)
    long_c = [r for r in closed_recs if r["direction"] == "LONG"]
    short_c = [r for r in closed_recs if r["direction"] == "SHORT"]
    long_npt = float(np.mean([float(r["pnl"]) - FEE for r in long_c]))
    short_npt = float(np.mean([float(r["pnl"]) - FEE for r in short_c]))

    guard_ok = (
        len(closed_recs) == EXPECT_CLOSED
        and abs(base_net - EXPECT_NET) <= TOL
        and abs(base_npt - EXPECT_NPT) <= TOL
        and abs(long_npt - EXPECT_LONG_NPT) <= TOL
        and abs(short_npt - EXPECT_SHORT_NPT) <= TOL
    )
    lookahead_violations = 0  # frozen prior causal replay; no new feature calc

    if not guard_ok:
        print(
            "BASELINE_GUARD_FAIL",
            {
                "closed": len(closed_recs),
                "net": base_net,
                "npt": base_npt,
                "long_npt": long_npt,
                "short_npt": short_npt,
            },
            flush=True,
        )
        # continue with actual numbers but flag

    # Build trade detail with all variant columns
    trade_detail_rows: list[dict[str, Any]] = []
    equity_rows: list[dict[str, Any]] = []
    sizing_summaries: list[dict[str, Any]] = []
    direction_rows: list[dict[str, Any]] = []
    symbol_rows: list[dict[str, Any]] = []
    time_rows: list[dict[str, Any]] = []

    # Precompute unscaled nets
    for r in closed_recs:
        r["unscaled_gross"] = float(r["pnl"])
        r["unscaled_net"] = float(r["pnl"]) - FEE

    base_wr = 100.0 * sum(1 for r in closed_recs if r["result"] == "WIN") / len(closed_recs)

    for vname, lsz, ssz in VARIANTS:
        # Included trades: LONG_ONLY drops shorts
        if vname == "LONG_ONLY":
            included = [r for r in closed_recs if r["direction"] == "LONG"]
        else:
            included = list(closed_recs)

        scaled = []
        for r in included:
            sz = size_for(r["direction"], lsz, ssz)
            scaled.append(
                {
                    **r,
                    "size": sz,
                    "scaled_gross": float(r["unscaled_gross"]) * sz,
                    "scaled_net": float(r["unscaled_net"]) * sz,
                }
            )

        summ = summarize_trades(scaled, label=vname, long_sz=lsz, short_sz=ssz)
        sizing_summaries.append(summ)

        # WR guard for non-LONG_ONLY
        if vname != "LONG_ONLY":
            if abs((summ["winrate"] or 0) - base_wr) > 1e-9:
                raise RuntimeError(f"WR changed for {vname}: {summ['winrate']} vs {base_wr}")

        # equity curve
        cum = 0.0
        peak = 0.0
        for i, r in enumerate(scaled):
            cum += float(r["scaled_net"])
            peak = max(peak, cum)
            equity_rows.append(
                {
                    "variant": vname,
                    "seq": i + 1,
                    "signal_id": r["signal_id"],
                    "symbol": r["symbol"],
                    "direction": r["direction"],
                    "entry_ts": pd.Timestamp(r["entry_ts"]).isoformat().replace("+00:00", "Z"),
                    "size": r["size"],
                    "scaled_net": r["scaled_net"],
                    "cum_net": cum,
                    "drawdown": cum - peak,
                }
            )

        # trade detail (all closed, with this variant's size; LONG_ONLY marks short size 0 excluded)
        for r in closed_recs:
            sz = size_for(r["direction"], lsz, ssz)
            included_flag = not (vname == "LONG_ONLY" and r["direction"] == "SHORT")
            trade_detail_rows.append(
                {
                    "variant": vname,
                    "signal_id": r["signal_id"],
                    "symbol": r["symbol"],
                    "direction": r["direction"],
                    "entry_ts": pd.Timestamp(r["entry_ts"]).isoformat().replace("+00:00", "Z"),
                    "result": r["result"],
                    "unscaled_gross": r["unscaled_gross"],
                    "unscaled_net": r["unscaled_net"],
                    "size": sz if included_flag else 0.0,
                    "scaled_net": (r["unscaled_net"] * sz) if included_flag else None,
                    "included": included_flag,
                }
            )

        # direction summary
        for side in ("LONG", "SHORT", "ALL"):
            if side == "ALL":
                sub = scaled
            else:
                sub = [x for x in scaled if x["direction"] == side]
            s = summarize_trades(sub, label=f"{vname}_{side}", long_sz=lsz, short_sz=ssz)
            direction_rows.append(
                {
                    "variant": vname,
                    "side": side,
                    "trades": s["total_trades"],
                    "winrate": s["winrate"],
                    "net": s["net"],
                    "net_per_trade": s["net_per_trade"],
                    "profit_factor": s["profit_factor"],
                    "max_drawdown": s["max_drawdown"],
                }
            )

        # symbol
        for sym in ("APTUSDT", "DOGEUSDT"):
            sub = [x for x in scaled if x["symbol"] == sym]
            s = summarize_trades(sub, label=f"{vname}_{sym}", long_sz=lsz, short_sz=ssz)
            lng = [x for x in sub if x["direction"] == "LONG"]
            sht = [x for x in sub if x["direction"] == "SHORT"]
            symbol_rows.append(
                {
                    "variant": vname,
                    "symbol": sym,
                    "trades": s["total_trades"],
                    "long_net": sum(float(x["scaled_net"]) for x in lng),
                    "short_net": sum(float(x["scaled_net"]) for x in sht),
                    "total_net": s["net"],
                    "profit_factor": s["profit_factor"],
                    "max_drawdown": s["max_drawdown"],
                    "winrate": s["winrate"],
                }
            )

        # time stability
        n = len(scaled)
        mid = n // 2
        blocks = {
            "first_half": scaled[:mid],
            "second_half": scaled[mid:],
            "block_1": scaled[: n // 3],
            "block_2": scaled[n // 3 : 2 * n // 3],
            "block_3": scaled[2 * n // 3 :],
        }
        for bname, blk in blocks.items():
            s = summarize_trades(blk, label=f"{vname}_{bname}", long_sz=lsz, short_sz=ssz)
            time_rows.append(
                {
                    "variant": vname,
                    "block": bname,
                    "trades": s["total_trades"],
                    "net": s["net"],
                    "net_per_trade": s["net_per_trade"],
                    "max_drawdown": s["max_drawdown"],
                    "winrate": s["winrate"],
                }
            )

    write_csv(OUT / "sizing_summary.csv", sizing_summaries)
    write_csv(OUT / "direction_summary.csv", direction_rows)
    write_csv(OUT / "symbol_summary.csv", symbol_rows)
    write_csv(OUT / "time_stability.csv", time_rows)
    write_csv(OUT / "equity_curve.csv", equity_rows)
    write_csv(OUT / "trade_detail.csv", trade_detail_rows)

    by_v = {s["variant"]: s for s in sizing_summaries}
    eq = by_v["BASELINE_EQUAL_SIZE"]
    s50 = by_v["SHORT_50"]
    lo = by_v["LONG_ONLY"]

    d_net = s50["net"] - eq["net"]
    d_dd = s50["max_drawdown"] - eq["max_drawdown"]  # less negative = improvement
    d_pf = (s50["profit_factor"] or 0) - (eq["profit_factor"] or 0)

    # Short-only diagnostic (linear scale of unscaled short nets)
    short_diag = []
    short_unscaled = [r for r in closed_recs if r["direction"] == "SHORT"]
    short_base_nets = [float(r["pnl"]) - FEE for r in short_unscaled]
    short_wins = sum(1 for r in short_unscaled if r["result"] == "WIN")
    for sz in (1.0, 0.75, 0.5, 0.25, 0.0):
        nets = [x * sz for x in short_base_nets]
        gp = sum(x for x in [float(r["pnl"]) * sz for r in short_unscaled] if x > 0)
        gl = sum(-x for x in [float(r["pnl"]) * sz for r in short_unscaled] if x < 0)
        pf = (gp / gl) if gl > 0 else None
        short_diag.append(
            {
                "short_size": sz,
                "count": len(short_unscaled) if sz > 0 else 0,
                "wins": short_wins if sz > 0 else 0,
                "losses": (len(short_unscaled) - short_wins) if sz > 0 else 0,
                "WR": (100.0 * short_wins / len(short_unscaled)) if short_unscaled and sz > 0 else None,
                "net": float(sum(nets)),
                "mean_pnl": float(np.mean(nets)) if nets and sz > 0 else None,
                "median_pnl": float(np.median(nets)) if nets and sz > 0 else None,
                "profit_factor": pf,
            }
        )

    # Stability: SHORT_50 better than equal in both halves?
    def half_net(variant: str, block: str) -> float | None:
        row = next((r for r in time_rows if r["variant"] == variant and r["block"] == block), None)
        return None if row is None else row["net"]

    fh_better = (half_net("SHORT_50", "first_half") or -1e9) > (half_net("BASELINE_EQUAL_SIZE", "first_half") or -1e9)
    sh_better = (half_net("SHORT_50", "second_half") or -1e9) > (half_net("BASELINE_EQUAL_SIZE", "second_half") or -1e9)
    both_halves = fh_better and sh_better

    # Symbol: short weakness on both?
    apt_short = next(r for r in symbol_rows if r["variant"] == "BASELINE_EQUAL_SIZE" and r["symbol"] == "APTUSDT")
    doge_short = next(r for r in symbol_rows if r["variant"] == "BASELINE_EQUAL_SIZE" and r["symbol"] == "DOGEUSDT")
    # use direction from trade detail for short nets already in symbol rows
    short_weak_both = apt_short["short_net"] < 0 and doge_short["short_net"] < 0
    short_weak_one = (apt_short["short_net"] < 0) != (doge_short["short_net"] < 0)

    # Does SHORT_50 improve vs equal on net AND dd?
    net_better = d_net > 0.05  # meaningful
    dd_better = d_dd > 0.05  # max_dd less negative
    pf_better = d_pf > 0.05
    net_only = net_better and not dd_better
    dd_only = dd_better and not net_better
    both = net_better and dd_better

    # LONG_ONLY vs SHORT_50
    long_only_better_net = lo["net"] > s50["net"] + 0.05
    long_only_better_dd = lo["max_drawdown"] > s50["max_drawdown"] + 0.05

    # Unstable if halves disagree on SHORT_50 benefit
    unstable = fh_better != sh_better

    n_closed = len(closed_recs)
    if n_closed < 30:
        primary = "INSUFFICIENT_SAMPLE"
        recommendation = "NEEDS_MORE_HISTORY"
    elif long_only_better_net and long_only_better_dd and lo["net"] > eq["net"]:
        # LONG_ONLY beats sized short on both
        if both or net_better:
            primary = "LONG_ONLY_BETTER_THAN_SIZED_SHORT"
            recommendation = "LONG_ONLY_WORTH_FURTHER_TESTING"
        else:
            primary = "LONG_ONLY_BETTER_THAN_SIZED_SHORT"
            recommendation = "LONG_ONLY_WORTH_FURTHER_TESTING"
    elif unstable and not both:
        primary = "DIRECTIONAL_SIZING_UNSTABLE"
        recommendation = "NEEDS_MORE_HISTORY"
    elif both and both_halves and not short_weak_one:
        primary = "SHORT_HALF_SIZE_IMPROVES_RISK_RETURN"
        recommendation = "USE_SHORT_50_FOR_NEXT_RESEARCH_STAGE"
    elif both and (both_halves or not unstable):
        # net+dd better; symbol split may still exist
        if short_weak_one and not both_halves:
            primary = "DIRECTIONAL_SIZING_UNSTABLE"
            recommendation = "NEEDS_MORE_HISTORY"
        else:
            primary = "SHORT_HALF_SIZE_IMPROVES_RISK_RETURN"
            recommendation = "USE_SHORT_50_FOR_NEXT_RESEARCH_STAGE"
    elif net_only:
        primary = "SHORT_REDUCED_SIZE_IMPROVES_NET_ONLY"
        recommendation = "USE_SHORT_50_FOR_NEXT_RESEARCH_STAGE"
    elif dd_only:
        primary = "SHORT_REDUCED_SIZE_REDUCES_DRAWDOWN_ONLY"
        recommendation = "USE_SHORT_50_FOR_NEXT_RESEARCH_STAGE"
    elif (by_v["SHORT_25"]["net"] > s50["net"] + 0.5) and (by_v["SHORT_25"]["max_drawdown"] > s50["max_drawdown"]):
        primary = "SHORT_HALF_SIZE_IMPROVES_RISK_RETURN" if both else "SHORT_REDUCED_SIZE_IMPROVES_NET_ONLY"
        recommendation = "REDUCE_SHORT_EVEN_MORE_WORTH_TESTING"
    else:
        primary = "SHORT_SIZE_REDUCTION_NO_MEANINGFUL_BENEFIT"
        recommendation = "KEEP_EQUAL_SIZE"

    # Refine: if SHORT_25 clearly better risk-return than SHORT_50, prefer REDUCE_EVEN_MORE
    s25 = by_v["SHORT_25"]
    if (
        primary in ("SHORT_HALF_SIZE_IMPROVES_RISK_RETURN", "SHORT_REDUCED_SIZE_IMPROVES_NET_ONLY", "SHORT_REDUCED_SIZE_REDUCES_DRAWDOWN_ONLY")
        and s25["net"] >= s50["net"] - 0.01
        and s25["max_drawdown"] > s50["max_drawdown"] + 0.2
        and s25["net"] > eq["net"]
    ):
        recommendation = "REDUCE_SHORT_EVEN_MORE_WORTH_TESTING"

    # If LONG_ONLY is clearly best on net and DD vs all sized variants
    if lo["net"] >= max(s["net"] for s in sizing_summaries) - 1e-9 and lo["max_drawdown"] >= max(
        s["max_drawdown"] for s in sizing_summaries
    ) - 1e-9:
        if lo["net"] > eq["net"] + 0.5:
            primary = "LONG_ONLY_BETTER_THAN_SIZED_SHORT"
            recommendation = "LONG_ONLY_WORTH_FURTHER_TESTING"

    meta = {
        "task": "AUDIT_15M_DIRECTIONAL_POSITION_SIZING",
        "source": str(SRC.relative_to(ROOT)),
        "variant_signal": "BASELINE_IMMEDIATE",
        "timeframe": "15m",
        "symbols": ["APTUSDT", "DOGEUSDT"],
        "fee": FEE,
        "lookahead_violations": lookahead_violations,
        "baseline_guard_ok": guard_ok,
        "baseline_signal_count": len(all_sigs),
        "baseline_long_count": int((df["direction"] == "LONG").sum()),
        "baseline_short_count": int((df["direction"] == "SHORT").sum()),
        "baseline_closed_trades": n_closed,
        "baseline_closed_long": len(long_c),
        "baseline_closed_short": len(short_c),
        "baseline_net": base_net,
        "baseline_npt": base_npt,
        "baseline_wr": base_wr,
        "capital_notional_available": False,
        "short50_vs_equal": {"delta_net": d_net, "delta_max_dd": d_dd, "delta_pf": d_pf},
        "short50_better_both_halves": both_halves,
        "short_weakness_both_symbols": short_weak_both,
        "short_weakness_one_symbol_only": short_weak_one,
        "primary_decision": primary,
        "recommendation": recommendation,
        "strategy_logic_changed": False,
        "short_diagnostic": short_diag,
    }

    (OUT / "summary.json").write_text(
        json.dumps(
            {
                "meta": meta,
                "sizing_summary": sizing_summaries,
                "short_diagnostic": short_diag,
            },
            indent=2,
            default=str,
        )
        + "\n",
        encoding="utf-8",
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
        "# AUDIT_15M_DIRECTIONAL_POSITION_SIZING",
        "",
        f"Primary: `{primary}`",
        f"Recommendation: `{recommendation}`",
        f"Source: `{SRC.relative_to(ROOT)}`",
        f"baseline_guard_ok={guard_ok} | lookahead_violations={lookahead_violations}",
        f"signals={len(all_sigs)} LONG={int((df['direction']=='LONG').sum())} SHORT={int((df['direction']=='SHORT').sum())} | closed={n_closed}",
        "",
        "## Haupttabelle",
        "",
        "| Variant | Long Size | Short Size | Net | Net/Trade | PF | Max DD | WR |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for s in sizing_summaries:
        lines.append(
            f"| {s['variant']} | {s['long_size']:.2f} | {s['short_size']:.2f} | {fmt(s['net'])} | "
            f"{fmt(s['net_per_trade'],3)} | {fmt(s['profit_factor'])} | {fmt(s['max_drawdown'])} | {fmt(s['winrate'],1)} |"
        )
    lines += [
        "",
        "## Equal vs SHORT_50",
        f"- ΔNet={fmt(d_net,3)} ΔMaxDD={fmt(d_dd,3)} ΔPF={fmt(d_pf,3)}",
        f"- SHORT_50 better both halves: `{both_halves}`",
        "",
        "## SHORT diagnostic (linear scale only)",
        "",
    ]
    for row in short_diag:
        lines.append(f"- size={row['short_size']}: net={fmt(row['net'])} mean={fmt(row['mean_pnl'],3)}")
    lines += ["", "## Strategy Logic Changed", "", "`NO`", ""]
    (OUT / "summary.md").write_text("\n".join(lines), encoding="utf-8")

    print("PRIMARY", primary, flush=True)
    print("REC", recommendation, flush=True)
    print("GUARD", guard_ok, "closed", n_closed, "net", base_net, flush=True)
    for s in sizing_summaries:
        print(
            f"  {s['variant']}: net={s['net']:.3f} npt={s['net_per_trade']:.3f} PF={s['profit_factor']} "
            f"DD={s['max_drawdown']:.3f} WR={s['winrate']}",
            flush=True,
        )
    print("Δ50 net", d_net, "dd", d_dd, "pf", d_pf, "halves", both_halves, flush=True)
    print("wrote", OUT, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
