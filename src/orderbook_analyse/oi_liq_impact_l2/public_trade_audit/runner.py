"""Orchestrator for public trade impact compression audit."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from orderbook_analyse.oi_liq_impact_l2.public_trade_audit.classify import (
    classify_impact_frame,
)
from orderbook_analyse.oi_liq_impact_l2.public_trade_audit.constants import (
    ALL_CATEGORIES,
    CATEGORY_SUSTAINED_FLOW_COMPRESSION,
    FORMAT_VERSION,
    IMPACT_CATEGORY_SUMMARY_FIELDS,
    IMPACT_CLASSIFICATION_FIELDS,
    MATCHED_CONTROL_COMPARISON_FIELDS,
    NON_OVERLAPPING_ROBUSTNESS_FIELDS,
    POST_COMPRESSION_OUTCOME_FIELDS,
    VERDICT_BLOCKED,
    VERDICT_COMPLETE,
    WINDOW_COMPARISONS,
)
from orderbook_analyse.oi_liq_impact_l2.public_trade_audit.outcomes import (
    compute_post_compression_outcomes,
    enrich_classification_context,
)
from orderbook_analyse.oi_liq_impact_l2.public_trade_audit.schema import (
    SchemaCheckResult,
    check_input_schema,
    required_impact_compression_columns,
)


@dataclass(frozen=True)
class PublicTradeAuditResult:
    verdict: str
    output_dir: Path
    cluster_count: int
    blocked: bool
    missing_fields: tuple[str, ...]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _write_json(path: Path, value: object) -> None:
    _atomic_write(
        path,
        json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True) + "\n",
    )


def _write_csv(path: Path, rows: list[Mapping[str, object]], fieldnames: tuple[str, ...]) -> None:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(fieldnames), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({name: row.get(name, "") for name in fieldnames})
    _atomic_write(path, buffer.getvalue())


def _median(values: list[float]) -> float | None:
    clean = [v for v in values if v is not None]
    if not clean:
        return None
    return statistics.median(clean)


def _build_category_summary(
    classifications: list[dict[str, Any]],
    *,
    scope: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    total = len({(r["cluster_id"], r["comparison_pair"]) for r in classifications})
    for comparison_pair, _, _ in WINDOW_COMPARISONS:
        subset = [r for r in classifications if r["comparison_pair"] == comparison_pair]
        scope_rows = [r for r in subset if scope == "ALL" or r["direction"] == scope]
        denom = len(scope_rows)
        for category in ALL_CATEGORIES:
            cat_rows = [r for r in scope_rows if r["category"] == category]
            rows.append(
                {
                    "scope": scope,
                    "comparison_pair": comparison_pair,
                    "category": category,
                    "cluster_count": len(cat_rows),
                    "cluster_fraction": (len(cat_rows) / denom) if denom else 0.0,
                    "median_first_aggressive_notional": _median(
                        [r.get("first_aggressive_notional") for r in cat_rows]
                    ),
                    "median_last_aggressive_notional": _median(
                        [r.get("last_aggressive_notional") for r in cat_rows]
                    ),
                    "median_notional_ratio_last_over_first": _median(
                        [r.get("notional_ratio_last_over_first") for r in cat_rows]
                    ),
                    "median_first_impact_per_notional": _median(
                        [r.get("first_impact_per_notional") for r in cat_rows]
                    ),
                    "median_last_impact_per_notional": _median(
                        [r.get("last_impact_per_notional") for r in cat_rows]
                    ),
                    "median_impact_ratio_last_over_first": _median(
                        [r.get("impact_ratio_last_over_first") for r in cat_rows]
                    ),
                }
            )
        _ = total
    return rows


def _select_non_overlapping(
    classifications: list[dict[str, Any]],
    events: pd.DataFrame,
    *,
    max_horizon_minutes: int,
) -> set[str]:
    event_times: dict[str, tuple[pd.Timestamp, pd.Timestamp]] = {}
    for _, row in events.iterrows():
        start = pd.Timestamp(str(row["cluster_start"]))
        end = pd.Timestamp(str(row["cluster_end"])) + pd.Timedelta(minutes=max_horizon_minutes + 1)
        event_times[str(row["cluster_id"])] = (start, end)
    ordered = sorted(
        {(r["cluster_id"], r["comparison_pair"]): r for r in classifications}.values(),
        key=lambda r: str(events.loc[events["cluster_id"] == r["cluster_id"], "cluster_start"].iloc[0]),
    )
    selected: set[str] = set()
    last_end: pd.Timestamp | None = None
    for row in ordered:
        cluster_id = str(row["cluster_id"])
        if cluster_id not in event_times:
            continue
        start, end = event_times[cluster_id]
        if last_end is not None and start < last_end:
            continue
        selected.add(cluster_id)
        last_end = end
    return selected


def _build_non_overlapping_robustness(
    classifications: list[dict[str, Any]],
    events: pd.DataFrame,
    *,
    max_horizon_minutes: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    non_overlap_ids = _select_non_overlapping(
        classifications, events, max_horizon_minutes=max_horizon_minutes
    )
    for subset_name, id_filter in (
        ("full", None),
        ("non_overlapping", non_overlap_ids),
    ):
        filtered = [
            r
            for r in classifications
            if id_filter is None or str(r["cluster_id"]) in id_filter
        ]
        for scope in ("ALL", "LONG", "SHORT"):
            scope_rows = [
                r for r in filtered if scope == "ALL" or r["direction"] == scope
            ]
            denom = len(scope_rows)
            for comparison_pair, _, _ in WINDOW_COMPARISONS:
                pair_rows = [
                    r for r in scope_rows if r["comparison_pair"] == comparison_pair
                ]
                for category in ALL_CATEGORIES:
                    cat_rows = [r for r in pair_rows if r["category"] == category]
                    rows.append(
                        {
                            "subset": subset_name,
                            "scope": scope,
                            "comparison_pair": comparison_pair,
                            "category": category,
                            "cluster_count": len(cat_rows),
                            "cluster_fraction": (len(cat_rows) / denom) if denom else 0.0,
                        }
                    )
    return rows


def _build_matched_control_comparison(
    classifications: list[dict[str, Any]],
    control_impact: pd.DataFrame | None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if control_impact is None or control_impact.empty:
        for comparison_pair, _, _ in WINDOW_COMPARISONS:
            flush_n = len(
                [
                    r
                    for r in classifications
                    if r["comparison_pair"] == comparison_pair
                    and r["category"] == CATEGORY_SUSTAINED_FLOW_COMPRESSION
                ]
            )
            rows.append(
                {
                    "comparison_pair": comparison_pair,
                    "metric": "sustained_flow_compression_count",
                    "flush_value": flush_n,
                    "control_value": "",
                    "flush_n": flush_n,
                    "control_n": 0,
                    "note": (
                        "Control artifacts lack required first5/last5 and first10/last10 "
                        "aggressive-notional and impact-per-notional fields; "
                        "fair matched-control classification not available."
                    ),
                }
            )
        return rows

    control_rows = classify_impact_frame(control_impact)
    for comparison_pair, _, _ in WINDOW_COMPARISONS:
        flush_rate = _category_rate(classifications, comparison_pair)
        control_rate = _category_rate(control_rows, comparison_pair)
        rows.append(
            {
                "comparison_pair": comparison_pair,
                "metric": "sustained_flow_compression_rate",
                "flush_value": flush_rate,
                "control_value": control_rate,
                "flush_n": _category_count(classifications, comparison_pair),
                "control_n": _category_count(control_rows, comparison_pair),
                "note": "",
            }
        )
    return rows


def _category_count(rows: list[dict[str, Any]], comparison_pair: str) -> int:
    return len([r for r in rows if r["comparison_pair"] == comparison_pair])


def _category_rate(rows: list[dict[str, Any]], comparison_pair: str) -> float:
    subset = [r for r in rows if r["comparison_pair"] == comparison_pair]
    if not subset:
        return 0.0
    hits = [
        r
        for r in subset
        if r["category"] == CATEGORY_SUSTAINED_FLOW_COMPRESSION
    ]
    return len(hits) / len(subset)


def _write_blocked_outputs(
    output_dir: Path,
    *,
    input_dir: Path,
    schema: SchemaCheckResult,
    windows: tuple[int, ...],
    horizons: tuple[int, ...],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "format_version": FORMAT_VERSION,
        "verdict": VERDICT_BLOCKED,
        "input_dir": str(input_dir.resolve()),
        "output_dir": str(output_dir.resolve()),
        "windows": list(windows),
        "horizons": list(horizons),
        "required_impact_compression_columns": list(required_impact_compression_columns()),
        "present_impact_compression_columns": list(schema.present_columns),
        "missing_impact_compression_fields": list(schema.missing_fields),
        "missing_input_files": list(schema.missing_files),
        "limitations": [
            "Classification requires precomputed aggressive-notional and trades_present "
            "fields for first5/last5, first10/last10, and first_half/second_half windows.",
            "Matched-control comparison blocked when control impact metrics are absent.",
        ],
    }
    _write_json(output_dir / "audit_manifest.json", manifest)
    _write_csv(output_dir / "impact_classification.csv", [], IMPACT_CLASSIFICATION_FIELDS)
    _write_csv(output_dir / "impact_category_summary.csv", [], IMPACT_CATEGORY_SUMMARY_FIELDS)
    _write_csv(output_dir / "post_compression_outcomes.csv", [], POST_COMPRESSION_OUTCOME_FIELDS)
    _write_csv(
        output_dir / "matched_control_comparison.csv",
        _build_matched_control_comparison([], None),
        MATCHED_CONTROL_COMPARISON_FIELDS,
    )
    _write_csv(
        output_dir / "non_overlapping_robustness.csv",
        [],
        NON_OVERLAPPING_ROBUSTNESS_FIELDS,
    )
    summary = _render_summary(
        verdict=VERDICT_BLOCKED,
        cluster_count=0,
        classifications=[],
        schema=schema,
        control_note="Control impact metrics unavailable.",
    )
    _atomic_write(output_dir / "public_trade_impact_summary.md", summary)


def _render_summary(
    *,
    verdict: str,
    cluster_count: int,
    classifications: list[dict[str, Any]],
    schema: SchemaCheckResult | None,
    control_note: str,
) -> str:
    lines = [
        "# Public Trade Impact Compression Audit",
        "",
        f"Verdict: `{verdict}`",
        "",
    ]
    if verdict == VERDICT_BLOCKED and schema is not None:
        lines.extend(
            [
                "## Blocked",
                "",
                "Required precomputed notional and trades_present fields are missing "
                "from `impact_compression_metrics.csv`.",
                "",
                "### Missing fields",
                "",
            ]
        )
        for field in schema.missing_fields:
            lines.append(f"- `{field}`")
        if schema.missing_files:
            lines.extend(["", "### Missing input files", ""])
            for name in schema.missing_files:
                lines.append(f"- `{name}`")
        lines.extend(
            [
                "",
                "## Limitations",
                "",
                f"- {control_note}",
                "",
            ]
        )
        return "\n".join(lines) + "\n"

    lines.extend(
        [
            f"Clusters audited: **{cluster_count}**",
            "",
            "## Category counts (first5_last5, ALL)",
            "",
        ]
    )
    subset = [
        r
        for r in classifications
        if r["comparison_pair"] == "first5_last5"
    ]
    for category in ALL_CATEGORIES:
        count = len([r for r in subset if r["category"] == category])
        frac = (count / len(subset)) if subset else 0.0
        lines.append(f"- `{category}`: {count} ({frac:.1%})")
    lines.extend(
        [
            "",
            "## Matched controls",
            "",
            control_note,
            "",
        ]
    )
    return "\n".join(lines) + "\n"


def run_public_trade_audit(
    *,
    input_dir: Path,
    output_dir: Path,
    windows: tuple[int, ...] = (5, 10),
    horizons: tuple[int, ...] = (1, 3, 5, 10, 15, 30, 60),
) -> PublicTradeAuditResult:
    schema = check_input_schema(input_dir)
    if not schema.ok:
        _write_blocked_outputs(
            output_dir,
            input_dir=input_dir,
            schema=schema,
            windows=windows,
            horizons=horizons,
        )
        return PublicTradeAuditResult(
            verdict=VERDICT_BLOCKED,
            output_dir=output_dir,
            cluster_count=0,
            blocked=True,
            missing_fields=schema.missing_fields,
        )

    impact = pd.read_csv(input_dir / "impact_compression_metrics.csv")
    events = pd.read_csv(input_dir / "proxy_events.csv")
    reclaims = pd.read_csv(input_dir / "proxy_reclaims.csv")
    recovery = pd.read_csv(input_dir / "aggregate_l2_recovery.csv")
    flip = pd.read_csv(input_dir / "orderflow_flip_metrics.csv")
    timeline_path = input_dir / "proxy_timeline_1s.csv"
    timeline = pd.read_csv(timeline_path) if timeline_path.is_file() else pd.DataFrame()

    classifications = classify_impact_frame(impact)
    recovery_lookup = recovery.groupby("cluster_id").first().to_dict("index")
    flip_lookup = flip.set_index("cluster_id", drop=False).to_dict("index")
    event_lookup = events.set_index("cluster_id", drop=False).to_dict("index")
    timeline_by_cluster: dict[str, pd.DataFrame] = {}
    if not timeline.empty:
        for cluster_id, group in timeline.groupby("cluster_id"):
            timeline_by_cluster[str(cluster_id)] = group.copy()

    enriched: list[dict[str, Any]] = []
    for row in classifications:
        cluster_id = str(row["cluster_id"])
        event = pd.Series(event_lookup.get(cluster_id, {}))
        rec = recovery_lookup.get(cluster_id)
        recovery_row = pd.Series(rec) if rec is not None else None
        flip_row = pd.Series(flip_lookup.get(cluster_id, {})) if cluster_id in flip_lookup else None
        tl = timeline_by_cluster.get(cluster_id)
        enriched.append(
            enrich_classification_context(
                row,
                event=event,
                recovery=recovery_row,
                flip=flip_row,
                timeline=tl,
                comparison_pair=str(row["comparison_pair"]),
            )
        )

    category_summary: list[dict[str, Any]] = []
    for scope in ("ALL", "LONG", "SHORT"):
        category_summary.extend(_build_category_summary(enriched, scope=scope))

    post_outcomes = compute_post_compression_outcomes(
        enriched,
        events=events,
        reclaims=reclaims,
        timeline_by_cluster=timeline_by_cluster,
        horizons=horizons,
    )

    control_impact_path = input_dir / "control_impact_compression_metrics.csv"
    control_impact = (
        pd.read_csv(control_impact_path)
        if control_impact_path.is_file()
        else None
    )
    control_comparison = _build_matched_control_comparison(enriched, control_impact)
    non_overlap = _build_non_overlapping_robustness(
        enriched,
        events,
        max_horizon_minutes=max(horizons),
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    cluster_count = len(impact)
    manifest = {
        "format_version": FORMAT_VERSION,
        "verdict": VERDICT_COMPLETE,
        "input_dir": str(input_dir.resolve()),
        "output_dir": str(output_dir.resolve()),
        "windows": list(windows),
        "horizons": list(horizons),
        "cluster_count": cluster_count,
        "input_hashes": {
            name: _sha256(input_dir / name)
            for name in (
                "impact_compression_metrics.csv",
                "proxy_events.csv",
                "proxy_reclaims.csv",
                "matched_controls.csv",
                "proxy_manifest.json",
            )
            if (input_dir / name).is_file()
        },
        "limitations": [
            "incremental_feature_groups.csv not present in input_dir; skipped.",
            "Matched controls lack dedicated impact_compression_metrics; "
            "control classification documented as unavailable.",
        ],
    }
    _write_json(output_dir / "audit_manifest.json", manifest)
    _write_csv(output_dir / "impact_classification.csv", enriched, IMPACT_CLASSIFICATION_FIELDS)
    _write_csv(output_dir / "impact_category_summary.csv", category_summary, IMPACT_CATEGORY_SUMMARY_FIELDS)
    _write_csv(output_dir / "post_compression_outcomes.csv", post_outcomes, POST_COMPRESSION_OUTCOME_FIELDS)
    _write_csv(output_dir / "matched_control_comparison.csv", control_comparison, MATCHED_CONTROL_COMPARISON_FIELDS)
    _write_csv(output_dir / "non_overlapping_robustness.csv", non_overlap, NON_OVERLAPPING_ROBUSTNESS_FIELDS)
    summary = _render_summary(
        verdict=VERDICT_COMPLETE,
        cluster_count=cluster_count,
        classifications=enriched,
        schema=None,
        control_note=(
            "Control artifacts lack required first5/last5 and first10/last10 "
            "aggressive-notional and impact-per-notional fields."
        ),
    )
    _atomic_write(output_dir / "public_trade_impact_summary.md", summary)

    return PublicTradeAuditResult(
        verdict=VERDICT_COMPLETE,
        output_dir=output_dir,
        cluster_count=cluster_count,
        blocked=False,
        missing_fields=(),
    )
