"""Run EMA59-touch-triggered pool/cluster calibration over full DOGE history."""

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

from ob_microstructure_breakout_bot.backtest.scan_touches import detect_ema59_touches
from ob_microstructure_breakout_bot.data.bars import load_5m_bars
from ob_microstructure_breakout_bot.exit_backtest.cluster_mass import DEFAULT_CLUSTER_GAP_PCT
from ob_microstructure_breakout_bot.exit_backtest.ema59_touch_cluster import (
    analyze_touch_clusters,
    summarize_touch_results,
)
from ob_microstructure_breakout_bot.models import TouchDirection

SCAN_FROM = datetime(2026, 9, 5, 17, 0, tzinfo=timezone.utc)
SCAN_TO = datetime(2026, 9, 19, 11, 0, tzinfo=timezone.utc)


def _load_dotenv() -> None:
    env_path = Path(
        "/home/telgenbuescher/projects/Signal_Generator_Ralf/"
        "signal_generator_stoch_waves/.env"
    )
    if env_path.exists():
        try:
            from dotenv import load_dotenv

            load_dotenv(env_path)
        except ImportError:
            pass


def _parse_utc(value: str) -> datetime:
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def derive_hypotheses(summary: dict[str, Any]) -> list[str]:
    hyps: list[str] = []
    by_side = summary.get("by_side") or {}
    for side, st in by_side.items():
        if not st.get("n"):
            continue
        hyps.append(
            f"{side}: n={st['n']}, mean max vs EMA59={st['mean_exc_vs_ema59_pct']:.2f}%, "
            f"p75={st['p75_exc_vs_ema59_pct']:.2f}%, strongest-hit={st['strongest_hit_rate']:.0%}, "
            f"EMA59-reclaim after extreme={st['pct_reclaimed_ema59']:.0%}."
        )
    grid = summary.get("dist_delta_hit_grid") or []
    near = [g for g in grid if g.get("dist_band") == "0.0-0.8%" and (g.get("n_clusters") or 0)]
    far = [
        g
        for g in grid
        if g.get("dist_band") == "1.5-2.5%" and g.get("delta_bucket") == "abs_delta_ge_200k"
    ]
    if near:
        n = sum(g["n_clusters"] for g in near)
        hit = sum((g.get("hit_rate") or 0) * g["n_clusters"] for g in near)
        if n and hit / n >= 0.65:
            hyps.append(
                "From EMA59 touch: near clusters (<0.8%) are usually reachable → checkpoint/noise."
            )
    if far and far[0].get("hit_rate") is not None and far[0]["hit_rate"] < 0.45:
        hyps.append(
            "From EMA59 touch: far clusters (1.5–2.5%) need strong |Δ| + mass/OB or stay unreachable."
        )
    return hyps


def write_md(path: Path, payload: dict[str, Any], hyps: list[str]) -> None:
    lines = [
        "# EMA59-Touch Pool/Cluster Calibration",
        "",
        "Trigger = **EMA59 touch** (same as DOGE OB calibration).",
        "Measure = max excursion **above/below EMA59**, then pool clusters on that path.",
        "",
        f"Symbol: `{payload.get('symbol')}`",
        f"Window: `{payload.get('scan_from')}` → `{payload.get('scan_to')}`",
        f"Touches analyzed: **{payload.get('n_analyzed')}** "
        f"(first-in-cluster only={payload.get('only_first_in_cluster')})",
        "",
        "## By side",
        "",
        "```json",
        json.dumps((payload.get("summary") or {}).get("by_side") or {}, indent=2),
        "```",
        "",
        "## Dist × |Δ| reachability",
        "",
        "```json",
        json.dumps((payload.get("summary") or {}).get("dist_delta_hit_grid") or [], indent=2),
        "```",
        "",
        "## Hypotheses",
        "",
    ]
    for h in hyps:
        lines.append(f"- {h}")
    lines += [
        "",
        "## Rule draft",
        "",
        "1. Start clock at EMA59 touch (`touch_ts`), not at decision_ts.",
        "2. Target = heaviest cluster on the continuation side of EMA59.",
        "3. Near micro-clusters under ~0.8% are usually noise.",
        "4. Far clusters only if |confirm Δ| strong and OB supportive.",
        "",
        f"JSON: `{payload.get('json_out')}`",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="EMA59-touch triggered pool/cluster calibration"
    )
    parser.add_argument("--symbol", default="DOGEUSDT")
    parser.add_argument("--scan-from", default=SCAN_FROM.isoformat())
    parser.add_argument("--scan-to", default=SCAN_TO.isoformat())
    parser.add_argument(
        "--side",
        default="both",
        choices=["long", "short", "both"],
        help="from_below=long, from_above=short",
    )
    parser.add_argument(
        "--all-cluster-mates",
        action="store_true",
        help="Include every touch, not only first-in-cluster",
    )
    parser.add_argument("--hold-bars", type=int, default=144, help="Forward 5m bars (default 12h)")
    parser.add_argument("--gap-pct", type=float, default=DEFAULT_CLUSTER_GAP_PCT)
    parser.add_argument(
        "--out",
        type=Path,
        default=_REPO / "results" / "ob_pool_ema59_touch_cluster_calibration.json",
    )
    parser.add_argument(
        "--md-out",
        type=Path,
        default=_REPO / "results" / "ob_pool_ema59_touch_cluster_calibration.md",
    )
    parser.add_argument("--limit", type=int, default=0, help="Debug: max touches")
    args = parser.parse_args(argv)

    _load_dotenv()
    symbol = args.symbol.upper().replace("/", "")
    start = _parse_utc(args.scan_from)
    end = _parse_utc(args.scan_to)
    only_first = not args.all_cluster_mates

    print("Loading 5m bars + detecting EMA59 touches ...", flush=True)
    warmup = start - timedelta(minutes=5 * 220)
    load_end = end + timedelta(minutes=5 * (args.hold_bars + 8))
    bars = load_5m_bars(symbol, warmup, load_end)
    touches = detect_ema59_touches(bars)
    selected = []
    for t in touches:
        if t.bar_ts < start or t.bar_ts >= end:
            continue
        if only_first and not t.is_first_in_cluster:
            continue
        if args.side == "long" and t.direction != TouchDirection.FROM_BELOW:
            continue
        if args.side == "short" and t.direction != TouchDirection.FROM_ABOVE:
            continue
        selected.append(t)
    if args.limit and args.limit > 0:
        selected = selected[: args.limit]

    print(
        f"  bars={len(bars)} touches_in_window={len(selected)} "
        f"(only_first={only_first}, side={args.side})",
        flush=True,
    )

    results: list[dict[str, Any]] = []
    errors = 0
    for i, touch in enumerate(selected, 1):
        print(
            f"[{i}/{len(selected)}] {touch.direction.value} {touch.bar_ts.isoformat()} ...",
            flush=True,
        )
        try:
            rec = analyze_touch_clusters(
                symbol,
                touch,
                bars,
                hold_bars=int(args.hold_bars),
                gap_pct=float(args.gap_pct),
            )
        except Exception as exc:  # noqa: BLE001
            errors += 1
            rec = {
                "touch_ts": touch.bar_ts.isoformat(),
                "direction": touch.direction.value,
                "error": str(exc),
            }
            print(f"  ERROR: {exc}", flush=True)
        results.append(rec)
        if not rec.get("error"):
            print(
                f"  side={rec.get('side')} vsEMA59=+{rec.get('max_exc_vs_ema59_pct'):.2f}% "
                f"clusters {rec.get('n_reached')}/{rec.get('n_clusters')} "
                f"Δc={rec.get('confirm_delta'):+.0f}",
                flush=True,
            )

    summary = summarize_touch_results(results)
    hyps = derive_hypotheses(summary)
    payload = {
        "symbol": symbol,
        "scan_from": start.isoformat(),
        "scan_to": end.isoformat(),
        "trigger": "ema59_touch",
        "only_first_in_cluster": only_first,
        "side_filter": args.side,
        "hold_bars": int(args.hold_bars),
        "gap_pct": float(args.gap_pct),
        "n_analyzed": len(results),
        "n_errors": errors,
        "summary": summary,
        "tp_hypotheses": hyps,
        "json_out": str(args.out),
        "touches": results,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    write_md(args.md_out, payload, hyps)

    print("\n=== HYPOTHESES ===")
    for h in hyps:
        print(f"- {h}")
    print(f"wrote {args.out}")
    print(f"wrote {args.md_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
