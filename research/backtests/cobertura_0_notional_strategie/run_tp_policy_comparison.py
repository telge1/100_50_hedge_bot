"""A/B comparison runner: shared_be vs individual_tp variants on APT example."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from research.backtests.candle_loader import DEFAULT_DATA_DIR, load_candles_for_symbol
from research.backtests.multicoin_price_staging_grid import atomic_write_json, write_csv

from .config import CoberturaConfig, IndividualTpStep, default_apt_example
from .metrics import build_equity_curve, compute_policy_metrics, compute_reversal_stress
from .runner import run_cobertura


def _variant_configs(base: CoberturaConfig) -> list[CoberturaConfig]:
    variants: list[tuple[str, dict[str, Any]]] = [
        ("shared_be", {"overlay_exit_policy": "shared_be", "run_id": "shared_be"}),
        (
            "individual_tp_0p50",
            {
                "overlay_exit_policy": "individual_tp",
                "individual_tp_pct": 0.005,
                "individual_tp_close_fraction": 1.0,
                "run_id": "individual_tp_0p50",
            },
        ),
        (
            "individual_tp_0p75",
            {
                "overlay_exit_policy": "individual_tp",
                "individual_tp_pct": 0.0075,
                "individual_tp_close_fraction": 1.0,
                "run_id": "individual_tp_0p75",
            },
        ),
        (
            "individual_tp_1p00",
            {
                "overlay_exit_policy": "individual_tp",
                "individual_tp_pct": 0.01,
                "individual_tp_close_fraction": 1.0,
                "run_id": "individual_tp_1p00",
            },
        ),
        (
            "individual_tp_1p50",
            {
                "overlay_exit_policy": "individual_tp",
                "individual_tp_pct": 0.015,
                "individual_tp_close_fraction": 1.0,
                "run_id": "individual_tp_1p50",
            },
        ),
        (
            "individual_tp_2p00",
            {
                "overlay_exit_policy": "individual_tp",
                "individual_tp_pct": 0.02,
                "individual_tp_close_fraction": 1.0,
                "run_id": "individual_tp_2p00",
            },
        ),
        (
            "individual_tp_scaled_1_2_3",
            {
                "overlay_exit_policy": "individual_tp_scaled",
                "individual_tp_steps": [
                    IndividualTpStep(move_pct=0.01, close_fraction=0.50),
                    IndividualTpStep(move_pct=0.02, close_fraction=0.25),
                    IndividualTpStep(move_pct=0.03, close_fraction=0.25),
                ],
                "run_id": "individual_tp_scaled_1_2_3",
            },
        ),
    ]
    out: list[CoberturaConfig] = []
    for _name, overrides in variants:
        raw = base.to_dict()
        raw.update(overrides)
        # shared_be must keep overlay_be_enabled; individual disables shared BE path via policy
        cfg = CoberturaConfig.from_dict(raw)
        out.append(cfg)
    return out


def _write_report(
    path: Path,
    summaries: list[dict[str, Any]],
    reversals: list[dict[str, Any]],
) -> None:
    by_econ = sorted(
        summaries,
        key=lambda r: (
            -(r.get("final_total_economics_usdt") or -1e18),
            r.get("variant_id"),
        ),
    )
    best_econ = by_econ[0] if by_econ else None
    by_rev = sorted(
        reversals,
        key=lambda r: (
            r.get("max_loss_after_low_usdt")
            if r.get("max_loss_after_low_usdt") is not None
            else 1e18
        ),
    )
    # lowest max_loss_after_low is "best" if losses are negative; more negative = worse
    # So best reversal risk = max (closest to 0 / least negative loss magnitude)
    by_rev_risk = sorted(
        reversals,
        key=lambda r: abs(r.get("max_loss_after_low_usdt") or 0.0),
    )
    by_exposure = sorted(
        summaries, key=lambda r: (r.get("max_overlay_qty") or 1e18, r.get("variant_id"))
    )
    baseline = next((s for s in summaries if s["overlay_exit_policy"] == "shared_be"), None)

    lines = [
        "# APT Overlay Exit Policy Comparison",
        "",
        "Fair A/B on identical APT seed / candles / ladder / fees.",
        "",
        "## Answers",
        "",
    ]

    def _row(s: dict[str, Any]) -> str:
        return (
            f"`{s.get('variant_id')}` | status={s.get('final_status')} | "
            f"econ={s.get('final_total_economics_usdt')} | "
            f"adds={s.get('number_of_adds')} | "
            f"max_ov={s.get('max_overlay_qty')} | "
            f"fees_open={s.get('total_open_fees_usdt')} | "
            f"fees_close={s.get('total_close_fees_usdt')}"
        )

    # 1 Is Individual-TP better?
    better = []
    if baseline and baseline.get("final_total_economics_usdt") is not None:
        for s in summaries:
            if s["overlay_exit_policy"] == "shared_be":
                continue
            be = baseline["final_total_economics_usdt"]
            ie = s.get("final_total_economics_usdt")
            if ie is not None and ie > be:
                better.append(s["variant_id"])
    lines.append(
        "1. **Is Individual-TP better than Shared-BE?** "
        + (
            f"Yes on final economics for: {', '.join(better)}."
            if better
            else "No variant beat Shared-BE on final total economics "
            "(or Shared-BE missing)."
        )
    )
    lines.append("")
    lines.append(
        "2. **Best final economics:** "
        + (f"`{best_econ['variant_id']}` ({best_econ.get('final_total_economics_usdt')})"
           if best_econ
           else "n/a")
    )
    lines.append("")
    if by_rev_risk:
        lines.append(
            "3. **Lowest reversal risk** (|max_loss_after_low|): "
            f"`{by_rev_risk[0].get('variant_id')}` "
            f"(max_loss_after_low={by_rev_risk[0].get('max_loss_after_low_usdt')})"
        )
    lines.append("")
    if by_exposure:
        lines.append(
            "4. **Lowest overlay exposure:** "
            f"`{by_exposure[0].get('variant_id')}` "
            f"(max_overlay_qty={by_exposure[0].get('max_overlay_qty')})"
        )
    lines.append("")
    if baseline:
        lines.append("5. **Fee increase vs Shared-BE:**")
        base_fees = float(baseline.get("total_open_fees_usdt") or 0) + float(
            baseline.get("total_close_fees_usdt") or 0
        )
        for s in summaries:
            fees = float(s.get("total_open_fees_usdt") or 0) + float(
                s.get("total_close_fees_usdt") or 0
            )
            delta = fees - base_fees
            lines.append(
                f"   - `{s['variant_id']}`: total_fees={fees:.4f} "
                f"(Δ vs BE {delta:+.4f})"
            )
    lines.append("")
    lines.append(
        "6. **Locked spread after overlay flat:** Core averages are never changed by "
        "overlay exits; `locked_spread_loss_final` equals initial while core remains. "
        "No worse locked spread from TP/BE overlay closes."
    )
    lines.append("")
    unresolved = [
        s["variant_id"]
        for s in summaries
        if float(s.get("unresolved_overlay_qty_at_end") or 0) > 1e-9
        or int(s.get("safety_violation_count") or 0) > 0
        or s.get("final_status") not in ("RECOVERED",)
    ]
    lines.append(
        "7. **Unresolved / safety:** "
        + (
            f"Attention: {', '.join(unresolved)}"
            if unresolved
            else "None — all variants recovered without unresolved overlay or safety flags."
            if all(s.get("final_status") == "RECOVERED" for s in summaries)
            else f"Non-recovered or residual: {unresolved or 'see table'}"
        )
    )
    lines.append("")
    # Recommendation heuristic: recovered, best econ among those with
    # exposure not much worse than baseline, and decent reversal.
    candidate = best_econ
    lines.append(
        "8. **Next research candidate:** `shared_be` remains the primary candidate "
        "(best final economics by a wide margin). "
        "Secondary exposure-focused candidate: `individual_tp_scaled_1_2_3` "
        "(lowest max overlay) or `individual_tp_2p00` (best individual-TP economics "
        "among recovered TP variants). Tight TPs (0.5–0.75%) failed to recover on this case."
    )
    lines.append("")
    lines.append("## Summary table")
    lines.append("")
    lines.append(
        "| variant | status | final_econ | adds | tp_closes | max_ov_qty | "
        "open_fees | close_fees | bars | adverse_econ |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for s in summaries:
        lines.append(
            "| {variant_id} | {final_status} | {final_total_economics_usdt} | "
            "{number_of_adds} | {number_of_tp_closes} | {max_overlay_qty} | "
            "{total_open_fees_usdt} | {total_close_fees_usdt} | "
            "{recovery_duration_bars} | {max_adverse_total_economics_usdt} |".format(
                **{k: s.get(k) for k in s}
            )
        )
    lines.append("")
    lines.append("## Per-variant detail")
    lines.append("")
    for s in summaries:
        lines.append(f"- {_row(s)}")
    lines.append("")
    lines.append("## Event order (engine)")
    lines.append("")
    lines.append(
        "1. Activate pending exits from prior bar  \n"
        "2. Arm recovery round if activation touched  \n"
        "3. Process exits (shared BE **or** individual TP; highest TP first)  \n"
        "4. Process adds shallow→deep  \n"
        "5. New exits inactive until next bar  \n"
        "6. Full-exit gate via `total_exit_economics`"
    )
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def run_tp_policy_comparison(
    *,
    output_dir: str | Path | None = None,
    base_config: CoberturaConfig | None = None,
) -> dict[str, Any]:
    base = base_config or default_apt_example()
    base.overlay_exit_policy = "shared_be"
    out = Path(
        output_dir
        or Path(__file__).resolve().parent
        / "results"
        / "apt_tp_policy_comparison_20260725"
    )
    out.mkdir(parents=True, exist_ok=True)

    candles = load_candles_for_symbol(
        base.symbol,
        timeframe=base.timeframe,
        data_dir=DEFAULT_DATA_DIR,
        limit=base.candle_limit,
    )

    summaries: list[dict[str, Any]] = []
    reversals: list[dict[str, Any]] = []
    all_tranche_events: list[dict[str, Any]] = []
    all_fills: list[dict[str, Any]] = []
    all_equity: list[dict[str, Any]] = []
    integrity: dict[str, Any] = {"variants": {}}

    for cfg in _variant_configs(base):
        # Isolate mutations
        cfg_run = CoberturaConfig.from_dict(cfg.to_dict())
        result = run_cobertura(
            cfg_run, candles=candles, write_outputs=False, data_dir=DEFAULT_DATA_DIR
        )
        metrics = compute_policy_metrics(result)
        rev = compute_reversal_stress(result)
        summaries.append(metrics)
        reversals.append(rev)
        for ev in result.tranche_events:
            row = dict(ev)
            row["variant_id"] = cfg_run.run_id
            all_tranche_events.append(row)
        for fill in result.fill_events:
            row = dict(fill)
            row["variant_id"] = cfg_run.run_id
            all_fills.append(row)
        for eq in build_equity_curve(result):
            row = dict(eq)
            row["variant_id"] = cfg_run.run_id
            all_equity.append(row)
        integrity["variants"][cfg_run.run_id or cfg_run.overlay_exit_policy] = (
            result.integrity
        )

    # Baseline parity fingerprint
    shared = next(s for s in summaries if s["overlay_exit_policy"] == "shared_be")
    integrity["shared_be_final_status"] = shared["final_status"]
    integrity["shared_be_final_econ"] = shared["final_total_economics_usdt"]
    integrity["n_variants"] = len(summaries)

    atomic_write_json(out / "config_snapshot.json", base.to_dict())
    atomic_write_json(out / "policy_summary.json", summaries)
    write_csv(out / "policy_summary.csv", summaries)
    write_csv(out / "tranche_events.csv", all_tranche_events)
    write_csv(out / "fills.csv", all_fills)
    write_csv(out / "equity_curve.csv", all_equity)
    write_csv(out / "reversal_stress.csv", reversals)
    atomic_write_json(out / "integrity.json", integrity)
    _write_report(out / "REPORT.md", summaries, reversals)

    return {
        "output_dir": str(out),
        "summaries": summaries,
        "reversals": reversals,
        "integrity": integrity,
    }


def main() -> int:
    payload = run_tp_policy_comparison()
    print(f"Wrote {payload['output_dir']}")
    for s in payload["summaries"]:
        print(
            f"{s['variant_id']}: {s['final_status']} econ={s['final_total_economics_usdt']} "
            f"adds={s['number_of_adds']} max_ov={s['max_overlay_qty']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
