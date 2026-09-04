"""CLI for the market profile research run.

Read-only: queries ClickHouse, writes PNG/JSON/CSV/Markdown into a results
directory. No ClickHouse writes, no strategy or execution imports.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import (
    ANCHOR_MODES,
    DEFAULT_SESSIONS,
    DEFAULT_TARGET_BINS,
    DEFAULT_VALUE_AREA_PCT,
    FORMAT_VERSION,
    SESSIONS,
)
from .anchor import build_windows
from .build import build_profiles, load_chart_candles, mark_naked_pocs
from .contracts import MarketProfile, RunSpec, ShapeThresholds
from .loader import default_client
from .render import render_profile_chart

OA_ROOT = Path(__file__).resolve().parents[3]


def _parse_ts(raw: str) -> datetime:
    s = str(raw).strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.isoformat().replace("+00:00", "Z")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_market_profile_v1",
        description=(
            "Volume-based market profile over explicitly anchored windows. "
            "Renders candles + per-window histograms with POC/VAH/VAL, HVN/LVN, "
            "single-print ranges and naked POCs."
        ),
    )
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument(
        "--start", required=True, help="UTC ISO timestamp, e.g. 2026-08-24T00:00:00Z"
    )
    p.add_argument("--end", required=True, help="UTC ISO timestamp (exclusive)")
    p.add_argument(
        "--anchor",
        default="day",
        choices=list(ANCHOR_MODES),
        help=(
            "day: one profile per UTC day. session: one per liquidity session "
            "(the crypto stand-in for a cash session). composite: one merged "
            "profile over the whole range."
        ),
    )
    p.add_argument(
        "--sessions",
        default=",".join(DEFAULT_SESSIONS),
        help=f"comma list for --anchor session; available: {','.join(SESSIONS)}",
    )
    p.add_argument("--timeframe", default="15m", help="candle timeframe for the chart")
    p.add_argument("--value-area-pct", type=float, default=DEFAULT_VALUE_AREA_PCT)
    p.add_argument("--target-bins", type=int, default=DEFAULT_TARGET_BINS)
    p.add_argument(
        "--no-final",
        action="store_true",
        help="skip FINAL on the trade scan (~60x faster, relies on merged parts)",
    )
    p.add_argument("--theme", default="dark", choices=["dark", "light"])
    p.add_argument("--no-single-prints", action="store_true")
    p.add_argument("--no-lvn", action="store_true")
    p.add_argument(
        "--out-dir",
        default=str(OA_ROOT / "results" / "market_profile_v1"),
        help="results directory (created if absent)",
    )
    p.add_argument("--quiet", action="store_true")
    return p


def _write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _write_levels_csv(path: Path, profiles: list[MarketProfile]) -> None:
    cols = [
        "window_id",
        "label",
        "anchor_mode",
        "start",
        "end",
        "shape_kind",
        "shape_letter",
        "poc",
        "vah",
        "val",
        "price_low",
        "price_high",
        "price_step",
        "va_range_share",
        "directional_share",
        "poc_position",
        "poc_concentration",
        "total_volume",
        "delta",
        "trades",
        "naked_poc",
        "poc_revisit_ts",
        "hvn_count",
        "lvn_count",
        "single_print_ranges",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for p in profiles:
            w.writerow(
                [
                    p.window.window_id,
                    p.window.label,
                    p.window.anchor_mode,
                    _iso(p.window.start),
                    _iso(p.window.end),
                    p.shape.kind,
                    p.shape.letter,
                    f"{p.value_area.poc:.10g}",
                    f"{p.value_area.vah:.10g}",
                    f"{p.value_area.val:.10g}",
                    f"{p.price_low:.10g}",
                    f"{p.price_high:.10g}",
                    f"{p.price_step:.10g}",
                    f"{p.shape.va_range_share:.4f}",
                    f"{p.shape.directional_share:.4f}",
                    f"{p.shape.poc_position:.4f}",
                    f"{p.shape.poc_concentration:.4f}",
                    f"{p.total_volume:.6f}",
                    f"{p.delta:.6f}",
                    p.trades,
                    p.naked_poc,
                    _iso(p.poc_revisit_ts),
                    len(p.nodes.hvn),
                    len(p.nodes.lvn),
                    len(p.nodes.single_print_ranges),
                ]
            )


def _write_report(
    path: Path, spec: RunSpec, profiles: list[MarketProfile], chart: Path
) -> None:
    lines: list[str] = []
    a = lines.append
    a("# Market Profile V1")
    a("")
    a(f"- format: `{FORMAT_VERSION}`")
    a(f"- symbol: **{spec.symbol}**")
    a(f"- range: `{_iso(spec.start)}` → `{_iso(spec.end)}` (end exclusive, UTC)")
    a(f"- anchor: **{spec.anchor_mode}**" + (f" ({', '.join(spec.sessions)})" if spec.anchor_mode == "session" else ""))
    a(f"- chart timeframe: `{spec.timeframe}`")
    a(f"- value area: {spec.value_area_pct:.0%} of volume, ~{spec.target_bins} bins per window")
    a(f"- trade scan dedupe (FINAL): `{spec.use_final}`")
    a(f"- profiles built: **{len(profiles)}**")
    a(f"- chart: `{chart.name}`")
    a("")
    a("## What the chart shows")
    a("")
    a("Volume is taker-side split per bin: green is aggressive buying, red is")
    a("aggressive selling. `side` in `public_trades_canonical` is the aggressor,")
    a("so this is real initiative, not an uptick/downtick proxy.")
    a("")
    a("Level lifetime is intentional: POC and value-area edges are solid inside")
    a("their own window and thinner across the following one. A naked POC keeps")
    a("running to the right edge because it has not been retested.")
    a("")
    a("## Shape distribution")
    a("")
    counts: dict[str, int] = {}
    for p in profiles:
        counts[p.shape.kind] = counts.get(p.shape.kind, 0) + 1
    a("| kind | windows |")
    a("|---|---|")
    for k in sorted(counts, key=lambda x: -counts[x]):
        a(f"| {k} | {counts[k]} |")
    a("")
    naked = [p for p in profiles if p.naked_poc]
    a(f"Naked POCs (untested since their window closed): **{len(naked)}**")
    if naked:
        a("")
        for p in naked:
            a(f"- `{p.window.label}` POC {p.value_area.poc:.10g}")
    a("")
    a("## Per-window levels")
    a("")
    a("| window | shape | POC | VAH | VAL | VA/range | dir | delta | naked |")
    a("|---|---|---|---|---|---|---|---|---|")
    for p in profiles:
        a(
            f"| {p.window.label} | {p.shape.letter} {p.shape.kind} "
            f"| {p.value_area.poc:.10g} | {p.value_area.vah:.10g} | {p.value_area.val:.10g} "
            f"| {p.shape.va_range_share:.2f} | {p.shape.directional_share:+.2f} "
            f"| {p.delta:+.2f} | {'yes' if p.naked_poc else 'no'} |"
        )
    a("")
    a("## Calibration status")
    a("")
    a("The levels (POC, VAH, VAL, HVN, LVN, naked POC) are mechanical and carry no")
    a("free parameters beyond the value-area share and the bin count.")
    a("")
    a("The balance/trend verdict does not have that status. Its cut-offs are")
    a("centred on the observed metric distribution of 42 day-anchored BTCUSDT")
    a("windows (2026-07-20..2026-08-31) and have **not** been validated against")
    a("realised outcomes — nobody has yet checked whether a window labelled")
    a("`BALANCE` actually respects its value-area edges more often than a")
    a("`TREND` one. Treat `kind` as a descriptive summary, not as an edge.")
    a("")
    a("`levels.csv` carries the raw metrics (`va_range_share`,")
    a("`directional_share`, `poc_position`, `poc_concentration`) so the rule can")
    a("be re-tuned without recomputing the profiles. Windows falling between the")
    a("cut-offs are reported as `UNCLEAR` rather than forced into a class.")
    a("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    quiet = bool(args.quiet)

    def log(msg: str) -> None:
        if not quiet:
            print(msg, flush=True)

    start, end = _parse_ts(args.start), _parse_ts(args.end)
    if end <= start:
        print("error: --end must be after --start", file=sys.stderr)
        return 2

    sessions = tuple(s.strip() for s in str(args.sessions).split(",") if s.strip())
    spec = RunSpec(
        symbol=str(args.symbol).strip().upper(),
        start=start,
        end=end,
        anchor_mode=args.anchor,
        sessions=sessions,
        timeframe=str(args.timeframe).strip().lower(),
        value_area_pct=float(args.value_area_pct),
        target_bins=int(args.target_bins),
        use_final=not bool(args.no_final),
        theme=str(args.theme),
        thresholds=ShapeThresholds(),
    )

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    try:
        windows = build_windows(
            anchor_mode=spec.anchor_mode,
            start=spec.start,
            end=spec.end,
            sessions=sessions,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    log(f"[market_profile] {spec.symbol} {spec.anchor_mode}: {len(windows)} window(s)")

    client = default_client()

    log("[market_profile] loading candles ...")
    candles_1m, candles_tf = load_chart_candles(
        client, spec.symbol, spec.start, spec.end, spec.timeframe
    )
    if candles_tf is None or candles_tf.empty:
        print("error: no candles in the requested range", file=sys.stderr)
        return 3
    log(f"[market_profile] candles: {len(candles_1m)} x 1m -> {len(candles_tf)} x {spec.timeframe}")

    log("[market_profile] building profiles ...")
    profiles = build_profiles(
        client,
        spec.symbol,
        windows,
        value_area_pct=spec.value_area_pct,
        target_bins=spec.target_bins,
        use_final=spec.use_final,
        thresholds=spec.thresholds,
        progress=not quiet,
    )
    if not profiles:
        print("error: no profile could be built (no trade data in range?)", file=sys.stderr)
        return 3

    profiles = mark_naked_pocs(profiles, candles_1m)

    chart = out / f"chart_{spec.symbol}_{spec.anchor_mode}_{spec.timeframe}.png"
    log("[market_profile] rendering ...")
    render_profile_chart(
        symbol=spec.symbol,
        candles_tf=candles_tf,
        profiles=profiles,
        out_path=chart,
        timeframe=spec.timeframe,
        anchor_mode=spec.anchor_mode,
        theme=spec.theme,
        show_single_prints=not bool(args.no_single_prints),
        show_lvn=not bool(args.no_lvn),
    )

    _write_json(out / "run_spec.json", {"format": FORMAT_VERSION, **spec.to_dict()})
    _write_json(
        out / "profiles.json",
        {
            "format": FORMAT_VERSION,
            "spec": spec.to_dict(),
            "profiles": [p.to_dict(include_bins=True) for p in profiles],
        },
    )
    _write_levels_csv(out / "levels.csv", profiles)
    _write_report(out / "report.md", spec, profiles, chart)

    log("")
    log(f"[market_profile] wrote {out}")
    for name in ("report.md", "levels.csv", "profiles.json", "run_spec.json", chart.name):
        log(f"  - {name}")

    naked = sum(1 for p in profiles if p.naked_poc)
    kinds: dict[str, int] = {}
    for p in profiles:
        kinds[p.shape.kind] = kinds.get(p.shape.kind, 0) + 1
    log("")
    log(f"[market_profile] shapes: {kinds}")
    log(f"[market_profile] naked POCs: {naked}/{len(profiles)}")
    return 0
