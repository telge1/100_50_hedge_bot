"""Write subgroup validation artifacts + REPORT."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for k in row:
            if k not in seen:
                seen.add(k)
                fields.append(k)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow(row)


def write_report(
    out_dir: Path,
    *,
    primary: str,
    counts: list[dict[str, Any]],
    classifications: list[dict[str, Any]],
    distance_rows: list[dict[str, Any]],
    early_rows: list[dict[str, Any]],
    jackknife_rows: list[dict[str, Any]],
    transfer_rows: list[dict[str, Any]],
    strongest: dict[str, Any] | None,
) -> str:
    lines = [
        "# CH Break/Reclaim Subgroup Validation",
        "",
        f"**Primary Decision:** `{primary}`",
        "",
        "Research only. No live gate. No productive thresholds.",
        "",
        "## Subgroup counts (DATA_VALID, EXCLUDED dropped)",
        "",
        "| subgroup | n | BREAK | RECLAIM_FAST | RECLAIM_SLOW | HOLD | ok vs RF | ok vs rest |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for c in counts:
        lines.append(
            f"| {c['subgroup']} | {c['n_events']} | {c['BREAK_ACCEPTED']} | {c['RECLAIM_FAST']} | "
            f"{c['RECLAIM_SLOW']} | {c['HOLD']} | {c['sufficient_vs_reclaim_fast']} | {c['sufficient_vs_rest']} |"
        )
    lines += ["", "## Per-subgroup classification", ""]
    for c in classifications:
        lines.append(
            f"- **{c['subgroup']}**: `{c['classification']}` — {c.get('reason')} "
            f"(best early={c.get('best_early_feature')}@{c.get('best_early_timepoint')} "
            f"AUC={c.get('best_early_auc')})"
        )
    lines += ["", "## Distance baseline (highlights)", ""]
    if strongest:
        sg = strongest.get("subgroup")
        lines.append(f"Strongest subgroup focus: `{sg}`")
        for tp in ("PRE_TOUCH_30S", "PRE_TOUCH_10S", "FIRST_TOUCH"):
            for feat in ("distance_only", "ob_only", "ob_plus_distance", "signed_distance_beyond_bps_univariate"):
                hit = [
                    r
                    for r in distance_rows
                    if r["subgroup"] == sg
                    and r["timepoint"] == tp
                    and r["feature"] == feat
                    and r["comparison"] == "vs_reclaim_fast"
                ]
                if hit:
                    r = hit[0]
                    lines.append(
                        f"- {tp} `{feat}`: AUC={r.get('auc')} CI=[{r.get('auc_ci_low')},{r.get('auc_ci_high')}] "
                        f"n={r.get('n_break')}/{r.get('n_other')}"
                    )
    lines += ["", "## Jackknife (early candidates)", ""]
    jk_focus = [
        r
        for r in jackknife_rows
        if r.get("full_auc") is not None and r["comparison"] == "vs_reclaim_fast"
    ]
    jk_focus = sorted(jk_focus, key=lambda r: r.get("full_auc") or 0, reverse=True)[:12]
    for r in jk_focus:
        lines.append(
            f"- {r['subgroup']} {r['feature']}@{r['timepoint']}: full={r.get('full_auc')} "
            f"loo_min={r.get('loo_auc_min')} max_drop={r.get('max_drop')}"
        )
    lines += ["", "## Symbol transfer (bearish scorecard)", ""]
    for r in transfer_rows:
        if r.get("direction") != "bearish" or r.get("feature") != "score_depth_imb_flow":
            continue
        if r.get("comparison") != "vs_reclaim_fast":
            continue
        if r.get("timepoint") not in {"PRE_TOUCH_30S", "PRE_TOUCH_10S", "FIRST_TOUCH"}:
            continue
        lines.append(
            f"- train {r['train_symbol']} → test {r['test_symbol']} @{r['timepoint']}: "
            f"train_auc={r.get('train_auc')} test_auc={r.get('test_auc_fixed_orientation')} "
            f"consistent={r.get('direction_consistent')} status={r.get('status')}"
        )
    lines += [
        "",
        "## Practical hedge-bot reading",
        "",
        "EARLY_GATE_CANDIDATE requires signal ≤ FIRST_TOUCH/+10s, OB beyond distance, "
        "stable jackknife, no lookahead.",
        "",
        f"Artifacts: `{out_dir}`",
        "",
    ]
    text = "\n".join(lines) + "\n"
    (out_dir / "REPORT.md").write_text(text, encoding="utf-8")
    return text


def write_summary(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
