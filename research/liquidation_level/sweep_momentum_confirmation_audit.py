"""CLI / exports for Phase E momentum confirmation audit."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research.liquidation_level.liquidation_audit import DEFAULT_FEATHER
from research.liquidation_level.sweep_momentum_confirmation import (
    DEFAULT_CANDIDATES,
    DEFAULT_FORWARD_HORIZONS,
    DEFAULT_MOMENTUM_WINDOWS,
    PHASE_D_EXPECTED_HASH,
    PhaseEValidationError,
    build_phase_e_bundle,
)


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(x) for x in obj]
    if isinstance(obj, (np.floating, float)):
        x = float(obj)
        return None if not np.isfinite(x) else x
    if isinstance(obj, (np.integer, int)):
        return int(obj)
    if isinstance(obj, (np.bool_, bool)):
        return bool(obj)
    if obj is None or (isinstance(obj, float) and np.isnan(obj)):
        return None
    return obj


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(_jsonable(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _write_csv(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)


def _parse_int_list(text: str) -> list[int]:
    return [int(x.strip()) for x in str(text).split(",") if x.strip()]


def build_summary(bundle, *, runtime_s: float, symbol: str) -> dict[str, Any]:
    conf = bundle.confirmation_results
    armed = conf.loc[conf["phase_e_state"] != "NOT_ARMED"] if len(conf) else conf
    confirmed = conf.loc[conf["cohort"] == "confirmed"] if len(conf) else conf
    rates: dict[str, Any] = {}
    ages: dict[str, Any] = {}
    armed_counts: dict[str, Any] = {}
    confirmed_counts: dict[str, Any] = {}
    if len(conf):
        for keys, g in conf.groupby(
            ["rule_family", "variant", "decision_offset", "momentum_window"], dropna=False
        ):
            rule, variant, offset, mwin = keys
            key = f"{rule}|{variant}|off{int(offset)}|M{int(mwin)}"
            a = g.loc[g["phase_e_state"] != "NOT_ARMED"]
            c = g.loc[g["cohort"] == "confirmed"]
            armed_counts[key] = int(len(a))
            confirmed_counts[key] = int(len(c))
            rates[key] = float(len(c) / len(a)) if len(a) else None
            ages[key] = (
                float(pd.to_numeric(c["confirmation_age"], errors="coerce").median())
                if len(c)
                else None
            )

    fwd_by_cand: dict[str, Any] = {}
    summ = bundle.confirmation_summary
    if len(summ):
        full = summ.loc[summ["sample"] == "full"]
        for r in full.itertuples():
            key = (
                f"{r.rule_family}|{r.variant}|off{int(r.decision_offset)}|"
                f"M{int(r.momentum_window)}|{r.confirmation_direction}"
            )
            fwd_by_cand[key] = {
                "median_dir_ret_h12": _jsonable(r.median_directional_close_return_h12),
                "median_mfe_h12": _jsonable(r.median_max_favorable_excursion_h12),
                "median_mae_h12": _jsonable(r.median_max_adverse_excursion_h12),
                "fba_rate_h12": _jsonable(r.favorable_before_adverse_rate_h12),
                "confirmation_rate": _jsonable(r.confirmation_rate),
                "confirmed_count": int(r.confirmed_count),
            }

    ready = bool(
        bundle.validation.get("ok")
        and bundle.leakage_checks.get("passed")
        and len(conf) > 0
    )
    # Recommendation required for phase_f_ready; structural readiness tracked separately.
    return {
        "symbol": symbol,
        "event_counts": bundle.validation.get("reproduced_events"),
        "candidates": bundle.config.get("candidates"),
        "momentum_windows": bundle.config.get("momentum_windows"),
        "forward_horizons": bundle.config.get("forward_horizons"),
        "armed_counts": armed_counts,
        "confirmed_counts": confirmed_counts,
        "confirmation_rates": rates,
        "median_confirmation_ages": ages,
        "forward_metrics_by_candidate": fwd_by_cand,
        "IS_OOS_stability": bundle.is_oos_comparison.to_dict(orient="records")
        if len(bundle.is_oos_comparison)
        else [],
        "monthly_stability": {
            "n_rows": int(len(bundle.monthly)),
            "n_months": int(bundle.monthly["year_month"].nunique())
            if len(bundle.monthly) and "year_month" in bundle.monthly.columns
            else 0,
        },
        "overlap_results": {
            "variants": sorted(bundle.overlap["overlap_variant"].unique().tolist())
            if len(bundle.overlap) and "overlap_variant" in bundle.overlap.columns
            else [],
            "n_rows": int(len(bundle.overlap)),
        },
        "leakage_checks_passed": bool(bundle.leakage_checks.get("passed")),
        "leakage_checks": bundle.leakage_checks,
        "deterministic_hash": bundle.deterministic_hash,
        "phase_e_ready_for_phase_f": bool(ready and bundle.recommended_candidate is not None),
        "recommended_candidate_for_phase_f": bundle.recommended_candidate,
        "phase_e_structural_ready": ready,
        "runtime_seconds": runtime_s,
        "expected_phase_d_hash": PHASE_D_EXPECTED_HASH,
        "observed_phase_d_hash": bundle.validation.get("observed_phase_d_hash"),
        "armed_event_rows": int(len(armed)),
        "confirmed_event_rows": int(len(confirmed)),
        "confirmation_row_count": int(len(conf)),
        "no_entry_pnl": True,
        "no_scanner_integration": True,
        "no_oos_grid_search": True,
    }


def write_readme(path: Path, summary: dict[str, Any]) -> None:
    rec = summary.get("recommended_candidate_for_phase_f")
    rec_txt = "keine Empfehlung (null) — Gates nicht erfüllt" if not rec else json.dumps(rec, indent=2)
    text = f"""# Phase E Results — Momentum-Bestätigung & Forward-Pfad

## Kein Entry / kein PnL

Phase E prüft nur, ob nach der Phase-D-Decision eine kausale Momentum-
Bestätigung innerhalb M2/M3 auftritt, und misst den **strikt nachgelagerten**
Forward-Pfad. Keine Order, kein TP/SL, keine Gebühren, kein PnL.

## Timing

- Decision = Close von `signal_index + decision_offset`
- Erste Momentum-Candle = Decision + 1 (= Scanner age 0)
- `break_close` wird nach age0 auf Decision-Close gesetzt
- Forward startet bei `confirming_candle_index + 1`

## Kandidaten

Primär: R2 / loose / offset 6

Vergleich: R2/loose/1, R2/loose/3, R3/loose/6, R4/loose/6, R5/loose/6

R1 ausgeschlossen.

## Phase F

recommended_candidate_for_phase_f = {rec_txt}

phase_e_ready_for_phase_f = **{summary.get('phase_e_ready_for_phase_f')}**
leakage_checks_passed = **{summary.get('leakage_checks_passed')}**
Hash: `{summary.get('deterministic_hash')}`

Keine Trading-Edge-Aussage. Keine Scanner-Integration.
"""
    path.write_text(text + "\n", encoding="utf-8")


def export_bundle(
    bundle,
    output_dir: Path,
    *,
    runtime_s: float,
    symbol: str,
) -> dict[str, Any]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    summary = build_summary(bundle, runtime_s=runtime_s, symbol=symbol)
    _write_json(out / "config.json", {**bundle.config, "symbol": symbol})
    _write_json(out / "input_validation.json", bundle.validation)
    _write_csv(out / "armed_events.csv", bundle.armed_events)
    _write_csv(out / "momentum_timelines.csv", bundle.momentum_timelines)
    _write_csv(out / "confirmation_results.csv", bundle.confirmation_results)
    _write_csv(out / "forward_path_metrics.csv", bundle.forward_path_metrics)
    _write_csv(out / "forward_targets.csv", bundle.forward_targets)
    _write_csv(out / "confirmation_summary.csv", bundle.confirmation_summary)
    _write_csv(out / "candidate_comparison.csv", bundle.candidate_comparison)
    _write_csv(out / "m2_m3_comparison.csv", bundle.m2_m3_comparison)
    _write_csv(out / "IS_OOS_comparison.csv", bundle.is_oos_comparison)
    _write_csv(out / "monthly_stability.csv", bundle.monthly)
    _write_csv(out / "overlap_comparison.csv", bundle.overlap)
    _write_csv(out / "decision_to_confirmation_latency.csv", bundle.latency)
    _write_json(out / "leakage_audit.json", bundle.leakage_checks)
    _write_csv(out / "timeline_samples.csv", bundle.timeline_samples)
    (out / "timeline_audit.md").write_text(bundle.timeline_audit_md + "\n", encoding="utf-8")
    _write_json(out / "summary.json", summary)
    write_readme(out / "README_results.md", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Phase E sweep momentum confirmation audit")
    p.add_argument(
        "--phase-a-dir",
        default="research/liquidation_level/results/APTUSDT_5m_sweep_scanner_phase_a",
    )
    p.add_argument(
        "--phase-b-dir",
        default="research/liquidation_level/results/APTUSDT_5m_sweep_scanner_phase_b",
    )
    p.add_argument(
        "--phase-c-dir",
        default="research/liquidation_level/results/APTUSDT_5m_sweep_scanner_phase_c",
    )
    p.add_argument(
        "--phase-d-dir",
        default="research/liquidation_level/results/APTUSDT_5m_sweep_scanner_phase_d",
    )
    p.add_argument(
        "--output-dir",
        default="research/liquidation_level/results/APTUSDT_5m_sweep_scanner_phase_e",
    )
    p.add_argument("--symbol", default="APTUSDT")
    p.add_argument("--feather", type=Path, default=DEFAULT_FEATHER)
    p.add_argument("--momentum-windows", default="2,3")
    p.add_argument("--forward-horizons", default="3,6,12,24,48")
    p.add_argument("--max-events", type=int, default=None)
    p.add_argument("--timeline-sample-size", type=int, default=50)
    p.add_argument("--random-seed", type=int, default=42)
    args = p.parse_args(argv)

    windows = _parse_int_list(args.momentum_windows) or list(DEFAULT_MOMENTUM_WINDOWS)
    horizons = _parse_int_list(args.forward_horizons) or list(DEFAULT_FORWARD_HORIZONS)

    t0 = time.perf_counter()
    print(f"symbol={args.symbol}")
    print("Inputs geladen / validating…")
    try:
        bundle = build_phase_e_bundle(
            phase_a_dir=Path(args.phase_a_dir),
            phase_b_dir=Path(args.phase_b_dir),
            phase_c_dir=Path(args.phase_c_dir),
            phase_d_dir=Path(args.phase_d_dir),
            feather_file=Path(args.feather),
            candidates=DEFAULT_CANDIDATES,
            momentum_windows=windows,
            forward_horizons=horizons,
            max_events=args.max_events,
            timeline_sample_size=args.timeline_sample_size,
            random_seed=args.random_seed,
            progress=lambda m: print(m),
        )
    except PhaseEValidationError as exc:
        out = Path(args.output_dir)
        out.mkdir(parents=True, exist_ok=True)
        diag = json.loads(str(exc))
        _write_json(out / "input_validation.json", diag)
        print("VALIDATION FAILED — aborted")
        print(json.dumps(diag, indent=2)[:2000])
        return 2

    runtime = time.perf_counter() - t0
    summary = export_bundle(
        bundle, Path(args.output_dir), runtime_s=runtime, symbol=args.symbol
    )
    print("Evaluationsmetriken / exports geschrieben")
    print(f"Leakage-Audit passed={summary.get('leakage_checks_passed')}")
    print(f"deterministic_hash={summary.get('deterministic_hash')}")
    print(f"phase_e_ready_for_phase_f={summary.get('phase_e_ready_for_phase_f')}")
    print(f"recommended_candidate_for_phase_f={summary.get('recommended_candidate_for_phase_f')}")
    print(f"Laufzeit={runtime:.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
