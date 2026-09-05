"""CLI for the market-profile validation run (read-only)."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from orderbook_analyse.market_profile.anchor import build_windows
from orderbook_analyse.market_profile.loader import default_client

from . import (
    DEFAULT_BOOTSTRAP_ITERS,
    DEFAULT_EDGE_MARGIN_FRAC,
    DEFAULT_MAX_HORIZON_MIN,
    DEFAULT_POC_UNIT_FRAC,
    DEFAULT_SEED,
    FORMAT_VERSION,
)
from .contracts import ValidationConfig
from .report import aggregate, write_markdown
from .runner import events_to_frame, preflight_final_parity, run_symbol

OA_ROOT = Path(__file__).resolve().parents[3]
UNIVERSE_PATH = OA_ROOT / "config" / "universe_tradeable_51.json"


def _parse_ts(raw: str) -> datetime:
    dt = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _load_universe() -> list[str]:
    raw = json.loads(UNIVERSE_PATH.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        for key in ("symbols", "universe", "tradeable"):
            if isinstance(raw.get(key), list):
                raw = raw[key]
                break
    if not isinstance(raw, list):
        raise ValueError(f"cannot read symbol list from {UNIVERSE_PATH}")
    out: list[str] = []
    for item in raw:
        sym = item if isinstance(item, str) else (item or {}).get("symbol")
        if sym:
            out.append(str(sym).strip().upper())
    return sorted(set(out))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_market_profile_validation_v1",
        description=(
            "Test whether the balance/trend classification predicts anything: "
            "do value-area edges reject in balance (H1), does the POC act as a "
            "way station in trend (H2), is the POC revisited in balance (H3)."
        ),
    )
    p.add_argument(
        "--symbols",
        default="universe",
        help="'universe' for the frozen 51, or a comma list e.g. BTCUSDT,ETHUSDT",
    )
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--anchor", default="day", choices=["day", "session"])
    p.add_argument("--value-area-pct", type=float, default=0.70)
    p.add_argument("--target-bins", type=int, default=160)
    p.add_argument(
        "--edge-margin-frac",
        type=float,
        default=DEFAULT_EDGE_MARGIN_FRAC,
        help="headline stop distance beyond the edge, as a fraction of the reference range",
    )
    p.add_argument("--poc-unit-frac", type=float, default=DEFAULT_POC_UNIT_FRAC)
    p.add_argument(
        "--edge-margin-grid",
        default="0.05,0.10,0.20,0.35",
        help="stop distances to re-run H1 against, so one arbitrary stop cannot decide the verdict",
    )
    p.add_argument("--poc-unit-grid", default="0.10,0.15,0.25,0.40")
    p.add_argument(
        "--max-horizon-min",
        type=int,
        default=DEFAULT_MAX_HORIZON_MIN,
        help="cap the barrier walk in minutes; 0 = until the test window ends",
    )
    p.add_argument(
        "--final",
        action="store_true",
        help="use FINAL on every trade scan (~60x slower; parity is checked either way)",
    )
    p.add_argument("--parity-sample", type=int, default=12)
    p.add_argument(
        "--cost-bps",
        type=float,
        default=11.0,
        help="round-trip cost in bps of notional used for the net expectancy column",
    )
    p.add_argument("--bootstrap-iters", type=int, default=DEFAULT_BOOTSTRAP_ITERS)
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    p.add_argument(
        "--out-dir",
        default=str(OA_ROOT / "results" / "market_profile_validation_v1"),
    )
    p.add_argument("--quiet", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    def log(msg: str = "") -> None:
        if not args.quiet:
            print(msg, flush=True)

    start, end = _parse_ts(args.start), _parse_ts(args.end)
    if end <= start:
        print("error: --end must be after --start", file=sys.stderr)
        return 2

    if str(args.symbols).strip().lower() == "universe":
        symbols = _load_universe()
    else:
        symbols = sorted(
            {s.strip().upper() for s in str(args.symbols).split(",") if s.strip()}
        )
    if not symbols:
        print("error: no symbols resolved", file=sys.stderr)
        return 2

    def _grid(raw: str, primary: float) -> tuple[float, ...]:
        vals = {round(float(x), 4) for x in str(raw).split(",") if x.strip()}
        vals.add(round(primary, 4))  # the headline setting must be in the sweep
        return tuple(sorted(v for v in vals if v > 0))

    cfg = ValidationConfig(
        symbols=tuple(symbols),
        start=start,
        end=end,
        anchor_mode=args.anchor,
        value_area_pct=float(args.value_area_pct),
        target_bins=int(args.target_bins),
        edge_margin_frac=float(args.edge_margin_frac),
        poc_unit_frac=float(args.poc_unit_frac),
        edge_margin_grid=_grid(args.edge_margin_grid, float(args.edge_margin_frac)),
        poc_unit_grid=_grid(args.poc_unit_grid, float(args.poc_unit_frac)),
        max_horizon_min=int(args.max_horizon_min),
        use_final=bool(args.final),
        bootstrap_iters=int(args.bootstrap_iters),
        seed=int(args.seed),
        cost_bps=float(args.cost_bps),
    )

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    client = default_client()

    windows = build_windows(anchor_mode=cfg.anchor_mode, start=start, end=end)
    log(f"[validation] {len(symbols)} symbols x {len(windows)} windows, anchor={cfg.anchor_mode}")

    log(f"[validation] FINAL parity preflight on {args.parity_sample} random windows ...")
    parity = preflight_final_parity(
        client,
        symbols=symbols,
        windows=windows,
        target_bins=cfg.target_bins,
        sample_n=int(args.parity_sample),
        seed=cfg.seed,
    )
    log(
        f"[validation] parity: sampled={parity['sampled']} "
        f"mismatches={parity['mismatches']} -> {'OK' if parity['parity'] else 'MISMATCH'}"
    )
    if not parity["parity"] and not cfg.use_final:
        print(
            "error: FINAL parity failed on the sample; re-run with --final",
            file=sys.stderr,
        )
        (out / "final_parity.json").write_text(
            json.dumps(parity, indent=2, default=str), encoding="utf-8"
        )
        return 4

    touch_events: list = []
    revisit_events: list = []
    per_symbol: list[dict] = []
    t0 = time.time()

    for i, sym in enumerate(symbols, start=1):
        try:
            run = run_symbol(client, sym, cfg)
        except Exception as exc:  # noqa: BLE001 - one bad symbol must not kill the sweep
            log(f"  [{i}/{len(symbols)}] {sym}: FAILED ({exc})")
            per_symbol.append({"symbol": sym, "error": str(exc)})
            continue
        touch_events.extend(run.touch_events)
        revisit_events.extend(run.revisit_events)
        per_symbol.append(
            {
                "symbol": sym,
                "windows": run.windows,
                "profiles": run.profiles,
                "skipped_thin": run.skipped_thin,
                "touch_events": len(run.touch_events),
                "revisit_windows": len(run.revisit_events),
                "error": run.error,
            }
        )
        log(
            f"  [{i}/{len(symbols)}] {sym}: profiles={run.profiles} "
            f"events={len(run.touch_events)} windows={len(run.revisit_events)}"
            + (f" ERROR={run.error}" if run.error else "")
        )

    elapsed = time.time() - t0
    log(f"[validation] scan finished in {elapsed:.1f}s")

    if not touch_events and not revisit_events:
        print("error: no events produced", file=sys.stderr)
        return 3

    log("[validation] aggregating + bootstrapping ...")
    results = aggregate(touch_events, revisit_events, cfg)
    results["final_parity"] = parity
    results["per_symbol"] = per_symbol
    results["elapsed_s"] = elapsed

    (out / "results.json").write_text(
        json.dumps(results, indent=2, default=str), encoding="utf-8"
    )
    (out / "final_parity.json").write_text(
        json.dumps(parity, indent=2, default=str), encoding="utf-8"
    )
    if touch_events:
        events_to_frame(touch_events).to_csv(out / "touch_events.csv", index=False)
    if revisit_events:
        events_to_frame(revisit_events).to_csv(out / "revisit_events.csv", index=False)
    write_markdown(out / "report.md", results)

    log("")
    log(f"[validation] wrote {out}")
    for name in ("report.md", "results.json", "touch_events.csv", "revisit_events.csv"):
        log(f"  - {name}")

    log("")
    for key in ("h1", "h2", "h3"):
        block = results[key]
        primary = block["primary_variant"]
        comp = block["variants"][primary]["comparison"]
        log(
            f"[{key.upper()} @ {primary}] {block['comparison_label']} "
            f"diff={comp['difference']:+.3f} "
            f"sym_ci=[{comp['cluster_symbol_ci'][0]:+.3f},{comp['cluster_symbol_ci'][1]:+.3f}] "
            f"date_ci=[{comp['cluster_date_ci'][0]:+.3f},{comp['cluster_date_ci'][1]:+.3f}] "
            f"-> {block.get('verdict_after_control', comp['verdict'])}"
        )
    dc = results["h3"].get("distance_control", {})
    if dc.get("strata"):
        log(
            f"[H3 distance control] holds in {dc['strata_supported']}/{dc['strata_total']} "
            f"distance bands -> {dc['verdict']}"
        )
    return 0
