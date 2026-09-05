"""FROZEN_HIGH_ACCEPTED_ENTRY_TIMING_V1 runner."""

from __future__ import annotations

import csv
import json
import math
import random
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from orderbook_analyse.aggressor_efficiency_flip.timeutil import iso_z, parse_utc
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.edge_catalog import (
    DEFAULT_RAW_ROOT,
    load_ob200_samples,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.entry_timing_contracts import (
    ACCEPTANCE_TO_TRADE_SIDE,
    COST_CONTRACT,
    EXECUTION_CONTRACT,
    EXIT_CONTRACT,
    EXPECTED_V2_FREEZE_PREFIX,
    NO_FIT_ENTRY,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.entry_timing_execution import (
    apply_entry_price,
    apply_exit_price,
    first_quote_at_or_after,
    path_mfe_mae,
    trade_economics,
    trade_side_from_acceptance,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.freeze_v1 import FreezeViolation
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.freeze_v2 import (
    verify_freeze_v2,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.reporting import (
    ensure_outdir,
    write_csv,
    write_json,
)

V2_DIR = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "frozen_high_accepted_contract_fix_refreeze_v2"
)
FREEZE_V2_DIR = V2_DIR / "freeze_bundle_v2"
DEFAULT_OUT = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "frozen_high_accepted_entry_timing_v1"
)

PRIMARY_LATENCY_S = 1
DIAG_LATENCIES = (0, 1, 2)
PRIMARY_SLIP_BPS = 1.0
DIAG_SLIPS = (0.0, 1.0, 2.0)
MAX_LOOKUP_S = 2
HORIZONS = (300, 900)
NOTIONAL = 1000.0
FEE = 0.00055


class FrozenV2BundleTampered(RuntimeError):
    pass


def _load_csv(path: Path) -> list[dict[str, Any]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _verify_v2(label: str) -> dict[str, Any]:
    try:
        out = {**verify_freeze_v2(FREEZE_V2_DIR), "label": label}
    except FreezeViolation as e:
        raise FrozenV2BundleTampered(f"FROZEN_V2_BUNDLE_TAMPERED ({label}): {e}") from e
    sha = str(out.get("freeze_bundle_sha256") or "")
    if not sha.startswith(EXPECTED_V2_FREEZE_PREFIX):
        raise FrozenV2BundleTampered(
            f"FROZEN_V2_BUNDLE_TAMPERED unexpected sha {sha}"
        )
    return out


def _load_cohort() -> list[dict[str, Any]]:
    rows = _load_csv(V2_DIR / "entry_eligible_events_v2.csv")
    rows = [r for r in rows if str(r.get("entry_eligible_v2")).lower() in {"true", "1"}]
    # dedupe by entry_signal_id_v2 (keep earliest decision)
    rows = sorted(rows, key=lambda r: parse_utc(r["earliest_causal_entry_ts_v2"]))
    seen: set[str] = set()
    out = []
    for r in rows:
        sid = r["entry_signal_id_v2"]
        if sid in seen:
            continue
        seen.add(sid)
        if r.get("symbol") != "BTCUSDT":
            continue
        out.append(r)
    if len(out) > 1207:
        raise RuntimeError("cohort exceeds 1207 after dedupe")
    return out


def _stats(xs: list[float]) -> dict[str, Any]:
    if not xs:
        return {
            "n": 0,
            "mean": None,
            "median": None,
            "positive_frac": None,
            "sum": 0.0,
            "worst": None,
            "best": None,
            "avg_win": None,
            "avg_loss": None,
            "profit_factor": None,
            "max_loss_streak": 0,
        }
    ys = sorted(xs)
    n = len(ys)
    mean = sum(ys) / n
    med = ys[n // 2] if n % 2 else 0.5 * (ys[n // 2 - 1] + ys[n // 2])
    wins = [x for x in ys if x > 0]
    losses = [x for x in ys if x < 0]
    gp = sum(wins)
    gl = -sum(losses)
    streak = max_streak = 0
    for x in xs:
        if x < 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    return {
        "n": n,
        "mean": mean,
        "median": med,
        "positive_frac": sum(1 for x in ys if x > 0) / n,
        "sum": sum(ys),
        "worst": ys[0],
        "best": ys[-1],
        "avg_win": (sum(wins) / len(wins)) if wins else None,
        "avg_loss": (sum(losses) / len(losses)) if losses else None,
        "profit_factor": (gp / gl) if gl > 0 else (math.inf if gp > 0 else None),
        "max_loss_streak": max_streak,
    }


def _bootstrap(xs: list[float], *, n_boot: int = 1000, seed: int = 42) -> dict[str, Any]:
    if len(xs) < 2:
        return {"mean_ci95": None, "median_ci95": None, "n_boot": n_boot}
    rng = random.Random(seed)
    means = []
    meds = []
    n = len(xs)
    for _ in range(n_boot):
        sample = [xs[rng.randrange(n)] for _ in range(n)]
        sample.sort()
        means.append(sum(sample) / n)
        meds.append(sample[n // 2] if n % 2 else 0.5 * (sample[n // 2 - 1] + sample[n // 2]))
    means.sort()
    meds.sort()
    lo = int(0.025 * n_boot)
    hi = int(0.975 * n_boot)
    return {
        "mean_ci95": [means[lo], means[hi]],
        "median_ci95": [meds[lo], meds[hi]],
        "n_boot": n_boot,
        "mean_ci_excludes_zero": means[lo] > 0 or means[hi] < 0,
    }


def _summarize_trades(trades: list[dict[str, Any]], *, label: str) -> dict[str, Any]:
    nets = [float(t["net_return"]) for t in trades if t.get("net_return") is not None]
    gross = [float(t["executable_gross_return"]) for t in trades if t.get("executable_gross_return") is not None]
    pnls = [float(t["net_pnl_usdt"]) for t in trades if t.get("net_pnl_usdt") is not None]
    ns = _stats(nets)
    gs = _stats(gross)
    boot = _bootstrap(nets)
    return {
        "label": label,
        "n_trades": len(trades),
        "mean_gross_return": gs["mean"],
        "median_gross_return": gs["median"],
        "gross_positive_frac": gs["positive_frac"],
        "mean_net_return": ns["mean"],
        "median_net_return": ns["median"],
        "net_positive_frac": ns["positive_frac"],
        "total_net_pnl_usdt": sum(pnls) if pnls else 0.0,
        "avg_pnl_usdt": (sum(pnls) / len(pnls)) if pnls else None,
        "profit_factor": ns["profit_factor"],
        "avg_win": ns["avg_win"],
        "avg_loss": ns["avg_loss"],
        "worst_trade": ns["worst"],
        "best_trade": ns["best"],
        "max_loss_streak": ns["max_loss_streak"],
        "bootstrap_mean_ci95": boot["mean_ci95"],
        "bootstrap_median_ci95": boot["median_ci95"],
        "bootstrap_mean_excludes_zero": boot.get("mean_ci_excludes_zero"),
    }


def run_entry_timing_v1(
    *,
    output_dir: Path = DEFAULT_OUT,
    raw_root: Path = DEFAULT_RAW_ROOT,
    max_events: Optional[int] = None,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    ensure_outdir(output_dir)

    before = _verify_v2("before")
    write_json(output_dir / "freeze_verification_before.json", before)
    write_json(output_dir / "execution_contract.json", EXECUTION_CONTRACT)
    write_json(output_dir / "cost_contract.json", COST_CONTRACT)
    write_json(output_dir / "exit_contract.json", EXIT_CONTRACT)

    cohort = _load_cohort()
    if max_events is not None:
        cohort = cohort[:max_events]

    # Group by hour of signal for OB200 loading (need signal..+15m+lookup+latency)
    by_hour: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in cohort:
        ts = parse_utc(r["earliest_causal_entry_ts_v2"])
        hour = ts.replace(minute=0, second=0, microsecond=0)
        by_hour[iso_z(hour)].append(r)

    entry_rows: list[dict[str, Any]] = []
    exit_5: list[dict[str, Any]] = []
    exit_15: list[dict[str, Any]] = []
    trades_5: list[dict[str, Any]] = []
    trades_15: list[dict[str, Any]] = []
    mfe_rows: list[dict[str, Any]] = []
    latency_diag: list[dict[str, Any]] = []
    query_count = 0

    hours = sorted(by_hour.keys())
    for hi, hour in enumerate(hours):
        rows = by_hour[hour]
        print(f"entry hour {hi+1}/{len(hours)} {hour} n={len(rows)}", flush=True)
        ht = parse_utc(hour)
        # load enough for 15m exits + latency + lookup spilling into next hours
        start = ht - timedelta(minutes=5)
        end = ht + timedelta(hours=2)
        samples_by, _, n_ok = load_ob200_samples(
            symbols=("BTCUSDT",),
            start=start,
            end=end,
            raw_root=raw_root,
            sample_ms=250,
        )
        query_count += 1  # local file replay; count as data access
        samples = samples_by.get("BTCUSDT") or []
        if n_ok == 0 or not samples:
            for r in rows:
                entry_rows.append(
                    {
                        "entry_signal_id_v2": r["entry_signal_id_v2"],
                        "episode_id_v2": r["episode_id_v2"],
                        "status": "ENTRY_UNAVAILABLE",
                        "reason": "no_ob200",
                    }
                )
            continue

        for r in rows:
            signal_ts = parse_utc(r["earliest_causal_entry_ts_v2"])
            acc = r["new_final_acceptance_state"]
            side = trade_side_from_acceptance(acc)

            # primary latency = 1s
            for lat in DIAG_LATENCIES:
                legal = signal_ts + timedelta(seconds=lat)
                q, st = first_quote_at_or_after(
                    samples, legal_ts=legal, max_lookup_seconds=MAX_LOOKUP_S
                )
                latency_diag.append(
                    {
                        "entry_signal_id_v2": r["entry_signal_id_v2"],
                        "latency_s": lat,
                        "status": st if q is None else "OK",
                        "entry_book_ts": iso_z(q.ts) if q else None,
                        "delay_ms": None
                        if q is None
                        else (q.ts - legal).total_seconds() * 1000.0,
                    }
                )

            legal_primary = signal_ts + timedelta(seconds=PRIMARY_LATENCY_S)
            q, st = first_quote_at_or_after(
                samples, legal_ts=legal_primary, max_lookup_seconds=MAX_LOOKUP_S
            )
            if q is None:
                entry_rows.append(
                    {
                        "entry_signal_id_v2": r["entry_signal_id_v2"],
                        "episode_id_v2": r["episode_id_v2"],
                        "old_event_id": r.get("old_event_id"),
                        "symbol": "BTCUSDT",
                        "acceptance_state": acc,
                        "trade_side": side,
                        "signal_available_ts": iso_z(signal_ts),
                        "legal_entry_ts": iso_z(legal_primary),
                        "status": "ENTRY_UNAVAILABLE",
                        "entry_coverage_status": st,
                        "latency_s": PRIMARY_LATENCY_S,
                        "matched_edge_id": r.get("matched_edge_id"),
                        "utc_day": signal_ts.strftime("%Y-%m-%d"),
                    }
                )
                continue

            assert q.ts >= legal_primary
            ep = apply_entry_price(side=side, quote=q, extra_slippage_bps=PRIMARY_SLIP_BPS)
            entry_exec_ts = q.ts
            entry_row = {
                "entry_signal_id_v2": r["entry_signal_id_v2"],
                "episode_id_v2": r["episode_id_v2"],
                "old_event_id": r.get("old_event_id"),
                "symbol": "BTCUSDT",
                "acceptance_state": acc,
                "trade_side": side,
                "signal_available_ts": iso_z(signal_ts),
                "legal_entry_ts": iso_z(legal_primary),
                "entry_book_ts": iso_z(q.ts),
                "entry_delay_ms": (q.ts - legal_primary).total_seconds() * 1000.0,
                "entry_source": "ob200_sample",
                "entry_coverage_status": "OK",
                "status": "OK",
                "latency_s": PRIMARY_LATENCY_S,
                "extra_slippage_bps": PRIMARY_SLIP_BPS,
                "matched_edge_id": r.get("matched_edge_id"),
                "utc_day": signal_ts.strftime("%Y-%m-%d"),
                **ep,
            }
            entry_rows.append(entry_row)

            for horizon, exit_list, trade_list in (
                (300, exit_5, trades_5),
                (900, exit_15, trades_15),
            ):
                exit_due = entry_exec_ts + timedelta(seconds=horizon)
                qe, est = first_quote_at_or_after(
                    samples, legal_ts=exit_due, max_lookup_seconds=MAX_LOOKUP_S
                )
                if qe is None:
                    exit_list.append(
                        {
                            "entry_signal_id_v2": r["entry_signal_id_v2"],
                            "horizon_s": horizon,
                            "status": "EXIT_UNAVAILABLE",
                            "exit_due_ts": iso_z(exit_due),
                        }
                    )
                    continue
                assert qe.ts >= exit_due
                xp = apply_exit_price(side=side, quote=qe, extra_slippage_bps=PRIMARY_SLIP_BPS)
                exit_list.append(
                    {
                        "entry_signal_id_v2": r["entry_signal_id_v2"],
                        "horizon_s": horizon,
                        "status": "OK",
                        "exit_due_ts": iso_z(exit_due),
                        "exit_book_ts": iso_z(qe.ts),
                        "exit_delay_ms": (qe.ts - exit_due).total_seconds() * 1000.0,
                        **xp,
                    }
                )
                eco = trade_economics(
                    side=side,
                    entry_mid=ep["entry_mid"],
                    exit_mid=xp["exit_mid"],
                    raw_entry=ep["raw_entry_price"],
                    raw_exit=xp["raw_exit_price"],
                    exec_entry=ep["executable_entry_price"],
                    exec_exit=xp["executable_exit_price"],
                    entry_fee_rate=FEE,
                    exit_fee_rate=FEE,
                    notional_usdt=NOTIONAL,
                )
                trade_list.append(
                    {
                        **{k: entry_row[k] for k in (
                            "entry_signal_id_v2",
                            "episode_id_v2",
                            "trade_side",
                            "acceptance_state",
                            "signal_available_ts",
                            "entry_book_ts",
                            "matched_edge_id",
                            "utc_day",
                            "executable_entry_price",
                            "entry_mid",
                            "spread_bps",
                        )},
                        "horizon_s": horizon,
                        "exit_book_ts": iso_z(qe.ts),
                        "executable_exit_price": xp["executable_exit_price"],
                        "exit_mid": xp["exit_mid"],
                        **eco,
                    }
                )
                mm = path_mfe_mae(
                    samples,
                    side=side,
                    entry_ts=entry_exec_ts,
                    entry_px=ep["executable_entry_price"],
                    horizon_end=exit_due,
                )
                mfe_rows.append(
                    {
                        "entry_signal_id_v2": r["entry_signal_id_v2"],
                        "horizon_s": horizon,
                        "trade_side": side,
                        **mm,
                    }
                )

    # --- Writes core ---
    write_csv(output_dir / "entry_execution.csv", entry_rows)
    write_csv(output_dir / "exit_execution_5m.csv", exit_5)
    write_csv(output_dir / "exit_execution_15m.csv", exit_15)
    write_csv(output_dir / "trade_results_5m.csv", trades_5)
    write_csv(output_dir / "trade_results_15m.csv", trades_15)
    write_csv(output_dir / "mfe_mae_from_entry.csv", mfe_rows)
    write_csv(output_dir / "latency_summary.csv", latency_diag)

    n_ok_entry = sum(1 for r in entry_rows if r.get("status") == "OK")
    n_unavail = sum(1 for r in entry_rows if r.get("status") == "ENTRY_UNAVAILABLE")
    coverage = {
        "n_signals": len(cohort),
        "n_unique_entry_signal_id": len({r["entry_signal_id_v2"] for r in cohort}),
        "n_entry_ok": n_ok_entry,
        "n_entry_unavailable": n_unavail,
        "frac_entry_ok": n_ok_entry / len(cohort) if cohort else 0.0,
        "n_exit_5m_ok": sum(1 for r in exit_5 if r.get("status") == "OK"),
        "n_exit_5m_unavailable": sum(1 for r in exit_5 if r.get("status") == "EXIT_UNAVAILABLE"),
        "n_exit_15m_ok": sum(1 for r in exit_15 if r.get("status") == "OK"),
        "n_exit_15m_unavailable": sum(1 for r in exit_15 if r.get("status") == "EXIT_UNAVAILABLE"),
        "mean_entry_delay_ms": (
            sum(float(r["entry_delay_ms"]) for r in entry_rows if r.get("entry_delay_ms") is not None)
            / max(1, n_ok_entry)
        ),
    }
    write_json(output_dir / "execution_coverage.json", coverage)

    spreads = [float(r["spread_bps"]) for r in entry_rows if r.get("spread_bps") is not None]
    write_csv(
        output_dir / "spread_summary.csv",
        [
            {
                "n": len(spreads),
                "mean_spread_bps": (sum(spreads) / len(spreads)) if spreads else None,
                "median_spread_bps": (
                    sorted(spreads)[len(spreads) // 2] if spreads else None
                ),
            }
        ],
    )

    # cost breakdown / breakeven
    be_rows = []
    for t in trades_5:
        be_rows.append(
            {
                "entry_signal_id_v2": t["entry_signal_id_v2"],
                "horizon_s": 300,
                "spread_bps": t.get("spread_bps"),
                "required_move_bps_for_break_even": t.get("required_move_bps_for_break_even"),
                "gross_move_bps": t.get("gross_move_bps"),
                "net_move_bps": t.get("net_move_bps"),
                "total_fee_bps": FEE * 2 * 1e4,
                "extra_slippage_bps_per_side": PRIMARY_SLIP_BPS,
            }
        )
    write_csv(output_dir / "breakeven_analysis.csv", be_rows)
    write_csv(
        output_dir / "cost_breakdown.csv",
        [
            {
                "entry_fee_bps": FEE * 1e4,
                "exit_fee_bps": FEE * 1e4,
                "roundtrip_fee_bps": FEE * 2 * 1e4,
                "primary_extra_slippage_bps_per_side": PRIMARY_SLIP_BPS,
                "mean_spread_bps": (sum(spreads) / len(spreads)) if spreads else None,
                "median_required_move_bps_5m": (
                    sorted(float(x["required_move_bps_for_break_even"]) for x in be_rows)[
                        len(be_rows) // 2
                    ]
                    if be_rows
                    else None
                ),
                "frac_gross_covers_costs_5m": (
                    sum(
                        1
                        for x in be_rows
                        if float(x["gross_move_bps"]) >= float(x["required_move_bps_for_break_even"])
                    )
                    / len(be_rows)
                    if be_rows
                    else None
                ),
            }
        ],
    )

    # cohort / long short / daily
    def _side_split(trades: list[dict[str, Any]], horizon: int) -> list[dict[str, Any]]:
        out = []
        for label, sub in (
            ("COMBINED", trades),
            ("LONG", [t for t in trades if t["trade_side"] == "LONG"]),
            ("SHORT", [t for t in trades if t["trade_side"] == "SHORT"]),
        ):
            s = _summarize_trades(sub, label=f"{label}_{horizon}s")
            out.append(s)
        return out

    ls_rows = _side_split(trades_5, 300) + _side_split(trades_15, 900)
    write_csv(output_dir / "long_short_summary.csv", ls_rows)
    write_csv(
        output_dir / "cohort_summary.csv",
        [
            _summarize_trades(trades_5, label="eventwise_5m"),
            _summarize_trades(trades_15, label="eventwise_15m"),
        ],
    )

    daily = []
    for day, group in sorted(
        defaultdict(list, {t["utc_day"]: [] for t in trades_5}).items()
    ):
        pass
    by_day: dict[str, list] = defaultdict(list)
    for t in trades_5:
        by_day[t["utc_day"]].append(t)
    for day, group in sorted(by_day.items()):
        s = _summarize_trades(group, label=f"day_{day}_5m")
        s["utc_day"] = day
        daily.append(s)
    write_csv(output_dir / "daily_summary.csv", daily)
    best_day_share = None
    if daily and sum(abs(d.get("total_net_pnl_usdt") or 0) for d in daily) > 0:
        total = sum(d.get("total_net_pnl_usdt") or 0 for d in daily)
        if total != 0:
            best_day_share = max(daily, key=lambda d: d.get("total_net_pnl_usdt") or 0)
            best_day_share = {
                "utc_day": best_day_share["utc_day"],
                "pnl": best_day_share["total_net_pnl_usdt"],
                "share_of_total": (best_day_share["total_net_pnl_usdt"] / total) if total else None,
            }

    # temporal split: first 2 UTC days development, last day holdout
    days_sorted = sorted(by_day.keys())
    split_manifest = {
        "days_present": days_sorted,
        "rule": "first_2_utc_days=development; last_utc_day=holdout",
        "outcome_used_for_split": False,
    }
    if len(days_sorted) < 3:
        split_manifest["status"] = "TEMPORAL_HOLDOUT_INSUFFICIENT"
        dev_days, hold_days = days_sorted, []
    else:
        split_manifest["status"] = "OK"
        dev_days = days_sorted[:2]
        hold_days = [days_sorted[-1]]
    split_manifest["development_days"] = dev_days
    split_manifest["holdout_days"] = hold_days
    write_json(output_dir / "temporal_split_manifest.json", split_manifest)

    dev_5 = [t for t in trades_5 if t["utc_day"] in dev_days]
    hold_5 = [t for t in trades_5 if t["utc_day"] in hold_days]
    write_csv(output_dir / "development_summary.csv", [_summarize_trades(dev_5, label="dev_5m")])
    write_csv(output_dir / "holdout_summary.csv", [_summarize_trades(hold_5, label="holdout_5m")])

    # one-position baselines
    def one_position(trades: list[dict[str, Any]], horizon_s: int) -> list[dict[str, Any]]:
        # chronological by entry_book_ts
        ordered = sorted(trades, key=lambda t: parse_utc(t["entry_book_ts"]))
        out = []
        free_at = None
        for t in ordered:
            ets = parse_utc(t["entry_book_ts"])
            if free_at is not None and ets < free_at:
                continue
            out.append({**t, "baseline": f"ONE_POSITION_{horizon_s}S"})
            free_at = ets + timedelta(seconds=horizon_s)
        return out

    op5 = one_position(trades_5, 300)
    op15 = one_position(trades_15, 900)
    write_csv(output_dir / "one_position_5m.csv", op5)
    write_csv(output_dir / "one_position_15m.csv", op15)

    # overlap diagnostics (eventwise)
    overlap = []
    ordered_e = sorted(
        [e for e in entry_rows if e.get("status") == "OK"],
        key=lambda x: parse_utc(x["entry_book_ts"]),
    )
    open_intervals = []
    for e in ordered_e:
        ets = parse_utc(e["entry_book_ts"])
        open_intervals.append((ets, ets + timedelta(seconds=300), e["trade_side"], e["matched_edge_id"]))
    # max concurrency
    events = []
    for a, b, side, edge in open_intervals:
        events.append((a, 1, side, edge))
        events.append((b, -1, side, edge))
    events.sort()
    cur = maxc = 0
    long_open = short_open = 0
    conflict_s = 0
    for ts, d, side, edge in events:
        if d == 1:
            if side == "LONG":
                long_open += 1
            else:
                short_open += 1
            cur += 1
            maxc = max(maxc, cur)
            if long_open > 0 and short_open > 0:
                conflict_s += 1
        else:
            if side == "LONG":
                long_open -= 1
            else:
                short_open -= 1
            cur -= 1
    by_min = Counter()
    for e in ordered_e:
        by_min[parse_utc(e["entry_book_ts"]).strftime("%Y-%m-%dT%H:%M")] += 1
    overlap.append(
        {
            "max_parallel_5m_assuming_all": maxc,
            "n_conflict_ticks_long_short": conflict_s,
            "max_entries_same_minute": max(by_min.values()) if by_min else 0,
            "n_one_position_5m": len(op5),
            "n_one_position_15m": len(op15),
            "n_eventwise_5m": len(trades_5),
        }
    )
    write_csv(output_dir / "overlap_diagnostics.csv", overlap)

    # bootstrap / LOO
    boot = _bootstrap([float(t["net_return"]) for t in trades_5])
    write_csv(
        output_dir / "bootstrap_summary.csv",
        [{"horizon_s": 300, "metric": "net_return", **boot}],
    )
    # leave-one-out day stability
    loo = []
    for day in days_sorted:
        sub = [t for t in trades_5 if t["utc_day"] != day]
        s = _summarize_trades(sub, label=f"loo_without_{day}")
        s["left_out_day"] = day
        loo.append(s)
    write_csv(output_dir / "leave_one_out.csv", loo)

    # DQ
    dq = {
        **NO_FIT_ENTRY,
        "entry_book_ts_ge_signal_plus_latency": all(
            parse_utc(r["entry_book_ts"])
            >= parse_utc(r["signal_available_ts"]) + timedelta(seconds=PRIMARY_LATENCY_S)
            for r in entry_rows
            if r.get("status") == "OK"
        ),
        "exit_after_entry_plus_horizon_5m": True,  # enforced in loop asserts
        "n_cohort": len(cohort),
        "n_unique_signals": len({r["entry_signal_id_v2"] for r in cohort}),
        "no_v1_raw_rows": True,
        "only_btcusdt": all(r.get("symbol") == "BTCUSDT" for r in cohort),
        **coverage,
    }
    write_json(output_dir / "data_quality_report.json", dq)

    after = _verify_v2("after")
    write_json(output_dir / "freeze_verification_after.json", after)
    if after["freeze_bundle_sha256"] != before["freeze_bundle_sha256"]:
        raise FrozenV2BundleTampered("sha changed during run")

    # Verdicts
    tech = "FROZEN_HIGH_ACCEPTED_ENTRY_TIMING_V1_COMPLETE"
    if coverage["frac_entry_ok"] < 0.90:
        tech = "FROZEN_HIGH_ACCEPTED_ENTRY_TIMING_V1_INSUFFICIENT_EXECUTION_COVERAGE"
    if not dq["entry_book_ts_ge_signal_plus_latency"]:
        tech = "FROZEN_HIGH_ACCEPTED_ENTRY_TIMING_V1_BLOCKED_DATA_QUALITY"

    hold_sum = _summarize_trades(hold_5, label="holdout_5m")
    econ = "NET_EDGE_INCONCLUSIVE"
    if split_manifest["status"] == "TEMPORAL_HOLDOUT_INSUFFICIENT":
        econ = "NET_EDGE_INCONCLUSIVE"
    elif hold_sum["n_trades"] < 100:
        econ = "NET_EDGE_HOLDOUT_POSITIVE_SMALL_N" if (
            (hold_sum.get("mean_net_return") or 0) > 0
            and (hold_sum.get("median_net_return") or 0) > 0
        ) else "NET_EDGE_NOT_SUPPORTED"
    else:
        hold_boot = _bootstrap([float(t["net_return"]) for t in hold_5])
        day_dom = False
        if hold_days:
            hd = hold_days[0]
            day_pnl = sum(float(t["net_pnl_usdt"]) for t in hold_5)
            # single day holdout by construction — concentration check on full cohort
            if best_day_share and best_day_share.get("share_of_total") is not None:
                day_dom = abs(best_day_share["share_of_total"]) > 0.50
        ok = (
            hold_sum["n_trades"] >= 100
            and (hold_sum.get("mean_net_return") or 0) > 0
            and (hold_sum.get("median_net_return") or 0) > 0
            and (hold_sum.get("net_positive_frac") or 0) > 0.50
            and (hold_sum.get("profit_factor") or 0) > 1
            and bool(hold_boot.get("mean_ci_excludes_zero"))
            and (hold_boot.get("mean_ci95") or [0, 0])[0] > 0
            and not day_dom
        )
        if ok:
            econ = "NET_EDGE_HOLDOUT_POSITIVE"
        elif (hold_sum.get("mean_net_return") or 0) > 0:
            econ = "NET_EDGE_DESCRIPTIVE_ONLY"
        else:
            econ = "NET_EDGE_NOT_SUPPORTED"

    # primary eventwise also descriptive
    prim = _summarize_trades(trades_5, label="primary_eventwise_5m")
    if econ == "NET_EDGE_INCONCLUSIVE" and (prim.get("mean_net_return") or 0) != 0:
        econ = "NET_EDGE_DESCRIPTIVE_ONLY"

    elapsed = time.perf_counter() - t0
    summary = {
        "technical_verdict": tech,
        "economic_verdict": econ,
        **NO_FIT_ENTRY,
        "freeze_sha_before": before["freeze_bundle_sha256"],
        "freeze_sha_after": after["freeze_bundle_sha256"],
        "n_v2_episodes_loaded": len(cohort),
        "n_executable_entries": n_ok_entry,
        "coverage": coverage,
        "primary_5m_eventwise": prim,
        "primary_15m_eventwise": _summarize_trades(trades_15, label="eventwise_15m"),
        "one_position_5m": _summarize_trades(op5, label="one_pos_5m"),
        "one_position_15m": _summarize_trades(op15, label="one_pos_15m"),
        "development_5m": _summarize_trades(dev_5, label="dev_5m"),
        "holdout_5m": hold_sum,
        "long_5m": _summarize_trades([t for t in trades_5 if t["trade_side"] == "LONG"], label="long_5m"),
        "short_5m": _summarize_trades([t for t in trades_5 if t["trade_side"] == "SHORT"], label="short_5m"),
        "best_day_share": best_day_share,
        "temporal_split": split_manifest,
        "elapsed_s": round(elapsed, 3),
        "query_count": query_count,
        "trading_edge_confirmed": econ == "NET_EDGE_HOLDOUT_POSITIVE",
        "discovery_data_overlap_warning": True,
    }
    write_json(output_dir / "verdict.json", summary)
    write_json(output_dir / "SUMMARY.json", summary)
    write_json(
        output_dir / "run_manifest.json",
        {
            **NO_FIT_ENTRY,
            "v2_dir": str(V2_DIR),
            "n_cohort": len(cohort),
            "primary_latency_s": PRIMARY_LATENCY_S,
            "primary_slippage_bps": PRIMARY_SLIP_BPS,
            "horizons_s": list(HORIZONS),
            "elapsed_s": round(elapsed, 3),
            "query_count": query_count,
        },
    )
    _write_abschluss(output_dir, summary)
    return summary


def _write_abschluss(output_dir: Path, summary: dict[str, Any]) -> None:
    import subprocess

    try:
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd="/home/telgenbuescher/projects/orderbook_analyse",
            text=True,
        ).strip()
        head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd="/home/telgenbuescher/projects/orderbook_analyse",
            text=True,
        ).strip()
        dirty = bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"],
                cwd="/home/telgenbuescher/projects/orderbook_analyse",
                text=True,
            ).strip()
        )
    except Exception:
        branch, head, dirty = "unknown", "unknown", True

    p5 = summary.get("primary_5m_eventwise") or {}
    p15 = summary.get("primary_15m_eventwise") or {}
    cov = summary.get("coverage") or {}
    next_step = (
        "Kein Live-Trading. Falls NET_EDGE_NOT_SUPPORTED: Exit-/Ausführungsdesign separat "
        "revisited (ohne Grid auf derselben Discovery-Stichprobe). "
        "Bei DESCRIPTIVE_ONLY: echte OOS-Periode nach Discovery-Ende sammeln."
    )
    lines = [
        "# ABSCHLUSSBERICHT — FROZEN_HIGH_ACCEPTED_ENTRY_TIMING_V1",
        "",
        f"1. Technisches Verdict: `{summary['technical_verdict']}`",
        f"2. Wirtschaftliches Verdict: `{summary['economic_verdict']}`",
        "3. Live-Sicherheit: read-only; keine CH-Writes; kein Collector-/Prozess-Change",
        f"4. Branch / HEAD / Dirty: `{branch}` / `{head}` / dirty={dirty}",
        f"5. V2 Freeze SHA vor/nach: `{summary.get('freeze_sha_before')}` / `{summary.get('freeze_sha_after')}`",
        f"6. Anzahl V2-Episoden (dedup entry_signal_id): {summary.get('n_v2_episodes_loaded')}",
        f"7. ausführbare Entries: {summary.get('n_executable_entries')}",
        f"8. Entry-/Exit-Coverage: {json.dumps(cov)}",
        f"9. Entry-Delay (mean ms): {cov.get('mean_entry_delay_ms')}",
        "10. Spread: siehe spread_summary.csv",
        "11. Kostenmodell: Taker/Taker 5.5+5.5 bps + 1 bp Slippage/Seite; Notional 1000 USDT",
        "12. Break-even: siehe breakeven_analysis.csv / cost_breakdown.csv",
        f"13. 5m Gross: mean={p5.get('mean_gross_return')} median={p5.get('median_gross_return')} pos={p5.get('gross_positive_frac')}",
        f"14. 5m Net: mean={p5.get('mean_net_return')} median={p5.get('median_net_return')} pos={p5.get('net_positive_frac')} total_pnl={p5.get('total_net_pnl_usdt')}",
        f"15. 15m Gross: mean={p15.get('mean_gross_return')} median={p15.get('median_gross_return')}",
        f"16. 15m Net: mean={p15.get('mean_net_return')} median={p15.get('median_net_return')} total_pnl={p15.get('total_net_pnl_usdt')}",
        f"17. LONG 5m: {json.dumps(summary.get('long_5m'))}",
        f"18. SHORT 5m: {json.dumps(summary.get('short_5m'))}",
        f"19. Eventweise: siehe cohort_summary.csv",
        f"20. One-position: 5m={json.dumps(summary.get('one_position_5m'))} 15m={json.dumps(summary.get('one_position_15m'))}",
        "21. MFE/MAE ab Entry: mfe_mae_from_entry.csv (nur diagnostisch)",
        f"22. Development: {json.dumps(summary.get('development_5m'))}",
        f"23. Temporal Holdout: {json.dumps(summary.get('holdout_5m'))} split={json.dumps(summary.get('temporal_split'))}",
        "24. Bootstrap/LOO: bootstrap_summary.csv / leave_one_out.csv",
        f"25. Tageskonzentration: {json.dumps(summary.get('best_day_share'))}",
        "26. Überschneidungen: overlap_diagnostics.csv",
        f"27. No-Fit-Flags: {json.dumps(NO_FIT_ENTRY)}",
        "28. Tests: tests/test_frozen_high_accepted_entry_timing_v1.py + test_results.txt",
        f"29. Laufzeit/Queries: {summary.get('elapsed_s')}s / {summary.get('query_count')}",
        f"30. Net-Edge nach Taker+Spread+Slippage: `{summary.get('economic_verdict')}` "
        f"(confirmed={summary.get('trading_edge_confirmed')})",
        "31. Einschränkung: dieselbe historische Basis war bereits Discovery/deskriptive Outcomes — kein endgültiger OOS-Edge.",
        f"32. Nächster Schritt: {next_step}",
        "",
    ]
    (output_dir / "ABSCHLUSSBERICHT.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument("--max-events", type=int, default=None)
    args = p.parse_args()
    s = run_entry_timing_v1(output_dir=args.output_dir, max_events=args.max_events)
    print(s["technical_verdict"], s["economic_verdict"], s.get("n_executable_entries"))
