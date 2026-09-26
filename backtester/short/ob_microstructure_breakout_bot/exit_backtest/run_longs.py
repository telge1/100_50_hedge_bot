"""Run calibrated / full-history longs on the mirrored 5m pool-bounce rule."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[2]
_EXTRA = [
    "/home/telgenbuescher/projects/Signal_Generator_Ralf/signal_generator_stoch_waves/src",
    "/home/telgenbuescher/projects/orderbook_analyse/src",
    "/home/telgenbuescher/projects/signal_research",
    str(_REPO),
    str(_REPO / "dashboard"),
]
for _p in reversed(_EXTRA):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ob_microstructure_breakout_bot.exit_backtest.pool_bounce_long_backtest import (
    analyze_signal_long_bounce_backtest,
    summarize_backtest,
)

_EVENTS = (
    Path(__file__).resolve().parent.parent / "calibration" / "events"
)
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
    for bar in bars:
        if bar.ts == decision_ts:
            return float(bar.open)
    prior = [bar for bar in bars if bar.ts < decision_ts]
    if prior:
        return float(prior[-1].close)
    raise RuntimeError(f"No entry bar near {decision_ts.isoformat()}")


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


_SOURCE_PRIORITY = {
    "scanner_breakout": 0,
    "strong_breakout": 1,
    "fakeout": 2,
}


def _dedupe_same_timestamp(longs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One setup per decision time. Scanner wins over strong, then fakeout."""
    best: dict[str, dict[str, Any]] = {}
    for row in longs:
        key = str(row.get("decision_ts") or "")
        prev = best.get(key)
        rank = _SOURCE_PRIORITY.get(str(row.get("source") or ""), 9)
        prev_rank = _SOURCE_PRIORITY.get(str((prev or {}).get("source") or ""), 9)
        if prev is None or rank < prev_rank:
            best[key] = row
    return [best[key] for key in sorted(best)]


def _fill_key_for_row(row: dict[str, Any]) -> tuple[Any, ...]:
    from bot.forward_test.fill_identity import fill_key

    return fill_key(
        symbol=str(row.get("symbol") or ""),
        side=str(row.get("side") or ""),
        entry_ts=row.get("long_entry_ts") or row.get("short_entry_ts"),
        stop_price=row.get("stop_price"),
        tp_price=row.get("tp_price"),
    )


def _collapse_duplicate_fills(signals: list[dict[str, Any]]) -> None:
    """Drop a later signal that would open the same fill again.

    Identity is entry bar + stop + take-profit. Rank and a one-tick entry
    price do not create a second trade.
    """
    root = str(Path(__file__).resolve().parents[4])
    if root not in sys.path:
        sys.path.insert(0, root)
    seen: set[tuple[Any, ...]] = set()
    for res in signals:
        for row in res.get("rows") or []:
            if not row.get("trade_taken") or row.get("pnl_pct") is None:
                continue
            entry_ts = row.get("long_entry_ts") or row.get("short_entry_ts")
            if not entry_ts:
                continue
            key = _fill_key_for_row(row)
            if key in seen:
                row["trade_taken"] = False
                row["trade_skip_reason"] = "duplicate_fill"
                row["pnl_pct"] = None
                row["exit_reason"] = "duplicate_fill"
                continue
            seen.add(key)


def apply_regime_filter(
    signals: list[dict[str, Any]],
    *,
    symbol: str,
    side: str,
    regime_at: Any | None = None,
) -> dict[str, Any]:
    """Drop trades the 1h/4h regime would block at the signal time.

    Longs pass in bullish and neutral. Shorts pass in bearish and neutral.
    Unknown blocks both. The check uses only bars closed at ``decision_ts``.
    """
    root = str(Path(__file__).resolve().parents[4])
    if root not in sys.path:
        sys.path.insert(0, root)
    if regime_at is None:
        from bot.forward_test.regime import market_regime

        def regime_at(ts: datetime) -> dict[str, Any]:
            return market_regime(symbol, now=ts)

    cache: dict[str, dict[str, Any]] = {}
    blocked = 0
    for sig in signals:
        raw_ts = str(sig.get("decision_ts") or "")
        if not raw_ts:
            rows = sig.get("rows") or []
            raw_ts = str(rows[0].get("decision_ts") or "") if rows else ""
        if not raw_ts:
            continue
        if raw_ts not in cache:
            cache[raw_ts] = regime_at(_parse_ts(raw_ts))
        snap = cache[raw_ts]
        label = str(snap.get("regime") or "unknown")
        allowed = bool(snap.get("allows_long")) if side == "long" else bool(snap.get("allows_short"))
        sig["regime"] = label
        sig["regime_allowed"] = allowed
        if allowed:
            for row in sig.get("rows") or []:
                row["regime"] = label
            continue
        reason = "regime_unknown" if label == "unknown" else f"regime_{label}"
        for row in sig.get("rows") or []:
            row["regime"] = label
            if not row.get("trade_taken"):
                if not row.get("trade_skip_reason"):
                    row["trade_skip_reason"] = reason
                continue
            blocked += 1
            row["trade_taken"] = False
            row["trade_skip_reason"] = reason
            row["exit_reason"] = reason
            row["pnl_pct"] = None
            row["exit_ts"] = None
            row["exit_price"] = None
    return {
        "enabled": True,
        "side": side,
        "n_blocked_trades": blocked,
        "n_timestamps": len(cache),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Long bounce backtest (same mirrored rules as the short bounce backtester)"
    )
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
    parser.add_argument("--hold-hours", type=int, default=336)
    parser.add_argument("--max-ranks", type=int, default=2)
    parser.add_argument(
        "--regime",
        action="store_true",
        help="Apply the 1h/4h regime filter. Omit it to compare the unfiltered run.",
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
    longs = _dedupe_same_timestamp(longs)

    print(
        f"universe={args.universe} n={len(longs)} regime={bool(args.regime)} "
        f"by_source={dict(Counter(r['source'] for r in longs))}",
        flush=True,
    )

    signals: list[dict[str, Any]] = []
    for row in longs:
        decision_ts = _parse_ts(str(row["decision_ts"]))
        src = row.get("source")
        print(
            f"backtest {decision_ts.isoformat()} [{src}] {row.get('tier')} ...",
            flush=True,
        )
        try:
            res = analyze_signal_long_bounce_backtest(
                symbol,
                decision_ts=decision_ts,
                entry_price=_entry_price(symbol, decision_ts),
                tier=str(row.get("tier") or ""),
                source=str(src or ""),
                hold_hours=int(args.hold_hours),
                max_ranks=int(args.max_ranks),
            )
        except Exception as exc:  # noqa: BLE001
            res = {
                "decision_ts": decision_ts.isoformat(),
                "source": src,
                "error": str(exc),
                "rows": [],
            }
            print(f"  ERROR: {exc}", flush=True)
        signals.append(res)
        for trade in (res.get("rows") or [])[:2]:
            pnl = trade.get("pnl_pct")
            print(
                f"  rank{trade.get('rank')} exit={trade.get('exit_reason')} "
                f"pnl={pnl if pnl is None else f'{pnl:+.3f}%'} "
                f"taken={trade.get('trade_taken')}",
                flush=True,
            )

    _collapse_duplicate_fills(signals)
    regime_report = {"enabled": False}
    if args.regime:
        regime_report = apply_regime_filter(signals, symbol=symbol, side="long")
        print(
            f"regime blocked {regime_report['n_blocked_trades']} trades "
            f"across {regime_report['n_timestamps']} signal times",
            flush=True,
        )
    summary = summarize_backtest(signals)
    payload = {
        "symbol": symbol,
        "universe": args.universe,
        "rule": "mirrored_5m_pool_bounce",
        "regime_filter": regime_report,
        "signals_path": str(args.signals),
        "n_longs": len(longs),
        "hold_hours": int(args.hold_hours),
        "summary": summary,
        "signals": signals,
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
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
