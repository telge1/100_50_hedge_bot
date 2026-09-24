"""Run 5m cluster bounce analysis (largest mass first, no OB)."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[2]
_EXTRA = [
    "/home/telgenbuescher/projects/Signal_Generator_Ralf/signal_generator_stoch_waves/src",
    "/home/telgenbuescher/projects/orderbook_analyse/src",
    str(_REPO),
    str(_REPO / "dashboard"),
]
for _p in reversed(_EXTRA):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ob_microstructure_breakout_bot.exit_backtest.pool_bounce import (
    BOUNCE_HOLD_BARS,
    BOUNCE_MIN_PCT,
    analyze_signal_bounce,
    summarize_bounce_by_rank,
)
from ob_microstructure_breakout_bot.exit_backtest.run_longs import (
    _load_dotenv,
    _parse_ts,
    load_calibrated_longs,
    load_full_history_longs,
)

_EVENTS = Path(__file__).resolve().parent.parent / "calibration" / "events"
_DEFAULT_SIGNALS = _EVENTS / "DOGEUSDT_backtest_legacy_vs_calibrated.json"
_DEFAULT_STRONG = _EVENTS / "DOGEUSDT_strong_breakouts_phase_a.json"
_DEFAULT_FAKEOUT = _EVENTS / "DOGEUSDT_fakeouts_phase_b.json"


def _entry_price(symbol: str, decision_ts: datetime) -> float:
    from ob_microstructure_breakout_bot.data.bars import load_5m_bars

    bars = load_5m_bars(
        symbol,
        decision_ts - timedelta(minutes=30),
        decision_ts + timedelta(minutes=10),
    )
    for b in bars:
        if b.ts == decision_ts:
            return float(b.open)
    prior = [b for b in bars if b.ts < decision_ts]
    if prior:
        return float(prior[-1].close)
    raise RuntimeError(f"No entry bar near {decision_ts.isoformat()}")


def _md_report(
    *,
    symbol: str,
    universe: str,
    summary: dict[str, Any],
    by_rank: dict[str, Any],
    n_signals: int,
) -> str:
    lines = [
        f"# 5m Cluster Bounce Analysis ({symbol})",
        "",
        f"- universe: `{universe}`",
        f"- n_signals: **{n_signals}**",
        f"- timeframe: **5m** ACTIVE upper pools → mass clusters",
        f"- bounce window: **{BOUNCE_HOLD_BARS}** × 5m bars (~{BOUNCE_HOLD_BARS * 5 / 60:.1f}h)",
        f"- bounce min reverse: **{BOUNCE_MIN_PCT}%** of entry",
        "- OB / public-trade: **not used** (price structure only)",
        "",
        "## Method",
        "",
        "1. At each long signal, load causal 5m ACTIVE upper pools.",
        "2. Group into clusters (gap ≤ 0.10%).",
        "3. Rank by `strength_sum` (rank 1 = heaviest).",
        "4. On first touch into each ranked cluster, measure reverse from local high.",
        "5. Aggregate bounce / pierce rates per rank.",
        "",
        "## By mass rank",
        "",
        "| rank | reached | bounce rate | pierce rate | mean bounce % (when bounced) | mean strength | mean dist % |",
        "|---|---|---|---|---|---|---|",
    ]
    for key in sorted(by_rank.keys(), key=lambda k: int(k.split("_")[-1])):
        r = by_rank[key]
        br = r.get("bounce_rate_of_reached")
        pr = r.get("pierce_rate_of_reached")
        mb = r.get("mean_bounce_pct_when_bounced")
        lines.append(
            "| {rank} | {nr}/{ns} ({rr}) | {br} | {pr} | {mb} | {ms} | {md} |".format(
                rank=r.get("rank"),
                nr=r.get("n_reached"),
                ns=r.get("n_signals_with_cluster"),
                rr=(
                    f"{100.0 * r['reach_rate']:.0f}%"
                    if r.get("reach_rate") is not None
                    else "—"
                ),
                br=(f"{100.0 * br:.0f}%" if br is not None else "—"),
                pr=(f"{100.0 * pr:.0f}%" if pr is not None else "—"),
                mb=(f"{mb:.2f}%" if mb is not None else "—"),
                ms=(
                    f"{r['mean_strength_sum']:.2f}"
                    if r.get("mean_strength_sum") is not None
                    else "—"
                ),
                md=(
                    f"{r['mean_dist_pct']:.2f}%"
                    if r.get("mean_dist_pct") is not None
                    else "—"
                ),
            )
        )
    lines.extend(
        [
            "",
            "## Overall",
            "",
            f"- signals analyzed: **{summary.get('n_signals')}**",
            f"- mean clusters/signal: **{summary.get('mean_clusters_per_signal')}**",
            f"- mean ranked reached/signal: **{summary.get('mean_reached_per_signal')}**",
            "",
            "## Takeaways",
            "",
        ]
    )
    r1 = by_rank.get("rank_1") or {}
    r2 = by_rank.get("rank_2") or {}
    if r1.get("bounce_rate_of_reached") is not None:
        lines.append(
            f"- Heaviest cluster (rank 1): bounce rate "
            f"**{100.0 * r1['bounce_rate_of_reached']:.0f}%** of reached"
            + (
                f", mean reverse **{r1['mean_bounce_pct_when_bounced']:.2f}%**"
                if r1.get("mean_bounce_pct_when_bounced") is not None
                else ""
            )
            + "."
        )
    if r2.get("bounce_rate_of_reached") is not None:
        lines.append(
            f"- Next-smaller (rank 2): bounce rate "
            f"**{100.0 * r2['bounce_rate_of_reached']:.0f}%** of reached"
            + (
                f", mean reverse **{r2['mean_bounce_pct_when_bounced']:.2f}%**"
                if r2.get("mean_bounce_pct_when_bounced") is not None
                else ""
            )
            + "."
        )
    lines.append("- Next: add OB/delta only after these structure rates look stable.")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="5m cluster bounce analysis (no OB)")
    parser.add_argument("--symbol", default="DOGEUSDT")
    parser.add_argument(
        "--universe",
        choices=("locked10", "scanner"),
        default="locked10",
    )
    parser.add_argument("--signals", type=Path, default=_DEFAULT_SIGNALS)
    parser.add_argument("--strong", type=Path, default=_DEFAULT_STRONG)
    parser.add_argument("--fakeouts", type=Path, default=_DEFAULT_FAKEOUT)
    parser.add_argument("--max-ranks", type=int, default=5)
    parser.add_argument("--hold-hours", type=int, default=24)
    parser.add_argument("--hold-bars", type=int, default=BOUNCE_HOLD_BARS)
    parser.add_argument("--bounce-min-pct", type=float, default=BOUNCE_MIN_PCT)
    parser.add_argument(
        "--out-json",
        type=Path,
        default=_REPO / "results" / "ob_pool_5m_cluster_bounce.json",
    )
    parser.add_argument(
        "--out-md",
        type=Path,
        default=_REPO / "results" / "ob_pool_5m_cluster_bounce.md",
    )
    args = parser.parse_args(argv)

    _load_dotenv()
    symbol = args.symbol.upper().replace("/", "")
    if args.universe == "scanner":
        longs = load_full_history_longs(
            symbol=symbol,
            scanner_path=args.signals,
            strong_path=args.strong,
            fakeout_path=args.fakeouts,
            sources=["scanner_breakout"],
        )
    else:
        longs = load_calibrated_longs(args.signals)

    if not longs:
        raise SystemExit("No long signals")

    print(f"universe={args.universe} n={len(longs)} symbol={symbol}", flush=True)
    rows: list[dict[str, Any]] = []
    for row in longs:
        decision_ts = _parse_ts(str(row["decision_ts"]))
        print(f"analyzing {decision_ts.isoformat()} [{row.get('source')}] ...", flush=True)
        try:
            entry = _entry_price(symbol, decision_ts)
            res = analyze_signal_bounce(
                symbol,
                decision_ts=decision_ts,
                entry_price=entry,
                tier=str(row.get("tier") or ""),
                source=str(row.get("source") or ""),
                hold_hours=int(args.hold_hours),
                hold_bars=int(args.hold_bars),
                bounce_min_pct=float(args.bounce_min_pct),
                max_ranks=int(args.max_ranks),
            )
        except Exception as exc:  # noqa: BLE001
            res = {
                "decision_ts": decision_ts.isoformat(),
                "error": str(exc),
                "events": [],
            }
            print(f"  ERROR: {exc}", flush=True)
        rows.append(res)
        evs = res.get("events") or []
        r1 = next((e for e in evs if e.get("rank") == 1), None)
        if r1:
            print(
                f"  rank1 strength={r1.get('strength_sum'):.2f} "
                f"reached={r1.get('reached')} bounced={r1.get('bounced')} "
                f"bounce_pct={r1.get('bounce_pct')} outcome={r1.get('outcome')}",
                flush=True,
            )

    by_rank = summarize_bounce_by_rank(rows)
    n_ok = sum(1 for r in rows if not r.get("error"))
    mean_clusters = (
        sum(int(r.get("n_clusters") or 0) for r in rows if not r.get("error")) / n_ok
        if n_ok
        else None
    )
    mean_reached = (
        sum(int(r.get("n_reached") or 0) for r in rows if not r.get("error")) / n_ok
        if n_ok
        else None
    )
    summary = {
        "n_signals": len(rows),
        "n_ok": n_ok,
        "mean_clusters_per_signal": mean_clusters,
        "mean_reached_per_signal": mean_reached,
        "bounce_min_pct": float(args.bounce_min_pct),
        "hold_bars": int(args.hold_bars),
        "max_ranks": int(args.max_ranks),
    }
    payload = {
        "symbol": symbol,
        "universe": args.universe,
        "summary": summary,
        "by_rank": by_rank,
        "signals": rows,
    }
    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    args.out_md.write_text(
        _md_report(
            symbol=symbol,
            universe=args.universe,
            summary=summary,
            by_rank=by_rank,
            n_signals=len(rows),
        ),
        encoding="utf-8",
    )
    print("\n=== BY RANK ===")
    print(json.dumps(by_rank, indent=2))
    print(f"wrote {args.out_json}")
    print(f"wrote {args.out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
