"""Run cluster-mass + delta reachability analysis on locked long signals."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
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

from ob_microstructure_breakout_bot.exit_backtest.cluster_mass import (
    DEFAULT_CLUSTER_GAP_PCT,
    analyze_signal_clusters,
    summarize_cross_signal,
)

# Locked long set from calibration analysis.
LOCKED_LONGS: list[tuple[str, float]] = [
    ("2026-09-07T08:45:00+00:00", 0.08965),
    ("2026-09-07T19:35:00+00:00", 0.09024),
    ("2026-09-08T20:25:00+00:00", 0.08990),
    ("2026-09-11T02:15:00+00:00", 0.08353),
    ("2026-09-13T14:25:00+00:00", 0.08363),
    ("2026-09-14T02:15:00+00:00", 0.08361),
    ("2026-09-15T17:50:00+00:00", 0.08226),
    ("2026-09-15T23:30:00+00:00", 0.08041),
    ("2026-09-16T18:15:00+00:00", 0.07956),
    ("2026-09-17T23:20:00+00:00", 0.08167),
]


def _parse_ts(raw: str) -> datetime:
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


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


def _load_calibrated_meta(path: Path) -> dict[str, dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, dict[str, Any]] = {}
    for row in data.get("calibrated", {}).get("breakouts", []):
        if row.get("side") != "long":
            continue
        ts = str(row.get("decision_ts") or "").replace("Z", "+00:00")[:16]
        out[ts] = row
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Cluster-mass + delta/OB reachability analysis"
    )
    parser.add_argument("--symbol", default="DOGEUSDT")
    parser.add_argument(
        "--signals",
        type=Path,
        default=_REPO
        / "ob_microstructure_breakout_bot"
        / "calibration"
        / "events"
        / "DOGEUSDT_backtest_legacy_vs_calibrated.json",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=_REPO / "results" / "ob_pool_cluster_mass_reachability.json",
    )
    parser.add_argument(
        "--gap-pct",
        type=float,
        default=DEFAULT_CLUSTER_GAP_PCT,
        help="Cluster merge gap in percent of mid price (LLD default 0.10)",
    )
    args = parser.parse_args(argv)

    _load_dotenv()
    meta = _load_calibrated_meta(args.signals)

    results: list[dict[str, Any]] = []
    for i, (ts_s, entry) in enumerate(LOCKED_LONGS, 1):
        decision_ts = _parse_ts(ts_s)
        key = decision_ts.strftime("%Y-%m-%dT%H:%M")
        row = meta.get(key, {})
        print(f"[{i}/{len(LOCKED_LONGS)}] {key} entry={entry} ...", flush=True)
        try:
            rec = analyze_signal_clusters(
                args.symbol.upper().replace("/", ""),
                decision_ts=decision_ts,
                entry_price=float(entry),
                tier=str(row.get("tier") or ""),
                confirm_delta=float(row.get("confirm_delta") or 0.0),
                followthrough_delta=float(row.get("followthrough_delta") or 0.0),
                gap_pct=float(args.gap_pct),
            )
        except Exception as exc:  # noqa: BLE001
            rec = {
                "signal_ts": decision_ts.isoformat(),
                "entry_price": entry,
                "error": str(exc),
            }
            print(f"  ERROR: {exc}", flush=True)
        results.append(rec)
        if rec.get("error"):
            continue
        rev = rec.get("reversal_cluster") or {}
        heavy = rec.get("heaviest_reached_cluster") or {}
        print(
            f"  max=+{rec.get('max_exc_pct'):.2f}% clusters "
            f"{rec.get('n_reached')}/{rec.get('n_clusters')} "
            f"rev_mass={rev.get('strength_sum')} n={rev.get('n_pools')} "
            f"heavy_reached={heavy.get('strength_sum')} "
            f"label={rev.get('label')}",
            flush=True,
        )

    summary = summarize_cross_signal(results)
    payload = {
        "symbol": args.symbol.upper().replace("/", ""),
        "gap_pct": float(args.gap_pct),
        "n_signals": len(results),
        "summary": summary,
        "signals": results,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print("\n=== SUMMARY ===")
    print(json.dumps(summary, indent=2))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
