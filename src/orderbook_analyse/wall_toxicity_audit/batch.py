"""Batch wall toxicity + forward outcome evaluation."""

from __future__ import annotations

import logging
import resource
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

from orderbook_analyse.wall_toxicity_audit.aggregates import (
    aggregate_baselines,
    aggregate_groups,
    assign_detail_groups,
)
from orderbook_analyse.wall_toxicity_audit.analysis import (
    AuditBundle,
    run_wall_toxicity_audit,
)
from orderbook_analyse.wall_toxicity_audit.batch_export import write_batch_outputs
from orderbook_analyse.wall_toxicity_audit.data_access import (
    ensure_utc,
    load_price_series,
    load_wall_sequences_from_csv,
    open_readonly_db,
    parse_utc,
    sequence_csv_time_span,
)
from orderbook_analyse.wall_toxicity_audit.data_quality import (
    assess_data_quality,
    detect_update_id_gap,
)
from orderbook_analyse.wall_toxicity_audit.level_state import iter_complete_changes
from orderbook_analyse.wall_toxicity_audit.outcomes import (
    evaluate_forward_path,
    evaluate_wall_role,
    outcome_row,
)
from orderbook_analyse.wall_toxicity_audit.types import (
    AUDIT_VERSION,
    OutcomeParams,
    WallSequenceRef,
    WallToxicityParams,
)

logger = logging.getLogger(__name__)


@dataclass
class BatchResult:
    details: list[dict[str, Any]]
    outcomes: list[dict[str, Any]]
    quality: list[dict[str, Any]]
    errors: list[dict[str, Any]]
    group_summary: list[dict[str, Any]]
    baselines: list[dict[str, Any]]
    summary: dict[str, Any]


def _bundle_detail_row(
    bundle: AuditBundle,
    *,
    wall_sequences_csv: str,
    quality: dict[str, Any],
    outcome_params: OutcomeParams,
) -> dict[str, Any]:
    seq = bundle.sequence
    res = bundle.result
    pull = res.pull
    mig = res.migration
    market = res.market
    sc = res.score_components
    age = None
    if seq.raw.get("age_seconds") not in (None, ""):
        age = float(seq.raw["age_seconds"])
    elif seq.closed_ts is not None:
        age = (seq.closed_ts - seq.first_seen_ts).total_seconds()
    else:
        age = (seq.last_seen_ts - seq.first_seen_ts).total_seconds()
    is_near = bool(
        market.min_distance_bps is not None
        and market.min_distance_bps <= bundle.params.near_market_bps
    ) or bool(seq.was_near_price)
    row = {
        "audit_version": AUDIT_VERSION,
        "symbol": seq.symbol,
        "wall_sequence_id": seq.wall_sequence_id,
        "segment_id": seq.segment_id,
        "side": seq.side,
        "resolution": seq.resolution,
        "first_seen_ts": seq.first_seen_ts.isoformat(),
        "last_seen_ts": seq.last_seen_ts.isoformat(),
        "closed_ts": None if seq.closed_ts is None else seq.closed_ts.isoformat(),
        "age_seconds": age,
        "end_reason": seq.end_reason,
        "was_tested": seq.was_tested,
        "disappeared_before_test": seq.disappeared_before_test,
        "primary_bucket_price": bundle.bucket["primary_bucket_price"],
        "band_low": bundle.bucket["band_low"],
        "band_high": bundle.bucket["band_high"],
        "classification": res.classification.value,
        "reliability_score": res.reliability_score,
        "toxicity_score": res.toxicity_score,
        "spoofing_suspicion": res.spoofing_suspicion.value,
        "gross_removed_qty": pull.gross_removed_qty,
        "gross_added_qty": pull.gross_added_qty,
        "net_bucket_change": pull.net_bucket_change,
        "removed_without_trade_qty": pull.removed_without_trade_qty,
        "removed_without_trade_ratio": pull.removed_without_trade_ratio,
        "large_pull_count": pull.large_pull_count,
        "largest_single_pull_qty": pull.largest_single_pull_qty,
        "largest_single_pull_pct": pull.largest_single_pull_pct,
        "pull_events_before_touch": pull.pull_events_before_touch,
        "trade_qty_in_bucket": pull.trade_qty_in_bucket,
        "trade_count_in_bucket": pull.trade_count_in_bucket,
        "migration_event_count": mig.migration_event_count,
        "migrated_qty": mig.migrated_qty,
        "migration_ratio": mig.migration_ratio,
        "oscillating_liquidity_count": mig.oscillating_liquidity_count,
        "min_distance_bps": market.min_distance_bps,
        "bucket_touched": market.bucket_touched,
        "trades_in_bucket": market.trades_in_bucket,
        "removed_before_touch": market.removed_before_touch,
        "remained_remote": market.remained_remote,
        "is_near_market": is_near,
        "persistence_score": sc.persistence_score,
        "executed_ratio_score": sc.executed_ratio_score,
        "absorption_score": sc.absorption_score,
        "cancellation_before_touch_score": sc.cancellation_before_touch_score,
        "order_chasing_score": sc.order_chasing_score,
        "remote_migration_score": sc.remote_migration_score,
        "notes": res.notes,
        "wall_sequences_csv": wall_sequences_csv,
        "n_level_events": len(bundle.level_events),
        **quality,
    }
    row.update(assign_detail_groups(row, params=outcome_params))
    return row


def _reference_times(
    bundle: AuditBundle,
) -> dict[str, datetime | None]:
    seq = bundle.sequence
    first_large_pull = None
    for ev in iter_complete_changes(bundle.level_events):
        ch = float(ev.qty_change or 0.0)
        if ch >= 0:
            continue
        removed = -ch
        prev = float(ev.previous_qty or 0.0)
        pct = (removed / prev * 100.0) if prev > 0 else 0.0
        if (
            removed >= bundle.params.large_pull_min_qty
            or pct >= bundle.params.large_pull_min_pct
        ):
            first_large_pull = ev.ts
            break
    first_migration = bundle.migrations[0].ts_remove if bundle.migrations else None
    first_touch = None
    # Approximate touch time from market flag + sequence first_test_ts if present
    raw_touch = seq.raw.get("first_test_ts")
    if raw_touch:
        first_touch = parse_utc(str(raw_touch))
    return {
        "FROM_FIRST_SEEN": seq.first_seen_ts,
        "FROM_LAST_ACTIVE": seq.last_seen_ts,
        "FROM_CLASSIFICATION": seq.closed_ts or seq.last_seen_ts,
        "FROM_FIRST_TOUCH": first_touch,
        "disappeared_at": seq.closed_ts,
        "first_large_pull": first_large_pull,
        "first_migration": first_migration,
        "classification_time": seq.closed_ts or seq.last_seen_ts,
    }


def _outcomes_for_bundle(
    bundle: AuditBundle,
    *,
    price_series: Sequence[tuple[datetime, float]],
    outcome_params: OutcomeParams,
    outcome_eligible: bool,
) -> list[dict[str, Any]]:
    refs = _reference_times(bundle)
    export_refs = ["FROM_FIRST_SEEN", "FROM_LAST_ACTIVE", "FROM_CLASSIFICATION"]
    if refs.get("FROM_FIRST_TOUCH") is not None or bundle.result.market.bucket_touched:
        export_refs.append("FROM_FIRST_TOUCH")
    rows: list[dict[str, Any]] = []
    band_low = bundle.bucket["band_low"]
    band_high = bundle.bucket["band_high"]
    for ref_name in export_refs:
        ref_ts = refs.get(ref_name)
        if ref_ts is None:
            # For FROM_FIRST_TOUCH without explicit ts, scan from first_seen
            if ref_name == "FROM_FIRST_TOUCH":
                # derive first touch inside forward from first_seen using role eval later
                ref_ts = bundle.sequence.first_seen_ts
            else:
                continue
        for horizon in outcome_params.forward_seconds:
            path = evaluate_forward_path(
                price_series,
                reference_ts=ref_ts,
                horizon_seconds=float(horizon),
                params=outcome_params,
            )
            role = evaluate_wall_role(
                price_series,
                reference_ts=ref_ts,
                horizon_seconds=float(horizon),
                side=bundle.sequence.side,
                band_low=band_low,
                band_high=band_high,
                params=outcome_params,
            )
            # If FROM_FIRST_TOUCH requested but touch happens after first_seen,
            # re-anchor when we discover touch time from role at FIRST_SEEN.
            if ref_name == "FROM_FIRST_TOUCH" and refs.get("FROM_FIRST_TOUCH") is None:
                if role.time_to_first_touch_seconds is not None:
                    touch_ts = bundle.sequence.first_seen_ts + timedelta(
                        seconds=role.time_to_first_touch_seconds
                    )
                    path = evaluate_forward_path(
                        price_series,
                        reference_ts=touch_ts,
                        horizon_seconds=float(horizon),
                        params=outcome_params,
                    )
                    role = evaluate_wall_role(
                        price_series,
                        reference_ts=touch_ts,
                        horizon_seconds=float(horizon),
                        side=bundle.sequence.side,
                        band_low=band_low,
                        band_high=band_high,
                        params=outcome_params,
                    )
                    ref_ts = touch_ts
                else:
                    continue
            row = outcome_row(
                sequence_id=bundle.sequence.wall_sequence_id,
                symbol=bundle.sequence.symbol,
                side=bundle.sequence.side,
                reference_point=ref_name,
                reference_ts=ref_ts,
                horizon_seconds=int(horizon),
                path=path,
                role=role,
            )
            row["outcome_eligible"] = bool(outcome_eligible)
            rows.append(row)
    return rows


def run_wall_toxicity_batch(
    *,
    symbol: str,
    wall_sequences_csv: Path,
    output_dir: Path,
    toxicity_params: WallToxicityParams | None = None,
    outcome_params: OutcomeParams | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int | None = None,
    sequence_status: str | None = None,
    continue_on_error: bool = True,
    overwrite: bool = False,
    price_series: Sequence[tuple[datetime, float]] | None = None,
    bundles_override: Sequence[AuditBundle] | None = None,
) -> BatchResult:
    """Analyze many wall sequences; one sequence at a time (streaming)."""
    t0 = time.time()
    toxicity_params = toxicity_params or WallToxicityParams()
    outcome_params = outcome_params or OutcomeParams()
    # Align touch_bps across toxicity + outcome defaults when caller sets near/touch
    if outcome_params.touch_bps != toxicity_params.touch_bps:
        # Prefer explicit outcome touch; keep toxicity params as-is for audit.
        pass

    out = Path(output_dir)
    if out.exists() and any(out.iterdir()) and not overwrite:
        raise FileExistsError(f"output_dir not empty (pass --overwrite): {out}")
    out.mkdir(parents=True, exist_ok=True)

    sequences = load_wall_sequences_from_csv(
        wall_sequences_csv,
        symbol=symbol,
        start=start,
        end=end,
        limit=limit,
        sequence_status=sequence_status,
    )
    if bundles_override is None and not sequences:
        raise RuntimeError(f"no sequences selected from {wall_sequences_csv}")
    if bundles_override is not None:
        sequences = [b.sequence for b in bundles_override]
    csv_start, csv_end = sequence_csv_time_span(wall_sequences_csv)
    span_start = start or csv_start or sequences[0].first_seen_ts
    span_end = end or csv_end or (sequences[-1].closed_ts or sequences[-1].last_seen_ts)
    max_fwd = max(outcome_params.forward_seconds)
    price_end = ensure_utc(span_end) + timedelta(seconds=max_fwd + 60)

    details: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    quality_rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    db = None
    try:
        if bundles_override is None and price_series is None:
            db = open_readonly_db()
            logger.info(
                "loading price series %s → %s",
                ensure_utc(span_start).isoformat(),
                price_end.isoformat(),
            )
            price_series = load_price_series(
                db,
                symbol=symbol,
                start=ensure_utc(span_start) - timedelta(seconds=60),
                end=price_end,
            )
            logger.info("price samples=%d", len(price_series))

        price_series = list(price_series or [])

        if bundles_override is not None:
            iterable: list[tuple[WallSequenceRef, AuditBundle | None]] = [
                (b.sequence, b) for b in bundles_override
            ]
        else:
            iterable = [(s, None) for s in sequences]

        for i, (seq, preset) in enumerate(iterable, start=1):
            logger.info(
                "[%d/%d] %s", i, len(iterable), seq.wall_sequence_id
            )
            try:
                if preset is not None:
                    bundle = preset
                else:
                    # Slice shared price series for quotes/mids window of this seq
                    bundle = run_wall_toxicity_audit(
                        symbol=symbol,
                        sequence_id=seq.wall_sequence_id,
                        output_dir=None,
                        params=toxicity_params,
                        wall_sequences_csv=wall_sequences_csv,
                        write_outputs=False,
                        db=db,
                        sequence=seq,
                        mids=price_series,
                        quotes=None,  # load per-seq quotes still for distance
                    )
                n_incomplete = sum(1 for e in bundle.level_events if e.incomplete_initial)
                n_snap = sum(1 for e in bundle.level_events if e.snapshot_boundary)
                gap = detect_update_id_gap(bundle.level_events)
                # provisional outcomes to see completeness
                tmp_out = _outcomes_for_bundle(
                    bundle,
                    price_series=price_series,
                    outcome_params=outcome_params,
                    outcome_eligible=True,
                )
                any_complete = any(r.get("forward_data_complete") for r in tmp_out)
                dq = assess_data_quality(
                    sequence_id=seq.wall_sequence_id,
                    symbol=symbol,
                    n_level_events=len(bundle.level_events),
                    n_incomplete_initial=n_incomplete,
                    n_snapshot_boundaries=n_snap,
                    n_ticker_samples=len(price_series),
                    n_trades_loaded=len(bundle.trades),
                    update_id_gap=gap,
                    ended_by_segment=str(seq.raw.get("ended_by_segment") or "")
                    .strip()
                    .lower()
                    in {"1", "true", "t", "yes"},
                    forward_any_complete=any_complete,
                    bucket_ok=bundle.bucket.get("bucket_size", 0) > 0,
                )
                qrow = dq.to_row(seq.wall_sequence_id, symbol)
                quality_rows.append(qrow)
                detail = _bundle_detail_row(
                    bundle,
                    wall_sequences_csv=str(wall_sequences_csv),
                    quality=qrow,
                    outcome_params=outcome_params,
                )
                details.append(detail)
                outs = _outcomes_for_bundle(
                    bundle,
                    price_series=price_series,
                    outcome_params=outcome_params,
                    outcome_eligible=dq.outcome_eligible,
                )
                for r in outs:
                    if not dq.outcome_eligible:
                        r["outcome_eligible"] = False
                    outcomes.append(r)
                # Drop heavy event lists from memory
                bundle.level_events.clear()
                bundle.migrations.clear()
                bundle.trade_rows.clear()
                bundle.trades.clear()
            except Exception as exc:  # noqa: BLE001
                logger.exception("sequence failed: %s", seq.wall_sequence_id)
                errors.append(
                    {
                        "wall_sequence_id": seq.wall_sequence_id,
                        "symbol": symbol,
                        "error": str(exc),
                        "traceback": traceback.format_exc(limit=5),
                    }
                )
                if not continue_on_error:
                    raise

    finally:
        if db is not None:
            db.close()

    group_summary = aggregate_groups(
        outcomes, details, params=outcome_params, reference_point="FROM_CLASSIFICATION"
    )
    baselines = aggregate_baselines(
        outcomes, details, params=outcome_params, reference_point="FROM_CLASSIFICATION"
    )

    elapsed = time.time() - t0
    maxrss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    from collections import Counter

    class_counts = Counter(d.get("classification") for d in details)
    dq_counts = Counter(q.get("data_quality_status") for q in quality_rows)
    summary = {
        "audit_version": AUDIT_VERSION,
        "symbol": symbol,
        "wall_sequences_csv": str(wall_sequences_csv),
        "n_sequences_selected": len(sequences) if bundles_override is None else len(bundles_override),
        "n_analyzed": len(details),
        "n_errors": len(errors),
        "n_outcome_eligible": sum(1 for d in details if d.get("outcome_eligible")),
        "classification_counts": dict(class_counts),
        "data_quality_counts": dict(dq_counts),
        "n_near": sum(1 for d in details if d.get("is_near_market")),
        "n_remote": sum(1 for d in details if not d.get("is_near_market")),
        "n_touched": sum(1 for d in details if d.get("bucket_touched") or d.get("was_tested")),
        "n_untouched": sum(
            1 for d in details if not (d.get("bucket_touched") or d.get("was_tested"))
        ),
        "elapsed_seconds": round(elapsed, 3),
        "maxrss_mb": round(maxrss_mb, 3),
        "toxicity_params": toxicity_params.to_dict(),
        "outcome_params": outcome_params.to_dict(),
        "continue_on_error": continue_on_error,
    }
    result = BatchResult(
        details=details,
        outcomes=outcomes,
        quality=quality_rows,
        errors=errors,
        group_summary=group_summary,
        baselines=baselines,
        summary=summary,
    )
    write_batch_outputs(out, result=result, outcome_params=outcome_params)
    return result
