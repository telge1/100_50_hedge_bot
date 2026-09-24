"""Run mirrored 5m lower-pool bounce backtest (OB + delta + SL/TP)."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
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

from ob_microstructure_breakout_bot.exit_backtest.pool_bounce_long_backtest import (
    BOUNCE_MIN_PCT,
    BOUNCE_HOLD_BARS,
    MIN_UPPER_GAP_PCT,
    SL_BELOW_POOL_BOTTOM_PCT,
    WATCH_BEFORE_PCT,
    analyze_signal_long_bounce_backtest,
    summarize_backtest,
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
    n_signals: int,
) -> str:
    overall = summary.get("overall") or {}
    tradeable = summary.get("tradeable_only") or {}
    lines = [
        f"# 5m Pool Bounce Long Backtest ({symbol})",
        "",
        "Mirrored companion to `results/ob_pool_5m_bounce_rule.md`.",
        "",
        f"- universe: `{universe}`",
        f"- n_signals: **{n_signals}**",
        f"- min TP room (entry -> next upper pool bottom): **{MIN_UPPER_GAP_PCT}%**",
        f"- long SL: touched pool bottom - **{SL_BELOW_POOL_BOTTOM_PCT}%**",
        f"- long TP: nearest ACTIVE upper pool **bottom**",
        f"- OB/delta watch start: **{WATCH_BEFORE_PCT}%** before support touch",
        "",
        "## Overall (all ranks)",
        "",
        f"- rows: **{overall.get('n')}**",
        f"- reached: **{overall.get('n_reached')}**",
        f"- bounce / pierce / weak: "
        f"**{overall.get('n_bounce')}** / **{overall.get('n_pierce')}** / **{overall.get('n_weak')}**",
        f"- bounce rate (reached): **"
        + (
            f"{100.0 * overall['bounce_rate_of_reached']:.0f}%"
            if overall.get("bounce_rate_of_reached") is not None
            else "—"
        )
        + "**",
        f"- mean bounce % when bounce: **"
        + (
            f"{overall['mean_bounce_pct_when_bounce']:.2f}%"
            if overall.get("mean_bounce_pct_when_bounce") is not None
            else "—"
        )
        + "**",
        "",
        "## Tradeable gap filter (TP room >= 0.8%)",
        "",
        f"- tradeable rows: **{tradeable.get('n_tradeable_gap')}**",
        f"- bounce rate among tradeable reached: **"
        + (
            f"{100.0 * tradeable['tradeable_bounce_rate']:.0f}%"
            if tradeable.get("tradeable_bounce_rate") is not None
            else "—"
        )
        + "**",
        f"- mean upper gap %: **"
        + (
            f"{tradeable['mean_upper_gap_pct']:.2f}%"
            if tradeable.get("mean_upper_gap_pct") is not None
            else "—"
        )
        + "**",
        "",
        "## Long trades (gap OK + reversal candle)",
        "",
        f"- trades taken: **{overall.get('n_trades')}**",
        f"- winrate: **"
        + (
            f"{100.0 * overall['trade_winrate']:.0f}%"
            if overall.get("trade_winrate") is not None
            else "—"
        )
        + "**",
        f"- mean / sum pnl %: **"
        + (
            f"{overall['trade_mean_pnl_pct']:.3f}% / {overall['trade_sum_pnl_pct']:.2f}%"
            if overall.get("trade_mean_pnl_pct") is not None
            else "—"
        )
        + "**",
        f"- exits: `{overall.get('by_exit_reason')}`",
        "",
        "## Flow-confirmed long trades only",
        "",
        f"- flow trades: **{overall.get('n_flow_trades')}**",
        f"- winrate: **"
        + (
            f"{100.0 * overall['flow_trade_winrate']:.0f}%"
            if overall.get("flow_trade_winrate") is not None
            else "—"
        )
        + "**",
        f"- mean / sum pnl %: **"
        + (
            f"{overall['flow_trade_mean_pnl_pct']:.3f}% / {overall['flow_trade_sum_pnl_pct']:.2f}%"
            if overall.get("flow_trade_mean_pnl_pct") is not None
            else "—"
        )
        + "**",
        "",
        "## Flow confirmation (OB<=0.95 + Δ<=-100k at touch)",
        "",
        f"- flow-confirmed reached: **{overall.get('n_flow_confirmed')}**",
        f"- bounce rate when flow-confirmed: **"
        + (
            f"{100.0 * overall['flow_confirmed_bounce_rate']:.0f}%"
            if overall.get("flow_confirmed_bounce_rate") is not None
            else "—"
        )
        + "**",
        f"- gap + flow bounce rate: **"
        + (
            f"{100.0 * overall['gap_and_flow_bounce_rate']:.0f}%"
            if overall.get("gap_and_flow_bounce_rate") is not None
            else "—"
        )
        + f"** (n={overall.get('n_gap_and_flow')})",
        "",
        "## By rank",
        "",
        "| rank | reached | bounce rate | pierce rate | tradeable bounce rate | mean bounce % |",
        "|---|---|---|---|---|---|",
    ]
    by_rank = summary.get("by_rank") or {}
    for key in sorted(by_rank.keys(), key=lambda k: int(k.split("_")[-1])):
        r = by_rank[key]
        br = r.get("bounce_rate_of_reached")
        pr = r.get("pierce_rate_of_reached")
        tbr = r.get("tradeable_bounce_rate")
        mb = r.get("mean_bounce_pct_when_bounce")
        lines.append(
            "| {rank} | {nr}/{n} | {br} | {pr} | {tbr} | {mb} |".format(
                rank=key.replace("rank_", ""),
                nr=r.get("n_reached"),
                n=r.get("n"),
                br=(f"{100.0 * br:.0f}%" if br is not None else "—"),
                pr=(f"{100.0 * pr:.0f}%" if pr is not None else "—"),
                tbr=(f"{100.0 * tbr:.0f}%" if tbr is not None else "—"),
                mb=(f"{mb:.2f}%" if mb is not None else "—"),
            )
        )
    by_source = summary.get("by_source") or {}
    if by_source:
        lines.extend(
            [
                "",
                "## By source",
                "",
                "| source | reached | bounce rate | flow-confirmed bounce | n_flow |",
                "|---|---|---|---|---|",
            ]
        )
        for src in sorted(by_source.keys()):
            r = by_source[src]
            br = r.get("bounce_rate_of_reached")
            fr = r.get("flow_confirmed_bounce_rate")
            lines.append(
                "| {src} | {nr}/{n} | {br} | {fr} | {nf} |".format(
                    src=src,
                    nr=r.get("n_reached"),
                    n=r.get("n"),
                    br=(f"{100.0 * br:.0f}%" if br is not None else "—"),
                    fr=(f"{100.0 * fr:.0f}%" if fr is not None else "—"),
                    nf=r.get("n_flow_confirmed"),
                )
            )
    lines.extend(
        [
            "",
            "## Columns",
            "",
            "Each row has: `decision_ts`, `touch_ts`, `rank`, `pool_bottom/top`, "
            "`next_upper_pool_gap_pct`, `tradeable_gap`, `watch_ts`, "
            "`bounce_pct`, `pierce_pct`, `outcome`, "
            "`ob_ratio_at_watch/touch`, `delta_at_watch/touch`.",
            "",
        ]
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="5m mirrored pool bounce backtest")
    parser.add_argument("--symbol", default="DOGEUSDT")
    parser.add_argument(
        "--universe",
        choices=("locked10", "full"),
        default="locked10",
        help="locked10 = calibrated 10 longs; full = scanner∪strong∪fakeout window",
    )
    parser.add_argument("--signals", type=Path, default=_DEFAULT_SIGNALS)
    parser.add_argument("--strong", type=Path, default=_DEFAULT_STRONG)
    parser.add_argument("--fakeouts", type=Path, default=_DEFAULT_FAKEOUT)
    parser.add_argument(
        "--sources",
        default="scanner_breakout,strong_breakout,fakeout",
        help="Comma list for --universe full",
    )
    parser.add_argument(
        "--max-ranks",
        type=int,
        default=2,
        help="Only trade top-N lower clusters by strength (default 2 = Rank1+Rank2)",
    )
    parser.add_argument("--hold-hours", type=int, default=24)
    parser.add_argument("--no-flow", action="store_true", help="Skip OB/delta sampling")
    parser.add_argument(
        "--out-json",
        type=Path,
        default=_REPO / "results" / "ob_pool_5m_bounce_long_backtest.json",
    )
    parser.add_argument(
        "--out-md",
        type=Path,
        default=_REPO / "results" / "ob_pool_5m_bounce_long_backtest.md",
    )
    args = parser.parse_args(argv)

    _load_dotenv()
    symbol = args.symbol.upper().replace("/", "")
    if args.universe == "full":
        sources = [s.strip() for s in str(args.sources).split(",") if s.strip()]
        longs = load_full_history_longs(
            symbol=symbol,
            scanner_path=args.signals,
            strong_path=args.strong,
            fakeout_path=args.fakeouts,
            sources=sources,
        )
    else:
        longs = load_calibrated_longs(args.signals)
    if not longs:
        raise SystemExit("No long signals")

    print(
        f"universe={args.universe} n={len(longs)} sample_flow={not args.no_flow}",
        flush=True,
    )
    signals: list[dict[str, Any]] = []
    for row in longs:
        decision_ts = _parse_ts(str(row["decision_ts"]))
        print(f"backtest {decision_ts.isoformat()} ...", flush=True)
        try:
            res = analyze_signal_long_bounce_backtest(
                symbol,
                decision_ts=decision_ts,
                entry_price=_entry_price(symbol, decision_ts),
                tier=str(row.get("tier") or ""),
                source=str(row.get("source") or ""),
                hold_hours=int(args.hold_hours),
                max_ranks=int(args.max_ranks),
                sample_flow=not bool(args.no_flow),
            )
        except Exception as exc:  # noqa: BLE001
            res = {
                "decision_ts": decision_ts.isoformat(),
                "error": str(exc),
                "rows": [],
            }
            print(f"  ERROR: {exc}", flush=True)
        signals.append(res)
        for r in (res.get("rows") or [])[:2]:
            print(
                f"  rank{r.get('rank')} gap={r.get('next_upper_pool_gap_pct')} "
                f"tradeable={r.get('tradeable_gap')} outcome={r.get('outcome')} "
                f"bounce={r.get('bounce_pct')} touch={r.get('touch_ts')} "
                f"taken={r.get('trade_taken')} exit={r.get('exit_reason')} "
                f"pnl={r.get('pnl_pct')}",
                flush=True,
            )

    summary = summarize_backtest(signals)
    payload = {
        "symbol": symbol,
        "universe": args.universe,
        "rule": "ob_pool_5m_bounce_long_rule.md",
        "params": {
            "min_upper_gap_pct": MIN_UPPER_GAP_PCT,
            "watch_before_pct": WATCH_BEFORE_PCT,
            "sl_below_pool_bottom_pct": SL_BELOW_POOL_BOTTOM_PCT,
            "bounce_min_pct": BOUNCE_MIN_PCT,
            "hold_bars": BOUNCE_HOLD_BARS,
            "sample_flow": not bool(args.no_flow),
        },
        "summary": summary,
        "signals": signals,
    }
    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    args.out_md.write_text(
        _md_report(
            symbol=symbol,
            universe=args.universe,
            summary=summary,
            n_signals=len(signals),
        ),
        encoding="utf-8",
    )
    print("\n=== SUMMARY ===")
    print(json.dumps(summary, indent=2))
    print(f"wrote {args.out_json}")
    print(f"wrote {args.out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

