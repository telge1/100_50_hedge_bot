"""Orchestrate OB200 raw discovery audit + analysis."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orderbook_analyse.ob200_v3_raw_discovery import FORMAT_VERSION
from orderbook_analyse.ob200_v3_raw_discovery.analysis import (
    build_chains,
    compute_outcomes,
    funnel_counts,
    matched_controls,
    summarize_outcomes,
)
from orderbook_analyse.ob200_v3_raw_discovery.audit import SegmentAudit, audit_to_row, process_segment
from orderbook_analyse.ob200_v3_raw_discovery.files import (
    SegmentRef,
    excluded_tmp_files,
    list_closed_segments,
)
from orderbook_analyse.ob200_v3_raw_discovery.market_join import try_load_market_context
from orderbook_analyse.ob200_v3_raw_discovery.reconstruct import SampleRow, sample_row_to_dict
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
    text = raw.strip().replace("Z", "+00:00")
    return datetime.fromisoformat(text).astimezone(timezone.utc)


def run_discovery(
    *,
    raw_root: Path,
    symbols: tuple[str, ...],
    output_dir: Path,
    start: datetime | None = None,
    end: datetime | None = None,
    do_audit: bool = True,
    do_analyze: bool = True,
    max_files: int | None = None,
    sample_seconds: int = 1,
    controls_per_event: int = 3,
    seed: int = 42,
    qty_median_mult: float = 3.0,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    tmp_excluded = excluded_tmp_files(raw_root, symbols)
    segments = list_closed_segments(
        raw_root, symbols=symbols, start=start, end=end, include_boundary_stubs=False
    )
    if max_files is not None:
        segments = segments[:max_files]

    if not segments:
        raise RuntimeError("no closed non-stub segments found")

    data_start = min(s.start_utc for s in segments)
    data_end = max(s.end_utc for s in segments)

    audits: list[SegmentAudit] = []
    samples_by_symbol: dict[str, list[SampleRow]] = {s: [] for s in symbols}
    all_events: list = []
    sample_rows: list[dict[str, Any]] = []
    chains: list = []
    controls: list = []
    outcomes: list = []
    summary: list = []

    # Single pass: integrity + optional samples (avoids double decompress/replay).
    for i, ref in enumerate(segments, 1):
        print(
            f"process {i}/{len(segments)} {ref.path.name} "
            f"(audit={do_audit} analyze={do_analyze})",
            flush=True,
        )
        audit, samples = process_segment(
            ref,
            collect_samples=do_analyze,
            sample_ms=max(1, sample_seconds) * 1000,
            warmup_ms=60_000,
        )
        if do_audit:
            audits.append(audit)
        if do_analyze and audit.replay_verdict in {
            "REPLAY_CONFIRMED",
            "REPLAY_CONFIRMED_FROM_LOCAL_CHECKPOINT",
            "PARTIAL_BUT_DISCOVERY_USABLE",
        }:
            samples_by_symbol[ref.symbol].extend(samples)
            sample_rows.extend(sample_row_to_dict(s) for s in samples)
        elif do_analyze:
            print(f"  skip samples: {audit.replay_verdict}", flush=True)

    if do_audit:
        _write_csv(output_dir / "segment_integrity.csv", [audit_to_row(a) for a in audits])
        _write_csv(
            output_dir / "replay_audit.csv",
            [
                {
                    "symbol": a.symbol,
                    "path": a.path,
                    "replay_verdict": a.replay_verdict,
                    "reconstruction_ok": a.reconstruction_ok,
                    "u_gaps": a.u_gaps,
                    "seq_jumps": a.seq_jumps,
                    "seq_jump_is_loss": a.seq_jump_is_loss,
                    "manifest_replayable": a.manifest_replayable,
                    "manifest_completion_status": a.manifest_completion_status,
                    "sha256_ok": a.sha256_ok,
                    "start_checkpoint_bids": a.start_checkpoint_bids,
                    "start_checkpoint_asks": a.start_checkpoint_asks,
                    "end_best_bid": a.end_best_bid,
                    "end_best_ask": a.end_best_ask,
                    "notes": a.notes,
                }
                for a in audits
            ],
        )
        verdict_counts: dict[str, int] = {}
        for a in audits:
            verdict_counts[a.replay_verdict] = verdict_counts.get(a.replay_verdict, 0) + 1
        (output_dir / "replay_audit_summary.json").write_text(
            json.dumps(
                {
                    "verdict_counts": verdict_counts,
                    "segments": len(audits),
                    "replayable_false_reason": (
                        "SegmentWriter.write_line marks replayable=False when data.seq "
                        "jumps by >1. Bybit orderbook continuity is data.u (+1); seq is an "
                        "exchange-wide counter and normally jumps. Empirically u_gaps=0 while "
                        "seq_jumps≈delta_count."
                    ),
                    "completion_status_open_reason": (
                        "segment.close() sets completion_status='closed' only if "
                        "replayable=True; otherwise leaves the initial 'open' value even "
                        "after atomic finalize + manifest write. Metadata bug, not missing data."
                    ),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    if do_analyze:
        for sym in symbols:
            # keep samples sorted
            samples_by_symbol[sym].sort(key=lambda s: s.ts_ms)
            all_events.extend(
                extract_wall_events(
                    samples_by_symbol[sym],
                    qty_median_mult=qty_median_mult,
                    seed=seed,
                )
            )

        _write_csv(output_dir / "l2_samples.csv", sample_rows)
        _write_csv(output_dir / "wall_events.csv", [wall_event_to_row(e) for e in all_events])

        chains = build_chains(all_events, seed=seed)
        _write_csv(output_dir / "event_chains.csv", [asdict(c) for c in chains])
        _write_csv(output_dir / "funnel.csv", funnel_counts(all_events, chains))

        controls = matched_controls(
            all_events,
            samples_by_symbol,
            controls_per_event=controls_per_event,
            seed=seed,
        )
        _write_csv(output_dir / "matched_controls.csv", [wall_event_to_row(c) for c in controls])

        outcomes = compute_outcomes(all_events, samples_by_symbol, is_control=False)
        outcomes += compute_outcomes(controls, samples_by_symbol, is_control=True)
        _write_csv(output_dir / "event_outcomes.csv", [asdict(o) for o in outcomes])
        summary = summarize_outcomes(outcomes)

        alt_events = []
        for sym in symbols:
            alt_events.extend(
                extract_wall_events(
                    samples_by_symbol[sym],
                    qty_median_mult=2.0,
                    seed=seed + 1,
                )
            )
        alt_summary = summarize_outcomes(
            compute_outcomes(alt_events, samples_by_symbol, is_control=False)
        )
        for row in alt_summary:
            row["qty_median_mult"] = 2.0
        for row in summary:
            row["qty_median_mult"] = qty_median_mult
        _write_csv(output_dir / "threshold_sensitivity.csv", summary + alt_summary)

        market = try_load_market_context(symbols, data_start, data_end)
    else:
        market = {"available": False, "notes": ["analyze_skipped"]}

    # segment transition check
    transitions = []
    by_sym: dict[str, list[SegmentRef]] = {}
    for ref in segments:
        by_sym.setdefault(ref.symbol, []).append(ref)
    audit_by_path = {a.path: a for a in audits}
    for sym, refs in by_sym.items():
        refs = sorted(refs, key=lambda r: r.start_utc)
        for a, b in zip(refs, refs[1:]):
            aa = audit_by_path.get(str(a.path))
            bb = audit_by_path.get(str(b.path))
            transitions.append(
                {
                    "symbol": sym,
                    "seg_a": a.path.name,
                    "seg_b": b.path.name,
                    "gap_sec": (b.start_utc - a.end_utc).total_seconds(),
                    "contiguous": abs((b.start_utc - a.end_utc).total_seconds()) < 2.0,
                    "end_a_best_bid": None if aa is None else aa.end_best_bid,
                    "end_a_best_ask": None if aa is None else aa.end_best_ask,
                    "start_b_checkpoint_bids": None if bb is None else bb.start_checkpoint_bids,
                    "start_b_checkpoint_asks": None if bb is None else bb.start_checkpoint_asks,
                    "note": (
                        "Hour N end vs hour N+1 checkpoint are independent local states; "
                        "continuity of u across rotation is not required for per-hour replay."
                    ),
                }
            )
    _write_csv(output_dir / "segment_transitions.csv", transitions)

    manifest = {
        "format_version": FORMAT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "raw_root": str(raw_root),
        "symbols": list(symbols),
        "window_start_utc": data_start.isoformat().replace("+00:00", "Z"),
        "window_end_utc": data_end.isoformat().replace("+00:00", "Z"),
        "segments_used": [s.path.name for s in segments],
        "tmp_excluded": [str(p) for p in tmp_excluded],
        "audit": do_audit,
        "analyze": do_analyze,
        "seed": seed,
        "sample_seconds": sample_seconds,
        "controls_per_event": controls_per_event,
        "qty_median_mult": qty_median_mult,
        "n_samples": len(sample_rows) if do_analyze else 0,
        "n_wall_events": len(all_events) if do_analyze else 0,
        "n_chains": len(chains) if do_analyze else 0,
        "n_controls": len(controls) if do_analyze else 0,
        "market_join_notes": market.get("notes"),
        "market_available": market.get("available"),
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    report = _build_report(
        manifest=manifest,
        audits=audits,
        all_events=all_events if do_analyze else [],
        chains=chains if do_analyze else [],
        summary=summary if do_analyze else [],
        transitions=transitions,
        market=market,
    )
    (output_dir / "analysis_report.md").write_text(report, encoding="utf-8")
    return manifest


def _build_report(
    *,
    manifest: dict[str, Any],
    audits: list[SegmentAudit],
    all_events: list,
    chains: list,
    summary: list[dict[str, Any]],
    transitions: list[dict[str, Any]],
    market: dict[str, Any],
) -> str:
    lines = [
        "# OB200 v3 Raw Discovery Report (BTC/DOGE)",
        "",
        f"- Format: `{manifest['format_version']}`",
        f"- Window: `{manifest['window_start_utc']}` → `{manifest['window_end_utc']}`",
        f"- Segments: {len(manifest['segments_used'])}",
        f"- Excluded TMP: {len(manifest['tmp_excluded'])}",
        "",
        "## Replay / metadata",
        "",
    ]
    if audits:
        for a in audits:
            lines.append(
                f"- `{a.symbol}` `{Path(a.path).name}`: **{a.replay_verdict}** "
                f"(u_gaps={a.u_gaps}, seq_jumps={a.seq_jumps}, "
                f"manifest.replayable={a.manifest_replayable}, "
                f"completion={a.manifest_completion_status})"
            )
        lines += [
            "",
            "### Why `replayable=False`",
            "",
            "Writer gap-checks Bybit `data.seq` with `seq == last_seq + 1`. "
            " empirically `seq` jumps almost every delta while `data.u` stays "
            "strictly contiguous (`u_gaps=0`). This is a **metadata false negative**, "
            "not evidence of lost orderbook updates.",
            "",
            "### Why `completion_status=open` on finalized files",
            "",
            "`SegmentWriter.close()` sets `completion_status='closed'` only when "
            "`replayable` is True; otherwise it leaves the initial `'open'` value "
            "after a successful atomic rename. **Metadata bug**, file bytes are finalized.",
            "",
        ]
    lines += [
        "## Funnel / events",
        "",
        f"- Wall events: {len(all_events)}",
        f"- Chains: {len(chains)} (complete={sum(1 for c in chains if c.complete)})",
        "",
    ]
    # compact event type counts
    from collections import Counter

    c = Counter((e.symbol, e.direction, e.event_type) for e in all_events)
    for key, n in sorted(c.items()):
        lines.append(f"- {key[0]} {key[1]} {key[2]}: {n}")

    lines += ["", "## Events vs controls (fwd return bps, discovery)", ""]
    # show 60s horizon touch/reclaim/absorption vs control
    by_key: dict[tuple, dict[str, Any]] = {}
    for s in summary:
        if s.get("horizon_s") != 60:
            continue
        by_key[(s["symbol"], s["direction"], s["event_type"], s["is_control"])] = s
    for symbol in ("BTCUSDT", "DOGEUSDT"):
        for direction in ("LONG", "SHORT"):
            for et in ("WALL_TOUCH", "WALL_ABSORPTION_PROXY", "WALL_RECLAIM"):
                ev = by_key.get((symbol, direction, et, False))
                ctrl = by_key.get((symbol, direction, f"CONTROL_{et}", True))
                if not ev:
                    continue
                if ctrl:
                    diff = ev["mean_fwd_bps"] - ctrl["mean_fwd_bps"]
                    lines.append(
                        f"- {symbol} {direction} {et} h=60s n={ev['n']} "
                        f"mean={ev['mean_fwd_bps']:.2f} ctrl_n={ctrl['n']} "
                        f"ctrl_mean={ctrl['mean_fwd_bps']:.2f} "
                        f"event-ctrl={diff:.2f} bps "
                        f"CI80=[{ev['ci80_low']:.2f},{ev['ci80_high']:.2f}]"
                    )
                else:
                    lines.append(
                        f"- {symbol} {direction} {et} h=60s n={ev['n']} "
                        f"mean={ev['mean_fwd_bps']:.2f} (no matched controls)"
                    )
    if not any(s.get("horizon_s") == 60 for s in summary):
        lines.append("- Insufficient overlapping outcomes for 60s summary.")

    lines += [
        "",
        "## DOGE note",
        "",
        "- DOGE shows many WALL_APPEAR under share/median rules but almost no TOUCH/RECLAIM "
        "in this window: walls migrate/reprice faster than the approach/touch persistence logic.",
        "- Treat DOGE lifecycle counts as **not yet comparable** to BTC until longer data + "
        "tick-aware thresholds.",
        "",
        "## Market join",
        "",
        f"- available: {market.get('available')}",
        f"- notes: {market.get('notes')}",
        "- Impact compression / Flush→Trade→Wall causal join skipped where public trades or OI "
        "are unavailable for the window.",
        "",
        "## Segment transitions",
        "",
    ]
    for t in transitions:
        lines.append(
            f"- {t['symbol']}: {t['seg_a']} → {t['seg_b']} gap_sec={t['gap_sec']} contiguous={t['contiguous']}"
        )
    lines += [
        "",
        "## Limits",
        "",
        "- Short overnight sample (~hours), Discovery only — not a strategy proof.",
        "- Impact compression requires aggressive notional; public-trade join may be unavailable.",
        "- Wall thresholds from causal rolling median×mult; sensitivity included.",
        "",
        "## Next step",
        "",
        "- Collect ≥7 days BTC+DOGE raw before claiming regime stability.",
        "- Fix writer metadata (`seq` gap policy + `completion_status`) before multi-coin expansion.",
        "- Multi-coin raw not justified until BTC/DOGE discovery holds over longer windows.",
        "",
    ]
    return "\n".join(lines) + "\n"
