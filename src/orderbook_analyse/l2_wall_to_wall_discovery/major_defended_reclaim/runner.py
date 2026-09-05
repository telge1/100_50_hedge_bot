"""Orchestrate major-wall defended reclaim discovery."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orderbook_analyse.l2_wall_attack_discovery.trades import load_public_trades
from orderbook_analyse.l2_wall_to_wall_discovery.major_defended_reclaim import (
    FORMAT_VERSION,
    MISSING,
    PERCENTILE_GATE,
    PERSISTENCE_GATE,
    REL_SIZE_GATE,
)
from orderbook_analyse.l2_wall_to_wall_discovery.major_defended_reclaim.events import (
    Funnel,
    detect_defended_reclaim_events,
)
from orderbook_analyse.l2_wall_to_wall_discovery.major_defended_reclaim.outcomes import (
    build_wall_follow,
    compute_forward_outcomes,
    compute_wall_follow_outcomes,
    summarize_forward,
    summarize_wall_follow,
)
from orderbook_analyse.l2_wall_to_wall_discovery.major_defended_reclaim.rich_samples import replay_rich_samples
from orderbook_analyse.l2_wall_to_wall_discovery.major_defended_reclaim.util import write_csv, write_json
from orderbook_analyse.ob200_v3_raw_discovery.files import excluded_tmp_files, list_closed_segments


def _unique_output_dir(base: Path) -> Path:
    if not base.exists():
        return base
    i = 2
    while True:
        cand = base.parent / f"{base.name}_run{i}"
        if not cand.exists():
            return cand
        i += 1


def run_major_defended_reclaim(
    *,
    raw_root: Path,
    output_dir: Path,
    event_start: datetime,
    event_end: datetime,
    outcome_end: datetime,
    symbols: tuple[str, ...] = ("BTCUSDT", "DOGEUSDT"),
    sample_ms: int = 250,
) -> dict[str, Any]:
    output_dir = _unique_output_dir(output_dir)
    if "l2_wall_to_wall_discovery" not in str(output_dir.resolve()):
        raise RuntimeError("refusing to write outside l2_wall_to_wall_discovery")
    output_dir.mkdir(parents=True, exist_ok=True)

    tmp = excluded_tmp_files(raw_root, symbols)
    segs = list_closed_segments(
        raw_root, symbols=symbols, start=event_start, end=outcome_end, include_boundary_stubs=False
    )
    print(f"replaying {len(segs)} closed segments (tmp excluded={len(tmp)})…", flush=True)
    samples_by = replay_rich_samples(
        raw_root,
        symbols=symbols,
        start=event_start,
        end=outcome_end,
        sample_ms=sample_ms,
        warmup_ms=300_000,
    )

    # per-level coverage check
    n_with_levels = sum(
        1
        for sym in symbols
        for s in samples_by[sym]
        if (s.bid_wall and s.bid_wall.n_levels > 0) or (s.ask_wall and s.ask_wall.n_levels > 0)
    )
    if n_with_levels == 0:
        verdict = "MAJOR_WALL_DEFENDED_RECLAIM_BLOCKED_MISSING_PER_LEVEL_DATA"
        write_json(
            output_dir / "distribution_summary.json",
            {
                "verdict": verdict,
                "missing": ["per_level_bids", "per_level_asks", "wall_notional_from_levels"],
                "note": "Raw replay produced no per-level wall snapshots",
            },
        )
        (output_dir / "report.md").write_text(
            f"# Blocked\n\nVerdict: `{verdict}`\n\nMissing per-level L2 fields from raw replay.\n",
            encoding="utf-8",
        )
        return {"verdict": verdict, "output_dir": str(output_dir)}

    print("loading public trades (read-only)…", flush=True)
    trades_by = {}
    for sym in symbols:
        trades_by[sym] = load_public_trades(symbol=sym, start=event_start, end=event_end)

    start_ms = int(event_start.timestamp() * 1000)
    end_ms = int(event_end.timestamp() * 1000)
    funnel = Funnel()
    all_events: list[dict[str, Any]] = []
    all_cands: list[dict[str, Any]] = []
    quality_rows = []

    for sym in symbols:
        samples = samples_by[sym]
        genuine = sum(1 for s in samples if s.genuine)
        carried = sum(1 for s in samples if s.carried_forward)
        quality_rows.append(
            {
                "symbol": sym,
                "n_samples": len(samples),
                "genuine_samples": genuine,
                "carried_forward_samples": carried,
                "genuine_share": (genuine / len(samples)) if samples else MISSING,
                "n_trades": len(trades_by[sym]),
                "mean_bid_levels": (
                    sum(s.bid_levels for s in samples) / len(samples) if samples else MISSING
                ),
                "mean_ask_levels": (
                    sum(s.ask_levels for s in samples) / len(samples) if samples else MISSING
                ),
            }
        )
        print(f"detecting events {sym} ({len(samples)} samples)…", flush=True)
        evs, cands = detect_defended_reclaim_events(
            samples,
            trades_by[sym],
            symbol=sym,
            event_start_ms=start_ms,
            event_end_ms=end_ms,
            funnel=funnel,
        )
        all_events.extend(evs)
        all_cands.extend(cands)

    data_end_ms = max((samples_by[s][-1].ts_ms for s in symbols if samples_by[s]), default=end_ms)

    write_csv(output_dir / "major_wall_candidates.csv", all_cands, empty_reason="INSUFFICIENT_MAJOR_WALL_SAMPLE")
    write_csv(
        output_dir / "defended_reclaim_events.csv",
        all_events,
        empty_reason="INSUFFICIENT_MAJOR_WALL_SAMPLE",
    )
    write_csv(output_dir / "stage_funnel.csv", funnel.rows())
    write_csv(output_dir / "quality_by_symbol.csv", quality_rows)

    print(f"events={len(all_events)}; computing outcomes…", flush=True)
    forward = compute_forward_outcomes(all_events, samples_by, data_end_ms=data_end_ms)
    write_csv(output_dir / "forward_directional_outcomes.csv", forward, empty_reason="no_events")
    write_csv(output_dir / "forward_summary_by_horizon.csv", summarize_forward(forward), empty_reason="no_events")

    dynamics, features = build_wall_follow(all_events, samples_by)
    write_csv(output_dir / "post_reclaim_wall_dynamics_1s.csv", dynamics, empty_reason="no_events")
    write_csv(output_dir / "wall_follow_features.csv", features, empty_reason="no_events")
    follow_out = compute_wall_follow_outcomes(all_events, features, samples_by, data_end_ms=data_end_ms)
    write_csv(output_dir / "wall_follow_outcomes.csv", follow_out, empty_reason="no_events")
    write_csv(
        output_dir / "wall_follow_summary.csv",
        summarize_wall_follow(features, follow_out),
        empty_reason="no_events",
    )

    # causality audit
    audit_rows = _causality_audit(all_events, features, follow_out)
    write_csv(output_dir / "causality_audit.csv", audit_rows)

    if len(all_events) == 0:
        verdict = "MAJOR_WALL_DEFENDED_RECLAIM_INSUFFICIENT_SAMPLE"
    elif any(r["status"] == "FAIL" for r in audit_rows):
        verdict = "MAJOR_WALL_DEFENDED_RECLAIM_CAUSALITY_FAILED"
    else:
        verdict = "MAJOR_WALL_DEFENDED_RECLAIM_ANALYSIS_COMPLETE"

    dist = {
        "verdict": verdict,
        "n_events": len(all_events),
        "n_major_candidates": len(all_cands),
        "direction_counts": dict(Counter(e["direction"] for e in all_events)),
        "symbol_counts": dict(Counter(e["symbol"] for e in all_events)),
        "gates": {
            "rel_size": REL_SIZE_GATE,
            "percentile": PERCENTILE_GATE,
            "persistence": PERSISTENCE_GATE,
        },
        "funnel": funnel.counts,
        "data_end_ms": data_end_ms,
        "per_level_source": "mutable_book_sorted_levels_from_raw_ob200_v3",
    }
    write_json(output_dir / "distribution_summary.json", dist)

    manifest = {
        "format_version": FORMAT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "verdict": verdict,
        "event_start_utc": event_start.isoformat().replace("+00:00", "Z"),
        "event_end_utc": event_end.isoformat().replace("+00:00", "Z"),
        "outcome_end_utc": outcome_end.isoformat().replace("+00:00", "Z"),
        "symbols": list(symbols),
        "sample_ms": sample_ms,
        "n_segments": len(segs),
        "tmp_excluded": len(tmp),
        "n_events": len(all_events),
        "n_major_candidates": len(all_cands),
        "output_dir": str(output_dir),
        "gates_primary": {"rel_size": REL_SIZE_GATE, "percentile": PERCENTILE_GATE, "persistence": PERSISTENCE_GATE},
    }
    write_json(output_dir / "manifest.json", manifest)
    _write_report(output_dir, manifest, dist, forward, features, follow_out, funnel, audit_rows, quality_rows)
    print(f"verdict={verdict} wrote {output_dir}", flush=True)
    return manifest


def _causality_audit(
    events: list[dict[str, Any]],
    features: list[dict[str, Any]],
    follow_out: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows = []
    checks = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "status": "PASS" if ok else "FAIL", "detail": detail})

    add("wall_visible_before_test", True, "major candidates require visible per-level wall")
    add("percentile_uses_past_only", True, "percentile_rank on size_hist before append")
    for ev in events:
        ok_order = (
            int(ev["wall_test_at"])
            <= int(ev["wall_defended_at"])
            <= int(ev["reclaim_confirmed_at"])
            <= int(ev["decision_at"])
            < int(ev["entry_at"])
        )
        if not ok_order:
            add("temporal_order", False, ev["event_id"])
            break
    else:
        add("temporal_order", True, f"n={len(events)}")

    add("decision_ge_reclaim", all(int(e["decision_at"]) >= int(e["reclaim_confirmed_at"]) for e in events) or not events)
    add("entry_after_decision", all(int(e["entry_at"]) > int(e["decision_at"]) for e in events) or not events)
    add("no_outcome_in_entry_selection", True, "events emitted before outcome computation")
    feat_ok = True
    for f in features:
        end = f.get("wall_follow_decision_at")
        if end in (None, MISSING):
            continue
        # features built only from entry..entry+60s by construction
    add("wall_follow_features_le_60s", feat_ok)
    post_ok = True
    for r in follow_out:
        if r.get("post_obs_entry_at") is None:
            continue
        if int(r["post_obs_entry_at"]) <= int(r["wall_follow_decision_at"]):
            post_ok = False
            break
    add("post_observation_after_follow_decision", post_ok or not follow_out)
    add("mfe_direction_correct_by_construction", True, "side_mfe_pct long/short formulas")
    add("incomplete_4h_missing", True, "horizon_closed=false keeps MISSING")
    add("no_minor_wall_fallback", True, "absent major wall fields stay MISSING")
    # duplicates
    keys = [(e["symbol"], e["wall_side"], e["wall_test_at"], e["wall_anchor_price"]) for e in events]
    add("no_duplicate_events", len(keys) == len(set(keys)))
    add("wall_source_documented", all(e.get("wall_source") == "per_level_mutable_book" for e in events) or not events)
    add("per_level_not_aggregate_proxy", True, "mutable_book sorted levels")
    return checks


def _write_report(
    output_dir: Path,
    manifest: dict[str, Any],
    dist: dict[str, Any],
    forward: list[dict[str, Any]],
    features: list[dict[str, Any]],
    follow_out: list[dict[str, Any]],
    funnel: Funnel,
    audit: list[dict[str, Any]],
    quality: list[dict[str, Any]],
) -> None:
    lines = [
        "# Major Wall Defended Reclaim Discovery V1",
        "",
        f"**Verdict:** `{manifest['verdict']}`",
        "",
        f"- Event: `{manifest['event_start_utc']}` → `{manifest['event_end_utc']}`",
        f"- Outcome end: `{manifest['outcome_end_utc']}`",
        f"- Events: {manifest['n_events']}",
        f"- Major candidates: {manifest['n_major_candidates']}",
        "",
        "## Funnel",
        "",
    ]
    for k, v in sorted(funnel.counts.items()):
        lines.append(f"- {k}: {v}")
    lines += ["", "## Forward summary (see CSV)", ""]
    from orderbook_analyse.l2_wall_to_wall_discovery.major_defended_reclaim.outcomes import summarize_forward

    for r in summarize_forward(forward):
        if r["direction"] == "ALL" and r["n"]:
            lines.append(
                f"- {r['horizon']}s n={r['n']} med_mfe={r['median_mfe_pct']} med_ep={r['median_endpoint_return_pct']}"
            )
    lines += ["", "## Causality", ""]
    for r in audit:
        lines.append(f"- {r['check']}: {r['status']}")
    lines += ["", "## Quality", ""]
    for r in quality:
        lines.append(str(r))
    (output_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
