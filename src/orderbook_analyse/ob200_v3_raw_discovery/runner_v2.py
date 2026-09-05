"""OB200 V2 discovery runner: coverage, lifecycles, non-overlapping chains."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orderbook_analyse.ob200_v3_raw_discovery import FORMAT_VERSION
from orderbook_analyse.ob200_v3_raw_discovery.analysis import build_chains
from orderbook_analyse.ob200_v3_raw_discovery.audit import audit_to_row, process_segment
from orderbook_analyse.ob200_v3_raw_discovery.files import excluded_tmp_files, list_closed_segments
from orderbook_analyse.ob200_v3_raw_discovery.lifecycles_v2 import (
    audit_v1_chain_overcount,
    build_chains_v2,
    build_wall_lifecycles,
    chain_v2_to_row,
    funnel_v2,
    lifecycle_to_row,
    overlap_to_row,
)
from orderbook_analyse.ob200_v3_raw_discovery.reconstruct import sample_row_to_dict
from orderbook_analyse.ob200_v3_raw_discovery.source_coverage import (
    audit_source_coverage,
    write_coverage_artifacts,
)
from orderbook_analyse.ob200_v3_raw_discovery.walls import extract_wall_events, wall_event_to_row


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    return datetime.fromisoformat(raw.strip().replace("Z", "+00:00")).astimezone(timezone.utc)


def run_discovery_v2(
    *,
    raw_root: Path,
    symbols: tuple[str, ...],
    output_dir: Path,
    start: datetime | None = None,
    end: datetime | None = None,
    max_files: int | None = None,
    sample_seconds: int = 1,
    seed: int = 42,
    qty_median_mult: float = 3.0,
) -> dict[str, Any]:
    if output_dir.resolve() == (raw_root.parent.parent / "results/ob200_v3_raw_discovery/btc_doge_initial").resolve():
        raise RuntimeError("refusing to write V2 into btc_doge_initial")
    output_dir.mkdir(parents=True, exist_ok=True)

    tmp_excluded = excluded_tmp_files(raw_root, symbols)
    segments = list_closed_segments(
        raw_root, symbols=symbols, start=start, end=end, include_boundary_stubs=False
    )
    if max_files is not None:
        segments = segments[:max_files]
    if not segments:
        raise RuntimeError("no closed segments for V2 window")

    data_start = min(s.start_utc for s in segments)
    data_end = max(s.end_utc for s in segments)

    # --- Source coverage (always query; never claim unavailable without probe) ---
    cov_rows, market_audit = audit_source_coverage(symbols, data_start, data_end)
    write_coverage_artifacts(output_dir, cov_rows, market_audit)

    samples_by_symbol: dict[str, list] = {s: [] for s in symbols}
    sample_rows: list[dict[str, Any]] = []
    audits = []

    for i, ref in enumerate(segments, 1):
        print(f"v2 process {i}/{len(segments)} {ref.path.name}", flush=True)
        audit, samples = process_segment(
            ref,
            collect_samples=True,
            sample_ms=max(1, sample_seconds) * 1000,
            warmup_ms=60_000,
        )
        audits.append(audit)
        if audit.replay_verdict in {
            "REPLAY_CONFIRMED",
            "REPLAY_CONFIRMED_FROM_LOCAL_CHECKPOINT",
            "PARTIAL_BUT_DISCOVERY_USABLE",
        }:
            samples_by_symbol[ref.symbol].extend(samples)
            sample_rows.extend(sample_row_to_dict(s) for s in samples)

    _write_csv(output_dir / "segment_integrity_v2.csv", [audit_to_row(a) for a in audits])

    all_events = []
    for sym in symbols:
        samples_by_symbol[sym].sort(key=lambda s: s.ts_ms)
        all_events.extend(
            extract_wall_events(
                samples_by_symbol[sym],
                qty_median_mult=qty_median_mult,
                seed=seed,
            )
        )
    _write_csv(output_dir / "wall_events_v2.csv", [wall_event_to_row(e) for e in all_events])
    _write_csv(output_dir / "l2_samples_v2.csv", sample_rows)

    # V1 chains retained only for overcount comparison (explicitly labeled)
    v1_chains = build_chains(all_events, seed=seed)
    lifecycles = build_wall_lifecycles(all_events, seed=seed)
    chains_v2 = build_chains_v2(lifecycles, seed=seed)
    overlap = audit_v1_chain_overcount(v1_chains, all_events, lifecycles, chains_v2)
    funnel = funnel_v2(lifecycles, chains_v2)

    _write_csv(output_dir / "wall_lifecycles.csv", [lifecycle_to_row(x) for x in lifecycles])
    _write_csv(output_dir / "event_chains_v2.csv", [chain_v2_to_row(c) for c in chains_v2])
    _write_csv(output_dir / "chain_overlap_audit.csv", [overlap_to_row(r) for r in overlap])
    _write_csv(output_dir / "funnel_v2.csv", funnel)
    # labeled V1 comparison artifact (not the V2 primary chain file)
    _write_csv(
        output_dir / "event_chains_v1_comparison.csv",
        [
            {
                "chain_id": c.chain_id,
                "symbol": c.symbol,
                "direction": c.direction,
                "complete": c.complete,
                "touch_ts": c.touch_ts,
                "reclaim_ts": c.reclaim_ts,
                "stages": c.stages,
                "note": "V1_COMPARISON_ONLY_one_chain_per_TOUCH",
            }
            for c in v1_chains
        ],
    )

    primary = [c for c in chains_v2 if c.is_primary]
    complete_primary = [c for c in primary if c.completion_class == "COMPLETE_PRIMARY"]

    manifest = {
        "format_version": "ob200_v3_raw_discovery/v2",
        "parent_format": FORMAT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "raw_root": str(raw_root),
        "symbols": list(symbols),
        "window_start_utc": data_start.isoformat().replace("+00:00", "Z"),
        "window_end_utc": data_end.isoformat().replace("+00:00", "Z"),
        "segments_used": [s.path.name for s in segments],
        "tmp_excluded": [str(p) for p in tmp_excluded],
        "seed": seed,
        "n_samples": len(sample_rows),
        "n_wall_events": len(all_events),
        "n_lifecycles": len(lifecycles),
        "n_chains_v2": len(chains_v2),
        "n_primary_chains_v2": len(primary),
        "n_complete_primary_v2": len(complete_primary),
        "v1_comparison_chains_total": len(v1_chains),
        "v1_comparison_chains_complete": sum(1 for c in v1_chains if c.complete),
        "writer_metadata_fix": {
            "continuity": "data.u (+1 on deltas); seq informational only",
            "completion_status": "closed after successful atomic finalize",
            "additive_fields": ["replay_source", "continuity_status", "first_u", "last_u", "u_gaps"],
            "historical_manifests": "unchanged",
            "collector_restart": False,
        },
        "market_join_audit": market_audit.get("sources"),
        "required_artifacts": [
            "source_coverage.csv",
            "market_join_audit.json",
            "wall_lifecycles.csv",
            "chain_overlap_audit.csv",
            "event_chains_v2.csv",
            "funnel_v2.csv",
            "analysis_report_v2.md",
        ],
    }
    (output_dir / "manifest_v2.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    report = _build_report_v2(manifest, audits, lifecycles, chains_v2, overlap, funnel, cov_rows, market_audit)
    (output_dir / "analysis_report_v2.md").write_text(report, encoding="utf-8")
    return manifest


def _build_report_v2(
    manifest: dict[str, Any],
    audits: list,
    lifecycles: list,
    chains_v2: list,
    overlap: list,
    funnel: list[dict[str, Any]],
    cov_rows: list,
    market_audit: dict[str, Any],
) -> str:
    lines = [
        "# OB200 v3 Raw Discovery V2 Report",
        "",
        f"- Format: `{manifest['format_version']}`",
        f"- Window: `{manifest['window_start_utc']}` → `{manifest['window_end_utc']}`",
        f"- Segments: {len(manifest['segments_used'])}",
        "",
        "## Writer metadata fix (future segments; historical unchanged)",
        "",
        "- Continuity: `data.u` (+1 on deltas); `seq` no longer flips `replayable`",
        "- `completion_status=closed` after successful atomic finalize",
        "- Additive: `replay_source`, `continuity_status`, `first_u`, `last_u`, `u_gaps`",
        "- Collector not restarted; historical manifests not rewritten",
        "",
        "## Source coverage (queried)",
        "",
    ]
    for r in cov_rows:
        if r.coverage_status == "AVAILABLE" or r.source in {"public_trades", "open_interest", "liquidations"}:
            lines.append(
                f"- {r.source} `{r.database}.{r.table}` `{r.time_column}` "
                f"{r.symbol}: rows={r.row_count} min={r.min_ts} max={r.max_ts} "
                f"status={r.coverage_status} {r.notes}"
            )
    lines += ["", "## Replay (raw)", ""]
    for a in audits:
        lines.append(
            f"- {a.symbol} `{Path(a.path).name}`: {a.replay_verdict} "
            f"u_gaps={a.u_gaps} sha={a.sha256_ok}"
        )
    lines += ["", "## Chain overcount (V1 comparison vs V2)", ""]
    for r in overlap:
        lines.append(
            f"- {r.symbol} {r.direction}: V1 complete={r.v1_chains_complete}/"
            f"{r.v1_chains_total} overlap_pairs={r.overlapping_complete_pairs} "
            f"→ V2 primary={r.primary_chains_v2} complete_primary={r.complete_primary_v2}"
        )
        lines.append(f"  - cause: {r.root_cause}")
    lines += ["", "## Funnel V2", ""]
    for f in funnel:
        lines.append(
            f"- {f['symbol']} {f['direction']}: lifecycles={f['lifecycles']} "
            f"touch={f['with_touch']} complete_primary_chains={f['complete_primary_chains']}"
        )
    lines += [
        "",
        "## Limits",
        "",
        "- Discovery V2 on ~7h sample; not a strategy proof.",
        "- V1 chain counts below are comparison-only.",
        f"- V1_COMPARISON complete={manifest['v1_comparison_chains_complete']}/"
        f"{manifest['v1_comparison_chains_total']}",
        "",
    ]
    return "\n".join(lines) + "\n"
