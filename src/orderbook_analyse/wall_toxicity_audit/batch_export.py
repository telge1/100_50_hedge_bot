"""Export batch wall toxicity / outcome artefacts."""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

from orderbook_analyse.wall_toxicity_audit.types import OutcomeParams


def _write_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for k in row:
            if k not in seen:
                seen.add(k)
                fields.append(k)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields or ["_empty"])
        w.writeheader()
        for row in rows:
            w.writerow(row)


def _fmt_rate(v: Any) -> str:
    if v is None or v == "":
        return "n/a"
    return f"{100.0 * float(v):.1f}%"


def _pick_baseline(baselines: Sequence[dict[str, Any]], name: str, horizon: int) -> dict[str, Any] | None:
    for r in baselines:
        if r.get("group_value") == name and int(r.get("horizon_seconds") or 0) == horizon:
            return r
    return None


def build_markdown_report(
    *,
    summary: dict[str, Any],
    details: Sequence[dict[str, Any]],
    baselines: Sequence[dict[str, Any]],
    group_summary: Sequence[dict[str, Any]],
    outcome_params: OutcomeParams,
) -> str:
    lines: list[str] = []
    lines.append("# Wall Toxicity Batch Report")
    lines.append("")
    lines.append(f"- Symbol: `{summary.get('symbol')}`")
    lines.append(f"- Sequences analyzed: **{summary.get('n_analyzed')}**")
    lines.append(f"- Outcome-eligible: **{summary.get('n_outcome_eligible')}**")
    lines.append(f"- Errors: {summary.get('n_errors')}")
    lines.append(f"- Elapsed: {summary.get('elapsed_seconds')}s · max RSS ≈ {summary.get('maxrss_mb')} MiB")
    lines.append("")
    lines.append("## Classification frequency")
    for k, v in sorted((summary.get("classification_counts") or {}).items(), key=lambda x: -x[1]):
        lines.append(f"- `{k}`: {v}")
    lines.append("")
    lines.append("## Market proximity / touch")
    lines.append(f"- Near: {summary.get('n_near')} · Remote: {summary.get('n_remote')}")
    lines.append(f"- Touched: {summary.get('n_touched')} · Untouched: {summary.get('n_untouched')}")
    lines.append("")
    lines.append("## Data quality")
    for k, v in sorted((summary.get("data_quality_counts") or {}).items()):
        lines.append(f"- `{k}`: {v}")
    lines.append("")

    horizon = 300 if 300 in outcome_params.forward_seconds else outcome_params.forward_seconds[0]
    lines.append(f"## Baseline comparison (FROM_CLASSIFICATION, {horizon}s)")
    lines.append("")
    lines.append("| Baseline | n | Hold | Break | Accept | Failed break | Med return bps | Uncertain |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---|")
    for name in (
        "ALL_WALLS",
        "NEAR_MARKET",
        "RELIABLE",
        "TOXIC_EXCLUDED",
        "TOUCHED",
        "TOUCHED_AND_RELIABLE",
    ):
        r = _pick_baseline(baselines, name, horizon)
        if not r:
            continue
        lines.append(
            "| {name} | {n} | {hold} | {brk} | {acc} | {fail} | {ret} | {unc} |".format(
                name=name,
                n=r.get("n"),
                hold=_fmt_rate(r.get("hold_rate")),
                brk=_fmt_rate(r.get("break_rate")),
                acc=_fmt_rate(r.get("acceptance_rate")),
                fail=_fmt_rate(r.get("failed_break_rate")),
                ret=("n/a" if r.get("median_forward_return_bps") is None else f"{float(r['median_forward_return_bps']):.1f}"),
                unc="yes" if r.get("sample_uncertain") else "no",
            )
        )
    lines.append("")

    # Hold leaders among classifications
    class_rows = [
        r
        for r in group_summary
        if r.get("group_name") == "classification"
        and int(r.get("horizon_seconds") or 0) == horizon
        and (r.get("n") or 0) > 0
    ]
    by_hold = sorted(
        class_rows,
        key=lambda r: (r.get("hold_rate") is not None, r.get("hold_rate") or -1),
        reverse=True,
    )
    lines.append(f"## Classification hold/break ({horizon}s)")
    for r in by_hold[:12]:
        lines.append(
            f"- `{r.get('group_value')}`: n={r.get('n')}, hold={_fmt_rate(r.get('hold_rate'))}, "
            f"break={_fmt_rate(r.get('break_rate'))}, uncertain={r.get('sample_uncertain')}"
        )
    lines.append("")

    rel_rows = [
        r
        for r in group_summary
        if r.get("group_name") == "reliability_bin" and int(r.get("horizon_seconds") or 0) == horizon
    ]
    lines.append("## Does reliability improve hold quality?")
    if not rel_rows:
        lines.append("- Insufficient grouped data.")
    else:
        for r in sorted(rel_rows, key=lambda x: str(x.get("group_value"))):
            lines.append(
                f"- Reliability `{r.get('group_value')}`: hold={_fmt_rate(r.get('hold_rate'))}, "
                f"break={_fmt_rate(r.get('break_rate'))}, n={r.get('n')}, uncertain={r.get('sample_uncertain')}"
            )
    lines.append("")

    all_b = _pick_baseline(baselines, "ALL_WALLS", horizon)
    tox_b = _pick_baseline(baselines, "TOXIC_EXCLUDED", horizon)
    lines.append("## Does excluding toxic walls help?")
    if all_b and tox_b:
        lines.append(
            f"- ALL hold={_fmt_rate(all_b.get('hold_rate'))} vs TOXIC_EXCLUDED "
            f"hold={_fmt_rate(tox_b.get('hold_rate'))} (n={tox_b.get('n')}, uncertain={tox_b.get('sample_uncertain')})"
        )
    else:
        lines.append("- Comparison unavailable.")
    lines.append("")

    remote = next(
        (
            r
            for r in group_summary
            if r.get("group_name") == "near_remote"
            and r.get("group_value") == "REMOTE"
            and int(r.get("horizon_seconds") or 0) == horizon
        ),
        None,
    )
    near = next(
        (
            r
            for r in group_summary
            if r.get("group_name") == "near_remote"
            and r.get("group_value") == "NEAR"
            and int(r.get("horizon_seconds") or 0) == horizon
        ),
        None,
    )
    lines.append("## Are remote walls practically irrelevant?")
    if remote and near:
        lines.append(
            f"- REMOTE n={remote.get('n')}, hold={_fmt_rate(remote.get('hold_rate'))}, "
            f"break={_fmt_rate(remote.get('break_rate'))}"
        )
        lines.append(
            f"- NEAR n={near.get('n')}, hold={_fmt_rate(near.get('hold_rate'))}, "
            f"break={_fmt_rate(near.get('break_rate'))}"
        )
        lines.append(
            "- Remote walls are often untouched; hold/break rates among them should be interpreted cautiously."
        )
    else:
        lines.append("- Near/remote split unavailable.")
    lines.append("")

    uncertain_groups = [
        r for r in group_summary if r.get("sample_uncertain") and int(r.get("horizon_seconds") or 0) == horizon
    ]
    lines.append("## Uncertain results (small n)")
    lines.append(
        f"- Groups with n < {outcome_params.uncertain_sample_n} at {horizon}s: {len(uncertain_groups)}"
    )
    lines.append("- No statistical significance is claimed for small samples.")
    lines.append("")
    lines.append("## Outcome definition defaults")
    lines.append("```")
    lines.append(json.dumps(outcome_params.to_dict(), indent=2))
    lines.append("```")
    lines.append("")
    return "\n".join(lines) + "\n"


def write_batch_outputs(
    output_dir: Path,
    *,
    result: Any,
    outcome_params: OutcomeParams,
) -> None:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    _write_csv(out / "wall_toxicity_batch_details.csv", result.details)
    _write_csv(out / "wall_forward_outcomes.csv", result.outcomes)
    _write_csv(out / "wall_outcome_group_summary.csv", result.group_summary)
    _write_csv(out / "wall_baseline_comparison.csv", result.baselines)
    _write_csv(out / "wall_data_quality.csv", result.quality)
    _write_csv(out / "wall_batch_errors.csv", result.errors)

    # Compact one-row summary CSV
    s = dict(result.summary)
    s["classification_counts"] = json.dumps(s.get("classification_counts") or {})
    s["data_quality_counts"] = json.dumps(s.get("data_quality_counts") or {})
    s["toxicity_params"] = json.dumps(s.get("toxicity_params") or {})
    s["outcome_params"] = json.dumps(s.get("outcome_params") or {})
    _write_csv(out / "wall_toxicity_batch_summary.csv", [s])

    report = {
        "summary": result.summary,
        "n_details": len(result.details),
        "n_outcomes": len(result.outcomes),
        "n_errors": len(result.errors),
    }
    (out / "wall_toxicity_batch_report.json").write_text(
        json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8"
    )
    md = build_markdown_report(
        summary=result.summary,
        details=result.details,
        baselines=result.baselines,
        group_summary=result.group_summary,
        outcome_params=outcome_params,
    )
    (out / "wall_toxicity_batch_report.md").write_text(md, encoding="utf-8")
