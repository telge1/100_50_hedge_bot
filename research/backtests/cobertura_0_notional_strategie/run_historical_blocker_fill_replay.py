"""CLI: fill-level pre-signal replay for 27 historical TEM blockers."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

from research.backtests.multicoin_price_staging_grid import atomic_write_json, write_csv
from research.regime_scanner.tem_structure_break.eval_common import csv_dicts

from .historical_blocker_fill_replay import (
    APT_REFERENCE_TRADE_ID,
    PROFILE,
    REPLAY_SEMANTICS_MD,
    TAKER_FEE_RATE_DEFAULT,
    CandleCache,
    apt_fill_replay_check,
    build_fill_ledger_rows,
    compare_replay_fingerprint,
    open_orders_at_cutoff,
    pre_signal_snapshot,
    run_full_isolated_replay,
    _f,
)

DEFAULT_STATE = Path(
    "research/backtests/cobertura_0_notional_strategie/results/historical_blocker_states_20260726"
)
DEFAULT_ROOT = Path(
    "research/backtests/results/tem_continuous_27_blocker_root_cause_20260722"
)
DEFAULT_OUT = Path(
    "research/backtests/cobertura_0_notional_strategie/results/historical_blocker_fill_replay_20260726"
)


def _read_csv(path: Path) -> list[dict[str, Any]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _b(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "1", "yes")


def run_fill_replay(
    *,
    state_dir: Path,
    root_cause_dir: Path = DEFAULT_ROOT,
    output_dir: Path = DEFAULT_OUT,
    trigger_mode: str = "first_break",
    strict_before_signal: bool = True,
    include_signal_bar_fills: bool = False,
    only_trade_id: str | None = None,
    max_cases: int | None = None,
    resume: bool = False,
    dump_full_ledger: bool = True,
    taker_fee_rate: float = TAKER_FEE_RATE_DEFAULT,
) -> dict[str, Any]:
    del include_signal_bar_fills  # enforced via strict_before_signal=True default
    state_dir = Path(state_dir)
    root_cause_dir = Path(root_cause_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    states = [
        r
        for r in _read_csv(state_dir / "historical_blocker_states.csv")
        if r.get("trigger_mode", trigger_mode) == trigger_mode
    ]
    if not states:
        # fallback: all rows
        states = _read_csv(state_dir / "historical_blocker_states.csv")
    markets = {
        (r["trade_id"], r.get("trigger_mode", trigger_mode)): r
        for r in _read_csv(state_dir / "blocker_market_prices.csv")
    }
    breaks = {
        (r["trade_id"], r.get("trigger_mode", trigger_mode)): r
        for r in _read_csv(state_dir / "blocker_break_events.csv")
    }
    blockers = {
        r["trade_id"]: r for r in csv_dicts(root_cause_dir / "tem_end_blockers_27.csv")
    }

    if only_trade_id:
        states = [s for s in states if s["trade_id"] == only_trade_id]
    if max_cases is not None:
        states = states[: int(max_cases)]

    done: set[str] = set()
    if resume and (output_dir / "blocker_pre_signal_states.csv").exists():
        done = {
            r["trade_id"]
            for r in _read_csv(output_dir / "blocker_pre_signal_states.csv")
            if r.get("trade_id")
        }
        states = [s for s in states if s["trade_id"] not in done]

    cache = CandleCache()
    ledgers: list[dict[str, Any]] = []
    snapshots: list[dict[str, Any]] = []
    open_orders_rows: list[dict[str, Any]] = []
    neut_inputs: list[dict[str, Any]] = []
    neut_calcs: list[dict[str, Any]] = []
    comparisons: list[dict[str, Any]] = []
    mismatches: list[dict[str, Any]] = []
    market_mismatches: list[dict[str, Any]] = []
    fee_issues: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    apt_check: dict[str, Any] = {"status": "SKIPPED", "details": []}

    if resume and done:
        snapshots.extend(_read_csv(output_dir / "blocker_pre_signal_states.csv"))
        ledgers.extend(_read_csv(output_dir / "blocker_fill_ledger.csv"))

    for state in states:
        trade_id = state["trade_id"]
        coin = state["coin"]
        print(f"replay {trade_id}", flush=True)
        blocker = blockers.get(trade_id)
        if blocker is None:
            unresolved.append(
                {
                    "trade_id": trade_id,
                    "reason": "missing_tem_end_blocker_row",
                    "ready_for_neutralization": False,
                }
            )
            continue

        sig = state.get("signal_available_ts") or breaks.get(
            (trade_id, trigger_mode), {}
        ).get("signal_available_ts")
        if not sig:
            unresolved.append(
                {
                    "trade_id": trade_id,
                    "reason": "BREAK_EVENT_UNRESOLVED_no_signal",
                    "ready_for_neutralization": False,
                }
            )
            snap = {
                "trade_id": trade_id,
                "coin": coin,
                "signal_available_ts": None,
                "source_quality": "BREAK_EVENT_UNRESOLVED",
                "replay_match_status": "SKIPPED",
                "ready_for_neutralization": False,
                "state_quality_flags": "BREAK_EVENT_UNRESOLVED",
            }
            snapshots.append(snap)
            continue

        start_bar = int(float(blocker["start_bar"]))
        candles = cache.get(coin)
        result, analysis, fills, order_log = run_full_isolated_replay(
            coin=coin, start_bar=start_bar, candles=candles
        )
        diffs = compare_replay_fingerprint(
            result=result, analysis=analysis, expected=blocker
        )
        match_status = "REPLAY_MATCH" if not diffs else "REPLAY_MISMATCH"
        for d in diffs:
            mismatches.append({"trade_id": trade_id, "status": match_status, **d})
        comparisons.append(
            {
                "trade_id": trade_id,
                "coin": coin,
                "replay_match_status": match_status,
                "n_diffs": len(diffs),
                "n_fills": len(fills),
                "cycles_seen": getattr(result, "cycles_seen", None),
                "candles_processed": getattr(result, "candles_processed", None),
                "realized_pnl": getattr(result, "realized_pnl", None),
                "expected_total_pnl": blocker.get("total_pnl"),
                "expected_final_long_qty": blocker.get("final_long_qty"),
                "expected_final_short_qty": blocker.get("final_short_qty"),
            }
        )

        ledger, viol = build_fill_ledger_rows(
            trade_id=trade_id,
            coin=coin,
            start_bar=start_bar,
            fills=fills,
            signal_available_ts=str(sig),
            strict_before_signal=strict_before_signal,
        )
        violations.extend(viol)
        if dump_full_ledger:
            ledgers.extend(ledger)
        else:
            ledgers.extend([r for r in ledger if r.get("before_signal")])

        for r in ledger:
            if "FEE_RECONSTRUCTION_UNRESOLVED" in str(r.get("fee_flags") or ""):
                fee_issues.append(
                    {
                        "trade_id": trade_id,
                        "fill_sequence": r.get("fill_sequence"),
                        "fill_timestamp": r.get("fill_timestamp"),
                        "purpose": r.get("purpose"),
                        "issue": "missing_entry_exit_fee_fields",
                    }
                )

        oo = open_orders_at_cutoff(
            trade_id=trade_id,
            coin=coin,
            order_log=order_log,
            signal_available_ts=str(sig),
        )
        open_orders_rows.extend(oo)

        market = markets.get((trade_id, trigger_mode)) or markets.get(
            (trade_id, "first_break")
        ) or {
            "tradeable_5m_timestamp": state.get("tradeable_5m_timestamp"),
            "tradeable_5m_open": state.get("tradeable_5m_open"),
            "neutralization_fill_price": state.get("neutralization_fill_price"),
            "neutralization_raw_fill_price": state.get("neutralization_raw_fill_price"),
        }

        # Market mismatch vs state extraction row
        for key in ("tradeable_5m_open", "neutralization_fill_price", "tradeable_5m_timestamp"):
            sv = state.get(key)
            mv = market.get(key)
            if sv not in (None, "") and mv not in (None, ""):
                if key.endswith("timestamp"):
                    if str(sv)[:19] != str(mv)[:19]:
                        market_mismatches.append(
                            {
                                "trade_id": trade_id,
                                "field": key,
                                "state_dir_value": sv,
                                "market_csv_value": mv,
                            }
                        )
                else:
                    try:
                        if abs(float(sv) - float(mv)) > 1e-9:
                            market_mismatches.append(
                                {
                                    "trade_id": trade_id,
                                    "field": key,
                                    "state_dir_value": sv,
                                    "market_csv_value": mv,
                                }
                            )
                    except (TypeError, ValueError):
                        pass

        snap = pre_signal_snapshot(
            trade_id=trade_id,
            coin=coin,
            signal_available_ts=str(sig),
            trade_entry_timestamp=state.get("trade_entry_timestamp")
            or blocker.get("start_time"),
            ledger=ledger,
            open_orders=oo,
            market=market,
            replay_match_status=match_status,
            replay_diffs=diffs,
            taker_fee_rate=taker_fee_rate,
        )
        # attach prior candidate for audit
        snap["prior_candidate_long_qty"] = _f(state.get("candidate_long_qty"))
        snap["prior_candidate_short_qty"] = _f(state.get("candidate_short_qty"))
        snapshots.append(snap)

        neut_inputs.append(
            {
                "trade_id": trade_id,
                "coin": coin,
                "signal_available_ts": sig,
                "long_qty_before": snap.get("long_qty_before"),
                "long_avg_before": snap.get("long_avg_before"),
                "short_qty_before": snap.get("short_qty_before"),
                "short_avg_before": snap.get("short_avg_before"),
                "neutralization_fill_price": snap.get("neutralization_fill_price"),
                "taker_fee_rate": taker_fee_rate,
                "ready_for_neutralization": snap.get("ready_for_neutralization"),
            }
        )
        neut_calcs.append(
            {
                "trade_id": trade_id,
                "coin": coin,
                "neutralization_status": snap.get("neutralization_status"),
                "neutralization_short_qty": snap.get("neutralization_short_qty"),
                "neutralization_notional": snap.get("neutralization_notional"),
                "neutralization_fill_price": snap.get("neutralization_fill_price"),
                "neutralization_fee": snap.get("neutralization_fee"),
                "post_neutralization_short_qty": snap.get("post_neutralization_short_qty"),
                "post_neutralization_short_avg": snap.get("post_neutralization_short_avg"),
                "post_neutralization_long_qty": snap.get("post_neutralization_long_qty"),
                "post_neutralization_avg_spread_pct_from_long": snap.get(
                    "post_neutralization_avg_spread_pct_from_long"
                ),
                "post_neutralization_net_qty": snap.get("post_neutralization_net_qty"),
                "post_neutralization_total_economics": snap.get(
                    "post_neutralization_total_economics"
                ),
                "total_economics_before": snap.get("total_economics_before"),
            }
        )

        if not snap.get("ready_for_neutralization"):
            unresolved.append(
                {
                    "trade_id": trade_id,
                    "reason": snap.get("source_quality"),
                    "flags": snap.get("state_quality_flags"),
                    "replay_match_status": match_status,
                    "ready_for_neutralization": False,
                }
            )

        if trade_id == APT_REFERENCE_TRADE_ID:
            apt_check = apt_fill_replay_check(
                snap,
                ledger,
                candidate_long=_f(state.get("candidate_long_qty")),
                candidate_short=_f(state.get("candidate_short_qty")),
            )

        # Post-neutralization qty invariant
        if (
            snap.get("neutralization_status") == "NEEDS_SHORT_FILL"
            and snap.get("post_neutralization_long_qty") is not None
            and snap.get("post_neutralization_short_qty") is not None
        ):
            if abs(
                float(snap["post_neutralization_long_qty"])
                - float(snap["post_neutralization_short_qty"])
            ) > 1e-6:
                violations.append(
                    {
                        "trade_id": trade_id,
                        "check": "post_neutralization_qty",
                        "detail": (
                            f"long={snap['post_neutralization_long_qty']} "
                            f"short={snap['post_neutralization_short_qty']}"
                        ),
                        "pass_fail": "FAIL",
                    }
                )

        # Lookahead: no before_signal fill at/after cutoff
        for r in ledger:
            if r.get("before_signal") and not (
                str(r.get("causal_status")) == "before_signal"
            ):
                violations.append(
                    {
                        "trade_id": trade_id,
                        "check": "before_signal_flag",
                        "detail": r.get("fill_timestamp"),
                        "pass_fail": "FAIL",
                    }
                )

    # Aggregates
    n = len(snapshots)
    n_match = sum(1 for r in comparisons if r.get("replay_match_status") == "REPLAY_MATCH")
    n_exact = sum(
        1 for r in snapshots if r.get("source_quality") == "EXACT_FILL_LEVEL_BEFORE_SIGNAL"
    )
    n_ready = sum(1 for r in snapshots if _b(r.get("ready_for_neutralization")))
    n_fail_inv = sum(1 for v in violations if v.get("pass_fail") == "FAIL")

    decision = "BLOCKER_FILL_REPLAY_PASS"
    if n_fail_inv or apt_check.get("status") == "APT_FILL_REPLAY_FAIL":
        decision = "BLOCKER_FILL_REPLAY_FAIL"
    elif (
        n_ready < n
        or apt_check.get("status") == "APT_FILL_REPLAY_WARNING"
        or mismatches
        or fee_issues
    ):
        decision = "BLOCKER_FILL_REPLAY_PASS_WITH_WARNINGS"

    write_csv(output_dir / "blocker_fill_ledger.csv", ledgers)
    with (output_dir / "blocker_fill_ledger.jsonl").open("w", encoding="utf-8") as fh:
        for row in ledgers:
            fh.write(json.dumps(row, default=str) + "\n")
    write_csv(output_dir / "blocker_pre_signal_states.csv", snapshots)
    atomic_write_json(output_dir / "blocker_pre_signal_states.json", snapshots)
    write_csv(
        output_dir / "blocker_open_orders_at_signal.csv",
        open_orders_rows
        or [{"trade_id": "", "order_id": "", "status": "none"}],
    )
    write_csv(output_dir / "blocker_neutralization_inputs.csv", neut_inputs)
    write_csv(output_dir / "blocker_neutralization_calculation.csv", neut_calcs)
    write_csv(output_dir / "replay_comparison.csv", comparisons)
    write_csv(
        output_dir / "replay_mismatches.csv",
        mismatches
        or [{"trade_id": "", "status": "none", "metric": "", "expected": "", "actual": ""}],
    )
    write_csv(
        output_dir / "market_price_mismatches.csv",
        market_mismatches
        or [{"trade_id": "", "field": "none", "state_dir_value": "", "market_csv_value": ""}],
    )
    write_csv(
        output_dir / "fee_reconstruction_issues.csv",
        fee_issues
        or [{"trade_id": "", "issue": "none"}],
    )
    write_csv(
        output_dir / "unresolved_replays.csv",
        unresolved
        or [{"trade_id": "", "reason": "none", "ready_for_neutralization": True}],
    )
    write_csv(
        output_dir / "invariant_violations.csv",
        violations
        or [{"trade_id": "", "check": "none", "detail": "no violations", "pass_fail": "PASS"}],
    )
    write_csv(
        output_dir / "source_manifest.csv",
        [
            {"label": "state_dir", "path": str(state_dir)},
            {"label": "root_cause_dir", "path": str(root_cause_dir)},
            {
                "label": "continuous_origin",
                "path": "research/backtests/results/staging_profiles_continuous_1000_500_20260722",
            },
            {
                "label": "replay_engine",
                "path": "research/backtests/multicoin_blocker_price_staging.py::run_isolated_blocker",
            },
            {"label": "profile", "path": PROFILE},
            {"label": "fee_rate_default", "path": str(taker_fee_rate)},
        ],
    )
    (output_dir / "replay_semantics.md").write_text(REPLAY_SEMANTICS_MD, encoding="utf-8")

    # APT detail for report
    apt_snap = next((s for s in snapshots if s["trade_id"] == APT_REFERENCE_TRADE_ID), None)
    apt_ledger = [r for r in ledgers if r.get("trade_id") == APT_REFERENCE_TRADE_ID]

    atomic_write_json(
        output_dir / "config_snapshot.json",
        {
            "state_dir": str(state_dir),
            "root_cause_dir": str(root_cause_dir),
            "trigger_mode": trigger_mode,
            "strict_before_signal": strict_before_signal,
            "include_signal_bar_fills": False,
            "profile": PROFILE,
            "taker_fee_rate": taker_fee_rate,
            "n_rows": n,
            "decision": decision,
            "apt_fill_replay": apt_check,
            "cutoff_rule": "fill_timestamp < signal_available_ts",
        },
    )
    atomic_write_json(
        output_dir / "integrity.json",
        {
            "n_rows": n,
            "n_replay_match": n_match,
            "n_exact_pre_signal": n_exact,
            "n_ready_for_neutralization": n_ready,
            "n_unresolved": len(unresolved),
            "n_replay_mismatches": len(mismatches),
            "n_fee_issues": len(fee_issues),
            "n_invariant_fails": n_fail_inv,
            "n_market_mismatches": len(market_mismatches),
            "apt_status": apt_check.get("status"),
            "decision": decision,
        },
    )

    report = [
        "# Historical Blocker Fill-Level Replay",
        "",
        f"**Decision: `{decision}`**",
        "",
        f"APT: **{apt_check.get('status')}**",
        "",
        *(f"- {d}" for d in apt_check.get("details") or []),
        "",
        "## Answers",
        "",
        f"1. Trades processed / exact pre-signal book: **{n_exact} / {n}**",
        f"2. Full-replay fingerprint match vs tem_end_blockers: **{n_match} / {len(comparisons)}**",
        f"3. Exact pre-signal states: **{n_exact}**",
        f"4. Ready for neutralization: **{n_ready}**",
        f"5. Unresolved: **{len(unresolved)}** — see `unresolved_replays.csv`",
        "6. APT fills before/after 00:00: see APT details above / ledger rows "
        f"(before={sum(1 for r in apt_ledger if _b(r.get('before_signal')))}, "
        f"after={sum(1 for r in apt_ledger if not _b(r.get('before_signal')))}).",
        "7. Old APT cycle-4 candidate vs true pre-signal: "
        + (
            f"candidate={apt_snap.get('prior_candidate_long_qty')}/{apt_snap.get('prior_candidate_short_qty')} "
            f"vs pre-signal={apt_snap.get('long_qty_before')}/{apt_snap.get('short_qty_before')}"
            if apt_snap
            else "APT not in run"
        ),
        "8. Long/short/net qty: `blocker_pre_signal_states.csv`",
        "9. Realized / fees / total economics at signal: same file "
        "(`FEE_RECONSTRUCTION_UNRESOLVED` where entry/exit fee fields missing).",
        "10. Required short fill qty: `blocker_neutralization_calculation.csv`",
        "11. New short average: same file (`post_neutralization_short_avg`).",
        f"12. Problems: invariant_fails={n_fail_inv}, replay_mismatch_rows={len(mismatches)}, "
        f"fee_issues={len(fee_issues)}, market_mismatches={len(market_mismatches)}.",
        "",
        "## Decision",
        "",
        f"`{decision}`",
        "",
    ]
    (output_dir / "REPORT.md").write_text("\n".join(report), encoding="utf-8")

    return {
        "output_dir": str(output_dir),
        "decision": decision,
        "n_rows": n,
        "n_ready": n_ready,
        "n_match": n_match,
        "apt": apt_check.get("status"),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Fill-level pre-signal replay for TEM blockers")
    p.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    p.add_argument("--root-cause-dir", type=Path, default=DEFAULT_ROOT)
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument("--trigger-mode", default="first_break")
    p.add_argument("--strict-before-signal", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument(
        "--include-signal-bar-fills",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="default false; fills at signal_available_ts excluded",
    )
    p.add_argument("--only-trade-id", type=str, default=None)
    p.add_argument("--max-cases", type=int, default=None)
    p.add_argument("--resume", action="store_true")
    p.add_argument("--dump-full-ledger", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--taker-fee-rate", type=float, default=TAKER_FEE_RATE_DEFAULT)
    args = p.parse_args(argv)
    # include_signal_bar_fills=true would mean strict=False; keep explicit
    strict = bool(args.strict_before_signal) and (not args.include_signal_bar_fills)
    payload = run_fill_replay(
        state_dir=args.state_dir,
        root_cause_dir=args.root_cause_dir,
        output_dir=args.output_dir,
        trigger_mode=args.trigger_mode,
        strict_before_signal=strict,
        include_signal_bar_fills=bool(args.include_signal_bar_fills),
        only_trade_id=args.only_trade_id,
        max_cases=args.max_cases,
        resume=args.resume,
        dump_full_ledger=bool(args.dump_full_ledger),
        taker_fee_rate=float(args.taker_fee_rate),
    )
    print(f"Wrote {payload['output_dir']}")
    print(
        f"Decision={payload['decision']} rows={payload['n_rows']} "
        f"ready={payload['n_ready']} match={payload['n_match']} apt={payload['apt']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
