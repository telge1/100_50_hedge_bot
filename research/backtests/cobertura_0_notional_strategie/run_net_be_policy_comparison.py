"""Netto-BE policy comparison: shared_be / TP variants × exit targets."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from research.backtests.candle_loader import DEFAULT_DATA_DIR, load_candles_for_symbol
from research.backtests.multicoin_price_staging_grid import atomic_write_json, write_csv

from .config import CoberturaConfig, IndividualTpStep, default_apt_example
from .metrics import build_equity_curve, compute_reversal_stress
from .runner import run_cobertura

SAFETY_BUFFER_USDT = 0.25
TARGETS = (0.0, 0.25, 0.50, 1.00)
BARS_PER_HOUR = 12  # 5m candles


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


def _variant_cfg(
    base: CoberturaConfig,
    *,
    policy: dict[str, Any],
    target: float,
    safety_buffer: float = SAFETY_BUFFER_USDT,
) -> CoberturaConfig:
    raw = base.to_dict()
    raw.update(policy)
    raw["full_exit_target_mode"] = "net_be"
    raw["full_exit_target_usdt"] = float(target)
    raw["full_exit_safety_buffer_usdt"] = float(safety_buffer)
    tag = f"{policy['run_id']}_target_{target:.2f}".replace(".", "p")
    raw["run_id"] = tag
    return CoberturaConfig.from_dict(raw)


def _shadow_cfg(base: CoberturaConfig, policy: dict[str, Any]) -> CoberturaConfig:
    """Never full-exit; used only for first-touch / wait outcome analysis."""
    raw = base.to_dict()
    raw.update(policy)
    raw["full_exit_target_mode"] = "legacy"
    raw["target_total_pnl_usdt"] = 1e12
    raw["target_profit_buffer_usdt"] = 0.0
    raw["run_id"] = f"{policy['run_id']}_shadow_no_exit"
    return CoberturaConfig.from_dict(raw)


def _net_be_threshold(target: float, safety_buffer: float, tol: float) -> float:
    return float(target) + float(safety_buffer) - float(tol)


def _find_first_be_on_shadow(
    shadow: Any,
    *,
    target: float,
    safety_buffer: float,
) -> dict[str, Any] | None:
    tol = float(shadow.cfg.pnl_tolerance_usdt)
    thr = _net_be_threshold(target, safety_buffer, tol)
    peak_cap = 0.0
    for i, bar in enumerate(shadow.per_bar_trace):
        econ = _f(bar, "total_exit_economics")
        gross = _f(bar, "gross_notional")
        if gross <= 0.0:
            px = _f(bar, "close")
            gross = (
                _f(bar, "overlay_short_qty")
                + _f(bar, "overlay_long_qty")
                + float(shadow.cfg.core_long_qty)
                + float(shadow.cfg.core_short_qty)
            ) * px
        peak_cap = max(peak_cap, gross)
        if econ + 1e-12 >= thr:
            return {
                "bar_index": i,
                "timestamp": bar.get("timestamp"),
                "close": _f(bar, "close"),
                "total_exit_economics": econ,
                "overlay_short_qty": _f(bar, "overlay_short_qty"),
                "gross_notional": gross,
                "peak_capital_to_first_be": peak_cap,
            }
    return None


def _wait_outcomes(shadow: Any, first: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "econ_after_1h": None,
        "econ_after_3h": None,
        "econ_after_6h": None,
        "econ_after_12h": None,
        "wait_raised_profit": None,
        "wait_raised_exposure": None,
        "wait_raised_capital": None,
        "wait_worsened_drawdown": None,
    }
    if first is None:
        return out
    i0 = int(first["bar_index"])
    trace = shadow.per_bar_trace
    e0 = float(first["total_exit_economics"])
    ov0 = float(first["overlay_short_qty"])
    cap0 = float(first["gross_notional"])
    min_after = e0
    max_ov = ov0
    max_cap = cap0
    for hours, key in ((1, "econ_after_1h"), (3, "econ_after_3h"), (6, "econ_after_6h"), (12, "econ_after_12h")):
        j = i0 + hours * BARS_PER_HOUR
        if j < len(trace):
            out[key] = _f(trace[j], "total_exit_economics")
    for bar in trace[i0:]:
        e = _f(bar, "total_exit_economics")
        ov = _f(bar, "overlay_short_qty")
        px = _f(bar, "close")
        cap = (
            ov
            + _f(bar, "overlay_long_qty")
            + float(shadow.cfg.core_long_qty)
            + float(shadow.cfg.core_short_qty)
        ) * px
        min_after = min(min_after, e)
        max_ov = max(max_ov, ov)
        max_cap = max(max_cap, cap)
    last = _f(trace[-1], "total_exit_economics") if trace else e0
    out["wait_raised_profit"] = last > e0 + 1e-9
    out["wait_raised_exposure"] = max_ov > ov0 + 1e-9
    out["wait_raised_capital"] = max_cap > cap0 + 1e-9
    out["wait_worsened_drawdown"] = min_after < e0 - 1e-9
    return out


def compute_net_be_metrics(
    result: Any,
    *,
    shadow: Any,
    target: float,
    safety_buffer: float,
) -> dict[str, Any]:
    cfg = result.cfg
    ledger = result.ledger
    fills = result.fill_events
    trace = result.per_bar_trace

    add_fills = [f for f in fills if f.get("kind") == "overlay_short_add"]
    tp_closes = [
        f
        for f in fills
        if f.get("kind") in ("overlay_tp_close", "overlay_be_close")
    ]
    tp_partials = [f for f in fills if f.get("kind") == "overlay_tp_partial"]

    max_ov_qty = 0.0
    max_ov_notional = 0.0
    peak_long_notional = 0.0
    peak_short_notional = 0.0
    max_gross = 0.0
    max_adverse = None
    best_econ = None
    max_dd_from_best = 0.0
    for bar in trace:
        ov_s = _f(bar, "overlay_short_qty")
        ov_l = _f(bar, "overlay_long_qty")
        px = _f(bar, "close")
        long_n = (float(cfg.core_long_qty) + ov_l) * px
        short_n = (float(cfg.core_short_qty) + ov_s) * px
        gross = long_n + short_n
        max_ov_qty = max(max_ov_qty, ov_s + ov_l)
        max_ov_notional = max(max_ov_notional, (ov_s + ov_l) * px)
        peak_long_notional = max(peak_long_notional, long_n)
        peak_short_notional = max(peak_short_notional, short_n)
        max_gross = max(max_gross, gross)
        econ = _f(bar, "total_exit_economics")
        max_adverse = econ if max_adverse is None else min(max_adverse, econ)
        best_econ = econ if best_econ is None else max(best_econ, econ)
        if best_econ is not None:
            max_dd_from_best = max(max_dd_from_best, best_econ - econ)

    final_econ = None
    unrealized_before = 0.0
    est_close_before = 0.0
    est_slip_before = 0.0
    exit_ts = None
    exit_px = None
    for ev in result.order_events:
        if ev.get("event") == "full_exit":
            final_econ = float(ev.get("total_exit_economics_pre"))
            exit_ts = ev.get("timestamp")
            est_close_before = float(ev.get("estimated_remaining_close_fees_pre") or 0.0)
            est_slip_before = float(ev.get("estimated_exit_slippage_pre") or 0.0)
            break
    for bar in reversed(trace):
        if bar.get("state") not in ("RECOVERED", "RECOVERED_BE"):
            unrealized_before = _f(bar, "overlay_open_pnl") + _f(bar, "core_open_pnl")
            if final_econ is None:
                final_econ = _f(bar, "total_exit_economics")
            if exit_px is None:
                exit_px = _f(bar, "close")
            if exit_ts is None and result.state in ("RECOVERED", "RECOVERED_BE"):
                exit_ts = bar.get("timestamp")
            break
    if exit_px is None and trace:
        exit_px = _f(trace[-1], "close")
        if final_econ is None:
            final_econ = _f(trace[-1], "total_exit_economics")

    recovered_be = result.state == "RECOVERED_BE" or (
        result.state == "RECOVERED" and result.exit_reason == "recovered_net_be"
    )
    thr = float(target) + float(safety_buffer)
    excess = None if final_econ is None else float(final_econ) - thr

    first_shadow = _find_first_be_on_shadow(
        shadow, target=target, safety_buffer=safety_buffer
    )
    first_engine = result.first_net_be_touch
    first_ts = None
    first_bar = None
    if first_engine:
        first_ts = first_engine.get("timestamp")
        first_bar = first_engine.get("bar_index")
    elif first_shadow:
        first_ts = first_shadow.get("timestamp")
        first_bar = first_shadow.get("bar_index")

    exit_bar = None
    if exit_ts:
        for i, bar in enumerate(trace):
            if bar.get("timestamp") == exit_ts:
                exit_bar = i
                break
    delay = None
    if first_bar is not None and exit_bar is not None:
        delay = int(exit_bar) - int(first_bar)

    wait = _wait_outcomes(shadow, first_shadow)
    core_qty = float(cfg.core_qty())
    ratio = (max_ov_qty / core_qty) if core_qty > 0 else None
    total_fees = float(ledger.cumulative_entry_fees) + float(
        ledger.cumulative_close_fees
    )

    # Realized core approx from full-exit fills
    realized_core = 0.0
    for f in fills:
        if f.get("kind") == "full_exit" and f.get("leg") == "core":
            realized_core += float(f.get("realized_pnl_delta") or 0.0)

    return {
        "policy_name": str(cfg.tags.get("policy_name") or cfg.overlay_exit_policy)
        if cfg.tags
        else cfg.overlay_exit_policy,
        "variant_id": cfg.run_id,
        "overlay_exit_policy": cfg.overlay_exit_policy,
        "full_exit_target_usdt": float(target),
        "full_exit_safety_buffer_usdt": float(safety_buffer),
        "final_status": result.state,
        "recovered_be": bool(recovered_be),
        "full_exit_timestamp": exit_ts,
        "full_exit_price": exit_px,
        "final_total_economics_usdt": final_econ,
        "excess_profit_above_target_usdt": excess,
        "initial_locked_spread_loss_usdt": result.locked_spread_loss,
        "realized_overlay_pnl_usdt": ledger.realized_overlay_pnl,
        "realized_core_pnl_usdt": realized_core,
        "unrealized_pnl_before_exit_usdt": unrealized_before,
        "estimated_remaining_close_fees_before_exit_usdt": est_close_before,
        "estimated_exit_slippage_before_exit_usdt": est_slip_before,
        "total_open_fees_usdt": ledger.cumulative_entry_fees,
        "total_close_fees_usdt": ledger.cumulative_close_fees,
        "total_fees_usdt": total_fees,
        "number_of_short_adds": len(add_fills),
        "number_of_tp_events": len(tp_closes) + len(tp_partials),
        "number_of_partial_tp_events": len(tp_partials),
        "number_of_overlay_rounds": result.recovery_rounds,
        "max_overlay_qty": max_ov_qty,
        "max_overlay_notional_usdt": max_ov_notional,
        "max_overlay_to_core_ratio": ratio,
        "peak_long_notional_usdt": peak_long_notional,
        "peak_short_notional_usdt": peak_short_notional,
        "max_total_gross_notional_usdt": max_gross,
        "peak_capital_required_usdt": max_gross,
        "max_adverse_total_economics_usdt": max_adverse,
        "max_drawdown_from_best_economics_usdt": max_dd_from_best,
        "recovery_duration_bars": result.bars_processed,
        "recovery_duration_hours": result.bars_processed * 5.0 / 60.0,
        "unresolved_overlay_qty_at_end": ledger.overlay_short.qty
        + ledger.overlay_long.qty,
        "final_long_qty": ledger.core_long.qty + ledger.overlay_long.qty,
        "final_short_qty": ledger.core_short.qty + ledger.overlay_short.qty,
        "final_net_exposure_qty": ledger.net_qty(),
        "safety_violation_count": int(
            result.integrity.get("safety_violation_count", 0)
        ),
        "data_end_open": result.state == "DATA_END_OPEN",
        "first_be_timestamp": first_ts,
        "first_be_bar_index": first_bar,
        "actual_exit_bar_index": exit_bar,
        "be_to_exit_delay_bars": delay,
        "overlay_qty_at_first_be": (first_shadow or {}).get("overlay_short_qty")
        if first_shadow
        else (first_engine or {}).get("overlay_short_qty"),
        "peak_capital_to_first_be": (first_shadow or {}).get("peak_capital_to_first_be"),
        "exit_reason": result.exit_reason,
        **wait,
    }


def _rank_key(row: dict[str, Any]) -> tuple:
    return (
        int(row.get("safety_violation_count") or 0),
        0 if row.get("recovered_be") else 1,
        1 if float(row.get("unresolved_overlay_qty_at_end") or 0) > 1e-9 else 0,
        float(row.get("max_overlay_qty") or 1e18),
        float(row.get("peak_capital_required_usdt") or 1e18),
        -float(row.get("max_adverse_total_economics_usdt") or -1e18),
        float(row.get("recovery_duration_bars") or 1e18),
        float(row.get("total_fees_usdt") or 1e18),
        -float(row.get("excess_profit_above_target_usdt") or 0.0),
        str(row.get("variant_id") or ""),
    )


def _write_report(
    path: Path,
    summaries: list[dict[str, Any]],
    ranking: list[dict[str, Any]],
) -> None:
    recovered = [s for s in summaries if s.get("recovered_be")]
    by_reliable = sorted(
        summaries,
        key=lambda r: (
            0 if r.get("recovered_be") else 1,
            int(r.get("safety_violation_count") or 0),
            r.get("variant_id"),
        ),
    )
    by_ov = sorted(
        recovered or summaries,
        key=lambda r: (r.get("max_overlay_qty") or 1e18, r.get("variant_id")),
    )
    by_cap = sorted(
        recovered or summaries,
        key=lambda r: (r.get("peak_capital_required_usdt") or 1e18, r.get("variant_id")),
    )
    by_dd = sorted(
        recovered or summaries,
        key=lambda r: (
            -(r.get("max_adverse_total_economics_usdt") or -1e18),
            r.get("variant_id"),
        ),
    )
    by_fast = sorted(
        recovered or summaries,
        key=lambda r: (r.get("recovery_duration_bars") or 1e18, r.get("variant_id")),
    )
    best = ranking[0] if ranking else None

    # Did shared_be trade past first BE under legacy high-profit exit?
    shared0 = [
        s
        for s in summaries
        if s.get("overlay_exit_policy") == "shared_be"
        and float(s.get("full_exit_target_usdt") or 0) == 0.0
    ]
    past_be = None
    if shared0:
        s = shared0[0]
        delay = s.get("be_to_exit_delay_bars")
        past_be = (
            f"delay_bars={delay}; first_be={s.get('first_be_timestamp')}; "
            f"exit={s.get('full_exit_timestamp')}; final_econ={s.get('final_total_economics_usdt')}"
        )

    lines = [
        "# APT Netto-BE Policy Comparison",
        "",
        "Objective: reach true net break-even robustly, early, with low overlay "
        "exposure and low peak capital — not maximum profit.",
        "",
        "## Answers",
        "",
        f"1. **Most reliable net BE:** `{by_reliable[0]['variant_id']}` "
        f"(recovered_be={by_reliable[0].get('recovered_be')})",
        "",
        f"2. **Lowest overlay among recovered:** `{by_ov[0]['variant_id']}` "
        f"(max_overlay_qty={by_ov[0].get('max_overlay_qty')})",
        "",
        f"3. **Lowest peak capital among recovered:** `{by_cap[0]['variant_id']}` "
        f"({by_cap[0].get('peak_capital_required_usdt')})",
        "",
        f"4. **Smallest adverse economics (drawdown proxy):** `{by_dd[0]['variant_id']}` "
        f"({by_dd[0].get('max_adverse_total_economics_usdt')})",
        "",
        f"5. **Fastest recovered BE:** `{by_fast[0]['variant_id']}` "
        f"({by_fast[0].get('recovery_duration_bars')} bars)",
        "",
        "6. **Target sensitivity (0 / 0.25 / 0.50 / 1.00):** see summary table; "
        "higher targets delay exit and may raise exposure/capital.",
        "",
        f"7. **shared_be past first BE?** {past_be or 'n/a'}",
        "",
        "8. **TP vs shared_be for pure BE:** compare recovered rows; prefer lower "
        "overlay / capital / duration among recovered_be=true.",
        "",
        f"9. **Best next research candidate:** `{best['variant_id'] if best else 'n/a'}` "
        "(lexicographic robust early low-exposure ranking).",
        "",
        "10. **Disproportionate capital/exposure cases:** flag any recovered row "
        "with max_overlay_to_core_ratio >> peers in the table below.",
        "",
        "## Ranking (best first)",
        "",
        "| rank | variant | recovered_be | max_ov | peak_cap | adverse | bars | fees | econ |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for i, s in enumerate(ranking, 1):
        lines.append(
            f"| {i} | {s.get('variant_id')} | {s.get('recovered_be')} | "
            f"{s.get('max_overlay_qty')} | {s.get('peak_capital_required_usdt')} | "
            f"{s.get('max_adverse_total_economics_usdt')} | "
            f"{s.get('recovery_duration_bars')} | {s.get('total_fees_usdt')} | "
            f"{s.get('final_total_economics_usdt')} |"
        )
    lines += [
        "",
        "## Summary table",
        "",
        "| policy | target | recovered_be | econ | first_be | exit | bars | max_ov | peak_cap | adverse | fees | adds | status |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for s in summaries:
        lines.append(
            f"| {s.get('overlay_exit_policy')} | {s.get('full_exit_target_usdt')} | "
            f"{s.get('recovered_be')} | {s.get('final_total_economics_usdt')} | "
            f"{s.get('first_be_timestamp')} | {s.get('full_exit_timestamp')} | "
            f"{s.get('recovery_duration_bars')} | {s.get('max_overlay_qty')} | "
            f"{s.get('peak_capital_required_usdt')} | "
            f"{s.get('max_adverse_total_economics_usdt')} | "
            f"{s.get('total_fees_usdt')} | {s.get('number_of_short_adds')} | "
            f"{s.get('final_status')} |"
        )
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def run_net_be_policy_comparison(
    *,
    output_dir: str | Path | None = None,
    safety_buffer: float = SAFETY_BUFFER_USDT,
) -> dict[str, Any]:
    base = default_apt_example()
    out = Path(
        output_dir
        or Path(__file__).resolve().parent
        / "results"
        / "apt_net_be_policy_comparison_20260725"
    )
    out.mkdir(parents=True, exist_ok=True)

    candles = load_candles_for_symbol(
        base.symbol,
        timeframe=base.timeframe,
        data_dir=DEFAULT_DATA_DIR,
        limit=base.candle_limit,
    )

    shadows: dict[str, Any] = {}
    for policy in _policy_specs():
        scfg = _shadow_cfg(base, policy)
        shadows[policy["run_id"]] = run_cobertura(
            scfg, candles=candles, write_outputs=False, data_dir=DEFAULT_DATA_DIR
        )

    summaries: list[dict[str, Any]] = []
    be_first_rows: list[dict[str, Any]] = []
    all_fills: list[dict[str, Any]] = []
    all_tranches: list[dict[str, Any]] = []
    all_equity: list[dict[str, Any]] = []
    reversals: list[dict[str, Any]] = []
    integrity: dict[str, Any] = {"variants": {}, "safety_buffer_usdt": safety_buffer}

    for policy in _policy_specs():
        shadow = shadows[policy["run_id"]]
        for target in TARGETS:
            cfg = _variant_cfg(
                base, policy=policy, target=target, safety_buffer=safety_buffer
            )
            # stamp policy_name for metrics
            cfg.tags = dict(cfg.tags or {})
            cfg.tags["policy_name"] = policy["run_id"]
            result = run_cobertura(
                cfg, candles=candles, write_outputs=False, data_dir=DEFAULT_DATA_DIR
            )
            metrics = compute_net_be_metrics(
                result,
                shadow=shadow,
                target=target,
                safety_buffer=safety_buffer,
            )
            metrics["policy_name"] = policy["run_id"]
            summaries.append(metrics)
            first = result.first_net_be_touch or {}
            shadow_first = _find_first_be_on_shadow(
                shadow, target=target, safety_buffer=safety_buffer
            )
            be_first_rows.append(
                {
                    "variant_id": cfg.run_id,
                    "policy_name": policy["run_id"],
                    "full_exit_target_usdt": target,
                    "engine_first_be_timestamp": first.get("timestamp"),
                    "engine_first_be_econ": first.get("total_exit_economics"),
                    "engine_deferred_same_candle_add": first.get(
                        "deferred_same_candle_add"
                    ),
                    "shadow_first_be_timestamp": (shadow_first or {}).get("timestamp"),
                    "shadow_first_be_econ": (shadow_first or {}).get(
                        "total_exit_economics"
                    ),
                    "actual_exit_timestamp": metrics.get("full_exit_timestamp"),
                    "be_to_exit_delay_bars": metrics.get("be_to_exit_delay_bars"),
                    **{k: metrics.get(k) for k in (
                        "econ_after_1h",
                        "econ_after_3h",
                        "econ_after_6h",
                        "econ_after_12h",
                        "wait_raised_profit",
                        "wait_raised_exposure",
                        "wait_raised_capital",
                        "wait_worsened_drawdown",
                    )},
                }
            )
            for fill in result.fill_events:
                row = dict(fill)
                row["variant_id"] = cfg.run_id
                all_fills.append(row)
            for ev in result.tranche_events:
                row = dict(ev)
                row["variant_id"] = cfg.run_id
                all_tranches.append(row)
            for eq in build_equity_curve(result):
                row = dict(eq)
                row["variant_id"] = cfg.run_id
                all_equity.append(row)
            rev = compute_reversal_stress(result)
            rev["variant_id"] = cfg.run_id
            rev["full_exit_target_usdt"] = target
            # Would earlier BE exit have avoided reversal path?
            rev["earlier_be_would_avoid_reversal"] = bool(
                metrics.get("recovered_be")
                and metrics.get("be_to_exit_delay_bars") is not None
                and int(metrics.get("be_to_exit_delay_bars") or 0) == 0
            )
            reversals.append(rev)
            integrity["variants"][cfg.run_id] = result.integrity

    ranking = sorted(summaries, key=_rank_key)
    for i, row in enumerate(ranking, 1):
        row["rank"] = i

    atomic_write_json(out / "config_snapshot.json", {
        **base.to_dict(),
        "comparison": {
            "full_exit_target_mode": "net_be",
            "full_exit_safety_buffer_usdt": safety_buffer,
            "targets": list(TARGETS),
            "policies": [p["run_id"] for p in _policy_specs()],
        },
    })
    atomic_write_json(out / "net_be_summary.json", summaries)
    write_csv(out / "net_be_summary.csv", summaries)
    write_csv(out / "be_first_touch.csv", be_first_rows)
    write_csv(out / "fills.csv", all_fills)
    write_csv(out / "tranche_events.csv", all_tranches)
    write_csv(out / "equity_curve.csv", all_equity)
    write_csv(out / "reversal_stress.csv", reversals)
    write_csv(out / "ranking.csv", ranking)
    atomic_write_json(out / "integrity.json", integrity)
    _write_report(out / "REPORT.md", summaries, ranking)

    return {
        "output_dir": str(out),
        "summaries": summaries,
        "ranking": ranking,
        "integrity": integrity,
    }


def main() -> int:
    payload = run_net_be_policy_comparison()
    print(f"Wrote {payload['output_dir']}")
    print(
        f"{'Policy':<22} {'Ziel':>6} {'BE':>5} {'Econ':>10} "
        f"{'FirstBE':<22} {'Exit':<22} {'Bars':>5} {'MaxOv':>8} "
        f"{'PeakCap':>10} {'Adverse':>10} {'Fees':>8} {'Adds':>5} Status"
    )
    for s in payload["summaries"]:
        print(
            f"{s['policy_name']:<22} {s['full_exit_target_usdt']:6.2f} "
            f"{'Y' if s['recovered_be'] else 'N':>5} "
            f"{(s['final_total_economics_usdt'] if s['final_total_economics_usdt'] is not None else float('nan')):10.2f} "
            f"{str(s.get('first_be_timestamp') or '-'):<22} "
            f"{str(s.get('full_exit_timestamp') or '-'):<22} "
            f"{int(s.get('recovery_duration_bars') or 0):5d} "
            f"{float(s.get('max_overlay_qty') or 0):8.1f} "
            f"{float(s.get('peak_capital_required_usdt') or 0):10.1f} "
            f"{float(s.get('max_adverse_total_economics_usdt') or 0):10.2f} "
            f"{float(s.get('total_fees_usdt') or 0):8.2f} "
            f"{int(s.get('number_of_short_adds') or 0):5d} "
            f"{s.get('final_status')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
