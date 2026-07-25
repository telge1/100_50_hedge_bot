"""A/B/C/D strategy comparison including dynamic_long_equalization."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from research.backtests.candle_loader import DEFAULT_DATA_DIR, load_candles_for_symbol
from research.backtests.multicoin_price_staging_grid import atomic_write_json, write_csv

from .config import CoberturaConfig, IndividualTpStep, default_apt_example
from .metrics import build_equity_curve
from .runner import run_cobertura


def _variant_configs(base: CoberturaConfig) -> list[CoberturaConfig]:
    specs: list[dict[str, Any]] = [
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
    for pct, tag in (
        (0.03, "3pct"),
        (0.04, "4pct"),
        (0.05, "5pct"),
        (0.06, "6pct"),
    ):
        specs.append(
            {
                "overlay_exit_policy": "dynamic_long_equalization",
                "max_locked_spread_pct": pct,
                "long_equalization_fee_buffer_usdt": 0.0,
                "long_equalization_require_recovery": True,
                "run_id": f"dynamic_long_equalization_{tag}",
            }
        )
    out: list[CoberturaConfig] = []
    for overrides in specs:
        raw = base.to_dict()
        raw.update(overrides)
        out.append(CoberturaConfig.from_dict(raw))
    return out


def _f(row: dict[str, Any], key: str, default: float = 0.0) -> float:
    val = row.get(key)
    if val is None or val == "":
        return float(default)
    return float(val)


def compute_strategy_metrics(result: Any) -> dict[str, Any]:
    cfg = result.cfg
    ledger = result.ledger
    trace = result.per_bar_trace
    fills = result.fill_events

    add_fills = [f for f in fills if f.get("kind") == "overlay_short_add"]
    tp_closes = [
        f
        for f in fills
        if f.get("kind")
        in ("overlay_tp_close", "overlay_tp_partial", "overlay_be_close")
    ]
    eq_fills = [f for f in fills if f.get("kind") == "long_equalization"]
    eq_events = [
        e
        for e in result.equalization_events
        if e.get("event") == "long_equalization_fill"
    ]

    max_ov_qty = 0.0
    max_ov_notional = 0.0
    max_gross = 0.0
    max_adverse = None
    for bar in trace:
        ov = _f(bar, "overlay_short_qty")
        px = _f(bar, "close")
        max_ov_qty = max(max_ov_qty, ov)
        max_ov_notional = max(max_ov_notional, ov * px)
        gross = _f(bar, "gross_notional")
        max_gross = max(max_gross, gross)
        econ = _f(bar, "total_exit_economics")
        max_adverse = econ if max_adverse is None else min(max_adverse, econ)

    final_econ = None
    unrealized = 0.0
    if result.state == "RECOVERED":
        for ev in result.order_events:
            if ev.get("event") == "full_exit":
                final_econ = float(ev.get("total_exit_economics_pre"))
                break
        for bar in reversed(trace):
            if bar.get("state") != "RECOVERED":
                unrealized = _f(bar, "overlay_open_pnl") + _f(bar, "core_open_pnl")
                if final_econ is None:
                    final_econ = _f(bar, "total_exit_economics")
                break
    elif trace:
        final_econ = _f(trace[-1], "total_exit_economics")
        unrealized = _f(trace[-1], "overlay_open_pnl") + _f(trace[-1], "core_open_pnl")

    # Locked spread final from current totals (or initial if flat recovered)
    if ledger.total_long_qty() > 0 and ledger.total_short_qty() > 0:
        q = min(ledger.total_long_qty(), ledger.total_short_qty())
        locked_final = q * (ledger.total_long_avg() - ledger.total_short_avg())
        final_long_avg = ledger.total_long_avg()
        final_short_avg = ledger.total_short_avg()
    else:
        locked_final = result.locked_spread_loss
        final_long_avg = float(cfg.core_long_avg)
        final_short_avg = float(cfg.core_short_avg)

    eq = eq_events[0] if eq_events else None
    recovery_reached = result.state in ("RECOVERED", "EQUALIZED_LOCKED") or (
        final_econ is not None and final_econ >= -float(cfg.pnl_tolerance_usdt)
    )

    return {
        "variant_id": cfg.run_id or cfg.overlay_exit_policy,
        "overlay_exit_policy": cfg.overlay_exit_policy,
        "max_locked_spread_pct": cfg.max_locked_spread_pct
        if cfg.overlay_exit_policy == "dynamic_long_equalization"
        else None,
        "final_status": result.state,
        "final_total_economics_usdt": final_econ,
        "realized_pnl_usdt": ledger.realized_overlay_pnl,
        "unrealized_pnl_usdt": unrealized,
        "initial_locked_spread_loss_usdt": result.locked_spread_loss,
        "final_locked_spread_loss_usdt": locked_final,
        "recovery_reached": bool(recovery_reached),
        "recovery_duration_bars": result.bars_processed,
        "number_of_short_adds": len(add_fills),
        "number_of_short_tp_closes": len(tp_closes),
        "number_of_long_equalization_fills": len(eq_fills),
        "long_equalization_timestamp": eq.get("timestamp") if eq else None,
        "long_equalization_trigger_price": eq.get("trigger_price") if eq else None,
        "long_equalization_fill_price": eq.get("fill_price") if eq else None,
        "long_equalization_qty": eq.get("qty") if eq else None,
        "long_qty_after_equalization": eq.get("long_qty_after") if eq else None,
        "short_qty_after_equalization": eq.get("short_qty_after") if eq else None,
        "long_avg_before_equalization": eq.get("long_avg_before") if eq else None,
        "long_avg_after_equalization": eq.get("long_avg_after") if eq else None,
        "short_avg_at_equalization": eq.get("short_avg") if eq else None,
        "locked_spread_pct_after_equalization": eq.get("locked_spread_pct_after")
        if eq
        else None,
        "max_overlay_short_qty": max_ov_qty,
        "max_overlay_short_notional": max_ov_notional,
        "max_total_gross_notional": max_gross,
        "max_capital_required_usdt": max_gross,  # proxy: peak gross notional
        "total_open_fees": ledger.cumulative_entry_fees,
        "total_close_fees": ledger.cumulative_close_fees,
        "max_adverse_total_economics": max_adverse,
        "safety_violation_count": int(
            result.integrity.get("safety_violation_count", 0)
        ),
        "data_end_open": result.state
        in ("DATA_END_OPEN", "EQUALIZED_LOCKED")
        and result.state != "RECOVERED",
        "final_net_exposure_qty": ledger.net_qty(),
        "final_long_avg": final_long_avg,
        "final_short_avg": final_short_avg,
        "exit_reason": result.exit_reason,
    }


def compute_post_equalization_stress(result: Any) -> dict[str, Any]:
    """Outcome-only path metrics after equalization fill (no lookahead decisions)."""
    base = {
        "variant_id": result.cfg.run_id,
        "overlay_exit_policy": result.cfg.overlay_exit_policy,
        "max_locked_spread_pct": result.cfg.max_locked_spread_pct
        if result.cfg.overlay_exit_policy == "dynamic_long_equalization"
        else None,
        "equalization_timestamp": None,
        "equalization_close": None,
    }
    for pct in (2, 4, 6, 10):
        base[f"economics_after_drop_{pct}pct"] = None
        base[f"economics_after_rally_{pct}pct"] = None

    eq_ts = None
    for e in result.equalization_events:
        if e.get("event") == "long_equalization_fill":
            eq_ts = e.get("timestamp")
            break
    if eq_ts is None:
        return base

    trace = result.per_bar_trace
    eq_i = None
    for i, bar in enumerate(trace):
        if bar.get("timestamp") == eq_ts:
            eq_i = i
            break
    if eq_i is None:
        return base

    eq_close = _f(trace[eq_i], "close")
    base["equalization_timestamp"] = eq_ts
    base["equalization_close"] = eq_close

    def _first_touch(direction: str, pct: float) -> float | None:
        if eq_close <= 0:
            return None
        if direction == "drop":
            target = eq_close * (1.0 - pct)
            for bar in trace[eq_i + 1 :]:
                if _f(bar, "low") <= target + 1e-12:
                    return _f(bar, "total_exit_economics")
        else:
            target = eq_close * (1.0 + pct)
            for bar in trace[eq_i + 1 :]:
                if _f(bar, "high") + 1e-12 >= target:
                    return _f(bar, "total_exit_economics")
        return None

    for pct in (0.02, 0.04, 0.06, 0.10):
        tag = int(pct * 100)
        base[f"economics_after_drop_{tag}pct"] = _first_touch("drop", pct)
        base[f"economics_after_rally_{tag}pct"] = _first_touch("rally", pct)
    return base


def _write_report(
    path: Path,
    summaries: list[dict[str, Any]],
    stresses: list[dict[str, Any]],
) -> None:
    by_econ = sorted(
        summaries,
        key=lambda r: (
            -(r.get("final_total_economics_usdt") or -1e18),
            r.get("variant_id"),
        ),
    )
    by_exp = sorted(
        summaries, key=lambda r: (r.get("max_overlay_short_qty") or 1e18, r["variant_id"])
    )
    by_cap = sorted(
        summaries,
        key=lambda r: (-(r.get("max_capital_required_usdt") or 0), r["variant_id"]),
    )
    eq_rows = [
        s
        for s in summaries
        if s["overlay_exit_policy"] == "dynamic_long_equalization"
    ]
    triggered = [
        s for s in eq_rows if int(s.get("number_of_long_equalization_fills") or 0) > 0
    ]
    shared = next(s for s in summaries if s["variant_id"] == "shared_be")
    tp2 = next(s for s in summaries if s["variant_id"] == "individual_tp_2p00")
    best_eq = (
        max(triggered, key=lambda r: r.get("final_total_economics_usdt") or -1e18)
        if triggered
        else None
    )

    lines = [
        "# APT Strategy Comparison (shared_be / TP / dynamic long equalization)",
        "",
        "## Answers",
        "",
        f"1. **Best final economics:** `{by_econ[0]['variant_id']}` "
        f"({by_econ[0].get('final_total_economics_usdt')})",
        "",
        f"2. **Lowest overlay exposure:** `{by_exp[0]['variant_id']}` "
        f"(max_overlay_short_qty={by_exp[0].get('max_overlay_short_qty')})",
        "",
        f"3. **Most additional capital (peak gross notional):** `{by_cap[0]['variant_id']}` "
        f"({by_cap[0].get('max_capital_required_usdt')})",
        "",
        "4. **Equalization triggered at spread caps:** "
        + (
            ", ".join(
                f"`{s['variant_id']}` (cap={s.get('max_locked_spread_pct')})"
                for s in triggered
            )
            if triggered
            else "none in this window"
        ),
        "",
        "5. **New long/short spread after equalization:** "
        + (
            "; ".join(
                f"`{s['variant_id']}` → {s.get('locked_spread_pct_after_equalization')}"
                for s in triggered
            )
            if triggered
            else "n/a"
        ),
        "",
        "6. **Equalization vs closing short-adds:** "
        + (
            f"best eq `{best_eq['variant_id']}` econ={best_eq.get('final_total_economics_usdt')} "
            f"vs shared_be={shared.get('final_total_economics_usdt')} "
            f"vs tp2%={tp2.get('final_total_economics_usdt')}."
            if best_eq
            else "no equalization fill to compare."
        ),
        "",
        "7. **Post-equalization path stress:** see `post_equalization_stress.csv`.",
    ]
    for st in stresses:
        if st.get("equalization_timestamp"):
            lines.append(
                f"   - `{st['variant_id']}` drop2={st.get('economics_after_drop_2pct')} "
                f"rally2={st.get('economics_after_rally_2pct')} "
                f"drop10={st.get('economics_after_drop_10pct')} "
                f"rally10={st.get('economics_after_rally_10pct')}"
            )
    lines += [
        "",
        "8. **Next research candidate:** keep `shared_be` as economics leader; "
        "treat dynamic_long_equalization as a capital/structure alternative when fills "
        "occur; `individual_tp_2p00` / scaled remain exposure-focused backups.",
        "",
        "## Summary table",
        "",
        "| variant | status | econ | short_adds | eq_fills | max_ov | capital | locked_final |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for s in summaries:
        lines.append(
            f"| {s.get('variant_id')} | {s.get('final_status')} | "
            f"{s.get('final_total_economics_usdt')} | {s.get('number_of_short_adds')} | "
            f"{s.get('number_of_long_equalization_fills')} | "
            f"{s.get('max_overlay_short_qty')} | {s.get('max_capital_required_usdt')} | "
            f"{s.get('final_locked_spread_loss_usdt')} |"
        )
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def run_strategy_comparison(
    *,
    output_dir: str | Path | None = None,
) -> dict[str, Any]:
    base = default_apt_example()
    base.overlay_exit_policy = "shared_be"
    out = Path(
        output_dir
        or Path(__file__).resolve().parent
        / "results"
        / "apt_strategy_comparison_20260725"
    )
    out.mkdir(parents=True, exist_ok=True)

    candles = load_candles_for_symbol(
        base.symbol,
        timeframe=base.timeframe,
        data_dir=DEFAULT_DATA_DIR,
        limit=base.candle_limit,
    )

    summaries: list[dict[str, Any]] = []
    stresses: list[dict[str, Any]] = []
    all_eq: list[dict[str, Any]] = []
    all_fills: list[dict[str, Any]] = []
    all_equity: list[dict[str, Any]] = []
    integrity: dict[str, Any] = {"variants": {}}

    for cfg in _variant_configs(base):
        cfg_run = CoberturaConfig.from_dict(cfg.to_dict())
        result = run_cobertura(
            cfg_run, candles=candles, write_outputs=False, data_dir=DEFAULT_DATA_DIR
        )
        metrics = compute_strategy_metrics(result)
        stress = compute_post_equalization_stress(result)
        summaries.append(metrics)
        stresses.append(stress)
        for ev in result.equalization_events:
            row = dict(ev)
            row["variant_id"] = cfg_run.run_id
            all_eq.append(row)
        for fill in result.fill_events:
            row = dict(fill)
            row["variant_id"] = cfg_run.run_id
            all_fills.append(row)
        for eq in build_equity_curve(result):
            row = dict(eq)
            row["variant_id"] = cfg_run.run_id
            all_equity.append(row)
        integrity["variants"][cfg_run.run_id] = result.integrity

    shared = next(s for s in summaries if s["variant_id"] == "shared_be")
    integrity["shared_be_final_econ"] = shared["final_total_economics_usdt"]
    integrity["shared_be_final_status"] = shared["final_status"]
    integrity["n_variants"] = len(summaries)

    atomic_write_json(out / "config_snapshot.json", base.to_dict())
    atomic_write_json(out / "strategy_summary.json", summaries)
    write_csv(out / "strategy_summary.csv", summaries)
    write_csv(out / "equalization_events.csv", all_eq)
    write_csv(out / "fills.csv", all_fills)
    write_csv(out / "equity_curve.csv", all_equity)
    write_csv(out / "post_equalization_stress.csv", stresses)
    atomic_write_json(out / "integrity.json", integrity)
    _write_report(out / "REPORT.md", summaries, stresses)

    return {
        "output_dir": str(out),
        "summaries": summaries,
        "stresses": stresses,
        "integrity": integrity,
    }


def main() -> int:
    payload = run_strategy_comparison()
    print(f"Wrote {payload['output_dir']}")
    for s in payload["summaries"]:
        print(
            f"{s['variant_id']}: {s['final_status']} "
            f"econ={s['final_total_economics_usdt']} "
            f"eq_fills={s['number_of_long_equalization_fills']} "
            f"max_ov={s['max_overlay_short_qty']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
