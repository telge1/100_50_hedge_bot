"""Run Phase-1 long exit backtest on calibrated / full-history long signals."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
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

from ob_microstructure_breakout_bot.exit_backtest.simulate_long import simulate_long

_EVENTS = (
    Path(__file__).resolve().parent.parent / "calibration" / "events"
)
_DEFAULT_SIGNALS = _EVENTS / "DOGEUSDT_backtest_legacy_vs_calibrated.json"
_DEFAULT_STRONG = _EVENTS / "DOGEUSDT_strong_breakouts_phase_a.json"
_DEFAULT_FAKEOUT = _EVENTS / "DOGEUSDT_fakeouts_phase_b.json"


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


def load_calibrated_longs(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("calibrated", {}).get("breakouts", [])
    out: list[dict[str, Any]] = []
    for r in rows:
        if r.get("side") != "long":
            continue
        out.append(
            {
                "source": "calibrated_locked",
                "decision_ts": r["decision_ts"],
                "tier": r.get("tier") or "",
                "confirm_delta": float(r.get("confirm_delta") or 0.0),
                "followthrough_delta": float(r.get("followthrough_delta") or 0.0),
            }
        )
    return out


def load_full_history_longs(
    *,
    symbol: str,
    scanner_path: Path,
    strong_path: Path,
    fakeout_path: Path,
    sources: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Same coverage window / cohorts as cluster full-history calibration."""
    from ob_microstructure_breakout_bot.exit_backtest.run_full_history_cluster_calibration import (
        build_phase_a_universe,
    )

    want = set(sources or ["scanner_breakout", "strong_breakout", "fakeout"])
    universe = build_phase_a_universe(
        symbol=symbol,
        scanner_path=scanner_path,
        strong_path=strong_path,
        fakeout_path=fakeout_path,
    )
    rows: list[dict[str, Any]] = []
    for u in universe:
        if u.source not in want:
            continue
        rows.append(
            {
                "source": u.source,
                "decision_ts": u.decision_ts.isoformat(),
                "tier": u.tier,
                "confirm_delta": u.confirm_delta,
                "followthrough_delta": u.followthrough_delta,
            }
        )
    return rows


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    traded = [r for r in results if not r.get("ignored")]
    ignored = [r for r in results if r.get("ignored")]
    pnls = [float(r["pnl_pct"]) for r in traded if r.get("pnl_pct") is not None]
    wins = [p for p in pnls if p > 0]
    by_reason: dict[str, int] = {}
    for r in results:
        key = str(r.get("exit_reason") or "unknown")
        by_reason[key] = by_reason.get(key, 0) + 1
    by_ignore = Counter(
        str(r.get("ignore_reason") or "unknown") for r in ignored
    )
    by_tp_mode = Counter(
        str(r.get("tp_mode") or "none") for r in traded if r.get("tp_mode")
    )
    return {
        "n_signals": len(results),
        "n_traded": len(traded),
        "n_ignored": len(ignored),
        "winrate": (len(wins) / len(pnls)) if pnls else None,
        "mean_pnl_pct": (sum(pnls) / len(pnls)) if pnls else None,
        "sum_pnl_pct": sum(pnls) if pnls else None,
        "best_pnl_pct": max(pnls) if pnls else None,
        "worst_pnl_pct": min(pnls) if pnls else None,
        "by_exit_reason": by_reason,
        "by_ignore_reason": dict(by_ignore),
        "by_tp_mode": dict(by_tp_mode),
    }


def summarize_by_source(results: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        groups.setdefault(str(r.get("source") or "unknown"), []).append(r)
    return {src: summarize(rows) for src, rows in sorted(groups.items())}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Long exit backtest (pool + OB/trades)")
    parser.add_argument("--symbol", default="DOGEUSDT")
    parser.add_argument(
        "--universe",
        choices=("locked10", "full"),
        default="locked10",
        help="locked10 = calibrated 10 longs; full = scanner∪strong∪fakeout window",
    )
    parser.add_argument(
        "--signals",
        type=Path,
        default=_DEFAULT_SIGNALS,
    )
    parser.add_argument("--strong", type=Path, default=_DEFAULT_STRONG)
    parser.add_argument("--fakeouts", type=Path, default=_DEFAULT_FAKEOUT)
    parser.add_argument(
        "--sources",
        default="scanner_breakout,strong_breakout,fakeout",
        help="Comma list for --universe full",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="JSON report path (default: exit_backtest/reports/<stamp>.json)",
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
        raise SystemExit(f"No long signals for universe={args.universe}")

    print(
        f"universe={args.universe} n={len(longs)} "
        f"by_source={dict(Counter(r['source'] for r in longs))}",
        flush=True,
    )

    results = []
    for row in longs:
        decision_ts = _parse_ts(str(row["decision_ts"]))
        src = row.get("source")
        print(
            f"simulating {decision_ts.isoformat()} [{src}] {row.get('tier')} ...",
            flush=True,
        )
        try:
            res = simulate_long(
                symbol,
                decision_ts=decision_ts,
                tier=str(row.get("tier") or ""),
                confirm_delta=float(row.get("confirm_delta") or 0.0),
                followthrough_delta=float(row.get("followthrough_delta") or 0.0),
            )
            d = res.to_dict()
        except Exception as exc:  # noqa: BLE001
            d = {
                "decision_ts": decision_ts.isoformat(),
                "tier": row.get("tier"),
                "error": str(exc),
                "ignored": True,
                "ignore_reason": "error",
                "exit_reason": "error",
                "pnl_pct": None,
            }
            print(f"  ERROR: {exc}", flush=True)
        d["source"] = src
        results.append(d)
        pnl = d.get("pnl_pct")
        print(
            f"  -> {d.get('exit_reason')} pnl={pnl if pnl is None else f'{pnl:+.3f}%'} "
            f"tp_mode={d.get('tp_mode')} ignore={d.get('ignore_reason')}",
            flush=True,
        )

    summary = summarize(results)
    by_source = summarize_by_source(results)
    payload = {
        "symbol": symbol,
        "universe": args.universe,
        "signals_path": str(args.signals),
        "n_longs": len(longs),
        "summary": summary,
        "by_source": by_source,
        "trades": results,
    }

    out = args.out
    if out is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        tag = "full_history" if args.universe == "full" else "locked10"
        out = Path(__file__).resolve().parent / "reports" / f"long_exit_{tag}_{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print("\n=== SUMMARY ===")
    print(json.dumps(summary, indent=2))
    print("\n=== BY SOURCE ===")
    print(json.dumps(by_source, indent=2))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
