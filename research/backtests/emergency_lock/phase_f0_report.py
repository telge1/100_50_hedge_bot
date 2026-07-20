"""Phase F0 aggregation, correlations, candidate gates, and markdown report."""

from __future__ import annotations

import csv
import json
import math
import statistics
from pathlib import Path
from typing import Any, Sequence

from .config import EmergencyLockRecoveryConfig
from .phase_f0_runner import DEFAULT_PHASE_F0_OUTPUT_DIR, run_phase_f0
from .phase_f0_speed import PhaseF0Config


def _write_csv(
    path: Path, rows: Sequence[dict[str, Any]], fields: Sequence[str] | None = None
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    cols = list(fields) if fields is not None else sorted({k for r in rows for k in r})
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


def spearman_rank(xs: list[float], ys: list[float]) -> float | None:
    """Deterministic Spearman rho without SciPy (average ranks for ties)."""
    n = len(xs)
    if n < 3 or n != len(ys):
        return None

    def ranks(vals: list[float]) -> list[float]:
        order = sorted(range(n), key=lambda i: vals[i])
        out = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                out[order[k]] = avg
            i = j + 1
        return out

    rx = ranks(xs)
    ry = ranks(ys)
    mx = sum(rx) / n
    my = sum(ry) / n
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    denx = math.sqrt(sum((rx[i] - mx) ** 2 for i in range(n)))
    deny = math.sqrt(sum((ry[i] - my) ** 2 for i in range(n)))
    if denx <= 1e-15 or deny <= 1e-15:
        return None
    return float(num / (denx * deny))


def _finite(v: Any) -> bool:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    return f == f and abs(f) != float("inf")


def aggregate_group(
    rows: Sequence[dict[str, Any]],
    *,
    group_name: str,
    group_value: str,
    min_sample: int,
    rebound_key: str = "max_rebound_pct",
    drop_key: str = "max_further_drop_pct",
    pnl_key: str = "net_attempt_pnl",
    winner_key: str = "winner",
) -> dict[str, Any]:
    n = len(rows)
    insufficient = n < int(min_sample)
    rebounds = [float(r[rebound_key]) for r in rows if _finite(r.get(rebound_key))]
    drops = [float(r[drop_key]) for r in rows if _finite(r.get(drop_key))]
    pnls = [float(r[pnl_key]) for r in rows if _finite(r.get(pnl_key))]
    winners = [str(r.get(winner_key)) for r in rows if r.get(winner_key)]
    tp = sum(1 for w in winners if w == "tp")
    stop = sum(1 for w in winners if w == "stop")
    neither = sum(1 for w in winners if w == "neither")
    decided = tp + stop + neither
    bars_tp = [
        float(r["bars_to_touch"])
        for r in rows
        if r.get(winner_key) == "tp" and _finite(r.get("bars_to_touch"))
    ]
    bars_stop = [
        float(r["bars_to_touch"])
        for r in rows
        if r.get(winner_key) == "stop" and _finite(r.get("bars_to_touch"))
    ]
    added = [
        float(r["max_added_loss_vs_full_lock"])
        for r in rows
        if _finite(r.get("max_added_loss_vs_full_lock"))
    ]
    wins = sum(1 for p in pnls if p > 1e-12)
    losses = sum(1 for p in pnls if p < -1e-12)
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    pf = (gross_win / gross_loss) if gross_loss > 1e-15 else (None if gross_win <= 0 else float("inf"))

    return {
        "group_name": group_name,
        "group_value": group_value,
        "sample_count": n,
        "insufficient_sample": insufficient,
        "median_forward_rebound": _median(rebounds),
        "mean_forward_rebound": _mean(rebounds),
        "p25_forward_rebound": _pct(rebounds, 0.25),
        "p50_forward_rebound": _pct(rebounds, 0.50),
        "p75_forward_rebound": _pct(rebounds, 0.75),
        "p90_forward_rebound": _pct(rebounds, 0.90),
        "median_further_drop": _median(drops),
        "p90_further_drop": _pct(drops, 0.90),
        "tp_before_stop_rate": (tp / decided) if decided else None,
        "stop_before_tp_rate": (stop / decided) if decided else None,
        "neither_rate": (neither / decided) if decided else None,
        "median_bars_to_tp": _median(bars_tp),
        "median_bars_to_stop": _median(bars_stop),
        "median_net_attempt_pnl": _median(pnls),
        "mean_net_attempt_pnl": _mean(pnls),
        "win_rate": (wins / len(pnls)) if pnls else None,
        "loss_rate": (losses / len(pnls)) if pnls else None,
        "profit_factor": pf,
        "median_added_loss": _median(added),
        "p90_added_loss": _pct(added, 0.90),
        "worst_added_loss": max(added) if added else None,
        "trigger_count": sum(1 for r in rows if r.get("started") is True or r.get("completed") is True),
        "completed_count": sum(1 for r in rows if r.get("completed") is True),
    }


def summarize_variants(
    attempts: Sequence[dict[str, Any]], *, min_sample: int
) -> list[dict[str, Any]]:
    names = sorted({str(r.get("variant")) for r in attempts if r.get("variant")})
    out = []
    for name in names:
        rows = [r for r in attempts if r.get("variant") == name]
        started = [r for r in rows if r.get("started") is True]
        completed = [r for r in started if r.get("completed") is True]
        # For races on completed attempts, map winner
        agg = aggregate_group(
            completed,
            group_name="variant",
            group_value=name,
            min_sample=min_sample,
        )
        agg["started_count"] = len(started)
        agg["skipped_count"] = len(rows) - len(started)
        # Event balance
        by_event: dict[str, float] = {}
        for r in completed:
            if not _finite(r.get("net_attempt_pnl")):
                continue
            eid = str(r.get("event_id"))
            by_event[eid] = by_event.get(eid, 0.0) + float(r["net_attempt_pnl"])
        pos_events = sum(1 for v in by_event.values() if v > 1e-12)
        neg_events = sum(1 for v in by_event.values() if v < -1e-12)
        total_pos = sum(v for v in by_event.values() if v > 0)
        max_pos = max((v for v in by_event.values() if v > 0), default=0.0)
        agg["positive_events"] = pos_events
        agg["negative_events"] = neg_events
        agg["max_event_share_of_positive_pnl"] = (
            (max_pos / total_pos) if total_pos > 1e-15 else None
        )
        agg["median_incremental_pnl_vs_full_lock"] = _median(
            [
                float(r["incremental_pnl_vs_full_lock"])
                for r in completed
                if _finite(r.get("incremental_pnl_vs_full_lock"))
            ]
        )
        out.append(agg)
    return out


def decide_candidates(
    variant_summary: Sequence[dict[str, Any]],
    *,
    baseline_variant: str = "R0_unfiltered",
    min_sample: int = 5,
) -> list[dict[str, Any]]:
    by = {r["group_value"]: r for r in variant_summary}
    baseline = by.get(baseline_variant)
    decisions = []
    for name, agg in sorted(by.items()):
        if not str(name).startswith(("R4_", "R5_")):
            decisions.append(
                {
                    "variant": name,
                    "phase_f1_candidate": False,
                    "failed_gates": "not_a_speed_filter_variant"
                    if name != baseline_variant
                    else "baseline_excluded",
                    "passed_gates": "",
                }
            )
            continue
        if baseline is None:
            decisions.append(
                {
                    "variant": name,
                    "phase_f1_candidate": False,
                    "failed_gates": "missing_R0_baseline",
                    "passed_gates": "",
                }
            )
            continue
        fails: list[str] = []
        passes: list[str] = []
        n = int(agg.get("completed_count") or agg.get("sample_count") or 0)
        if n < min_sample:
            fails.append("trigger_count_lt_5")
        else:
            passes.append("trigger_count_ge_5")

        checks = [
            (
                "median_net_gt_R0",
                (agg.get("median_net_attempt_pnl") or -1e18)
                > (baseline.get("median_net_attempt_pnl") or 0),
            ),
            (
                "mean_net_gt_R0",
                (agg.get("mean_net_attempt_pnl") or -1e18)
                > (baseline.get("mean_net_attempt_pnl") or 0),
            ),
            (
                "win_rate_gt_R0",
                (agg.get("win_rate") or -1)
                > (baseline.get("win_rate") or 0),
            ),
            (
                "median_added_le_R0",
                (agg.get("median_added_loss") or 1e18)
                <= (baseline.get("median_added_loss") or 0) + 1e-12,
            ),
            (
                "p90_added_le_R0",
                (agg.get("p90_added_loss") or 1e18)
                <= (baseline.get("p90_added_loss") or 0) + 1e-12,
            ),
            (
                "worst_added_le_R0",
                (agg.get("worst_added_loss") or 1e18)
                <= (baseline.get("worst_added_loss") or 0) + 1e-12,
            ),
            (
                "pos_events_ge_neg",
                int(agg.get("positive_events") or 0)
                >= int(agg.get("negative_events") or 0),
            ),
            (
                "no_single_event_gt_50pct_pos",
                (agg.get("max_event_share_of_positive_pnl") or 1.0) <= 0.50 + 1e-12,
            ),
            (
                "median_incr_vs_fl_gt_0",
                (agg.get("median_incremental_pnl_vs_full_lock") or -1e18) > 0,
            ),
        ]
        for label, ok in checks:
            (passes if ok else fails).append(label)

        decisions.append(
            {
                "variant": name,
                "phase_f1_candidate": len(fails) == 0,
                "passed_gates": ";".join(passes),
                "failed_gates": ";".join(fails),
            }
        )
    return decisions


def correlation_table(
    legs: Sequence[dict[str, Any]],
    forwards: Sequence[dict[str, Any]],
    attempts: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    # Join legs to horizon=48 forward at to_level
    fo48 = [
        r
        for r in forwards
        if int(r.get("horizon_bars") or 0) == 48 and r.get("horizon_complete")
    ]
    fo_by = {(r.get("event_id"), float(r.get("level_pct"))): r for r in fo48}
    r0 = [
        r
        for r in attempts
        if r.get("variant") == "R0_unfiltered" and r.get("completed")
    ]
    r0_by = {(r.get("event_id"), float(r.get("level_pct"))): r for r in r0}

    predictors = [
        "bars_for_leg",
        "slowdown_ratio",
        "speed_pct_per_hour",
        "close_path_efficiency",
        "tr_path_efficiency",
        "max_intermediate_rebound_pct",
        "atr_mean_pct",
    ]
    outcomes = [
        ("forward_mfe", "mfe_pct", fo_by),
        ("forward_mae", "mae_pct", fo_by),
        ("close_return", "close_return_pct", fo_by),
        ("r0_net_pnl", "net_attempt_pnl", r0_by),
    ]
    rows = []
    for pred in predictors:
        for out_name, out_key, mapping in outcomes:
            xs: list[float] = []
            ys: list[float] = []
            for leg in legs:
                key = (leg.get("event_id"), float(leg.get("to_level_pct")))
                other = mapping.get(key)
                if other is None:
                    continue
                if not _finite(leg.get(pred)) or not _finite(other.get(out_key)):
                    continue
                xs.append(float(leg[pred]))
                ys.append(float(other[out_key]))
            # Binary TP outcome for R0
            rho = spearman_rank(xs, ys)
            rows.append(
                {
                    "predictor": pred,
                    "outcome": out_name,
                    "sample_count": len(xs),
                    "spearman_rho": rho,
                }
            )
        # TP-before-stop binary
        xs = []
        ys = []
        for leg in legs:
            key = (leg.get("event_id"), float(leg.get("to_level_pct")))
            other = r0_by.get(key)
            if other is None or not _finite(leg.get(pred)):
                continue
            if other.get("winner") not in {"tp", "stop"}:
                continue
            xs.append(float(leg[pred]))
            ys.append(1.0 if other.get("winner") == "tp" else 0.0)
        rows.append(
            {
                "predictor": pred,
                "outcome": "r0_tp_before_stop",
                "sample_count": len(xs),
                "spearman_rho": spearman_rank(xs, ys),
            }
        )
    return rows


SLOWDOWN_ORDER = [
    "<0.50_stark_beschleunigt",
    "0.50-0.80_beschleunigt",
    "0.80-1.25_aehnlich",
    "1.25-2.00_verlangsamt",
    ">2.00_stark_verlangsamt",
]


def _is_monotonic(values: list[float | None], *, increasing: bool) -> bool | None:
    clean = [v for v in values if v is not None]
    if len(clean) < 3:
        return None
    if increasing:
        return all(clean[i] <= clean[i + 1] + 1e-12 for i in range(len(clean) - 1))
    return all(clean[i] >= clean[i + 1] - 1e-12 for i in range(len(clean) - 1))


def monotonicity_table(
    speed_bucket_summary: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    by = {r["group_value"]: r for r in speed_bucket_summary}
    ordered = [by.get(b) for b in SLOWDOWN_ORDER]
    present = [r for r in ordered if r is not None]
    if len(present) < 3:
        return [
            {
                "metric": "insufficient_buckets",
                "monotonic_increasing": None,
                "monotonic_decreasing": None,
                "bucket_count": len(present),
            }
        ]
    rebound = [r.get("median_forward_rebound") for r in ordered]
    tp = [r.get("tp_before_stop_rate") for r in ordered]
    pnl = [r.get("median_net_attempt_pnl") for r in ordered]
    drop = [r.get("median_further_drop") for r in ordered]
    return [
        {
            "metric": "median_forward_rebound",
            "monotonic_increasing": _is_monotonic(rebound, increasing=True),
            "monotonic_decreasing": _is_monotonic(rebound, increasing=False),
            "values": ";".join("" if v is None else f"{v:.6f}" for v in rebound),
        },
        {
            "metric": "tp_before_stop_rate",
            "monotonic_increasing": _is_monotonic(tp, increasing=True),
            "monotonic_decreasing": _is_monotonic(tp, increasing=False),
            "values": ";".join("" if v is None else f"{v:.6f}" for v in tp),
        },
        {
            "metric": "median_net_attempt_pnl",
            "monotonic_increasing": _is_monotonic(pnl, increasing=True),
            "monotonic_decreasing": _is_monotonic(pnl, increasing=False),
            "values": ";".join("" if v is None else f"{v:.6f}" for v in pnl),
        },
        {
            "metric": "median_further_drop",
            "monotonic_increasing": _is_monotonic(drop, increasing=True),
            "monotonic_decreasing": _is_monotonic(drop, increasing=False),
            "values": ";".join("" if v is None else f"{v:.6f}" for v in drop),
        },
    ]


def _join_leg_forward_attempts(
    legs: Sequence[dict[str, Any]],
    forwards: Sequence[dict[str, Any]],
    attempts: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    fo = {
        (r.get("event_id"), float(r.get("level_pct"))): r
        for r in forwards
        if int(r.get("horizon_bars") or 0) == 48 and r.get("horizon_complete")
    }
    r0 = {
        (r.get("event_id"), float(r.get("level_pct"))): r
        for r in attempts
        if r.get("variant") == "R0_unfiltered"
    }
    joined = []
    for leg in legs:
        key = (leg.get("event_id"), float(leg.get("to_level_pct")))
        f = fo.get(key, {})
        a = r0.get(key, {})
        joined.append(
            {
                **leg,
                "max_rebound_pct": f.get("max_rebound_pct"),
                "max_further_drop_pct": f.get("max_further_drop_pct"),
                "mfe_pct": f.get("mfe_pct"),
                "mae_pct": f.get("mae_pct"),
                "winner": a.get("winner"),
                "bars_to_touch": a.get("bars_to_touch"),
                "net_attempt_pnl": a.get("net_attempt_pnl"),
                "max_added_loss_vs_full_lock": a.get("max_added_loss_vs_full_lock"),
                "started": a.get("started"),
                "completed": a.get("completed"),
            }
        )
    return joined


def _export_traces(payload: dict[str, Any], root: Path) -> None:
    legs = payload["legs"]
    attempts = [r for r in payload["recovery_attempts"] if r.get("completed")]
    root.mkdir(parents=True, exist_ok=True)
    selections: dict[str, list[dict[str, Any]]] = {
        "fastest_leg": sorted(
            [lg for lg in legs if _finite(lg.get("minutes_for_leg"))],
            key=lambda r: float(r["minutes_for_leg"]),
        )[:2],
        "slowest_leg": sorted(
            [lg for lg in legs if _finite(lg.get("minutes_for_leg"))],
            key=lambda r: float(r["minutes_for_leg"]),
            reverse=True,
        )[:2],
        "strongest_acceleration": sorted(
            [
                lg
                for lg in legs
                if _finite(lg.get("slowdown_ratio")) and float(lg["slowdown_ratio"]) > 0
            ],
            key=lambda r: float(r["slowdown_ratio"]),
        )[:2],
        "strongest_slowdown": sorted(
            [lg for lg in legs if _finite(lg.get("slowdown_ratio"))],
            key=lambda r: float(r["slowdown_ratio"]),
            reverse=True,
        )[:2],
        "highest_path_efficiency": sorted(
            [lg for lg in legs if _finite(lg.get("close_path_efficiency"))],
            key=lambda r: float(r["close_path_efficiency"]),
            reverse=True,
        )[:2],
        "lowest_path_efficiency": sorted(
            [lg for lg in legs if _finite(lg.get("close_path_efficiency"))],
            key=lambda r: float(r["close_path_efficiency"]),
        )[:2],
        "best_recovery": sorted(
            [a for a in attempts if _finite(a.get("net_attempt_pnl"))],
            key=lambda r: float(r["net_attempt_pnl"]),
            reverse=True,
        )[:2],
        "worst_recovery": sorted(
            [a for a in attempts if _finite(a.get("net_attempt_pnl"))],
            key=lambda r: float(r["net_attempt_pnl"]),
        )[:2],
        "same_bar_collision": [
            a for a in attempts if a.get("same_bar_collision")
        ][:3],
    }
    for folder, rows in selections.items():
        dest = root / folder
        dest.mkdir(parents=True, exist_ok=True)
        if rows:
            _write_csv(dest / "rows.csv", rows)
    with (root / "selected.json").open("w", encoding="utf-8") as handle:
        json.dump(
            {k: [{kk: rr.get(kk) for kk in ("event_id", "to_level_pct", "variant", "level_pct", "minutes_for_leg", "slowdown_ratio", "net_attempt_pnl") if kk in rr} for rr in v] for k, v in selections.items()},
            handle,
            indent=2,
            sort_keys=True,
        )
        handle.write("\n")


def _write_report(
    path: Path,
    *,
    payload: dict[str, Any],
    speed_sum: Sequence[dict[str, Any]],
    dur_sum: Sequence[dict[str, Any]],
    path_sum: Sequence[dict[str, Any]],
    variant_sum: Sequence[dict[str, Any]],
    corr: Sequence[dict[str, Any]],
    mono: Sequence[dict[str, Any]],
    decisions: Sequence[dict[str, Any]],
) -> None:
    candidates = [d for d in decisions if d.get("phase_f1_candidate")]
    legs = payload["legs"]
    mins = [float(lg["minutes_for_leg"]) for lg in legs if _finite(lg.get("minutes_for_leg"))]
    by_var = {r["group_value"]: r for r in variant_sum}
    r0 = by_var.get("R0_unfiltered", {})

    if candidates:
        verdict = (
            "Ergebnis 3: Vorab definierter Speed-/Rebound-Filter verbessert Netto-PnL "
            "und Tail-Risk gegenüber ungefiltertem Versuch. Kandidat für eine separate "
            "Policy-Phase, noch keine Runtime-Freigabe. Varianten: "
            + ", ".join(d["variant"] for d in candidates)
        )
    elif len(legs) < 15 or all(
        (r.get("insufficient_sample") for r in speed_sum if r.get("sample_count", 0) > 0)
    ):
        verdict = (
            "Ergebnis 2: Diagnostischer Zusammenhang vorhanden oder unsicher, aber "
            "Sample/Stabilität unzureichend. Erst auf mehr Events/Coins validieren."
        )
    else:
        # Check if any monotonic rebound improvement with slowdown
        mono_reb = next((m for m in mono if m.get("metric") == "median_forward_rebound"), {})
        if mono_reb.get("monotonic_increasing") or (
            r0.get("median_net_attempt_pnl") is not None
            and float(r0.get("median_net_attempt_pnl") or 0) > 0
        ):
            verdict = (
                "Ergebnis 2: Einzelne diagnostische Muster sichtbar, aber keine "
                "vordefinierten Kandidaten-Gates erfüllt. Sample und Stabilität "
                "reichen nicht für eine Policy-Phase."
            )
        else:
            verdict = (
                "Ergebnis 1: Kein belastbarer Zusammenhang zwischen 2%-Leg-Geschwindigkeit "
                "und Forward-Outcomes. Kein neuer Policy-Kandidat."
            )

    lines = [
        "# Phase F0 – 2%-Leg Speed & Forward Outcome Audit",
        "",
        "## Entscheidung",
        "",
        f"> **{verdict}**",
        "",
        f"- Events: {len(payload['events'])}",
        f"- Legs: {len(legs)}",
        f"- Median Leg-Dauer: {_median(mins)} Minuten",
        f"- All-History: {payload.get('all_history_implemented')} "
        f"({payload.get('all_history_skip_reason')})",
        "",
        "## A. Geschwindigkeit",
        "",
        f"- Fastest leg: {min(mins) if mins else None} min",
        f"- Slowest leg: {max(mins) if mins else None} min",
        "",
        "### Slowdown-Buckets (mit R0 Forward h=48)",
        "",
        "| Bucket | n | insuff | med rebound | med drop | TP-rate | med net PnL |",
        "|---|---:|:---:|---:|---:|---:|---:|",
    ]
    for r in speed_sum:
        lines.append(
            f"| {r['group_value']} | {r['sample_count']} | {r['insufficient_sample']} | "
            f"{r.get('median_forward_rebound')} | {r.get('median_further_drop')} | "
            f"{r.get('tp_before_stop_rate')} | {r.get('median_net_attempt_pnl')} |"
        )
    lines.extend(["", "### Duration-Buckets", ""])
    lines.append("| Bucket | n | med rebound | med drop | med net |")
    lines.append("|---|---:|---:|---:|---:|")
    for r in dur_sum:
        lines.append(
            f"| {r['group_value']} | {r['sample_count']} | {r.get('median_forward_rebound')} | "
            f"{r.get('median_further_drop')} | {r.get('median_net_attempt_pnl')} |"
        )

    lines.extend(
        [
            "",
            "## B. Vorhersagekraft",
            "",
            "### Spearman (Legs ↔ Forward h=48 / R0)",
            "",
        ]
    )
    for r in corr:
        if r.get("sample_count", 0) >= 5:
            lines.append(
                f"- {r['predictor']} vs {r['outcome']}: rho={r.get('spearman_rho')} (n={r['sample_count']})"
            )
    lines.extend(["", "### Monotonie über Slowdown-Buckets", ""])
    for m in mono:
        lines.append(
            f"- {m.get('metric')}: increasing={m.get('monotonic_increasing')} "
            f"decreasing={m.get('monotonic_decreasing')}"
        )

    lines.extend(["", "## C. Recovery-Varianten", ""])
    lines.append(
        "| Variant | started | completed | med net | mean net | win% | med add | p90 add | worst add |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for name in sorted(by_var):
        r = by_var[name]
        if not (
            name.startswith("R0")
            or name.startswith("R1")
            or name.startswith("R2")
            or name.startswith("R3")
            or name.startswith("R4_slowdown")
            or name.startswith("R5_")
        ):
            continue
        lines.append(
            f"| {name} | {r.get('started_count')} | {r.get('completed_count')} | "
            f"{r.get('median_net_attempt_pnl')} | {r.get('mean_net_attempt_pnl')} | "
            f"{r.get('win_rate')} | {r.get('median_added_loss')} | "
            f"{r.get('p90_added_loss')} | {r.get('worst_added_loss')} |"
        )

    lines.extend(
        [
            "",
            "## D. Robustheit",
            "",
            "- Prefix-Parität: siehe Unit-Tests.",
            "- Lookahead: Level-/Filterentscheidungen nutzen nur Bars bis Touch/Entry.",
            "- PnL: nur Netto-Long-Move zwischen Unlock und Relock; alter Short-Gewinn ausgeschlossen.",
            "",
            "## E. Kandidaten-Gates (R4/R5 vs R0)",
            "",
        ]
    )
    for d in decisions:
        if str(d["variant"]).startswith(("R4_", "R5_")):
            lines.append(
                f"- `{d['variant']}`: candidate={d['phase_f1_candidate']}; "
                f"failed=`{d.get('failed_gates')}`"
            )

    lines.extend(
        [
            "",
            "## Einschränkungen",
            "",
            "- Nur APTUSDT 5m, 14 Emergency-Lock-Events.",
            "- All-History-Audit nicht umgesetzt.",
            "- Kleine Bucket-Samples → `insufficient_sample` markiert.",
            "- Kein Optimizer, keine Runtime-Änderung.",
            "",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_phase_f0_outputs(
    payload: dict[str, Any],
    output_dir: str | Path = DEFAULT_PHASE_F0_OUTPUT_DIR,
) -> dict[str, Path]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    f0: PhaseF0Config = payload["f0_config"]
    min_n = int(f0.minimum_group_sample_size)

    _write_csv(out / "phase_f0_level_crossings.csv", payload["crossings"])
    # also dump close-mode separately appended with touch_mode already set
    _write_csv(
        out / "phase_f0_level_crossings_close_below.csv",
        payload["crossings_close"],
    )
    _write_csv(out / "phase_f0_leg_metrics.csv", payload["legs"])
    _write_csv(out / "phase_f0_forward_outcomes.csv", payload["forward_outcomes"])
    _write_csv(out / "phase_f0_first_touch_outcomes.csv", payload["first_touch"])
    _write_csv(out / "phase_f0_recovery_attempts.csv", payload["recovery_attempts"])
    _write_csv(out / "phase_f0_per_event_summary.csv", payload["per_event_rows"])

    joined = _join_leg_forward_attempts(
        payload["legs"], payload["forward_outcomes"], payload["recovery_attempts"]
    )

    speed_sum = []
    for bucket in SLOWDOWN_ORDER:
        rows = [r for r in joined if r.get("slowdown_bucket") == bucket]
        speed_sum.append(
            aggregate_group(
                rows, group_name="slowdown_bucket", group_value=bucket, min_sample=min_n
            )
        )
    _write_csv(out / "phase_f0_speed_bucket_summary.csv", speed_sum)

    dur_vals = sorted({r.get("duration_bucket") for r in joined if r.get("duration_bucket")})
    dur_sum = [
        aggregate_group(
            [r for r in joined if r.get("duration_bucket") == b],
            group_name="duration_bucket",
            group_value=str(b),
            min_sample=min_n,
        )
        for b in dur_vals
    ]
    _write_csv(out / "phase_f0_duration_bucket_summary.csv", dur_sum)

    pe_vals = sorted(
        {r.get("path_efficiency_bucket") for r in joined if r.get("path_efficiency_bucket")}
    )
    path_sum = [
        aggregate_group(
            [r for r in joined if r.get("path_efficiency_bucket") == b],
            group_name="path_efficiency_bucket",
            group_value=str(b),
            min_sample=min_n,
        )
        for b in pe_vals
    ]
    _write_csv(out / "phase_f0_path_efficiency_summary.csv", path_sum)

    variant_sum = summarize_variants(payload["recovery_attempts"], min_sample=min_n)
    _write_csv(out / "phase_f0_variant_summary.csv", variant_sum)

    corr = correlation_table(
        payload["legs"], payload["forward_outcomes"], payload["recovery_attempts"]
    )
    _write_csv(out / "phase_f0_correlations.csv", corr)

    mono = monotonicity_table(speed_sum)
    _write_csv(out / "phase_f0_monotonicity.csv", mono)

    decisions = decide_candidates(variant_sum, min_sample=min_n)
    _write_csv(out / "phase_f0_candidate_gates.csv", decisions)

    _write_report(
        out / "phase_f0_report.md",
        payload=payload,
        speed_sum=speed_sum,
        dur_sum=dur_sum,
        path_sum=path_sum,
        variant_sum=variant_sum,
        corr=corr,
        mono=mono,
        decisions=decisions,
    )
    _export_traces(payload, out / "selected_traces")

    manifest = {
        "event_count": len(payload["events"]),
        "leg_count": len(payload["legs"]),
        "crossing_count": len(payload["crossings"]),
        "f0_config": f0.as_dict(),
        "phase_f1_candidates": [d for d in decisions if d.get("phase_f1_candidate")],
        "all_history_implemented": payload.get("all_history_implemented"),
        "all_history_skip_reason": payload.get("all_history_skip_reason"),
        "same_bar_collision_policy": f0.same_bar_collision_policy,
        "reference": "short_avg_after_lock",
        "primary_touch_mode": "first_low_touch",
    }
    with (out / "manifest.json").open("w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")

    return {
        "phase_f0_report_md": out / "phase_f0_report.md",
        "manifest_json": out / "manifest.json",
        "output_dir": out,
    }


def run_phase_f0_to_disk(
    *,
    output_dir: str | Path = DEFAULT_PHASE_F0_OUTPUT_DIR,
    cfg: EmergencyLockRecoveryConfig | None = None,
    f0_cfg: PhaseF0Config | None = None,
) -> dict[str, Any]:
    payload = run_phase_f0(cfg=cfg, f0_cfg=f0_cfg)
    paths = write_phase_f0_outputs(payload, output_dir=output_dir)
    payload["output_paths"] = {k: str(v) for k, v in paths.items()}
    return payload
