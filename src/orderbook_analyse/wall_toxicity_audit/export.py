"""Export wall toxicity audit artefacts."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from orderbook_analyse.wall_toxicity_audit.types import AUDIT_VERSION


OUTPUT_FILES = (
    "wall_toxicity_summary.csv",
    "wall_level_events.csv",
    "wall_migration_events.csv",
    "wall_trade_alignment.csv",
    "wall_toxicity_report.json",
    "params.json",
)


def ensure_output_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    if fieldnames is None:
        fields: list[str] = []
        seen: set[str] = set()
        for row in rows:
            for k in row:
                if k not in seen:
                    seen.add(k)
                    fields.append(k)
    else:
        fields = list(fieldnames)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields or ["_empty"])
        w.writeheader()
        for row in rows:
            w.writerow(row)


def write_audit_outputs(
    output_dir: Path,
    *,
    bundle: Any,
    wall_sequences_csv: str,
) -> None:
    out = ensure_output_dir(output_dir)
    seq = bundle.sequence
    res = bundle.result
    pull = res.pull
    mig = res.migration
    market = res.market
    sc = res.score_components

    summary = [
        {
            "audit_version": AUDIT_VERSION,
            "symbol": seq.symbol,
            "wall_sequence_id": seq.wall_sequence_id,
            "side": seq.side,
            "resolution": seq.resolution,
            "first_seen_ts": seq.first_seen_ts.isoformat(),
            "closed_ts": None if seq.closed_ts is None else seq.closed_ts.isoformat(),
            "primary_bucket_price": bundle.bucket["primary_bucket_price"],
            "band_low": bundle.bucket["band_low"],
            "band_high": bundle.bucket["band_high"],
            "analysis_low": bundle.bucket["analysis_low"],
            "analysis_high": bundle.bucket["analysis_high"],
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
            "pull_events_near_touch": pull.pull_events_near_touch,
            "trade_qty_in_bucket": pull.trade_qty_in_bucket,
            "trade_count_in_bucket": pull.trade_count_in_bucket,
            "migration_event_count": mig.migration_event_count,
            "migrated_qty": mig.migrated_qty,
            "migration_ratio": mig.migration_ratio,
            "median_migration_delay_ms": mig.median_migration_delay_ms,
            "median_migration_distance_ticks": mig.median_migration_distance_ticks,
            "moved_toward_market_qty": mig.moved_toward_market_qty,
            "moved_away_from_market_qty": mig.moved_away_from_market_qty,
            "oscillating_liquidity_count": mig.oscillating_liquidity_count,
            "min_distance_bps": market.min_distance_bps,
            "bucket_touched": market.bucket_touched,
            "trades_in_bucket": market.trades_in_bucket,
            "removed_before_touch": market.removed_before_touch,
            "remained_remote": market.remained_remote,
            "persistence_score": sc.persistence_score,
            "executed_ratio_score": sc.executed_ratio_score,
            "absorption_score": sc.absorption_score,
            "refill_score": sc.refill_score,
            "cancellation_before_touch_score": sc.cancellation_before_touch_score,
            "order_chasing_score": sc.order_chasing_score,
            "layering_score": sc.layering_score,
            "remote_migration_score": sc.remote_migration_score,
            "notes": res.notes,
            "wall_sequences_csv": wall_sequences_csv,
            "disclaimer": (
                "spoofing_suspicion is not proof of spoofing; "
                "migration matching is quantity/time based only"
            ),
        }
    ]
    _write_csv(out / "wall_toxicity_summary.csv", summary)

    level_rows = [
        {
            "ts": e.ts.isoformat(),
            "symbol": e.symbol,
            "side": e.side,
            "price": e.price,
            "previous_qty": e.previous_qty,
            "new_qty": e.new_qty,
            "qty_change": e.qty_change,
            "message_type": e.message_type,
            "update_id": e.update_id,
            "cross_sequence": e.cross_sequence,
            "incomplete_initial": e.incomplete_initial,
            "snapshot_boundary": e.snapshot_boundary,
            "in_primary_bucket": e.in_primary_bucket,
        }
        for e in bundle.level_events
    ]
    _write_csv(out / "wall_level_events.csv", level_rows)

    mig_rows = [
        {
            "ts_remove": m.ts_remove.isoformat(),
            "ts_add": m.ts_add.isoformat(),
            "delay_ms": m.delay_ms,
            "side": m.side,
            "price_from": m.price_from,
            "price_to": m.price_to,
            "distance_ticks": m.distance_ticks,
            "removed_qty": m.removed_qty,
            "added_qty": m.added_qty,
            "matched_qty": m.matched_qty,
            "toward_market": m.toward_market,
            "mid_at_event": m.mid_at_event,
            "trade_explained_qty": m.trade_explained_qty,
        }
        for m in bundle.migrations
    ]
    _write_csv(out / "wall_migration_events.csv", mig_rows)

    trade_rows = [
        {
            "ts": t.ts.isoformat(),
            "symbol": t.symbol,
            "side": t.side,
            "price": t.price,
            "qty": t.qty,
            "notional": t.notional,
            "in_bucket": t.in_bucket,
            "aggressive_vs_wall": t.aggressive_vs_wall,
        }
        for t in bundle.trade_rows
    ]
    _write_csv(
        out / "wall_trade_alignment.csv",
        trade_rows,
        fieldnames=[
            "ts",
            "symbol",
            "side",
            "price",
            "qty",
            "notional",
            "in_bucket",
            "aggressive_vs_wall",
        ],
    )

    report = {
        "audit_version": AUDIT_VERSION,
        "summary": summary[0],
        "params": bundle.params.to_dict(),
        "bucket": bundle.bucket,
        "n_level_events": len(bundle.level_events),
        "n_migrations": len(bundle.migrations),
        "n_trades_aligned": len(bundle.trade_rows),
        "classification": res.classification.value,
        "reliability_score": res.reliability_score,
        "toxicity_score": res.toxicity_score,
        "spoofing_suspicion": res.spoofing_suspicion.value,
        "score_components": asdict(sc),
        "caveats": [
            "orderbook_deltas.quantity is absolute resting size, not an incremental delta",
            "qty_change = new_quantity - previous_quantity",
            "migration events are quantity/time associations, not order-id identity",
            "spoofing_suspicion is not a legal or exchange finding",
        ],
    }
    (out / "wall_toxicity_report.json").write_text(
        json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8"
    )
    (out / "params.json").write_text(
        json.dumps(bundle.params.to_dict(), indent=2) + "\n", encoding="utf-8"
    )
