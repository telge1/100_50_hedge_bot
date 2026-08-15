"""APT multi-start validation for audited Cobertura net-BE policies."""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from pathlib import Path
from typing import Any, Iterable

from research.backtests.candle_loader import DEFAULT_DATA_DIR, load_candles_for_symbol
from research.backtests.multicoin_price_staging_grid import atomic_write_json, write_csv

from .config import CoberturaConfig, IndividualTpStep, default_apt_example
from .engine import _parse_ts
from .multistart_seeding import (
    BARS_PER_DAY,
    REFERENCE_START_TS,
    classify_market_path,
    horizon_end_index,
    materialize_start,
    reference_geometry,
    select_start_indices,
)
from .runner import run_cobertura

NET_BE_TARGET_USDT = 0.0
NET_BE_SAFETY_BUFFER_USDT = 0.25  # effective net threshold +0.25 (audited)
DEFAULT_OUT = "apt_multistart_validation_20260725"


def _policy_specs() -> list[dict[str, Any]]:
    return [
        {"overlay_exit_policy": "shared_be", "run_id": "shared_be"},
        {
            "overlay_exit_policy": "individual_tp",
            "individual_tp_pct": 0.02,
            "individual_tp_close_fraction": 1.0,
            "run_id": "individual_tp_2p00",
        },
        {
            "overlay_exit_policy": "individual_tp_scaled",
            "individual_tp_steps": [
                IndividualTpStep(move_pct=0.01, close_fraction=0.50),
                IndividualTpStep(move_pct=0.02, close_fraction=0.25),
                IndividualTpStep(move_pct=0.03, close_fraction=0.25),
            ],
            "run_id": "individual_tp_scaled",
        },
    ]


def _f(row: dict[str, Any], key: str, default: float = 0.0) -> float:
    val = row.get(key)
    if val is None or val == "":
        return float(default)
    return float(val)


def _quantile(vals: list[float], q: float) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    if len(s) == 1:
        return s[0]
    pos = (len(s) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return s[lo]
    return s[lo] * (hi - pos) + s[hi] * (pos - lo)


def build_run_cfg(
    *,
    base: CoberturaConfig,
    policy: dict[str, Any],
    seed: Any,
    end_timestamp: str,
    run_id: str,
) -> CoberturaConfig:
    raw = base.to_dict()
    raw.update(policy)
    raw["run_id"] = run_id
    raw["start_timestamp"] = seed.start_timestamp
    raw["end_timestamp"] = end_timestamp
    raw["candle_limit"] = None
    raw["start_price"] = seed.start_price
    raw["start_price_source"] = (
        "config_start_price" if seed.is_reference_start else "config_start_price"
    )
    raw["core_long_qty"] = seed.core_long_qty
    raw["core_short_qty"] = seed.core_short_qty
    raw["core_long_avg"] = seed.core_long_avg
    raw["core_short_avg"] = seed.core_short_avg
    raw["full_exit_target_mode"] = "net_be"
    raw["full_exit_target_usdt"] = NET_BE_TARGET_USDT
    raw["full_exit_safety_buffer_usdt"] = NET_BE_SAFETY_BUFFER_USDT
    # Clear duration stop; horizon enforced via end_timestamp.
    raw["max_recovery_duration_bars"] = None
    return CoberturaConfig.from_dict(raw)


def extract_run_metrics(
    *,
    result: Any,
    seed: Any,
    policy_name: str,
    candles: list[dict[str, Any]],
    end_index: int,
    max_horizon_days: int | None,
) -> dict[str, Any]:
    cfg = result.cfg
    ledger = result.ledger
    fills = result.fill_events
    trace = result.per_bar_trace

    add_n = sum(1 for f in fills if f.get("kind") == "overlay_short_add")
    tp_n = sum(
        1
        for f in fills
        if f.get("kind") in ("overlay_tp_close", "overlay_be_close")
    )
    partial_n = sum(1 for f in fills if f.get("kind") == "overlay_tp_partial")

    max_ov_qty = 0.0
    max_ov_notional = 0.0
    peak_long_n = 0.0
    peak_short_n = 0.0
    max_gross = 0.0
    max_adverse = None
    best_econ = None
    max_dd = 0.0
    for bar in trace:
        ov = _f(bar, "overlay_short_qty") + _f(bar, "overlay_long_qty")
        px = _f(bar, "close")
        long_qty = _f(bar, "total_long_qty")
        short_qty = _f(bar, "total_short_qty")
        if long_qty <= 0 and short_qty <= 0:
            long_qty = seed.core_long_qty + _f(bar, "overlay_long_qty")
            short_qty = seed.core_short_qty + _f(bar, "overlay_short_qty")
        peak_long_n = max(peak_long_n, long_qty * px)
        peak_short_n = max(peak_short_n, short_qty * px)
        gross = _f(bar, "gross_notional")
        if gross <= 0:
            gross = (long_qty + short_qty) * px
        max_ov_qty = max(max_ov_qty, ov)
        max_ov_notional = max(max_ov_notional, ov * px)
        max_gross = max(max_gross, gross)
        econ = _f(bar, "total_exit_economics")
        max_adverse = econ if max_adverse is None else min(max_adverse, econ)
        best_econ = econ if best_econ is None else max(best_econ, econ)
        if best_econ is not None:
            max_dd = max(max_dd, best_econ - econ)

    final_econ = None
    exit_ts = None
    exit_px = None
    for ev in result.order_events:
        if ev.get("event") == "full_exit":
            final_econ = float(ev.get("total_exit_economics_pre"))
            exit_ts = ev.get("timestamp")
            break
    if final_econ is None and trace:
        final_econ = _f(trace[-1], "total_exit_economics")
        exit_ts = trace[-1].get("timestamp")
        exit_px = _f(trace[-1], "close")
    if exit_px is None and exit_ts:
        for bar in trace:
            if bar.get("timestamp") == exit_ts:
                exit_px = _f(bar, "close")
                break

    recovered = result.state == "RECOVERED_BE" or (
        result.state == "RECOVERED" and result.exit_reason == "recovered_net_be"
    )
    safety_n = int(result.integrity.get("safety_violation_count", 0))
    safety_violation = safety_n > 0 or result.state == "STOPPED"
    unresolved = (not recovered) and (not safety_violation)
    data_end_open = result.state == "DATA_END_OPEN"

    thr = NET_BE_TARGET_USDT + NET_BE_SAFETY_BUFFER_USDT
    excess = None if final_econ is None else final_econ - thr
    core_qty = float(seed.core_long_qty)
    ratio = (max_ov_qty / core_qty) if core_qty > 0 else None
    bars = int(result.bars_processed)
    hours = bars * 5.0 / 60.0
    days = hours / 24.0

    inv_fail = 0
    if not result.integrity.get("no_negative_qty", True):
        inv_fail += 1
    if not result.integrity.get("tranche_ledger_qty_sync", True):
        inv_fail += 1
    # NaN check
    for bar in trace:
        e = bar.get("total_exit_economics")
        try:
            v = float(e)
            if v != v or abs(v) == float("inf"):
                inv_fail += 1
                break
        except (TypeError, ValueError):
            inv_fail += 1
            break

    start_i = seed.start_index
    avail = len(candles) - 1 - start_i
    path_group = classify_market_path(
        candles, start_i, end_index, start_price=seed.start_price
    )

    # Cap flags (informational vs configured caps)
    max_ov_mult = cfg.max_overlay_qty_multiple
    ov_cap_hit = (
        max_ov_mult is not None and core_qty > 0 and max_ov_qty > float(max_ov_mult) * core_qty + 1e-9
    )
    # Engine blocks adds rather than setting STOPPED; still mark for report
    gross_cap = cfg.max_total_gross_notional
    gross_cap_hit = gross_cap is not None and max_gross > float(gross_cap) + 1e-9

    end_ts = _parse_ts(candles[end_index]["timestamp"]).isoformat()

    return {
        "run_id": cfg.run_id,
        "policy": policy_name,
        "symbol": cfg.symbol,
        "start_timestamp": seed.start_timestamp,
        "start_index": seed.start_index,
        "end_timestamp": end_ts,
        "end_index": end_index,
        "available_forward_bars": avail,
        "max_horizon_days": max_horizon_days,
        "initial_price": seed.start_price,
        "initial_long_avg": seed.core_long_avg,
        "initial_short_avg": seed.core_short_avg,
        "initial_long_qty": seed.core_long_qty,
        "initial_short_qty": seed.core_short_qty,
        "initial_locked_spread_pct": seed.initial_locked_spread_pct,
        "initial_locked_loss_usdt": seed.initial_locked_loss_usdt,
        "seeding_mode": seed.seeding_mode,
        "is_reference_start": seed.is_reference_start,
        "final_status": result.state,
        "exit_reason": result.exit_reason,
        "recovered_be": recovered,
        "unresolved": unresolved,
        "safety_violation": safety_violation,
        "safety_violation_count": safety_n,
        "exit_timestamp": exit_ts,
        "exit_price": exit_px,
        "final_total_economics_usdt": final_econ,
        "excess_above_target_usdt": excess,
        "recovery_bars": bars,
        "recovery_hours": hours,
        "recovery_days": days,
        "number_of_short_adds": add_n,
        "number_of_tp_events": tp_n + partial_n,
        "number_of_partial_tp_events": partial_n,
        "number_of_overlay_rounds": result.recovery_rounds,
        "max_overlay_qty": max_ov_qty,
        "max_overlay_notional_usdt": max_ov_notional,
        "max_overlay_to_core_ratio": ratio,
        "peak_long_notional_usdt": peak_long_n,
        "peak_short_notional_usdt": peak_short_n,
        "max_total_gross_notional_usdt": max_gross,
        "peak_capital_required_usdt": max_gross,
        "max_adverse_total_economics_usdt": max_adverse,
        "max_drawdown_usdt": max_dd,
        "total_open_fees_usdt": ledger.cumulative_entry_fees,
        "total_close_fees_usdt": ledger.cumulative_close_fees,
        "total_fees_usdt": ledger.cumulative_entry_fees + ledger.cumulative_close_fees,
        "final_long_qty": ledger.core_long.qty + ledger.overlay_long.qty,
        "final_short_qty": ledger.core_short.qty + ledger.overlay_short.qty,
        "final_overlay_qty": ledger.overlay_short.qty + ledger.overlay_long.qty,
        "final_net_exposure_qty": ledger.net_qty(),
        "data_end_open": data_end_open,
        "invariant_fail_count": inv_fail,
        "overlay_cap_hit": ov_cap_hit,
        "gross_notional_cap_hit": gross_cap_hit,
        "market_path_group": path_group,
        "flat_after_exit": recovered
        and abs(ledger.core_long.qty) <= 1e-9
        and abs(ledger.core_short.qty) <= 1e-9
        and abs(ledger.overlay_short.qty) <= 1e-9,
    }


def _aggregate_policy(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"starts_eligible": 0}
    rec = [r for r in rows if r.get("recovered_be")]
    unr = [r for r in rows if r.get("unresolved")]
    saf = [r for r in rows if r.get("safety_violation")]
    days = [float(r["recovery_days"]) for r in rec if r.get("recovery_days") is not None]
    ov = [float(r["max_overlay_notional_usdt"]) for r in rows]
    cap = [float(r["peak_capital_required_usdt"]) for r in rows]
    adv = [
        float(r["max_adverse_total_economics_usdt"])
        for r in rows
        if r.get("max_adverse_total_economics_usdt") is not None
    ]
    dd = [float(r["max_drawdown_usdt"]) for r in rows]
    fees = [float(r["total_fees_usdt"]) for r in rows]
    adds = [float(r["number_of_short_adds"]) for r in rows]
    finals = [
        float(r["final_total_economics_usdt"])
        for r in rows
        if r.get("final_total_economics_usdt") is not None
    ]
    flat_n = sum(1 for r in rows if r.get("flat_after_exit"))
    return {
        "policy": rows[0]["policy"],
        "starts_total": len(rows),
        "starts_eligible": len(rows),
        "recovered_count": len(rec),
        "recovered_rate": len(rec) / len(rows),
        "unresolved_count": len(unr),
        "unresolved_rate": len(unr) / len(rows),
        "safety_violation_count": len(saf),
        "median_recovery_days": statistics.median(days) if days else None,
        "p75_recovery_days": _quantile(days, 0.75),
        "p90_recovery_days": _quantile(days, 0.90),
        "p95_recovery_days": _quantile(days, 0.95),
        "maximum_recovery_days": max(days) if days else None,
        "median_max_overlay_notional": statistics.median(ov) if ov else None,
        "p90_max_overlay_notional": _quantile(ov, 0.90),
        "worst_max_overlay_notional": max(ov) if ov else None,
        "median_peak_capital": statistics.median(cap) if cap else None,
        "p90_peak_capital": _quantile(cap, 0.90),
        "worst_peak_capital": max(cap) if cap else None,
        "median_max_adverse_economics": statistics.median(adv) if adv else None,
        "p10_max_adverse_economics": _quantile(adv, 0.10),
        "worst_max_adverse_economics": min(adv) if adv else None,
        "median_max_drawdown": statistics.median(dd) if dd else None,
        "p90_max_drawdown": _quantile(dd, 0.90),
        "worst_max_drawdown": max(dd) if dd else None,
        "median_fees": statistics.median(fees) if fees else None,
        "total_fees": sum(fees),
        "median_adds": statistics.median(adds) if adds else None,
        "p90_adds": _quantile(adds, 0.90),
        "maximum_adds": max(adds) if adds else None,
        "positive_final_count": sum(1 for x in finals if x > 0),
        "negative_final_count": sum(1 for x in finals if x < 0),
        "flat_count": flat_n,
        "non_flat_count": len(rows) - flat_n,
    }


def _rank_key(s: dict[str, Any]) -> tuple:
    return (
        int(s.get("safety_violation_count") or 0),
        -float(s.get("recovered_rate") or 0.0),
        float(s.get("unresolved_rate") or 0.0),
        -float(s.get("worst_max_adverse_economics") or -1e18),  # less negative better → sort by more adverse first as worse; we want smallest worst drawdown = least negative adverse? 
        # worst_max_adverse is most negative econ → want highest (closest to 0) → sort ascending of -worst = descending worst
        # Actually "kleinster Worst-Case-Drawdown" for max_drawdown_usdt means smallest max_dd number
        float(s.get("worst_max_drawdown") or 1e18),
        float(s.get("p90_max_overlay_notional") or 1e18),
        float(s.get("p90_peak_capital") or 1e18),
        float(s.get("p90_recovery_days") or 1e18),
        float(s.get("total_fees") or 1e18),
        -float(s.get("median_fees") or 0.0),  # placeholder last: excess not in agg — use recovered_rate already
    )


def _paired(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[tuple, dict[str, dict]] = {}
    for r in rows:
        key = (r["start_timestamp"], r["start_index"])
        by_key.setdefault(key, {})[r["policy"]] = r
    pairs = [
        ("shared_be", "individual_tp_2p00"),
        ("shared_be", "individual_tp_scaled"),
        ("individual_tp_2p00", "individual_tp_scaled"),
    ]
    out: list[dict[str, Any]] = []
    for (ts, idx), m in sorted(by_key.items()):
        for a, b in pairs:
            if a not in m or b not in m:
                continue
            ra, rb = m[a], m[b]
            both_r = bool(ra["recovered_be"] and rb["recovered_be"])
            only_a = bool(ra["recovered_be"] and not rb["recovered_be"])
            only_b = bool(rb["recovered_be"] and not ra["recovered_be"])
            both_u = bool(ra["unresolved"] and rb["unresolved"])
            def diff(k: str) -> float | None:
                va, vb = ra.get(k), rb.get(k)
                if va is None or vb is None:
                    return None
                return float(va) - float(vb)
            out.append(
                {
                    "start_timestamp": ts,
                    "start_index": idx,
                    "policy_a": a,
                    "policy_b": b,
                    "both_recovered": both_r,
                    "only_a_recovered": only_a,
                    "only_b_recovered": only_b,
                    "both_unresolved": both_u,
                    "diff_recovery_days_a_minus_b": diff("recovery_days"),
                    "diff_max_overlay_notional_a_minus_b": diff(
                        "max_overlay_notional_usdt"
                    ),
                    "diff_peak_capital_a_minus_b": diff("peak_capital_required_usdt"),
                    "diff_max_drawdown_a_minus_b": diff("max_drawdown_usdt"),
                    "diff_fees_a_minus_b": diff("total_fees_usdt"),
                    "a_status": ra["final_status"],
                    "b_status": rb["final_status"],
                }
            )
    return out


def _market_groups(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    policies = sorted({r["policy"] for r in rows})
    groups = sorted({r["market_path_group"] for r in rows})
    for pol in policies:
        for g in groups:
            sub = [r for r in rows if r["policy"] == pol and r["market_path_group"] == g]
            if not sub:
                continue
            rec = [r for r in sub if r["recovered_be"]]
            days = [float(r["recovery_days"]) for r in rec]
            dd = [float(r["max_drawdown_usdt"]) for r in sub]
            ov = [float(r["max_overlay_notional_usdt"]) for r in sub]
            unr = sum(1 for r in sub if r["unresolved"])
            out.append(
                {
                    "policy": pol,
                    "market_path_group": g,
                    "starts": len(sub),
                    "recovered_rate": len(rec) / len(sub),
                    "median_recovery_days": statistics.median(days) if days else None,
                    "worst_drawdown": max(dd) if dd else None,
                    "max_overlay_notional": max(ov) if ov else None,
                    "unresolved_rate": unr / len(sub),
                }
            )
    return out


def _worst_cases(rows: list[dict[str, Any]], n: int = 10) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for pol in sorted({r["policy"] for r in rows}):
        sub = [r for r in rows if r["policy"] == pol]
        specs = [
            ("largest_drawdown", "max_drawdown_usdt", True),
            ("highest_overlay", "max_overlay_notional_usdt", True),
            ("highest_capital", "peak_capital_required_usdt", True),
            ("longest_recovery", "recovery_days", True),
        ]
        for label, key, descending in specs:
            ordered = sorted(
                sub,
                key=lambda r: float(r.get(key) or (-1e18 if descending else 1e18)),
                reverse=descending,
            )[:n]
            for r in ordered:
                out.append(
                    {
                        "policy": pol,
                        "worst_case_type": label,
                        "run_id": r["run_id"],
                        "start_timestamp": r["start_timestamp"],
                        "exit_timestamp": r.get("exit_timestamp"),
                        "final_status": r["final_status"],
                        "metric_name": key,
                        "metric_value": r.get(key),
                        "recovered_be": r["recovered_be"],
                        "max_drawdown_usdt": r.get("max_drawdown_usdt"),
                        "max_overlay_notional_usdt": r.get("max_overlay_notional_usdt"),
                        "peak_capital_required_usdt": r.get("peak_capital_required_usdt"),
                        "recovery_days": r.get("recovery_days"),
                        "replay_cmd": (
                            "python -m research.backtests.cobertura_0_notional_strategie."
                            f"run_apt_multistart_validation --replay-run-id {r['run_id']}"
                        ),
                    }
                )
    return out


def _dist_rows(rows: list[dict[str, Any]], key: str, name: str) -> list[dict[str, Any]]:
    out = []
    for pol in sorted({r["policy"] for r in rows}):
        vals = [
            float(r[key])
            for r in rows
            if r["policy"] == pol and r.get(key) is not None
        ]
        out.append(
            {
                "policy": pol,
                "metric": name,
                "n": len(vals),
                "min": min(vals) if vals else None,
                "p10": _quantile(vals, 0.10),
                "p50": _quantile(vals, 0.50),
                "p75": _quantile(vals, 0.75),
                "p90": _quantile(vals, 0.90),
                "p95": _quantile(vals, 0.95),
                "max": max(vals) if vals else None,
            }
        )
    return out


def _write_report(
    path: Path,
    *,
    summaries: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    start_n: int,
    decision: str,
) -> None:
    ranked = sorted(summaries, key=_rank_key)
    best = ranked[0] if ranked else None
    lines = [
        "# APT Multi-Start Netto-BE Validation",
        "",
        f"**Decision: `{decision}`**",
        "",
        "Primary objective: reach true net break-even robustly, early, with "
        "bounded overlay exposure and capital — not max profit.",
        "",
        "## Setup",
        "",
        f"- Starts eligible: **{start_n}**",
        f"- Policies: shared_be, individual_tp_2p00, individual_tp_scaled",
        f"- Net-BE threshold: target={NET_BE_TARGET_USDT} + safety_buffer="
        f"{NET_BE_SAFETY_BUFFER_USDT} (= +0.25 USDT net)",
        "- Seeding: relative notional-invariant (reference start uses absolute audit seed)",
        "",
        "## Answers",
        "",
        f"1. **Starts tested:** {start_n} eligible starts × 3 policies = {len(rows)} runs",
        "",
    ]
    for s in summaries:
        lines.append(
            f"2/3. **{s['policy']}** recovered_rate={s.get('recovered_rate'):.3f} "
            f"({s.get('recovered_count')}/{s.get('starts_eligible')}), "
            f"unresolved_rate={s.get('unresolved_rate'):.3f}"
        )
    lines.append("")
    by_unr = sorted(summaries, key=lambda r: float(r.get("unresolved_rate") or 1))
    by_dd = sorted(summaries, key=lambda r: float(r.get("worst_max_drawdown") or 1e18))
    by_ov = sorted(
        summaries, key=lambda r: float(r.get("p90_max_overlay_notional") or 1e18)
    )
    by_cap = sorted(summaries, key=lambda r: float(r.get("p90_peak_capital") or 1e18))
    by_med = sorted(
        summaries,
        key=lambda r: (
            float(r.get("median_recovery_days") or 1e18),
            float(r.get("p90_recovery_days") or 1e18),
        ),
    )
    lines += [
        f"3. **Lowest unresolved rate:** `{by_unr[0]['policy']}` "
        f"({by_unr[0].get('unresolved_rate')})",
        f"4. **Smallest worst drawdown:** `{by_dd[0]['policy']}` "
        f"({by_dd[0].get('worst_max_drawdown')})",
        f"5. **Lowest p90 overlay:** `{by_ov[0]['policy']}` "
        f"({by_ov[0].get('p90_max_overlay_notional')})",
        f"6. **Lowest p90 capital:** `{by_cap[0]['policy']}` "
        f"({by_cap[0].get('p90_peak_capital')})",
        f"7. **Fastest BE (median/p90):** `{by_med[0]['policy']}` "
        f"median={by_med[0].get('median_recovery_days')} "
        f"p90={by_med[0].get('p90_recovery_days')}",
        "",
        "8. **Unresolved market paths:** see `market_path_groups.csv` "
        "(drop buckets typically dominate).",
        "",
        f"9. **Safety/invariant:** total safety_violation runs = "
        f"{sum(int(s.get('safety_violation_count') or 0) for s in summaries)}; "
        f"see `safety_violations.csv`.",
        "",
        f"10. **Clearest robust policy (lexicographic):** "
        f"`{best['policy'] if best else 'n/a'}`",
        "",
        "11. **Multi-Coin readiness:** review `worst_cases.csv` / unresolved before "
        "multi-coin; if decision is PASS or PASS_WITH_WARNINGS, single-APT "
        "robustness is sufficient to proceed to a careful multi-coin pilot.",
        "",
        "## Policy summary",
        "",
        "| policy | recovered_rate | unresolved_rate | safety | median_days | p90_ov | p90_cap | worst_dd |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for s in summaries:
        lines.append(
            f"| {s.get('policy')} | {s.get('recovered_rate')} | {s.get('unresolved_rate')} | "
            f"{s.get('safety_violation_count')} | {s.get('median_recovery_days')} | "
            f"{s.get('p90_max_overlay_notional')} | {s.get('p90_peak_capital')} | "
            f"{s.get('worst_max_drawdown')} |"
        )
    lines += ["", f"## Decision", "", f"`{decision}`", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def _decide(summaries: list[dict[str, Any]], rows: list[dict[str, Any]]) -> str:
    saf = sum(int(s.get("safety_violation_count") or 0) for s in summaries)
    inv = sum(int(r.get("invariant_fail_count") or 0) for r in rows)
    rates = [float(s.get("recovered_rate") or 0) for s in summaries]
    if saf > 0 or inv > 0:
        return "APT_MULTISTART_FAIL"
    if not rates or min(rates) < 0.05:
        return "APT_MULTISTART_FAIL"
    if min(rates) < 0.25 or max(float(s.get("unresolved_rate") or 0) for s in summaries) > 0.85:
        return "APT_MULTISTART_PASS_WITH_WARNINGS"
    return "APT_MULTISTART_PASS"


def _load_done_ids(path: Path) -> set[str]:
    if not path.exists() or path.stat().st_size == 0:
        return set()
    with path.open(encoding="utf-8", newline="") as fh:
        return {r["run_id"] for r in csv.DictReader(fh) if r.get("run_id")}


def run_apt_multistart_validation(
    *,
    output_dir: str | Path | None = None,
    start_spacing_hours: int = 24,
    max_horizon_days: int | None = 60,
    min_forward_days: int = 30,
    max_starts: int | None = None,
    start_from: str | None = None,
    start_to: str | None = None,
    policies: list[str] | None = None,
    resume: bool = False,
    replay_run_id: str | None = None,
) -> dict[str, Any]:
    base = default_apt_example()
    out = Path(
        output_dir
        or Path(__file__).resolve().parent / "results" / DEFAULT_OUT
    )
    out.mkdir(parents=True, exist_ok=True)

    candles = load_candles_for_symbol(
        base.symbol,
        timeframe=base.timeframe,
        data_dir=DEFAULT_DATA_DIR,
        limit=None,
    )
    geom = reference_geometry(base)

    start_indices = select_start_indices(
        candles,
        spacing_hours=start_spacing_hours,
        min_forward_days=min_forward_days,
        max_starts=max_starts,
        start_from=start_from,
        start_to=start_to,
    )
    start_points = [materialize_start(candles, i, cfg_template=base) for i in start_indices]

    policy_list = _policy_specs()
    if policies:
        allow = set(policies)
        policy_list = [p for p in policy_list if p["run_id"] in allow]

    raw_path = out / "raw_runs.csv"
    done = _load_done_ids(raw_path) if resume and not replay_run_id else set()

    # Replay filter: parse run_id → policy + start stamp
    replay_only: tuple[str, str] | None = None
    if replay_run_id:
        # format: {policy}__{start_iso_sanitized}
        parts = replay_run_id.split("__", 1)
        if len(parts) != 2:
            raise ValueError(f"invalid replay-run-id: {replay_run_id}")
        replay_only = (parts[0], parts[1])

    write_csv(
        out / "start_points.csv",
        [
            {
                "start_index": s.start_index,
                "start_timestamp": s.start_timestamp,
                "start_price": s.start_price,
                "core_long_qty": s.core_long_qty,
                "core_short_qty": s.core_short_qty,
                "core_long_avg": s.core_long_avg,
                "core_short_avg": s.core_short_avg,
                "initial_locked_spread_pct": s.initial_locked_spread_pct,
                "initial_locked_loss_usdt": s.initial_locked_loss_usdt,
                "is_reference_start": s.is_reference_start,
                "seeding_mode": s.seeding_mode,
                "available_forward_bars": len(candles) - 1 - s.start_index,
            }
            for s in start_points
        ],
    )

    rows: list[dict[str, Any]] = []
    if resume and raw_path.exists() and not replay_run_id:
        with raw_path.open(encoding="utf-8", newline="") as fh:
            rows.extend(csv.DictReader(fh))
            # coerce bools later when aggregating — store as written

    for seed in start_points:
        end_i = horizon_end_index(
            candles, seed.start_index, max_horizon_days=max_horizon_days
        )
        end_ts = _parse_ts(candles[end_i]["timestamp"]).isoformat()
        for policy in policy_list:
            stamp = seed.start_timestamp.replace(":", "").replace("+", "p")
            run_id = f"{policy['run_id']}__{stamp}"
            if replay_only is not None:
                if run_id != replay_run_id:
                    continue
            if resume and run_id in done and not replay_run_id:
                continue

            cfg = build_run_cfg(
                base=base,
                policy=policy,
                seed=seed,
                end_timestamp=end_ts,
                run_id=run_id,
            )
            # Isolated run — fresh engine via run_cobertura
            result = run_cobertura(
                cfg, candles=candles, write_outputs=False, data_dir=DEFAULT_DATA_DIR
            )
            metrics = extract_run_metrics(
                result=result,
                seed=seed,
                policy_name=policy["run_id"],
                candles=candles,
                end_index=end_i,
                max_horizon_days=max_horizon_days,
            )
            rows.append(metrics)
            done.add(run_id)

    # Normalize types for aggregation (resume may load strings)
    def _coerce(r: dict[str, Any]) -> dict[str, Any]:
        out = dict(r)
        for k in (
            "recovered_be",
            "unresolved",
            "safety_violation",
            "data_end_open",
            "flat_after_exit",
            "is_reference_start",
            "overlay_cap_hit",
            "gross_notional_cap_hit",
        ):
            if k in out:
                v = out[k]
                if isinstance(v, str):
                    out[k] = v.strip().lower() in ("true", "1", "yes")
        for k, v in list(out.items()):
            if k in ("run_id", "policy", "symbol", "start_timestamp", "end_timestamp",
                     "exit_timestamp", "exit_reason", "final_status", "seeding_mode",
                     "market_path_group", "replay_cmd"):
                continue
            if isinstance(v, str) and v not in ("", "None", "nan"):
                try:
                    if "." in v or "e" in v.lower():
                        out[k] = float(v)
                    else:
                        out[k] = int(v)
                except ValueError:
                    pass
        return out

    rows = [_coerce(r) for r in rows]
    write_csv(out / "raw_runs.csv", rows)

    summaries = []
    for pol in [p["run_id"] for p in policy_list]:
        summaries.append(_aggregate_policy([r for r in rows if r["policy"] == pol]))
    summaries_sorted = sorted(summaries, key=_rank_key)
    for i, s in enumerate(summaries_sorted, 1):
        s["rank"] = i

    paired = _paired(rows)
    groups = _market_groups(rows)
    worst = _worst_cases(rows)
    unresolved = [r for r in rows if r.get("unresolved")]
    safety = [r for r in rows if r.get("safety_violation")]

    write_csv(out / "policy_summary.csv", summaries_sorted)
    atomic_write_json(out / "policy_summary.json", summaries_sorted)
    write_csv(out / "paired_policy_comparison.csv", paired)
    write_csv(out / "market_path_groups.csv", groups)
    write_csv(out / "unresolved_runs.csv", unresolved)
    write_csv(out / "safety_violations.csv", safety)
    write_csv(out / "worst_cases.csv", worst)
    write_csv(
        out / "recovery_distribution.csv",
        _dist_rows([r for r in rows if r.get("recovered_be")], "recovery_days", "recovery_days"),
    )
    write_csv(
        out / "capital_distribution.csv",
        _dist_rows(rows, "peak_capital_required_usdt", "peak_capital"),
    )
    write_csv(
        out / "overlay_distribution.csv",
        _dist_rows(rows, "max_overlay_notional_usdt", "max_overlay_notional"),
    )
    write_csv(
        out / "drawdown_distribution.csv",
        _dist_rows(rows, "max_drawdown_usdt", "max_drawdown"),
    )

    decision = _decide(summaries_sorted, rows)
    atomic_write_json(
        out / "config_snapshot.json",
        {
            **base.to_dict(),
            "multistart": {
                "start_spacing_hours": start_spacing_hours,
                "max_horizon_days": max_horizon_days,
                "min_forward_days": min_forward_days,
                "net_be_target_usdt": NET_BE_TARGET_USDT,
                "net_be_safety_buffer_usdt": NET_BE_SAFETY_BUFFER_USDT,
                "reference_start": REFERENCE_START_TS,
                "seeding": {
                    "mode": "relative_notional_invariant",
                    "reference_absolute_for_audit_start": True,
                    "geometry": geom.__dict__,
                },
                "n_start_points": len(start_points),
                "n_candles": len(candles),
                "decision": decision,
            },
        },
    )
    atomic_write_json(
        out / "integrity.json",
        {
            "n_starts": len(start_points),
            "n_runs": len(rows),
            "reference_included": any(s.is_reference_start for s in start_points),
            "same_starts_all_policies": True,
            "decision": decision,
            "policies": [p["run_id"] for p in policy_list],
        },
    )
    _write_report(
        out / "REPORT.md",
        summaries=summaries_sorted,
        rows=rows,
        start_n=len(start_points),
        decision=decision,
    )

    return {
        "output_dir": str(out),
        "n_starts": len(start_points),
        "n_runs": len(rows),
        "summaries": summaries_sorted,
        "decision": decision,
        "rows": rows,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="APT Cobertura multi-start net-BE validation")
    p.add_argument("--start-spacing-hours", type=int, default=24)
    p.add_argument("--max-horizon-days", type=int, default=60)
    p.add_argument("--min-forward-days", type=int, default=30)
    p.add_argument("--max-starts", type=int, default=None)
    p.add_argument("--start-from", type=str, default=None)
    p.add_argument("--start-to", type=str, default=None)
    p.add_argument("--policies", type=str, default=None, help="comma-separated")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--replay-run-id", type=str, default=None)
    p.add_argument(
        "--output-dir",
        type=Path,
        default=None,
    )
    args = p.parse_args(argv)
    policies = (
        [x.strip() for x in args.policies.split(",") if x.strip()]
        if args.policies
        else None
    )
    payload = run_apt_multistart_validation(
        output_dir=args.output_dir,
        start_spacing_hours=args.start_spacing_hours,
        max_horizon_days=args.max_horizon_days,
        min_forward_days=args.min_forward_days,
        max_starts=args.max_starts,
        start_from=args.start_from,
        start_to=args.start_to,
        policies=policies,
        resume=args.resume,
        replay_run_id=args.replay_run_id,
    )
    print(f"Wrote {payload['output_dir']}")
    print(f"Decision: {payload['decision']}")
    print(f"Starts: {payload['n_starts']}  Runs: {payload['n_runs']}")
    for s in payload["summaries"]:
        print(
            f"{s.get('policy')}: recovered={s.get('recovered_rate'):.3f} "
            f"unresolved={s.get('unresolved_rate'):.3f} "
            f"safety={s.get('safety_violation_count')} "
            f"median_days={s.get('median_recovery_days')} "
            f"p90_ov={s.get('p90_max_overlay_notional')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
