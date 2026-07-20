"""Phase D.1 aggregation, Phase-E gates, markdown report, selected traces."""

from __future__ import annotations

import csv
import json
import math
import statistics
from pathlib import Path
from typing import Any, Sequence

from .config import EmergencyLockRecoveryConfig
from .phase_d1_runner import DEFAULT_PHASE_D1_OUTPUT_DIR, run_phase_d1

VARIANT_ORDER = (
    "full_lock_control",
    "rebound_baseline",
    "swing_break_with_ema_existing",
    "micro_unlock_10",
    "micro_unlock_10_10",
    "micro_unlock_10_15",
)


def _write_csv(path: Path, rows: Sequence[dict[str, Any]], fields: Sequence[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    cols = list(fields) if fields is not None else sorted({k for r in rows for k in r.keys()})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k) for k in cols})


def _median(xs: list[float]) -> float | None:
    return float(statistics.median(xs)) if xs else None


def _mean(xs: list[float]) -> float | None:
    return float(statistics.fmean(xs)) if xs else None


def _pct(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    ordered = sorted(xs)
    if len(ordered) == 1:
        return float(ordered[0])
    idx = (len(ordered) - 1) * p
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return float(ordered[lo])
    w = idx - lo
    return float(ordered[lo] * (1 - w) + ordered[hi] * w)


def aggregate_variant(rows: Sequence[dict[str, Any]], variant: str) -> dict[str, Any]:
    filtered = [r for r in rows if r.get("variant") == variant]
    n = len(filtered)
    if n == 0:
        return {"variant": variant, "event_count": 0}

    incr = [
        float(r["incremental_final_pnl_vs_full_lock"])
        for r in filtered
        if r.get("incremental_final_pnl_vs_full_lock") is not None
    ]
    added = [
        float(r["max_added_loss_after_lock"])
        for r in filtered
        if r.get("max_added_loss_after_lock") is not None
    ]
    fees = [float(r["total_fees"]) for r in filtered if r.get("total_fees") is not None]
    frozen = [
        float(r["frozen_deficit_usdt"])
        for r in filtered
        if r.get("frozen_deficit_usdt") is not None
    ]
    if not frozen:
        frozen = [
            abs(float(r["basket_pnl_at_lock"]))
            for r in filtered
            if r.get("basket_pnl_at_lock") is not None
        ]
    better = sum(1 for r in filtered if r.get("better_than_full_lock") is True)
    worse = sum(1 for r in filtered if r.get("worse_than_full_lock") is True)
    equal = n - better - worse
    be = sum(1 for r in filtered if r.get("break_even_reached") is True)
    oracle_possible = sum(1 for r in filtered if r.get("oracle_break_even_possible") is True)
    oracle_cap = sum(1 for r in filtered if r.get("oracle_captured") is True)
    unlock_trig = sum(1 for r in filtered if int(r.get("unlock_count") or 0) > 0)
    stage2 = sum(1 for r in filtered if int(r.get("stage_2_unlock_count") or 0) > 0)
    relock = sum(1 for r in filtered if int(r.get("relock_count") or 0) > 0)
    s1_be = sum(1 for r in filtered if r.get("stage_1_break_even_confirmed") is True)
    bars_s2 = [
        float(r["bars_to_stage_2"])
        for r in filtered
        if r.get("bars_to_stage_2") is not None
    ]
    bars_rl = [
        float(r["bars_to_relock"])
        for r in filtered
        if r.get("bars_to_relock") is not None
    ]
    typical_frozen = _median(frozen) or 0.0
    over_frozen = sum(
        1
        for r in filtered
        if r.get("max_added_loss_after_lock") is not None
        and float(r["max_added_loss_after_lock"]) > typical_frozen + 1e-12
    )
    multi_esc = sum(
        1
        for r in filtered
        if int(r.get("stage_2_unlock_count") or 0) > 0
        and int(r.get("relock_count") or 0) > 0
    )

    return {
        "variant": variant,
        "event_count": n,
        "break_even_count": be,
        "break_even_rate": be / n,
        "better_than_full_lock_count": better,
        "worse_than_full_lock_count": worse,
        "equal_to_full_lock_count": equal,
        "better_rate": better / n,
        "worse_rate": worse / n,
        "equal_rate": equal / n,
        "median_incremental_pnl": _median(incr),
        "mean_incremental_pnl": _mean(incr),
        "median_added_loss": _median(added),
        "p90_added_loss": _pct(added, 0.90),
        "worst_max_added_loss": max(added) if added else None,
        "median_fees": _median(fees),
        "total_fees": sum(fees) if fees else 0.0,
        "oracle_possible_event_count": oracle_possible,
        "oracle_capture_count": oracle_cap,
        "oracle_capture_rate": (oracle_cap / oracle_possible) if oracle_possible else None,
        "unlock_trigger_rate": unlock_trig / n,
        "stage_2_rate": stage2 / n,
        "relock_rate": relock / n,
        "stage_1_be_confirm_rate": s1_be / n,
        "median_bars_to_stage_2": _median(bars_s2),
        "median_bars_to_relock": _median(bars_rl),
        "typical_frozen_deficit": typical_frozen,
        "events_added_loss_over_frozen": over_frozen,
        "events_with_multi_escalation": multi_esc,
    }


def _stage2_vs_stage1_gate(
    rows: Sequence[dict[str, Any]],
    *,
    with_stage2: str,
    baseline_10: str,
) -> tuple[bool, str]:
    """Stage-2 variant must not raise added loss vs micro_10 without improving incr PnL."""
    a = {r["event_id"]: r for r in rows if r.get("variant") == with_stage2}
    b = {r["event_id"]: r for r in rows if r.get("variant") == baseline_10}
    violations = []
    for eid, ra in a.items():
        rb = b.get(eid)
        if rb is None:
            continue
        if int(ra.get("stage_2_unlock_count") or 0) <= 0:
            continue
        add_a = ra.get("max_added_loss_after_lock")
        add_b = rb.get("max_added_loss_after_lock")
        incr_a = ra.get("incremental_final_pnl_vs_full_lock")
        incr_b = rb.get("incremental_final_pnl_vs_full_lock")
        if add_a is None or add_b is None or incr_a is None or incr_b is None:
            continue
        if float(add_a) > float(add_b) + 1e-12 and float(incr_a) <= float(incr_b) + 1e-12:
            violations.append(eid)
    if violations:
        return False, f"stage2_added_loss_without_pnl_gain:{','.join(violations[:5])}"
    return True, ""


def decide_phase_e_gates(
    aggregates: Sequence[dict[str, Any]],
    rows: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    by = {r["variant"]: r for r in aggregates}
    rebound = by.get("rebound_baseline")
    decisions: list[dict[str, Any]] = []
    for variant in VARIANT_ORDER:
        agg = by.get(variant)
        if agg is None:
            continue
        if variant in {
            "full_lock_control",
            "rebound_baseline",
            "swing_break_with_ema_existing",
        }:
            decisions.append(
                {
                    "variant": variant,
                    "phase_e_candidate": False,
                    "passes_better_vs_worse": None,
                    "passes_median_incremental_pnl": None,
                    "passes_median_added_loss": None,
                    "passes_p90_added_loss": None,
                    "passes_worst_vs_frozen": None,
                    "passes_oracle_capture": None,
                    "passes_stage2_vs_micro10": None,
                    "passed_gates": "",
                    "failed_gates": "control_variant_excluded",
                }
            )
            continue

        reasons_fail: list[str] = []
        reasons_pass: list[str] = []

        p1 = (agg.get("better_rate") or 0) > (agg.get("worse_rate") or 0)
        (reasons_pass if p1 else reasons_fail).append("better_rate_gt_worse_rate")
        p2 = (agg.get("median_incremental_pnl") or -1e18) > 0
        (reasons_pass if p2 else reasons_fail).append("median_incremental_pnl_gt_0")

        p3 = p4 = p5 = p6 = True
        if rebound is not None:
            p3 = (agg.get("median_added_loss") or 1e18) <= (
                rebound.get("median_added_loss") or 0
            ) + 1e-12
            (reasons_pass if p3 else reasons_fail).append(
                "median_added_loss_le_rebound"
            )
            p4 = (agg.get("p90_added_loss") or 1e18) <= (
                rebound.get("p90_added_loss") or 0
            ) + 1e-12
            (reasons_pass if p4 else reasons_fail).append("p90_added_loss_le_rebound")
            p6 = int(agg.get("oracle_capture_count") or 0) >= int(
                rebound.get("oracle_capture_count") or 0
            )
            (reasons_pass if p6 else reasons_fail).append(
                "oracle_capture_ge_rebound"
            )

        frozen = agg.get("typical_frozen_deficit") or 0.0
        p5 = (agg.get("worst_max_added_loss") or 1e18) <= float(frozen) + 1e-12
        (reasons_pass if p5 else reasons_fail).append("worst_added_loss_le_frozen")

        p7 = True
        fail7 = ""
        if variant in {"micro_unlock_10_10", "micro_unlock_10_15"}:
            p7, fail7 = _stage2_vs_stage1_gate(
                rows, with_stage2=variant, baseline_10="micro_unlock_10"
            )
            if p7:
                reasons_pass.append("stage2_vs_micro10")
            else:
                reasons_fail.append(fail7 or "stage2_vs_micro10")

        passes = all([p1, p2, p3, p4, p5, p6, p7])
        decisions.append(
            {
                "variant": variant,
                "phase_e_candidate": passes,
                "passes_better_vs_worse": p1,
                "passes_median_incremental_pnl": p2,
                "passes_median_added_loss": p3,
                "passes_p90_added_loss": p4,
                "passes_worst_vs_frozen": p5,
                "passes_oracle_capture": p6,
                "passes_stage2_vs_micro10": p7 if variant.startswith("micro_unlock_10_") else None,
                "passed_gates": ";".join(reasons_pass),
                "failed_gates": ";".join(reasons_fail),
            }
        )
    return decisions


def _comparison_rows(aggregates: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    by = {r["variant"]: r for r in aggregates}
    fl = by.get("full_lock_control", {})
    rb = by.get("rebound_baseline", {})
    sw = by.get("swing_break_with_ema_existing", {})
    out = []
    for variant in VARIANT_ORDER:
        a = by.get(variant)
        if not a:
            continue
        out.append(
            {
                "variant": variant,
                "median_incremental_pnl": a.get("median_incremental_pnl"),
                "better_rate": a.get("better_rate"),
                "worse_rate": a.get("worse_rate"),
                "median_added_loss": a.get("median_added_loss"),
                "p90_added_loss": a.get("p90_added_loss"),
                "worst_max_added_loss": a.get("worst_max_added_loss"),
                "oracle_capture_count": a.get("oracle_capture_count"),
                "break_even_rate": a.get("break_even_rate"),
                "vs_full_lock_median_incr": a.get("median_incremental_pnl"),
                "vs_rebound_median_added_loss_delta": (
                    None
                    if a.get("median_added_loss") is None
                    or rb.get("median_added_loss") is None
                    else float(a["median_added_loss"]) - float(rb["median_added_loss"])
                ),
                "vs_swing_ema_median_added_loss_delta": (
                    None
                    if a.get("median_added_loss") is None
                    or sw.get("median_added_loss") is None
                    else float(a["median_added_loss"]) - float(sw["median_added_loss"])
                ),
                "full_lock_total_fees": fl.get("total_fees"),
                "variant_total_fees": a.get("total_fees"),
            }
        )
    return out


def _write_markdown(
    path: Path,
    aggregates: Sequence[dict[str, Any]],
    decisions: Sequence[dict[str, Any]],
    rows: Sequence[dict[str, Any]],
) -> None:
    by = {r["variant"]: r for r in aggregates}
    dec = {d["variant"]: d for d in decisions}
    candidates = [d for d in decisions if d.get("phase_e_candidate")]
    lines = [
        "# Phase D.1 – Defensive Micro Unlock",
        "",
        "## Verdict",
        "",
    ]
    if candidates:
        lines.append(
            "Phase-E-Kandidaten: "
            + ", ".join(d["variant"] for d in candidates)
        )
    else:
        lines.extend(
            [
                "> **Kein Phase-E-Kandidat. Full Lock bleibt Emergency-Default. "
                "Automatisches Unlock wird nicht weiter optimiert.**",
            ]
        )
    lines.extend(["", "## Kennzahlen je Variante", ""])
    lines.append(
        "| Variant | BE% | Better% | Worse% | Med ΔPnL | Med Add | p90 Add | Worst Add | Oracle | Relock% | Stage2% | S1-BE% |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for v in VARIANT_ORDER:
        a = by.get(v)
        if not a:
            continue
        lines.append(
            "| {v} | {be:.1f} | {bt:.1f} | {ws:.1f} | {mi} | {ma} | {p90} | {w} | {oc} | {rl:.1f} | {s2:.1f} | {s1:.1f} |".format(
                v=v,
                be=100 * float(a.get("break_even_rate") or 0),
                bt=100 * float(a.get("better_rate") or 0),
                ws=100 * float(a.get("worse_rate") or 0),
                mi=("n/a" if a.get("median_incremental_pnl") is None else f"{float(a['median_incremental_pnl']):.3f}"),
                ma=("n/a" if a.get("median_added_loss") is None else f"{float(a['median_added_loss']):.3f}"),
                p90=("n/a" if a.get("p90_added_loss") is None else f"{float(a['p90_added_loss']):.3f}"),
                w=("n/a" if a.get("worst_max_added_loss") is None else f"{float(a['worst_max_added_loss']):.3f}"),
                oc=a.get("oracle_capture_count"),
                rl=100 * float(a.get("relock_rate") or 0),
                s2=100 * float(a.get("stage_2_rate") or 0),
                s1=100 * float(a.get("stage_1_be_confirm_rate") or 0),
            )
        )

    # Q&A
    m10 = by.get("micro_unlock_10", {})
    m1010 = by.get("micro_unlock_10_10", {})
    m1015 = by.get("micro_unlock_10_15", {})
    sw = by.get("swing_break_with_ema_existing", {})
    fl = by.get("full_lock_control", {})
    rb = by.get("rebound_baseline", {})

    def _f(a: dict, k: str) -> float:
        return float(a.get(k) or 0)

    lines.extend(
        [
            "",
            "## Forschungsfragen",
            "",
            f"1. **Reduziert Micro Unlock die Verlust-Tails vs. Struktur-Unlock?** "
            f"p90 AddLoss micro_10={_f(m10,'p90_added_loss'):.3f} vs swing_ema={_f(sw,'p90_added_loss'):.3f}; "
            f"worst micro_10={_f(m10,'worst_max_added_loss'):.3f} vs swing_ema={_f(sw,'worst_max_added_loss'):.3f}.",
            f"2. **Ist 10% allein besser als 10%+Stage2?** "
            f"med ΔPnL 10={_f(m10,'median_incremental_pnl'):.3f}, 10_10={_f(m1010,'median_incremental_pnl'):.3f}, "
            f"10_15={_f(m1015,'median_incremental_pnl'):.3f}; "
            f"med Add 10={_f(m10,'median_added_loss'):.3f}, 10_10={_f(m1010,'median_added_loss'):.3f}, "
            f"10_15={_f(m1015,'median_added_loss'):.3f}.",
            f"3. **Hilft Stage 2 oder erhöht sie Added Losses?** "
            f"Stage2-Rate 10_10={100*_f(m1010,'stage_2_rate'):.1f}%, 10_15={100*_f(m1015,'stage_2_rate'):.1f}%. "
            f"Siehe Gate `stage2_vs_micro10` in `phase_d1_gate_results.csv`.",
            f"4. **Oracle-Capture:** rebound={rb.get('oracle_capture_count')}, "
            f"swing_ema={sw.get('oracle_capture_count')}, "
            f"micro_10={m10.get('oracle_capture_count')}, "
            f"10_10={m1010.get('oracle_capture_count')}, "
            f"10_15={m1015.get('oracle_capture_count')} (von {rb.get('oracle_possible_event_count')} möglich).",
            f"5. **Re-Lock nach Stage 1:** Relock-Rate micro_10={100*_f(m10,'relock_rate'):.1f}%, "
            f"median Bars bis Relock={m10.get('median_bars_to_relock')}.",
            f"6. **Stage-1 Fee-BE bestätigt:** Rate micro_10={100*_f(m10,'stage_1_be_confirm_rate'):.1f}%.",
            f"7. **Besser als Full Lock (Better-Rate):** "
            + ", ".join(
                f"{v}={100*_f(by.get(v,{}),'better_rate'):.1f}%"
                for v in VARIANT_ORDER
                if v != "full_lock_control" and v in by
            ),
            f"8. **Phase-E-Gates erfüllt?** {'Ja: ' + ', '.join(c['variant'] for c in candidates) if candidates else 'Nein — keine Variante.'}",
            f"9. **Empfohlener Default:** Full Lock "
            f"(med PnL≈{_f(fl,'median_incremental_pnl') + float(fl.get('typical_frozen_deficit') or 0):.3f} frozen geometry; "
            f"AddLoss≈0).",
            "",
            "## Gate-Details",
            "",
        ]
    )
    for v in ("micro_unlock_10", "micro_unlock_10_10", "micro_unlock_10_15"):
        d = dec.get(v, {})
        lines.append(
            f"- `{v}`: candidate={d.get('phase_e_candidate')}; "
            f"failed=`{d.get('failed_gates')}`"
        )

    # Notable events
    lines.extend(["", "## Auffällige Events", ""])
    micros = [r for r in rows if str(r.get("variant", "")).startswith("micro_unlock")]
    if micros:
        worst = max(
            micros,
            key=lambda r: float(r.get("max_added_loss_after_lock") or 0),
        )
        best = max(
            micros,
            key=lambda r: float(r.get("incremental_final_pnl_vs_full_lock") or -1e18),
        )
        lines.append(
            f"- Worst added loss: `{worst.get('event_id')}` / `{worst.get('variant')}` "
            f"add={worst.get('max_added_loss_after_lock')} incr={worst.get('incremental_final_pnl_vs_full_lock')}"
        )
        lines.append(
            f"- Best incr vs FL: `{best.get('event_id')}` / `{best.get('variant')}` "
            f"incr={best.get('incremental_final_pnl_vs_full_lock')}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _export_traces(payload: dict[str, Any], root: Path) -> None:
    rows = [
        r
        for r in payload["per_event_rows"]
        if str(r.get("variant", "")).startswith("micro_unlock")
    ]
    results = payload["results"]

    def pick(items: list[dict[str, Any]], key, reverse: bool, k: int = 2) -> list[tuple[str, str]]:
        ordered = sorted(items, key=key, reverse=reverse)
        out: list[tuple[str, str]] = []
        for r in ordered:
            pair = (str(r["event_id"]), str(r["variant"]))
            if pair not in out:
                out.append(pair)
            if len(out) >= k:
                break
        return out

    selections = {
        "best_vs_full_lock": pick(
            [r for r in rows if r.get("incremental_final_pnl_vs_full_lock") is not None],
            key=lambda r: float(r["incremental_final_pnl_vs_full_lock"]),
            reverse=True,
        ),
        "worst_added_loss": pick(
            [r for r in rows if r.get("max_added_loss_after_lock") is not None],
            key=lambda r: float(r["max_added_loss_after_lock"]),
            reverse=True,
        ),
        "stage2_examples": pick(
            [r for r in rows if int(r.get("stage_2_unlock_count") or 0) > 0],
            key=lambda r: float(r.get("incremental_final_pnl_vs_full_lock") or 0),
            reverse=True,
        ),
        "relock_examples": pick(
            [r for r in rows if int(r.get("relock_count") or 0) > 0],
            key=lambda r: int(r.get("relock_count") or 0),
            reverse=True,
        ),
        "successful_break_even": pick(
            [r for r in rows if r.get("break_even_reached")],
            key=lambda r: float(r.get("final_net_pnl") or 0),
            reverse=True,
        ),
    }
    for folder, pairs in selections.items():
        dest = root / folder
        dest.mkdir(parents=True, exist_ok=True)
        for event_id, variant in pairs:
            result = results.get((event_id, variant))
            if result is None:
                continue
            sub = dest / f"{event_id}__{variant}"
            sub.mkdir(parents=True, exist_ok=True)
            with (sub / "summary.json").open("w", encoding="utf-8") as handle:
                json.dump(result.get("summary", result), handle, indent=2, sort_keys=True, default=str)
                handle.write("\n")
            if result.get("transitions"):
                _write_csv(sub / "transitions.csv", result["transitions"])
            if result.get("actions"):
                _write_csv(sub / "actions.csv", result["actions"])
            # Dense diagnostics only for selected traces
            if result.get("diagnostics"):
                _write_csv(sub / "diagnostics.csv", result["diagnostics"])
    with (root / "selected.json").open("w", encoding="utf-8") as handle:
        json.dump(selections, handle, indent=2, sort_keys=True)
        handle.write("\n")


def write_phase_d1_outputs(
    payload: dict[str, Any],
    output_dir: str | Path = DEFAULT_PHASE_D1_OUTPUT_DIR,
) -> dict[str, Path]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    rows = payload["per_event_rows"]
    _write_csv(out / "phase_d1_per_event.csv", rows)

    agg_rows = [aggregate_variant(rows, v) for v in VARIANT_ORDER]
    _write_csv(out / "phase_d1_summary.csv", agg_rows)

    decisions = decide_phase_e_gates(agg_rows, rows)
    _write_csv(out / "phase_d1_gate_results.csv", decisions)

    _write_csv(out / "phase_d1_comparison_vs_controls.csv", _comparison_rows(agg_rows))
    _write_csv(out / "phase_d1_stage_transitions.csv", payload.get("transitions") or [])

    _write_markdown(out / "phase_d1_report.md", agg_rows, decisions, rows)
    _export_traces(payload, out / "selected_traces")

    manifest = {
        "event_count": len(payload["events"]),
        "variants": list(VARIANT_ORDER),
        "micro_configs": payload.get("micro_configs"),
        "phase_e_candidates": [d for d in decisions if d.get("phase_e_candidate")],
        "note": (
            "Controls reuse Phase-D runner (common_pct). "
            "Micro variants use dedicated D.1 policy engine. "
            "max_unlock_attempts_after_relock=1."
        ),
    }
    with (out / "manifest.json").open("w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")

    return {
        "phase_d1_summary_csv": out / "phase_d1_summary.csv",
        "phase_d1_per_event_csv": out / "phase_d1_per_event.csv",
        "phase_d1_gate_results_csv": out / "phase_d1_gate_results.csv",
        "phase_d1_report_md": out / "phase_d1_report.md",
        "manifest_json": out / "manifest.json",
        "output_dir": out,
    }


def run_phase_d1_to_disk(
    *,
    output_dir: str | Path = DEFAULT_PHASE_D1_OUTPUT_DIR,
    cfg: EmergencyLockRecoveryConfig | None = None,
) -> dict[str, Any]:
    payload = run_phase_d1(cfg=cfg)
    paths = write_phase_d1_outputs(payload, output_dir=output_dir)
    payload["output_paths"] = {k: str(v) for k, v in paths.items()}
    return payload
