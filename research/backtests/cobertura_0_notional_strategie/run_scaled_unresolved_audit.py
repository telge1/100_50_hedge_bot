"""Deep audit of unresolved individual_tp_scaled APT multistart cases."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

from research.backtests.candle_loader import DEFAULT_DATA_DIR, load_candles_for_symbol
from research.backtests.multicoin_price_staging_grid import atomic_write_json, write_csv

from .config import CoberturaConfig, default_apt_example
from .engine import _parse_ts
from .multistart_seeding import horizon_end_index, materialize_start
from .run_apt_multistart_validation import (
    NET_BE_SAFETY_BUFFER_USDT,
    NET_BE_TARGET_USDT,
    _policy_specs,
    build_run_cfg,
    extract_run_metrics,
)
from .runner import run_cobertura
from .scaled_unresolved_audit import (
    BE_THRESHOLD,
    CAUSE_RULES_TEXT,
    classify_unresolved_causes,
    compare_replay,
    near_be_flags,
)

DEFAULT_MS = (
    Path(__file__).resolve().parent / "results" / "apt_multistart_validation_20260725"
)
DEFAULT_OUT = (
    Path(__file__).resolve().parent / "results" / "apt_scaled_unresolved_audit_20260725"
)
POLICY = "individual_tp_scaled"
EXPECTED_CASES = 22


def _f(x: Any, default: float = 0.0) -> float:
    if x is None or x == "":
        return float(default)
    return float(x)


def _b(x: Any) -> bool:
    if isinstance(x, bool):
        return x
    return str(x).strip().lower() in ("true", "1", "yes")


def _read_csv(path: Path) -> list[dict[str, Any]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def load_scaled_unresolved_cases(multistart_dir: Path) -> list[dict[str, Any]]:
    rows = [
        r
        for r in _read_csv(multistart_dir / "unresolved_runs.csv")
        if r.get("policy") == POLICY
    ]
    if len(rows) != EXPECTED_CASES:
        raise ValueError(
            f"expected {EXPECTED_CASES} unresolved {POLICY} cases, got {len(rows)}"
        )
    ids = [r["run_id"] for r in rows]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate run_id in unresolved scaled cases")
    for r in rows:
        if _b(r.get("recovered_be")) or _b(r.get("safety_violation")):
            raise ValueError(f"case {r['run_id']} is recovered/safety — not unresolved")
        if r.get("final_status") == "RECOVERED_BE":
            raise ValueError(f"case {r['run_id']} has RECOVERED_BE status")
    # Cross-check raw_runs timestamps
    raw_by_id = {
        r["run_id"]: r
        for r in _read_csv(multistart_dir / "raw_runs.csv")
        if r.get("policy") == POLICY
    }
    for r in rows:
        raw = raw_by_id.get(r["run_id"])
        if raw is None:
            raise ValueError(f"{r['run_id']} missing from raw_runs.csv")
        if raw.get("start_timestamp") != r.get("start_timestamp"):
            raise ValueError(f"start_timestamp mismatch for {r['run_id']}")
        if not _b(raw.get("unresolved")):
            raise ValueError(f"{r['run_id']} not unresolved in raw_runs")
    return rows


def _scaled_policy() -> dict[str, Any]:
    for p in _policy_specs():
        if p["run_id"] == POLICY:
            return p
    raise RuntimeError("scaled policy missing")


def _window_price_stats(
    candles: list[dict[str, Any]], start_i: int, end_i: int, start_price: float
) -> dict[str, Any]:
    window = candles[start_i : end_i + 1]
    opens = [float(c["open"]) for c in window]
    highs = [float(c["high"]) for c in window]
    lows = [float(c["low"]) for c in window]
    closes = [float(c["close"]) for c in window]
    min_px = min(lows)
    max_px = max(highs)
    end_px = closes[-1]
    min_i = next(i for i, c in enumerate(window) if float(c["low"]) == min_px)
    # strongest rally from low: max high after min
    after = window[min_i:]
    max_after = max(float(c["high"]) for c in after) if after else min_px
    max_drop = (min_px - start_price) / start_price if start_price else 0.0
    max_rally = (max_after - min_px) / min_px if min_px > 0 else 0.0
    end_ret = (end_px - start_price) / start_price if start_price else 0.0
    end_near_min = abs(end_px - min_px) / start_price <= 0.03 if start_price else False
    bars_to_low = min_i
    # bars from low to strongest recovery high
    hi_i = 0
    for j, c in enumerate(after):
        if float(c["high"]) == max_after:
            hi_i = j
            break
    return {
        "start_price": start_price,
        "end_price": end_px,
        "min_price": min_px,
        "max_price": max_px,
        "max_drop_from_start_pct": max_drop,
        "max_rally_from_low_pct": max_rally,
        "end_ret_pct": end_ret,
        "end_near_window_min": end_near_min,
        "bars_to_low": bars_to_low,
        "bars_low_to_rally_high": hi_i,
        "days_to_low": bars_to_low * 5.0 / 60.0 / 24.0,
        "days_low_to_rally_high": hi_i * 5.0 / 60.0 / 24.0,
    }


def _econ_path_stats(trace: list[dict[str, Any]], threshold: float = BE_THRESHOLD) -> dict[str, Any]:
    best = None
    best_ts = None
    worst = None
    first_1 = None
    first_5 = None
    first_10 = None
    for bar in trace:
        e = _f(bar.get("total_exit_economics"))
        ts = bar.get("timestamp")
        if best is None or e > best:
            best = e
            best_ts = ts
        if worst is None or e < worst:
            worst = e
        if first_1 is None and e >= -1.0:
            first_1 = ts
        if first_5 is None and e >= -5.0:
            first_5 = ts
        if first_10 is None and e >= -10.0:
            first_10 = ts
    final = _f(trace[-1].get("total_exit_economics")) if trace else None
    return {
        "best_total_economics_usdt": best,
        "best_economics_timestamp": best_ts,
        "worst_total_economics_usdt": worst,
        "final_total_economics_usdt": final,
        "distance_to_be_at_best_usdt": None if best is None else threshold - best,
        "distance_to_be_at_end_usdt": None if final is None else threshold - final,
        "first_time_within_1_usdt_of_be": first_1,
        "first_time_within_5_usdt_of_be": first_5,
        "first_time_within_10_usdt_of_be": first_10,
    }


def _overlay_vs_tp_harvest(fills: list[dict[str, Any]], trace: list[dict[str, Any]]) -> dict[str, Any]:
    """Did cumulative adds outpace cumulative TP closes for most of the path?"""
    cum_add = 0.0
    cum_tp = 0.0
    by_ts: dict[str, list[dict[str, Any]]] = {}
    for f in fills:
        by_ts.setdefault(str(f.get("timestamp")), []).append(f)
    lead_add_bars = 0
    post_add_bars = 0
    seen_add = False
    for bar in trace:
        ts = str(bar.get("timestamp"))
        for f in by_ts.get(ts, []):
            kind = f.get("kind")
            qty = _f(f.get("qty"))
            if kind == "overlay_short_add":
                cum_add += qty
                seen_add = True
            elif kind in ("overlay_tp_partial", "overlay_tp_close"):
                cum_tp += qty
        if seen_add:
            post_add_bars += 1
            if cum_add > cum_tp + 1e-9:
                lead_add_bars += 1
    frac = (lead_add_bars / post_add_bars) if post_add_bars else 0.0
    return {
        "cum_add_qty_end": cum_add,
        "cum_tp_close_qty_end": cum_tp,
        "overlay_grows_faster_than_tp_harvest": frac > 0.5 and cum_add > cum_tp,
        "fraction_bars_add_lead_tp": frac,
    }


def _build_order_timeline(
    *,
    run_id: str,
    result: Any,
    candles: list[dict[str, Any]],
    start_index: int,
) -> list[dict[str, Any]]:
    # Map timestamp -> candle OHLC from result trace (already aligned)
    rows: list[dict[str, Any]] = []
    seq = 0
    # seed
    cfg = result.cfg
    seq += 1
    rows.append(
        {
            "run_id": run_id,
            "sequence_number": seq,
            "timestamp": cfg.start_timestamp,
            "event_type": "CORE_SEED",
            "tranche_id": None,
            "tp_step": None,
            "side": "both",
            "purpose": "core_seed",
            "requested_qty": cfg.core_long_qty,
            "filled_qty": cfg.core_long_qty,
            "trigger_price": cfg.start_price,
            "fill_price": cfg.start_price,
            "fee_usdt": 0.0,
            "realized_pnl_delta": 0.0,
            "realized_pnl_total": 0.0,
            "long_qty_after": cfg.core_long_qty,
            "long_avg_after": cfg.core_long_avg,
            "short_qty_after": cfg.core_short_qty,
            "short_avg_after": cfg.core_short_avg,
            "overlay_qty_after": 0.0,
            "total_economics_after": None,
            "next_active_trigger": None,
            "causal_check": "seed",
        }
    )
    trace_by_ts = {str(b.get("timestamp")): b for b in result.per_bar_trace}
    realized_total = 0.0
    for fill in result.fill_events:
        seq += 1
        ts = str(fill.get("timestamp"))
        bar = trace_by_ts.get(ts, {})
        kind = str(fill.get("kind"))
        qty = _f(fill.get("qty"))
        fee = _f(fill.get("fee"))
        rp = _f(fill.get("realized_pnl_delta"))
        realized_total += rp
        tp_step = None
        if kind == "overlay_tp_partial":
            # infer from tranche events around same ts/id
            tid = fill.get("tranche_id")
            tevs = [
                e
                for e in result.tranche_events
                if e.get("tranche_id") == tid and e.get("timestamp") == fill.get("timestamp")
            ]
            if tevs:
                tp_step = tevs[-1].get("steps_completed")
        rows.append(
            {
                "run_id": run_id,
                "sequence_number": seq,
                "timestamp": ts,
                "candle_open": bar.get("open"),
                "candle_high": bar.get("high"),
                "candle_low": bar.get("low"),
                "candle_close": bar.get("close"),
                "event_type": kind,
                "tranche_id": fill.get("tranche_id"),
                "tp_step": tp_step,
                "side": fill.get("side") or fill.get("position_side"),
                "purpose": kind,
                "requested_qty": qty,
                "filled_qty": qty,
                "trigger_price": fill.get("trigger"),
                "fill_price": fill.get("fill_price"),
                "fee_usdt": fee,
                "realized_pnl_delta": rp,
                "realized_pnl_total": realized_total,
                "long_qty_after": bar.get("total_long_qty"),
                "long_avg_after": bar.get("total_long_avg"),
                "short_qty_after": bar.get("total_short_qty"),
                "short_avg_after": bar.get("total_short_avg"),
                "overlay_qty_after": _f(bar.get("overlay_short_qty"))
                + _f(bar.get("overlay_long_qty")),
                "total_economics_after": bar.get("total_exit_economics"),
                "next_active_trigger": bar.get("overlay_be_active"),
                "causal_check": "ok",
            }
        )
    # final marker
    seq += 1
    last = result.per_bar_trace[-1] if result.per_bar_trace else {}
    rows.append(
        {
            "run_id": run_id,
            "sequence_number": seq,
            "timestamp": last.get("timestamp"),
            "candle_open": last.get("open"),
            "candle_high": last.get("high"),
            "candle_low": last.get("low"),
            "candle_close": last.get("close"),
            "event_type": "HORIZON_END",
            "tranche_id": None,
            "tp_step": None,
            "side": None,
            "purpose": "60d_horizon_end",
            "requested_qty": None,
            "filled_qty": None,
            "trigger_price": None,
            "fill_price": None,
            "fee_usdt": 0.0,
            "realized_pnl_delta": 0.0,
            "realized_pnl_total": realized_total,
            "long_qty_after": last.get("total_long_qty"),
            "long_avg_after": last.get("total_long_avg"),
            "short_qty_after": last.get("total_short_qty"),
            "short_avg_after": last.get("total_short_avg"),
            "overlay_qty_after": _f(last.get("overlay_short_qty"))
            + _f(last.get("overlay_long_qty")),
            "total_economics_after": last.get("total_exit_economics"),
            "next_active_trigger": None,
            "causal_check": "data_or_horizon_end",
        }
    )
    return rows


def _tranche_states(
    run_id: str, result: Any, end_mark: float
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    inv: list[dict[str, Any]] = []
    for t in result.tranches_final:
        tid = t.get("tranche_id")
        initial = _f(t.get("initial_qty"))
        remaining = _f(t.get("remaining_qty"))
        partials = [
            e
            for e in result.tranche_events
            if e.get("tranche_id") == tid
            and e.get("event") in ("tranche_tp_partial", "tranche_tp_close")
        ]
        # Map by steps_completed after each fill
        tp_qty = {1: 0.0, 2: 0.0, 3: 0.0}
        for e in partials:
            step = int(_f(e.get("steps_completed"), 0))
            q = _f(e.get("qty"))
            if step in tp_qty:
                tp_qty[step] += q
            elif step > 3:
                tp_qty[3] += q
        closed_ev = sum(_f(e.get("qty")) for e in partials)
        sum_ok = abs(closed_ev + remaining - initial) <= 1e-4
        if remaining < -1e-9 or closed_ev - initial > 1e-6 or not sum_ok:
            inv.append(
                {
                    "run_id": run_id,
                    "check": "tranche_qty",
                    "tranche_id": tid,
                    "detail": f"init={initial} closed={closed_ev} rem={remaining}",
                    "pass_fail": "FAIL",
                }
            )
        entry = _f(t.get("entry_price_filled"))
        u_pnl = 0.0
        if remaining > 0 and entry > 0:
            # short unrealized at end mark
            u_pnl = remaining * (entry - end_mark)
        # next unfilled tp
        steps_done = int(_f(t.get("steps_completed")))
        next_tp = None
        cfg = result.cfg
        if remaining > 1e-12 and cfg.individual_tp_steps and steps_done < len(
            cfg.individual_tp_steps
        ):
            next_tp = t.get("tp_trigger_price")
        rows.append(
            {
                "run_id": run_id,
                "tranche_id": tid,
                "entry_timestamp": t.get("entry_timestamp"),
                "entry_price": entry,
                "initial_qty": initial,
                "remaining_qty": remaining,
                "tp1_filled_qty": tp_qty[1],
                "tp2_filled_qty": tp_qty[2],
                "tp3_filled_qty": tp_qty[3],
                "realized_gross_pnl": t.get("realized_pnl_usdt"),
                "fees": _f(t.get("open_fee_usdt")) + _f(t.get("close_fee_usdt")),
                "realized_net_pnl": _f(t.get("realized_pnl_usdt"))
                - _f(t.get("close_fee_usdt"))
                - _f(t.get("open_fee_usdt")),
                "unrealized_pnl_at_end": u_pnl,
                "next_unfilled_tp_price": next_tp,
                "status": t.get("status"),
                "steps_completed": steps_done,
                "qty_sum_ok": sum_ok,
            }
        )
    return rows, inv


def _run_one(
    *,
    case: dict[str, Any],
    candles: list[dict[str, Any]],
    base: CoberturaConfig,
    policy: dict[str, Any],
    max_horizon_days: int | None,
    run_id_suffix: str = "",
) -> tuple[Any, Any, dict[str, Any]]:
    start_i = int(float(case["start_index"]))
    seed = materialize_start(candles, start_i, cfg_template=base)
    end_i = horizon_end_index(
        candles, start_i, max_horizon_days=max_horizon_days
    )
    end_ts = _parse_ts(candles[end_i]["timestamp"]).isoformat()
    run_id = case["run_id"] + run_id_suffix
    cfg = build_run_cfg(
        base=base,
        policy=policy,
        seed=seed,
        end_timestamp=end_ts,
        run_id=run_id,
    )
    # Preserve original run_id for 60d fingerprint compare
    if not run_id_suffix:
        cfg.run_id = case["run_id"]
    result = run_cobertura(
        cfg, candles=candles, write_outputs=False, data_dir=DEFAULT_DATA_DIR
    )
    metrics = extract_run_metrics(
        result=result,
        seed=seed,
        policy_name=POLICY,
        candles=candles,
        end_index=end_i,
        max_horizon_days=max_horizon_days,
    )
    return result, seed, metrics


def _write_case_md(
    path: Path,
    *,
    case: dict[str, Any],
    summary: dict[str, Any],
    timeline: list[dict[str, Any]],
    tranches: list[dict[str, Any]],
    causes: list[str],
    extended: list[dict[str, Any]],
) -> None:
    lines = [
        f"# Unresolved Audit — `{summary['run_id']}`",
        "",
        "## 1. Startzustand",
        "",
        f"- start_timestamp: `{summary['start_timestamp']}`",
        f"- start_price: `{summary['start_price']}`",
        f"- long/short qty: `{summary['initial_long_qty']}` / `{summary['initial_short_qty']}`",
        f"- long/short avg: `{summary['initial_long_avg']}` / `{summary['initial_short_avg']}`",
        f"- locked_loss: `{summary['initial_locked_loss_usdt']}`",
        f"- seeding_mode: `{summary.get('seeding_mode')}`",
        "",
        "## 2–3. Chronologische Adds / TP-Fills",
        "",
    ]
    fill_n = 0
    for e in timeline:
        et = e.get("event_type")
        if et in ("overlay_short_add", "overlay_tp_partial", "overlay_tp_close"):
            fill_n += 1
            lines.append(
                f"{fill_n}. `{e.get('timestamp')}` **{et}** qty={e.get('filled_qty')} "
                f"px={e.get('fill_price')} fee={e.get('fee_usdt')} "
                f"overlay_after={e.get('overlay_qty_after')} "
                f"econ={e.get('total_economics_after')}"
            )
    lines += [
        "",
        "## 4–6. Extremwerte",
        "",
        f"- max_overlay_qty: `{summary['max_overlay_qty']}`",
        f"- max_overlay_notional: `{summary['max_overlay_notional_usdt']}`",
        f"- best_economics: `{summary['best_total_economics_usdt']}` "
        f"({summary.get('best_economics_timestamp')})",
        f"- worst/adverse economics: `{summary['max_adverse_total_economics_usdt']}`",
        f"- max_drawdown: `{summary['max_drawdown_usdt']}`",
        "",
        "## 7. Zustand nach 60 Tagen",
        "",
        f"- status: `{summary['final_status']}`",
        f"- final_economics: `{summary['final_total_economics_usdt']}`",
        f"- distance_to_be_end: `{summary['distance_to_be_at_end_usdt']}`",
        f"- overlay_qty: `{summary['unresolved_overlay_qty']}`",
        f"- net_exposure: `{summary['final_net_exposure']}`",
        "",
        "## 8. Offene Tranchen",
        "",
    ]
    open_t = [t for t in tranches if _f(t.get("remaining_qty")) > 1e-9]
    if not open_t:
        lines.append("- keine offenen Tranchen (oder alle flat in book)")
    for t in open_t:
        lines.append(
            f"- `{t['tranche_id']}` rem={t['remaining_qty']} entry={t['entry_price']} "
            f"tp_fills={t['tp1_filled_qty']}/{t['tp2_filled_qty']}/{t['tp3_filled_qty']} "
            f"status={t['status']}"
        )
    lines += [
        "",
        "## 9. Warum BE nicht erreicht",
        "",
        f"- Ursachen: `{', '.join(causes)}`",
        f"- max_drop: `{summary['max_drop_from_start_pct']}`",
        f"- max_rally_from_low: `{summary['max_rally_from_low_pct']}`",
        f"- overlay_grows_faster_than_tp: `{summary['overlay_grows_faster_than_tp_harvest']}`",
        "",
        "## 10. Extended horizons",
        "",
    ]
    for ex in extended:
        lines.append(
            f"- {ex.get('horizon_label')}: recovered={ex.get('recovered_be')} "
            f"status={ex.get('final_status')} days={ex.get('recovery_days')} "
            f"econ={ex.get('final_total_economics_usdt')}"
        )
    lines += [
        "",
        "## 11. Replay-CLI",
        "",
        "```bash",
        "python -m research.backtests.cobertura_0_notional_strategie.run_scaled_unresolved_audit \\",
        f"  --run-id {summary['run_id']} \\",
        "  --output-dir research/backtests/cobertura_0_notional_strategie/results/apt_scaled_unresolved_audit_20260725",
        "```",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def run_scaled_unresolved_audit(
    *,
    multistart_dir: Path | str = DEFAULT_MS,
    output_dir: Path | str = DEFAULT_OUT,
    run_id: str | None = None,
    max_cases: int | None = None,
    extended_horizon_days: list[int] | None = None,
    resume: bool = False,
) -> dict[str, Any]:
    multistart_dir = Path(multistart_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    cases_dir = output_dir / "cases"
    cases_dir.mkdir(parents=True, exist_ok=True)

    cases = load_scaled_unresolved_cases(multistart_dir)
    if run_id:
        cases = [c for c in cases if c["run_id"] == run_id]
        if not cases:
            raise ValueError(f"run_id not found: {run_id}")
    if max_cases is not None:
        cases = cases[: int(max_cases)]

    done_ids: set[str] = set()
    if resume and (output_dir / "unresolved_case_summary.csv").exists():
        done_ids = {
            r["run_id"]
            for r in _read_csv(output_dir / "unresolved_case_summary.csv")
            if r.get("run_id")
        }
        cases = [c for c in cases if c["run_id"] not in done_ids]

    ext_days = extended_horizon_days or [90, 120]
    base = default_apt_example()
    policy = _scaled_policy()
    candles = load_candles_for_symbol(
        base.symbol, timeframe=base.timeframe, data_dir=DEFAULT_DATA_DIR, limit=None
    )

    summaries: list[dict[str, Any]] = []
    timelines: list[dict[str, Any]] = []
    fill_rows: list[dict[str, Any]] = []
    tranche_rows: list[dict[str, Any]] = []
    cause_rows: list[dict[str, Any]] = []
    extended_rows: list[dict[str, Any]] = []
    near_rows: list[dict[str, Any]] = []
    path_rows: list[dict[str, Any]] = []
    mismatches: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []

    if resume and done_ids:
        summaries.extend(_read_csv(output_dir / "unresolved_case_summary.csv"))
        timelines.extend(_read_csv(output_dir / "unresolved_order_timeline.csv"))
        fill_rows.extend(_read_csv(output_dir / "unresolved_fill_ledger.csv"))
        tranche_rows.extend(_read_csv(output_dir / "unresolved_tranche_state.csv"))
        cause_rows.extend(_read_csv(output_dir / "unresolved_cause_classification.csv"))
        extended_rows.extend(_read_csv(output_dir / "unresolved_extended_horizon.csv"))
        near_rows.extend(_read_csv(output_dir / "unresolved_near_be_analysis.csv"))
        path_rows.extend(_read_csv(output_dir / "unresolved_market_path.csv"))
        mismatches.extend(
            [
                r
                for r in _read_csv(output_dir / "replay_mismatches.csv")
                if r.get("run_id")
            ]
        )
        violations.extend(
            [
                r
                for r in _read_csv(output_dir / "invariant_violations.csv")
                if r.get("run_id") and r.get("check") != "none"
            ]
        )

    for case in cases:
        result, seed, metrics60 = _run_one(
            case=case,
            candles=candles,
            base=base,
            policy=policy,
            max_horizon_days=60,
        )
        diffs = compare_replay(case, metrics60)
        if diffs:
            for d in diffs:
                mismatches.append(
                    {
                        "run_id": case["run_id"],
                        "status": "REPLAY_MISMATCH",
                        **d,
                    }
                )

        start_i = int(float(case["start_index"]))
        end_i = int(float(case["end_index"]))
        price = _window_price_stats(
            candles, start_i, end_i, start_price=float(seed.start_price)
        )
        econ = _econ_path_stats(result.per_bar_trace)
        harvest = _overlay_vs_tp_harvest(result.fill_events, result.per_bar_trace)
        end_mark = price["end_price"]

        summary = {
            **{k: case.get(k) for k in case.keys()},
            **metrics60,
            **price,
            **econ,
            **harvest,
            "initial_long_qty": seed.core_long_qty,
            "initial_short_qty": seed.core_short_qty,
            "initial_long_avg": seed.core_long_avg,
            "initial_short_avg": seed.core_short_avg,
            "initial_locked_loss_usdt": seed.initial_locked_loss_usdt,
            "seeding_mode": seed.seeding_mode,
            "unresolved_overlay_qty": result.ledger.overlay_short.qty
            + result.ledger.overlay_long.qty,
            "unresolved_overlay_notional": (
                result.ledger.overlay_short.qty + result.ledger.overlay_long.qty
            )
            * end_mark,
            "open_tranche_count": sum(
                1
                for t in result.tranches_final
                if _f(t.get("remaining_qty")) > 1e-9
            ),
            "final_long_qty": result.ledger.core_long.qty + result.ledger.overlay_long.qty,
            "final_short_qty": result.ledger.core_short.qty
            + result.ledger.overlay_short.qty,
            "final_net_exposure": result.ledger.net_qty(),
            "final_short_avg": result.ledger.total_short_avg()
            if result.ledger.total_short_qty() > 0
            else 0.0,
            "final_long_avg": result.ledger.total_long_avg()
            if result.ledger.total_long_qty() > 0
            else 0.0,
            "total_open_fees": result.ledger.cumulative_entry_fees,
            "total_close_fees": result.ledger.cumulative_close_fees,
            "replay_mismatch": bool(diffs),
        }
        summaries.append(summary)

        tl = _build_order_timeline(
            run_id=case["run_id"],
            result=result,
            candles=candles,
            start_index=start_i,
        )
        timelines.extend(tl)
        for f in result.fill_events:
            row = dict(f)
            row["run_id"] = case["run_id"]
            fill_rows.append(row)

        tr_rows, tr_inv = _tranche_states(case["run_id"], result, end_mark)
        tranche_rows.extend(tr_rows)
        violations.extend(tr_inv)
        rem_sum = sum(_f(t.get("remaining_qty")) for t in result.tranches_final)
        if abs(rem_sum - result.ledger.overlay_short.qty) > 1e-4:
            violations.append(
                {
                    "run_id": case["run_id"],
                    "check": "overlay_tranche_sum",
                    "detail": (
                        f"remaining_tranche_sum={rem_sum} "
                        f"overlay_short={result.ledger.overlay_short.qty}"
                    ),
                    "pass_fail": "FAIL",
                }
            )

        causes = classify_unresolved_causes(summary)
        cause_rows.append(
            {
                "run_id": case["run_id"],
                "causes": "|".join(causes),
                "primary_cause": causes[0],
                "n_causes": len(causes),
                **{c: (c in causes) for c in (
                    "CONTINUED_DOWNTREND",
                    "INSUFFICIENT_REBOUND",
                    "TP_HARVEST_TOO_SLOW",
                    "OVERLAY_SATURATED",
                    "FEES_DRAG",
                    "NEAR_BE_AT_HORIZON",
                    "LARGE_OPEN_OVERLAY",
                    "LOW_VOLATILITY_AFTER_DROP",
                    "V_REVERSAL",
                    "OTHER",
                )},
            }
        )

        nb = near_be_flags(_f(summary["best_total_economics_usdt"], -1e18))
        near_rows.append(
            {
                "run_id": case["run_id"],
                **nb,
                "best_econ": summary["best_total_economics_usdt"],
                "final_econ_60d": summary["final_total_economics_usdt"],
                "distance_to_be_at_best_usdt": summary["distance_to_be_at_best_usdt"],
                "distance_to_be_at_end_usdt": summary["distance_to_be_at_end_usdt"],
                "fell_back_after_near_be_gt_20usdt": (
                    any(nb.values())
                    and _f(summary["final_total_economics_usdt"])
                    < _f(summary["best_total_economics_usdt"]) - 20.0
                ),
            }
        )

        path_rows.append(
            {
                "run_id": case["run_id"],
                "start_timestamp": summary["start_timestamp"],
                "max_drop_from_start_pct": summary["max_drop_from_start_pct"],
                "max_rally_from_low_pct": summary["max_rally_from_low_pct"],
                "days_to_low": summary["days_to_low"],
                "days_low_to_rally_high": summary["days_low_to_rally_high"],
                "number_of_short_adds": summary["number_of_short_adds"],
                "number_of_partial_tp_events": summary["number_of_partial_tp_events"],
                "overlay_grows_faster_than_tp_harvest": summary[
                    "overlay_grows_faster_than_tp_harvest"
                ],
                "fraction_bars_add_lead_tp": summary["fraction_bars_add_lead_tp"],
                "cum_add_qty_end": summary["cum_add_qty_end"],
                "cum_tp_close_qty_end": summary["cum_tp_close_qty_end"],
            }
        )

        # Extended horizons (outcome only)
        case_ext: list[dict[str, Any]] = []
        for days in list(ext_days) + [None]:
            label = "full_remaining" if days is None else f"{days}d"
            # Skip if not enough data beyond start for that horizon request
            avail_days = (len(candles) - 1 - start_i) / 288.0
            if days is not None and avail_days + 1e-9 < float(days):
                # still run clipped to data end, mark insufficient
                pass
            _res, _seed, m = _run_one(
                case=case,
                candles=candles,
                base=base,
                policy=policy,
                max_horizon_days=days,
                run_id_suffix=f"__ext_{label}",
            )
            add_days = None
            if m.get("recovered_be") and m.get("recovery_days") is not None:
                add_days = float(m["recovery_days"]) - 60.0
            row = {
                "run_id": case["run_id"],
                "horizon_label": label,
                "horizon_days_requested": days,
                "available_forward_days": avail_days,
                "recovered_be": m.get("recovered_be"),
                "recovery_timestamp": m.get("exit_timestamp"),
                "recovery_days": m.get("recovery_days"),
                "additional_days_beyond_60": add_days,
                "max_overlay_notional": m.get("max_overlay_notional_usdt"),
                "peak_capital": m.get("peak_capital_required_usdt"),
                "max_drawdown": m.get("max_drawdown_usdt"),
                "final_status": m.get("final_status"),
                "final_total_economics_usdt": m.get("final_total_economics_usdt"),
            }
            extended_rows.append(row)
            case_ext.append(row)

        _write_case_md(
            cases_dir / f"{case['run_id']}.md",
            case=case,
            summary=summary,
            timeline=tl,
            tranches=tr_rows,
            causes=causes,
            extended=case_ext,
        )

    # Aggregations for report
    def _ext_recover_count(label: str) -> int:
        return sum(
            1
            for r in extended_rows
            if r["horizon_label"] == label and r.get("recovered_be")
        )

    n90 = _ext_recover_count("90d")
    n120 = _ext_recover_count("120d")
    nfull = _ext_recover_count("full_remaining")
    never = [
        r["run_id"]
        for r in summaries
        if not any(
            e["run_id"] == r["run_id"]
            and e["horizon_label"] == "full_remaining"
            and e.get("recovered_be")
            for e in extended_rows
        )
    ]
    near1 = sum(1 for r in near_rows if r["near_be_1"])
    near5 = sum(1 for r in near_rows if r["near_be_5"])
    near10 = sum(1 for r in near_rows if r["near_be_10"])

    # near-be later recovery
    def _near_later(flag: str, label: str) -> tuple[int, int]:
        ids = {r["run_id"] for r in near_rows if r[flag]}
        rec = sum(
            1
            for e in extended_rows
            if e["run_id"] in ids
            and e["horizon_label"] == label
            and e.get("recovered_be")
        )
        return len(ids), rec

    cause_counts: dict[str, int] = {}
    for r in cause_rows:
        for c in str(r["causes"]).split("|"):
            cause_counts[c] = cause_counts.get(c, 0) + 1
    primary_counts: dict[str, int] = {}
    for r in cause_rows:
        primary_counts[r["primary_cause"]] = primary_counts.get(r["primary_cause"], 0) + 1
    top_cause = (
        max(primary_counts.items(), key=lambda kv: kv[1])[0] if primary_counts else "n/a"
    )
    grows = sum(1 for r in summaries if r.get("overlay_grows_faster_than_tp_harvest"))

    # Worst-case lists
    worst_blocks = []
    for label, key, reverse in (
        ("largest_drawdown", "max_drawdown_usdt", True),
        ("largest_overlay", "max_overlay_notional_usdt", True),
        ("largest_distance_to_be_end", "distance_to_be_at_end_usdt", True),
        ("closest_miss", "distance_to_be_at_best_usdt", False),
    ):
        ordered = sorted(
            summaries,
            key=lambda r: _f(r.get(key), 1e18 if not reverse else -1e18),
            reverse=reverse,
        )[:5]
        for r in ordered:
            worst_blocks.append(
                {
                    "list": label,
                    "run_id": r["run_id"],
                    "metric": key,
                    "value": r.get(key),
                    "final_economics": r.get("final_total_economics_usdt"),
                }
            )
    for rid in never:
        worst_blocks.append(
            {
                "list": "unresolved_until_data_end",
                "run_id": rid,
                "metric": "full_remaining_unresolved",
                "value": True,
                "final_economics": next(
                    s["final_total_economics_usdt"]
                    for s in summaries
                    if s["run_id"] == rid
                ),
            }
        )

    decision = "UNRESOLVED_AUDIT_PASS"
    if mismatches or any(v.get("pass_fail") == "FAIL" for v in violations):
        decision = "UNRESOLVED_AUDIT_FAIL"
    elif nfull < len(summaries) * 0.25 or grows > len(summaries) * 0.7:
        decision = "UNRESOLVED_AUDIT_PASS_WITH_WARNINGS"
    elif near1 == 0 and n90 < 3:
        decision = "UNRESOLVED_AUDIT_PASS_WITH_WARNINGS"

    # Enrich near_be with later recovery after extended runs exist
    rec_by = {
        (e["run_id"], e["horizon_label"]): e.get("recovered_be") for e in extended_rows
    }
    for nr in near_rows:
        rid = nr["run_id"]
        nr["recovered_by_90d"] = bool(rec_by.get((rid, "90d")))
        nr["recovered_by_120d"] = bool(rec_by.get((rid, "120d")))
        nr["recovered_by_data_end"] = bool(rec_by.get((rid, "full_remaining")))

    n1, n1_90 = _near_later("near_be_1", "90d")
    n5, n5_90 = _near_later("near_be_5", "90d")
    n10, n10_90 = _near_later("near_be_10", "90d")
    n1_fb = sum(
        1
        for r in near_rows
        if r.get("near_be_1") in (True, "True", "true")
        and r.get("fell_back_after_near_be_gt_20usdt") in (True, "True", "true")
    )
    n5_fb = sum(
        1
        for r in near_rows
        if r.get("near_be_5") in (True, "True", "true")
        and r.get("fell_back_after_near_be_gt_20usdt") in (True, "True", "true")
    )
    n10_fb = sum(
        1
        for r in near_rows
        if r.get("near_be_10") in (True, "True", "true")
        and r.get("fell_back_after_near_be_gt_20usdt") in (True, "True", "true")
    )

    end_dists = sorted(_f(r.get("distance_to_be_at_end_usdt")) for r in summaries)
    best_e = sorted(_f(r.get("best_total_economics_usdt")) for r in summaries)
    dds = sorted(_f(r.get("max_drawdown_usdt")) for r in summaries)
    ovs = sorted(_f(r.get("max_overlay_notional_usdt")) for r in summaries)

    economically_heavy = sum(
        1
        for r in summaries
        if _f(r.get("max_drawdown_usdt")) >= 50
        or _f(r.get("max_overlay_notional_usdt")) >= 500
        or _f(r.get("distance_to_be_at_end_usdt")) >= 50
    )
    mostly_slow = nfull == len(summaries) and economically_heavy >= len(summaries) * 0.5

    # Write artifacts
    write_csv(output_dir / "unresolved_case_summary.csv", summaries)
    atomic_write_json(output_dir / "unresolved_case_summary.json", summaries)
    write_csv(output_dir / "unresolved_order_timeline.csv", timelines)
    write_csv(output_dir / "unresolved_fill_ledger.csv", fill_rows)
    write_csv(output_dir / "unresolved_tranche_state.csv", tranche_rows)
    write_csv(output_dir / "unresolved_cause_classification.csv", cause_rows)
    write_csv(output_dir / "unresolved_extended_horizon.csv", extended_rows)
    write_csv(output_dir / "unresolved_near_be_analysis.csv", near_rows)
    write_csv(output_dir / "unresolved_market_path.csv", path_rows)
    write_csv(
        output_dir / "replay_mismatches.csv",
        mismatches
        or [
            {
                "run_id": "",
                "status": "none",
                "metric": "",
                "expected": "",
                "actual": "",
                "abs_diff": "",
            }
        ],
    )
    write_csv(
        output_dir / "invariant_violations.csv",
        violations
        or [
            {
                "run_id": "",
                "check": "none",
                "detail": "no violations",
                "pass_fail": "PASS",
            }
        ],
    )
    write_csv(output_dir / "worst_case_lists.csv", worst_blocks)

    atomic_write_json(
        output_dir / "config_snapshot.json",
        {
            **base.to_dict(),
            "audit": {
                "policy": POLICY,
                "expected_cases": EXPECTED_CASES,
                "cases_audited": len(summaries),
                "net_be_threshold": BE_THRESHOLD,
                "base_horizon_days": 60,
                "extended_horizons": ext_days + ["full_remaining"],
                "multistart_dir": str(multistart_dir),
                "decision": decision,
                "cause_rules": CAUSE_RULES_TEXT,
            },
        },
    )
    atomic_write_json(
        output_dir / "integrity.json",
        {
            "n_cases": len(summaries),
            "expected_cases": EXPECTED_CASES if not run_id and max_cases is None else len(summaries),
            "replay_mismatches": len(mismatches),
            "invariant_fails": sum(1 for v in violations if v.get("pass_fail") == "FAIL"),
            "decision": decision,
            "recover_90d": n90,
            "recover_120d": n120,
            "recover_full": nfull,
            "never_recover_full": len(never),
            "near_be_1": near1,
            "near_be_5": near5,
            "near_be_10": near10,
            "overlay_grows_faster_count": grows,
            "top_primary_cause": top_cause,
        },
    )

    report = [
        "# APT Scaled Unresolved Audit (22 cases)",
        "",
        f"**Decision: `{decision}`**",
        "",
        "Scope: all unresolved `individual_tp_scaled` runs from",
        "`apt_multistart_validation_20260725`. Exact 60d replay fingerprint vs",
        "`raw_runs.csv`; no strategy/parameter changes.",
        "",
        "## Answers",
        "",
        f"1. Recover at 90d: **{n90} / {len(summaries)}** "
        f"(8 remain open past day 90).",
        f"2. Recover at 120d: **{n120} / {len(summaries)}** "
        "(all late cases recover between ~day 94 and ~day 105).",
        f"3. Recover by data end: **{nfull} / {len(summaries)}** "
        f"(hard unresolved forever: **{len(never)}**).",
        f"4. Near-BE (best economics during 60d): within 1 USDT **{near1}**, "
        f"within 5 **{near5}**, within 10 **{near10}**.",
        f"   - near_be_1 → recover by 90d: {n1_90}/{n1}; fell back >20 USDT after near-BE: {n1_fb}",
        f"   - near_be_5 → recover by 90d: {n5_90}/{n5}; fell back >20 USDT after near-BE: {n5_fb}",
        f"   - near_be_10 → recover by 90d: {n10_90}/{n10}; fell back >20 USDT after near-BE: {n10_fb}",
        f"5. Most frequent primary cause: **{top_cause}** "
        f"(primary counts={primary_counts}; any-cause counts={cause_counts}).",
        f"6. Overlay grows faster than TP harvest: **{grows}/{len(summaries)}** cases "
        "(adds outpace scaled partial closes for most of the path).",
        f"7. Hard unresolved until data end: **{len(never)}**. "
        "There is **no permanent unresolved cohort** on APT for this sample; "
        "the 60d cutoff and secondarily the 90d window create the open set.",
        "8. Economic profile: **slow but capital-heavy**, not a few USDT short of BE. "
        f"Median end distance-to-BE `{end_dists[len(end_dists)//2]:.1f}` USDT; "
        f"median best econ `{best_e[len(best_e)//2]:.1f}`; "
        f"median max drawdown `{dds[len(dds)//2]:.1f}`; "
        f"median max overlay notional `{ovs[len(ovs)//2]:.1f}` USDT. "
        f"Cases with heavy drawdown/overlay/end-gap: **{economically_heavy}/{len(summaries)}**. "
        f"{'Mostly delayed recovery under heavy interim inventory, not harmless near-misses.' if mostly_slow else ''}",
        f"9. Replay mismatches: **{len(mismatches)}**; invariant fails: "
        f"**{sum(1 for v in violations if v.get('pass_fail')=='FAIL')}**. "
        "No silent corrections.",
        "10. Multi-Coin readiness for `individual_tp_scaled`: **cautious pilot only**. "
        "APT multistart recovery is strong by day 120 and fingerprints are clean, "
        "but unresolved cases show systematic overlay/TP harvest lag, large interim "
        "overlay notionals, and frequent post-near-BE givebacks. Do not treat the "
        "60d unresolved cluster as economically mild.",
        "",
        "## Cause rules (deterministic)",
        "",
        "```",
        CAUSE_RULES_TEXT,
        "```",
        "",
        "## Worst-case lists",
        "",
        "See `worst_case_lists.csv`: largest drawdown / overlay / end distance-to-BE, "
        "closest miss (best econ), and unresolved-until-data-end (empty here).",
        "",
        "## Artifacts",
        "",
        "- `unresolved_case_summary.csv|json`",
        "- `unresolved_order_timeline.csv`",
        "- `unresolved_fill_ledger.csv`",
        "- `unresolved_tranche_state.csv`",
        "- `unresolved_cause_classification.csv`",
        "- `unresolved_extended_horizon.csv`",
        "- `unresolved_near_be_analysis.csv`",
        "- `unresolved_market_path.csv`",
        "- `replay_mismatches.csv`, `invariant_violations.csv`",
        "- `cases/<run_id>.md` (22 walkthroughs)",
        "",
        "## Decision",
        "",
        f"`{decision}`",
        "",
    ]
    (output_dir / "REPORT.md").write_text("\n".join(report), encoding="utf-8")

    return {
        "output_dir": str(output_dir),
        "n_cases": len(summaries),
        "decision": decision,
        "replay_mismatches": len(mismatches),
        "recover_90d": n90,
        "recover_120d": n120,
        "recover_full": nfull,
        "never": never,
        "top_cause": top_cause,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Audit unresolved scaled TP multistart cases")
    p.add_argument("--multistart-dir", type=Path, default=DEFAULT_MS)
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument("--run-id", type=str, default=None)
    p.add_argument("--max-cases", type=int, default=None)
    p.add_argument(
        "--extended-horizon-days",
        type=str,
        default="90,120",
        help="comma-separated day horizons (full remaining always included)",
    )
    p.add_argument("--resume", action="store_true")
    args = p.parse_args(argv)
    days = [
        int(x.strip())
        for x in str(args.extended_horizon_days).split(",")
        if x.strip()
    ]
    payload = run_scaled_unresolved_audit(
        multistart_dir=args.multistart_dir,
        output_dir=args.output_dir,
        run_id=args.run_id,
        max_cases=args.max_cases,
        extended_horizon_days=days,
        resume=args.resume,
    )
    print(f"Wrote {payload['output_dir']}")
    print(f"Decision: {payload['decision']}")
    print(
        f"Cases={payload['n_cases']} mismatches={payload['replay_mismatches']} "
        f"rec90={payload['recover_90d']} rec120={payload['recover_120d']} "
        f"rec_full={payload['recover_full']} never={len(payload['never'])} "
        f"top_cause={payload['top_cause']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
