"""CLI / exports for Phase D causal path classification."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research.liquidation_level.sweep_path_classifier import (
    DEFAULT_DECISION_OFFSETS,
    DEFAULT_RULE_FAMILIES,
    DEFAULT_VARIANTS,
    PHASE_C_EXPECTED_HASH,
    PhaseDValidationError,
    build_phase_d_bundle,
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


def _parse_csv_list(text: str) -> list[str]:
    return [x.strip() for x in str(text).split(",") if x.strip()]


def _parse_int_list(text: str) -> list[int]:
    return [int(x.strip()) for x in str(text).split(",") if x.strip()]


def build_summary(bundle, *, runtime_s: float) -> dict[str, Any]:
    cls = bundle.classifications
    counts = cls["classification"].value_counts().to_dict() if len(cls) else {}
    full = bundle.classification_summary.loc[
        bundle.classification_summary["sample"] == "full"
    ]
    coverage_by_rule = {
        f"{r.rule_family}|{r.variant}|off{int(r.decision_offset)}": float(r.coverage_pct)
        for r in full.itertuples()
    }
    precision_by_rule = {
        f"{r.rule_family}|{r.variant}|off{int(r.decision_offset)}": {
            "short_vs_ended_below": _jsonable(r.short_precision_vs_ended_below),
            "bull_vs_ended_above": _jsonable(r.bull_precision_vs_ended_above),
        }
        for r in full.itertuples()
    }
    ready = bool(
        bundle.validation.get("ok")
        and bundle.leakage_checks.get("passed")
        and len(cls) > 0
        and int(bundle.snapshots["event_id"].nunique())
        == int(bundle.validation["reproduced_events"]["full"])
    )
    return {
        "event_counts": bundle.validation.get("reproduced_events"),
        "decision_offsets": bundle.config.get("decision_offsets"),
        "rule_families": bundle.config.get("rule_families"),
        "variants": bundle.config.get("variants"),
        "classification_counts": {str(k): int(v) for k, v in counts.items()},
        "coverage_by_rule": coverage_by_rule,
        "precision_by_rule": precision_by_rule,
        "IS_OOS_stability": bundle.sample_comparison.to_dict(orient="records")
        if len(bundle.sample_comparison)
        else [],
        "monthly_stability": {
            "n_rows": int(len(bundle.monthly)),
            "n_months": int(bundle.monthly["year_month"].nunique()) if len(bundle.monthly) else 0,
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
        "phase_d_ready_for_phase_e": ready,
        "recommended_rule_for_phase_e": bundle.recommended_rule,
        "runtime_seconds": runtime_s,
        "expected_phase_c_hash": PHASE_C_EXPECTED_HASH,
        "observed_phase_c_hash": bundle.validation.get("observed_phase_c_hash"),
        "snapshot_count": int(len(bundle.snapshots)),
        "classification_row_count": int(len(cls)),
        "no_entry_pnl": True,
        "no_scanner_integration": True,
    }


def write_readme(path: Path, summary: dict[str, Any]) -> None:
    rec = summary.get("recommended_rule_for_phase_e")
    rec_txt = "keine Empfehlung (null) — Gates nicht erfüllt" if not rec else json.dumps(rec)
    text = f"""# Phase D Results — Kausale Pfad-Klassifikation

## Sweep ist weiterhin kein Entry

Phase D klassifiziert nur die *sichtbare* Anschlussentwicklung nach dem
oberen 50x-Sweep. Es gibt keine Entry-Simulation, kein TP/SL, keine Gebühren
und keinen PnL.

## Was unterscheiden die Klassen?

- **SHORT_REVERSAL**: Der Sweep wird zurückgewiesen; sichtbare Daten
  sprechen überwiegend für fallende Fortsetzung.
- **BULLISH_BREAKOUT_CONTINUATION**: Preis akzeptiert oberhalb des Levels;
  sichtbare Daten sprechen für bullische Fortsetzung.
- **UNCLEAR**: widersprüchlich, schwach oder unzureichend (bewusst häufig).
- **TECHNICAL_INVALID**: nur bei fehlenden/beschädigten Pflichtdaten,
  nicht wegen „ungünstiger“ Preisbewegung.

## Timeframes

- 5m-Pfad bis Decision-Offset (1/3/6/12 Folgcandles)
- zuletzt kausal geschlossener 15m- und 30m-Zustand am Decision-Zeitpunkt
- PRE/SWEEP-Kontext aus Phase C (frozen), keine END-Features späterer Fenster

## Score-Komponenten

Getrennt und gewichtet in `config.json`:

- level_response, trend_5m, structure_5m, volatility_5m, volume_5m
- context_15m, structure_15m, context_30m, structure_30m
- blocker_score (HTF-/Akzeptanz-Blocker)

Vorzeichen: negativ = Short/Reversal-Unterstützung, positiv = Bull-Breakout.

## Warum UNCLEAR wichtig ist

Lieber keine Richtungsaussage als eine erzwungene. Coverage unter 100 % ist
ein Feature, kein Fehler.

## Stabilität / Phase E

recommended_rule_for_phase_e = {rec_txt}

phase_d_ready_for_phase_e = **{summary.get('phase_d_ready_for_phase_e')}**
leakage_checks_passed = **{summary.get('leakage_checks_passed')}**
Hash: `{summary.get('deterministic_hash')}`

Keine Trading-Edge- oder PnL-Aussage. Keine Scanner-Integration.
"""
    path.write_text(text + "\n", encoding="utf-8")


def rule_family_comparison(summary: pd.DataFrame) -> pd.DataFrame:
    full = summary.loc[summary["sample"] == "full"].copy()
    return full.sort_values(
        ["decision_offset", "rule_family", "variant"]
    ).reset_index(drop=True)


def export_bundle(bundle, output_dir: Path, *, runtime_s: float) -> dict[str, Any]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    summary = build_summary(bundle, runtime_s=runtime_s)
    _write_json(out / "config.json", bundle.config)
    _write_json(out / "input_validation.json", bundle.validation)
    _write_csv(out / "decision_snapshots.csv", bundle.snapshots)
    _write_csv(out / "classification_results.csv", bundle.classifications)
    _write_csv(out / "decision_traces.csv", bundle.traces)
    _write_csv(out / "classification_summary.csv", bundle.classification_summary)
    _write_csv(out / "confusion_summary.csv", bundle.confusion)
    _write_csv(out / "sample_comparison.csv", bundle.sample_comparison)
    _write_csv(out / "monthly_stability.csv", bundle.monthly)
    _write_csv(out / "overlap_comparison.csv", bundle.overlap)
    _write_csv(out / "rule_family_comparison.csv", rule_family_comparison(bundle.classification_summary))
    _write_csv(out / "score_distributions.csv", bundle.score_distributions)
    _write_csv(out / "feature_usage.csv", bundle.feature_usage)
    _write_json(out / "leakage_audit.json", bundle.leakage_checks)
    _write_json(out / "summary.json", summary)
    write_readme(out / "README_results.md", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Phase D causal sweep path classifier audit")
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
        "--output-dir",
        default="research/liquidation_level/results/APTUSDT_5m_sweep_scanner_phase_d",
    )
    p.add_argument("--symbol", default="APTUSDT")
    p.add_argument("--decision-offsets", default="1,3,6,12")
    p.add_argument("--rule-families", default="R1,R2,R3,R4,R5")
    p.add_argument("--variants", default="strict,medium,loose")
    p.add_argument("--max-events", type=int, default=None)
    p.add_argument("--timeline-sample-size", type=int, default=50)
    p.add_argument("--random-seed", type=int, default=42)
    args = p.parse_args(argv)

    offsets = _parse_int_list(args.decision_offsets) or list(DEFAULT_DECISION_OFFSETS)
    rules = _parse_csv_list(args.rule_families) or list(DEFAULT_RULE_FAMILIES)
    variants = _parse_csv_list(args.variants) or list(DEFAULT_VARIANTS)

    t0 = time.perf_counter()
    print(f"symbol={args.symbol}")
    print("Inputs geladen / validating…")
    try:
        bundle = build_phase_d_bundle(
            phase_a_dir=Path(args.phase_a_dir),
            phase_b_dir=Path(args.phase_b_dir),
            phase_c_dir=Path(args.phase_c_dir),
            decision_offsets=offsets,
            rule_families=rules,
            variants=variants,
            max_events=args.max_events,
            timeline_sample_size=args.timeline_sample_size,
            random_seed=args.random_seed,
            progress=lambda m: print(m),
        )
    except PhaseDValidationError as exc:
        out = Path(args.output_dir)
        out.mkdir(parents=True, exist_ok=True)
        diag = json.loads(str(exc))
        _write_json(out / "input_validation.json", diag)
        print("VALIDATION FAILED — aborted")
        print(json.dumps(diag, indent=2)[:2000])
        return 2

    runtime = time.perf_counter() - t0
    summary = export_bundle(bundle, Path(args.output_dir), runtime_s=runtime)
    print("Evaluationsmetriken / exports geschrieben")
    print(f"Leakage-Audit passed={summary.get('leakage_checks_passed')}")
    print(f"deterministic_hash={summary.get('deterministic_hash')}")
    print(f"phase_d_ready_for_phase_e={summary.get('phase_d_ready_for_phase_e')}")
    print(f"recommended_rule_for_phase_e={summary.get('recommended_rule_for_phase_e')}")
    print(f"Laufzeit={runtime:.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
