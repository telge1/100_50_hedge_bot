"""F3 aggregate wall proxy discovery orchestrator."""

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

from orderbook_analyse.oi_liq_impact_l2.aggregate_proxy.analysis import analyze_cluster
from orderbook_analyse.oi_liq_impact_l2.aggregate_proxy.constants import (
    CLUSTER_GAP_SENSITIVITIES,
    DEFAULT_F1_DIR,
    DEFAULT_F2_DIR,
    DEFAULT_OUTPUT_DIR,
    FORMAT_VERSION,
    PRIMARY_CLUSTER_GAP,
    PROXY_SEMANTICS,
    SYMBOL,
    WINDOW_END,
    WINDOW_START,
)
from orderbook_analyse.oi_liq_impact_l2.aggregate_proxy.controls import build_matched_controls
from orderbook_analyse.oi_liq_impact_l2.aggregate_proxy.loaders import (
    AggregateProxyError,
    load_candles_1m,
    load_f1_bundle,
    load_ob_1s,
    load_trades_1s,
    parse_window,
)
from orderbook_analyse.oi_liq_impact_l2.wall_absorption.clusters import (
    build_flush_clusters,
    cluster_sensitivity_counts,
)


@dataclass(frozen=True)
class AggregateProxyRunResult:
    passed: bool
    output_dir: Path
    verdict: str
    cluster_count: int


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


def _quality_audit(ob_1s: pd.DataFrame, trades_1s: pd.DataFrame) -> dict[str, Any]:
    start, end = parse_window()
    expected = int((end - start).total_seconds())
    genuine = int(ob_1s["is_genuine"].sum()) if not ob_1s.empty else 0
    return {
        "symbol": SYMBOL,
        "window": {"start": WINDOW_START, "end": WINDOW_END},
        "expected_seconds": expected,
        "ob_rows": len(ob_1s),
        "genuine_seconds": genuine,
        "genuine_coverage_rate": genuine / expected if expected else 0,
        "trade_seconds": len(trades_1s),
        "aggregate_only": True,
        "per_level_reconstruction": False,
    }


def _funnel_rows(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    total = len(results)
    ok = [r for r in results if not r.get("data_abort") and r.get("abort_reason") != "NO_GENUINE_ANCHOR"]
    stable = [
        r for r in ok if r.get("stability", {}).get("exact_stable_fraction", 0) == 1.0
    ]
    recovery = [r for r in ok if any(x.get("aggregate_depth_recovery_observed") for x in r.get("recovery", []))]
    compression = [
        r for r in ok if r.get("compression", {}).get("compression_observed_first5_vs_last5")
    ]
    flip = [r for r in ok if r.get("flip", {}).get("first_any_flip_second")]
    reclaim = [
        r for r in ok
        if any(x.get("proxy_reclaim_within_mark") and x.get("mark_minutes") == 60 for x in r.get("reclaims", []))
    ]
    return [
        {"stage": "clusters_total", "count": total},
        {"stage": "analyzed_no_abort", "count": len(ok)},
        {"stage": "dominant_wall_exact_stable_whole_post", "count": len(stable)},
        {"stage": "aggregate_depth_recovery_any_mark", "count": len(recovery)},
        {"stage": "impact_compression_first5_vs_last5", "count": len(compression)},
        {"stage": "orderflow_flip_any_component", "count": len(flip)},
        {"stage": "proxy_reclaim_within_60m_any_anchor", "count": len(reclaim)},
    ]


def _distributions(results: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [r for r in results if not r.get("data_abort")]
    exact_fracs = [r["stability"]["exact_stable_fraction"] for r in ok if r.get("stability")]
    minutes_reclaim = [
        x["minutes_to_1s_reclaim"]
        for r in ok
        for x in r.get("reclaims", [])
        if x.get("minutes_to_1s_reclaim") is not None and x.get("reclaim_anchor") == "PRE_FLUSH_CLOSE"
    ]

    def q(vals: list[float], p: float) -> float | None:
        if not vals:
            return None
        vals_sorted = sorted(vals)
        idx = int(round((len(vals_sorted) - 1) * p))
        return vals_sorted[idx]

    return {
        "exact_stable_fraction": {
            "count": len(exact_fracs),
            "median": statistics.median(exact_fracs) if exact_fracs else None,
            "p25": q(exact_fracs, 0.25),
            "p75": q(exact_fracs, 0.75),
        },
        "minutes_to_pre_flush_close_1s_reclaim": {
            "count": len(minutes_reclaim),
            "median": statistics.median(minutes_reclaim) if minutes_reclaim else None,
        },
    }


def _select_examples(results: list[dict[str, Any]]) -> dict[str, list[str]]:
    ok = [r for r in results if not r.get("data_abort") and r.get("timeline")]
    aborted = [r["cluster_id"] for r in results if r.get("data_abort")]

    def pick(pred, n=3) -> list[str]:
        out = []
        for r in sorted(ok, key=lambda x: x["cluster_id"]):
            if pred(r) and len(out) < n:
                out.append(r["cluster_id"])
        return out

    return {
        "long_sequences": pick(lambda r: r["event"]["direction"] == "LONG"),
        "short_sequences": pick(lambda r: r["event"]["direction"] == "SHORT"),
        "wall_stable": pick(lambda r: r["stability"].get("exact_stable_fraction", 0) >= 0.9),
        "wall_changed": pick(lambda r: r["stability"].get("wall_change_count", 0) >= 5),
        "reclaims": pick(
            lambda r: any(
                x.get("proxy_reclaim_within_mark") and x.get("mark_minutes") == 15
                for x in r.get("reclaims", [])
            )
        ),
        "non_reclaims": pick(
            lambda r: not any(
                x.get("proxy_reclaim_within_mark") and x.get("mark_minutes") == 60
                for x in r.get("reclaims", [])
            )
        ),
        "data_aborts": aborted,
    }


def _write_examples_md(path: Path, results: list[dict[str, Any]], examples: dict[str, list[str]]) -> None:
    lookup = {r["cluster_id"]: r for r in results}
    lines = ["# Proxy event examples\n", "Aggregate proxy semantics only.\n"]
    for group, ids in examples.items():
        lines.append(f"\n## {group}\n")
        for cid in ids:
            r = lookup.get(cid)
            if not r:
                lines.append(f"- `{cid}`: not found\n")
                continue
            lines.append(f"### `{cid}`\n")
            if r.get("data_abort"):
                lines.append(f"- **abort**: {r.get('abort_reason')}\n")
                continue
            timeline = r.get("timeline", [])[:8]
            for row in timeline:
                lines.append(
                    f"- {row['second']} price={row.get('mid_price')} "
                    f"wall={row.get('dominant_wall_price')} status={row.get('wall_status')} "
                    f"depth={row.get('directional_depth_l50')} ofi={row.get('directional_ofi')}\n"
                )
    _atomic_write(path, "".join(lines))


def run_aggregate_proxy_discovery(
    *,
    f1_dir: Path | str = DEFAULT_F1_DIR,
    f2_dir: Path | str | None = DEFAULT_F2_DIR,
    output_dir: Path | str = DEFAULT_OUTPUT_DIR,
    client: Any | None = None,
    query_clickhouse: bool = True,
    smoke_cluster_id: str | None = None,
    max_clusters: int | None = None,
) -> AggregateProxyRunResult:
    f1_dir = Path(f1_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    manifest, minute_features, flush_candidates, input_hashes = load_f1_bundle(f1_dir)
    if f2_dir:
        f2_path = Path(f2_dir)
        ep_path = f2_path / "event_chain_manifest.json"
        if ep_path.is_file():
            input_hashes["event_chain_manifest.json"] = _sha256(ep_path)

    candidates = flush_candidates.to_dict("records")
    clusters = build_flush_clusters(candidates, gap_minutes=PRIMARY_CLUSTER_GAP)
    sensitivity = cluster_sensitivity_counts(candidates, gaps=CLUSTER_GAP_SENSITIVITIES)

    if smoke_cluster_id:
        clusters = [c for c in clusters if c.cluster_id == smoke_cluster_id]
        if not clusters:
            raise AggregateProxyError(f"smoke cluster not found: {smoke_cluster_id}")
    elif max_clusters is not None:
        clusters = clusters[:max_clusters]

    if query_clickhouse and client is None:
        from orderbook_analyse.orderbook_v2.ch_client import get_clickhouse_client

        client = get_clickhouse_client()

    start, end = parse_window()
    if query_clickhouse and client is not None:
        ob_1s = load_ob_1s(client, symbol=SYMBOL, start=start, end=end)
        trades_1s = load_trades_1s(client, symbol=SYMBOL, start=start, end=end)
        candles_1m = load_candles_1m(client, symbol=SYMBOL, start=start, end=end)
    else:
        raise AggregateProxyError("ClickHouse client required for aggregate proxy discovery")

    quality = _quality_audit(ob_1s, trades_1s)
    if quality["genuine_coverage_rate"] < 0.5:
        raise AggregateProxyError("insufficient genuine OB coverage for proxy discovery")

    results: list[dict[str, Any]] = []
    for cluster in clusters:
        results.append(
            analyze_cluster(cluster, ob_1s, trades_1s, candles_1m, minute_features)
        )

    controls, unmatched = build_matched_controls(
        minute_features, flush_candidates, clusters
    )

    # Flatten outputs
    proxy_events = [r["event"] for r in results if "event" in r]
    timeline_rows = [row for r in results for row in r.get("timeline", [])]
    stability_rows = [r["stability"] for r in results if r.get("stability")]
    recovery_rows = [row for r in results for row in r.get("recovery", [])]
    compression_rows = [r["compression"] for r in results if r.get("compression")]
    flip_rows = [r["flip"] for r in results if r.get("flip")]
    reclaim_rows = [row for r in results for row in r.get("reclaims", [])]
    continuation_rows = [r["continuation"] for r in results if r.get("continuation")]

    event_fields = tuple(proxy_events[0].keys()) if proxy_events else ("cluster_id",)
    timeline_fields = tuple(timeline_rows[0].keys()) if timeline_rows else ("cluster_id",)
    stability_fields = tuple(stability_rows[0].keys()) if stability_rows else ("cluster_id",)
    recovery_fields = tuple(recovery_rows[0].keys()) if recovery_rows else ("cluster_id",)
    compression_fields = tuple(compression_rows[0].keys()) if compression_rows else ("cluster_id",)
    flip_fields = tuple(flip_rows[0].keys()) if flip_rows else ("cluster_id",)
    reclaim_fields = tuple(reclaim_rows[0].keys()) if reclaim_rows else ("cluster_id",)
    continuation_fields = tuple(continuation_rows[0].keys()) if continuation_rows else ("cluster_id",)
    control_fields = tuple(controls[0].keys()) if controls else ("control_id",)

    _write_csv(output_dir / "proxy_events.csv", proxy_events, event_fields)
    _write_csv(output_dir / "proxy_timeline_1s.csv", timeline_rows, timeline_fields)
    _write_csv(output_dir / "dominant_wall_stability.csv", stability_rows, stability_fields)
    _write_csv(output_dir / "aggregate_l2_recovery.csv", recovery_rows, recovery_fields)
    _write_csv(output_dir / "impact_compression_metrics.csv", compression_rows, compression_fields)
    _write_csv(output_dir / "orderflow_flip_metrics.csv", flip_rows, flip_fields)
    _write_csv(output_dir / "proxy_reclaims.csv", reclaim_rows, reclaim_fields)
    _write_csv(output_dir / "proxy_continuations.csv", continuation_rows, continuation_fields)
    _write_csv(output_dir / "matched_controls.csv", controls, control_fields)

    funnel = _funnel_rows(results)
    _write_csv(
        output_dir / "proxy_funnel.csv",
        funnel,
        ("stage", "count"),
    )
    _write_json(output_dir / "proxy_distributions.json", _distributions(results))
    _write_json(output_dir / "proxy_quality_audit.json", quality)

    examples = _select_examples(results)
    _write_examples_md(output_dir / "proxy_event_examples.md", results, examples)

    proxy_manifest = {
        "format_version": FORMAT_VERSION,
        "verdict": "BTC_F3_AGGREGATE_WALL_PROXY_COMPLETE",
        "symbol": SYMBOL,
        "window": {"start": WINDOW_START, "end": WINDOW_END, "semantics": "[start,end)"},
        "f1_input_dir": str(f1_dir.resolve()),
        "f2_input_dir": str(Path(f2_dir).resolve()) if f2_dir else None,
        "input_hashes": input_hashes,
        "cluster_gap_primary": PRIMARY_CLUSTER_GAP,
        "cluster_sensitivity": sensitivity,
        "cluster_count": len(clusters),
        "candidate_count": len(flush_candidates),
        "controls_matched": len(controls),
        "controls_unmatched": len(unmatched),
        "proxy_semantics": list(PROXY_SEMANTICS),
        "threshold_search": False,
        "profitability_claim": False,
        "smoke_cluster_id": smoke_cluster_id,
    }
    _write_json(output_dir / "proxy_manifest.json", proxy_manifest)

    summary_lines = [
        "# BTC F3 Aggregate Wall Proxy Summary\n",
        f"- Verdict: **BTC_F3_AGGREGATE_WALL_PROXY_COMPLETE**\n",
        f"- Clusters analyzed (gap={PRIMARY_CLUSTER_GAP}): **{len(clusters)}**\n",
        f"- Genuine OB coverage: **{quality['genuine_coverage_rate']:.1%}**\n",
        f"- Matched controls: **{len(controls)}** (unmatched: {len(unmatched)})\n",
        "\n## Funnel\n",
    ]
    for row in funnel:
        summary_lines.append(f"- {row['stage']}: {row['count']}\n")
    summary_lines.append("\n## Proxy semantics\n")
    for line in PROXY_SEMANTICS:
        summary_lines.append(f"- {line}\n")
    summary_lines.append("\nNo profitability claim.\n")
    _atomic_write(output_dir / "proxy_summary.md", "".join(summary_lines))

    return AggregateProxyRunResult(
        passed=True,
        output_dir=output_dir,
        verdict="BTC_F3_AGGREGATE_WALL_PROXY_COMPLETE",
        cluster_count=len(clusters),
    )
