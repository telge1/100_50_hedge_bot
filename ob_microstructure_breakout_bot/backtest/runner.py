from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

# Ensure CH + OB deps resolve when launched from repo root.
_REPO = Path(__file__).resolve().parents[2]
_EXTRA = [
    "/home/telgenbuescher/projects/Signal_Generator_Ralf/signal_generator_stoch_waves/src",
    "/home/telgenbuescher/projects/orderbook_analyse/src",
    str(_REPO),
]
for p in reversed(_EXTRA):
    if p not in sys.path:
        sys.path.insert(0, p)

from ob_microstructure_breakout_bot.backtest.cases_doge import get_case, list_cases
from ob_microstructure_breakout_bot.models import ClassificationResult, MarketState
from ob_microstructure_breakout_bot.rule_engine import RuleEngine
from ob_microstructure_breakout_bot.thresholds import list_configured_symbols, load_thresholds


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


def _run_case(engine: RuleEngine, case) -> ClassificationResult:
    if case.kind == "accumulation":
        return engine.classify_accumulation_box(
            box_window=case.box,
            breakout_confirm=None,
            ob_near_low=case.ob,
            ema=case.ema,
            price_above_box_high=case.price_above_box_high,
        )
    if case.kind == "ema_exit":
        res = engine.classify_ema_exit(
            case.ema, sell_followthrough=case.sell_followthrough
        )
        if res is None:
            return ClassificationResult(
                state=MarketState.HOLD,
                reasons=["no exit signal"],
            )
        return res
    return engine.classify_long_breakout(
        confirm=case.confirm,
        followthrough=case.followthrough,
        ob_at_event=case.ob,
        ema=case.ema,
    )


def _pass(case, result: ClassificationResult) -> bool:
    if result.state != case.expected_state:
        return False
    if case.expected_tier is not None and result.tier != case.expected_tier:
        return False
    return True


def _metrics_summary(case) -> dict:
    out: dict = {}
    if case.confirm is not None:
        out["confirm_delta"] = case.confirm.delta_notional
        out["confirm_buy"] = case.confirm.buy_notional
        out["confirm_sell"] = case.confirm.sell_notional
        out["confirm_trades"] = case.confirm.trade_count
    if case.followthrough is not None:
        out["followthrough_delta"] = case.followthrough.delta_notional
    if case.box is not None:
        out["box_delta"] = case.box.delta_notional
    if case.ob is not None:
        out["ob_bid_5bps"] = case.ob.bid_5bps
        out["ob_ask_5bps"] = case.ob.ask_5bps
        out["ob_ratio_5bps"] = case.ob.bid_ask_ratio_5bps
    if case.ema is not None:
        out["ema9"] = case.ema.ema9
        out["ema20"] = case.ema.ema20
        out["ema59"] = case.ema.ema59
        out["ema200"] = case.ema.ema200
        out["ema_price"] = case.ema.price
    return out


def _run_scan(args, thresholds) -> int:
    from ob_microstructure_breakout_bot.backtest.scan_touches import scan_ema59_touches

    start = _parse_utc(args.scan_from)
    end = _parse_utc(args.scan_to)
    scanned = scan_ema59_touches(
        args.symbol,
        start,
        end,
        thresholds,
        fetch_ob=not args.no_ob,
        only_first_in_cluster=not args.all_touches,
    )

    counts = Counter(s.result.state.value for s in scanned)
    rows = []
    for s in scanned:
        row = {
            "touch_ts": s.touch.bar_ts.isoformat(),
            "decision_ts": s.decision_ts.isoformat(),
            "direction": s.touch.direction.value,
            "first_in_cluster": s.touch.is_first_in_cluster,
            "state": s.result.state.value,
            "tier": s.result.tier.value if s.result.tier else None,
            "confirm_delta": s.confirm.delta_notional,
            "followthrough_delta": s.followthrough.delta_notional,
            "context_delta": s.context.delta_notional,
            "ob_ok": s.ob_ok,
            "ob_error": s.ob_error,
            "ob_ratio_5bps": s.result.metrics.get("ob_bid_ask_ratio_5bps"),
            "ema9": s.touch.ema.ema9,
            "ema20": s.touch.ema.ema20,
            "ema59": s.touch.ema.ema59,
            "ema200": s.touch.ema.ema200,
            "price": s.touch.ema.price,
            "reasons": s.result.reasons,
        }
        rows.append(row)
        if not args.json:
            print(
                f"{s.decision_ts.isoformat()}  {s.touch.direction.value:11}  "
                f"{s.result.state.value:20}  "
                f"tier={s.result.tier.value if s.result.tier else '-':16}  "
                f"Δc={s.confirm.delta_notional:+.0f}  "
                f"Δft={s.followthrough.delta_notional:+.0f}"
                + ("" if s.ob_ok else f"  OB_ERR={s.ob_error}")
            )

    interesting = [
        r
        for r in rows
        if r["state"]
        in {
            "breakout_confirmed",
            "accumulation",
            "exit_warning",
            "exit_confirmed",
            "fakeout",
        }
    ]

    if args.json:
        print(
            json.dumps(
                {
                    "symbol": thresholds.symbol,
                    "from": start.isoformat(),
                    "to": end.isoformat(),
                    "touch_count": len(rows),
                    "state_counts": dict(counts),
                    "interesting": interesting,
                    "touches": rows,
                },
                indent=2,
            )
        )
    else:
        print()
        print(f"scan {args.symbol} {start.isoformat()} -> {end.isoformat()}")
        print(f"touches classified: {len(rows)}")
        for state, n in sorted(counts.items()):
            print(f"  {state}: {n}")
        print()
        print("interesting (non-chop/setup/hold):")
        if not interesting:
            print("  (none)")
        else:
            for r in interesting:
                print(
                    f"  {r['decision_ts']}  touch={r['touch_ts']}  {r['state']}"
                    f"  tier={r['tier']}  dir={r['direction']}"
                    f"  Δc={r['confirm_delta']:+.0f}  Δft={r['followthrough_delta']:+.0f}"
                )
    return 0


def _run_known_cases(args, thresholds) -> int:
    engine = RuleEngine(thresholds)

    cases = list_cases()
    if args.case:
        cases = [get_case(args.case)]
    elif not args.all_cases:
        raise SystemExit("internal: known-case mode without cases")

    if args.live:
        from ob_microstructure_breakout_bot.backtest.live_fetch import hydrate_case

        hydrated = []
        for case in cases:
            try:
                hydrated.append(hydrate_case(case, symbol=args.symbol))
            except Exception as exc:  # noqa: BLE001
                print(f"[ERROR] {case.case_id}: live hydrate failed: {exc}", file=sys.stderr)
                return 3
        cases = hydrated

    results = []
    failed = 0
    for case in cases:
        result = _run_case(engine, case)
        ok = _pass(case, result)
        if not ok:
            failed += 1
        row = {
            "case_id": case.case_id,
            "label": case.label,
            "mode": "live" if args.live else "seeded",
            "ok": ok,
            "expected_state": case.expected_state.value,
            "expected_tier": case.expected_tier.value if case.expected_tier else None,
            "live_metrics": _metrics_summary(case),
            "got": result.to_dict(),
        }
        results.append(row)
        if not args.json:
            status = "PASS" if ok else "FAIL"
            print(
                f"[{status}] {case.case_id}: got={result.state.value}"
                f" tier={result.tier.value if result.tier else None}"
                f" | {'; '.join(result.reasons)}"
            )
            if args.live:
                m = row["live_metrics"]
                bits = []
                if "confirm_delta" in m:
                    bits.append(f"Δconfirm={m['confirm_delta']:.0f}")
                if "followthrough_delta" in m:
                    bits.append(f"Δft={m['followthrough_delta']:.0f}")
                if "box_delta" in m:
                    bits.append(f"Δbox={m['box_delta']:.0f}")
                if m.get("ob_ratio_5bps") is not None:
                    bits.append(f"OB5={m['ob_ratio_5bps']:.2f}")
                if "ema9" in m:
                    bits.append(
                        f"EMA9/20/59/200="
                        f"{m['ema9']:.5f}/{m['ema20']:.5f}/{m['ema59']:.5f}/"
                        f"{m.get('ema200')}"
                    )
                if bits:
                    print(f"       live: {', '.join(bits)}")

    if args.json:
        print(json.dumps({"symbol": thresholds.symbol, "results": results}, indent=2))
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="ob_microstructure_breakout_bot backtest / discovery scanner"
    )
    parser.add_argument("--symbol", default="DOGEUSDT")
    parser.add_argument("--list-cases", action="store_true")
    parser.add_argument("--case", help="Run a single known case id")
    parser.add_argument("--all-cases", action="store_true", help="Run all known cases")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Hydrate known cases from ClickHouse + Full-OB archives",
    )
    parser.add_argument(
        "--scan",
        action="store_true",
        help="Discover EMA59 touches in a date range (no hardcoded labels)",
    )
    parser.add_argument("--scan-from", help="UTC start, e.g. 2026-09-16T00:00:00")
    parser.add_argument("--scan-to", help="UTC end, e.g. 2026-09-20T00:00:00")
    parser.add_argument(
        "--no-ob",
        action="store_true",
        help="Skip Full-OB sampling during --scan (faster)",
    )
    parser.add_argument(
        "--all-touches",
        action="store_true",
        help="Include repeated EMA59 retests inside a cluster",
    )
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args(argv)

    if args.symbol.upper().replace("/", "") not in list_configured_symbols():
        print(f"No config for {args.symbol}", file=sys.stderr)
        return 2

    thresholds = load_thresholds(args.symbol)

    if args.list_cases:
        for c in list_cases():
            print(f"{c.case_id}\t{c.label}\texpect={c.expected_state.value}")
        return 0

    if args.scan:
        if not args.scan_from or not args.scan_to:
            parser.error("--scan requires --scan-from and --scan-to")
        _load_dotenv()
        return _run_scan(args, thresholds)

    if not args.case and not args.all_cases:
        parser.error("use --list-cases, --case ID, --all-cases, or --scan")

    if args.live:
        _load_dotenv()
    return _run_known_cases(args, thresholds)


if __name__ == "__main__":
    raise SystemExit(main())
