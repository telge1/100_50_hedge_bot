"""Chunked offline EXECUTION_WALL analysis (read-only ClickHouse)."""

from __future__ import annotations

import csv
import gc
import logging
import resource
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TextIO

from orderbook_analyse.bearish_orderbook_strategy_research.sample_source import (
    bootstrap_feed_chunked,
)
from orderbook_analyse.dynamic_wall_detector import ReadOnlyClickHouse, connect_readonly
from orderbook_analyse.execution_wall_detector.compare import (
    candidate_distance_distribution,
    distance_distribution,
    load_structure_csv_optional,
    structure_vs_execution_comparison,
)
from orderbook_analyse.execution_wall_detector.export import write_outputs
from orderbook_analyse.execution_wall_detector.interactions import (
    absorption_event_rows,
    forward_outcome_rows,
    toxicity_rows,
)
from orderbook_analyse.execution_wall_detector.local_score import (
    book_side_to_float_map,
    infer_tick_from_levels,
    score_near_levels,
)
from orderbook_analyse.execution_wall_detector.tracker import (
    ExecutionWallTracker,
    apply_price_break_checks,
)
from orderbook_analyse.execution_wall_detector.types import (
    DETECTOR_VERSION,
    ExecutionWallParams,
    ExecutionWallSequence,
)
from orderbook_analyse.live_level_watch import LiveBookFeed
from orderbook_analyse.orderbook_replay import ReplayError
from orderbook_analyse.wall_toxicity_audit.data_access import (
    ensure_utc,
    load_trades,
    load_ticker_mids,
)
from orderbook_analyse.wall_toxicity_audit.metrics import aggressive_side_for_wall

logger = logging.getLogger(__name__)


CASE_TYPES = (
    "NEAR_ASK_ABSORPTION_CANDIDATE",
    "NEAR_BID_ABSORPTION_CANDIDATE",
    "ASK_WALL_CONSUMED_BREAKOUT_ACCEPTED",
    "BID_WALL_CONSUMED_BREAKDOWN_ACCEPTED",
    "ASK_PULL_BEFORE_TOUCH",
    "BID_PULL_BEFORE_TOUCH",
    "FAILED_ASK_BREAKOUT",
    "FAILED_BID_BREAKDOWN",
)

_CANDIDATE_FIELDS = [
    "sample_ts",
    "symbol",
    "side",
    "price",
    "bucket_price",
    "level_qty",
    "level_notional",
    "distance_bps",
    "same_side_near_depth",
    "same_side_local_median_qty",
    "same_side_local_mean_qty",
    "same_side_local_percentile",
    "opposite_side_near_depth",
    "local_depth_share",
    "local_multiple",
    "book_imbalance_near",
    "level_rank_within_near_band",
    "band_label",
    "wall_type",
    "wall_scope",
]

_TRANSITION_FIELDS = [
    "wall_sequence_id",
    "transition_ts",
    "side",
    "transition_type",
    "price",
    "qty",
    "details",
]


class _CsvStream:
    def __init__(self, path: Path, fieldnames: list[str]) -> None:
        self.path = path
        self.fieldnames = fieldnames
        self._fh: TextIO = path.open("w", newline="", encoding="utf-8")
        self._w = csv.DictWriter(self._fh, fieldnames=fieldnames, extrasaction="ignore")
        self._w.writeheader()

    def write(self, row: dict[str, Any]) -> None:
        self._w.writerow(row)

    def close(self) -> None:
        self._fh.close()


@dataclass
class AnalysisResult:
    sequences: list[ExecutionWallSequence]
    output_paths: dict[str, Path]
    report: dict[str, Any]
    errors: list[dict[str, Any]] = field(default_factory=list)


def _rss_mib() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def _book_mid(feed: LiveBookFeed) -> tuple[float | None, float | None, float | None]:
    book = feed.book
    bb = book.best_bid()
    ba = book.best_ask()
    best_bid = float(bb) if bb is not None else None
    best_ask = float(ba) if ba is not None else None
    mid = None
    if best_bid is not None and best_ask is not None and best_bid > 0 and best_ask > 0:
        mid = (best_bid + best_ask) / 2.0
    return mid, best_bid, best_ask


def select_case_dumps(
    sequences: list[ExecutionWallSequence],
    *,
    break_events: list[dict[str, Any]],
) -> dict[str, Any]:
    dumps: dict[str, Any] = {}
    for side, label in (
        ("ask", "NEAR_ASK_ABSORPTION_CANDIDATE"),
        ("bid", "NEAR_BID_ABSORPTION_CANDIDATE"),
    ):
        hit = next(
            (
                s
                for s in sequences
                if s.side == side and s.absorption_candidate and (s.min_distance_bps or 999) <= 30
            ),
            None,
        )
        dumps[label] = _seq_dump(hit) if hit else {"present": False, "note": "not observed"}

    for side, label in (
        ("ask", "ASK_WALL_CONSUMED_BREAKOUT_ACCEPTED"),
        ("bid", "BID_WALL_CONSUMED_BREAKDOWN_ACCEPTED"),
    ):
        hit = next(
            (
                s
                for s in sequences
                if s.side == side
                and s.breakout_accepted
                and s.executed_qty_estimate >= 0.5 * max(s.peak_qty, 1e-9)
            ),
            None,
        )
        dumps[label] = _seq_dump(hit) if hit else {"present": False, "note": "not observed"}

    for side, label in (
        ("ask", "ASK_PULL_BEFORE_TOUCH"),
        ("bid", "BID_PULL_BEFORE_TOUCH"),
    ):
        hit = next(
            (s for s in sequences if s.side == side and s.pulled_before_touch),
            None,
        )
        dumps[label] = _seq_dump(hit) if hit else {"present": False, "note": "not observed"}

    for side, label, ev_label in (
        ("ask", "FAILED_ASK_BREAKOUT", "ASK_BREAKOUT_FAILED"),
        ("bid", "FAILED_BID_BREAKDOWN", "BID_BREAKDOWN_FAILED"),
    ):
        ev = next(
            (
                e
                for e in break_events
                if e.get("event_label") == ev_label and e.get("failed_break")
            ),
            None,
        )
        if ev is None:
            dumps[label] = {"present": False, "note": "not observed"}
        else:
            seq = next(
                (s for s in sequences if s.wall_sequence_id == ev["wall_sequence_id"]),
                None,
            )
            dumps[label] = {
                "present": True,
                "break_event": ev,
                "sequence": _seq_dump(seq) if seq else None,
            }
    for ct in CASE_TYPES:
        dumps.setdefault(ct, {"present": False, "note": "not observed"})
    return dumps


def _seq_dump(seq: ExecutionWallSequence | None) -> dict[str, Any]:
    if seq is None:
        return {"present": False}
    return {
        "present": True,
        "wall_sequence_id": seq.wall_sequence_id,
        "side": seq.side,
        "representative_price": seq.representative_price,
        "price_min": seq.price_min,
        "price_max": seq.price_max,
        "first_seen": seq.first_seen.isoformat() if seq.first_seen else None,
        "last_active": seq.last_active.isoformat() if seq.last_active else None,
        "lifetime_ms": seq.lifetime_ms,
        "peak_qty": seq.peak_qty,
        "min_distance_bps": seq.min_distance_bps,
        "touch_status": seq.touch_status,
        "terminal_state": seq.terminal_state,
        "executed_qty_estimate": seq.executed_qty_estimate,
        "pulled_before_touch": seq.pulled_before_touch,
        "absorption_candidate": seq.absorption_candidate,
        "breakout_accepted": seq.breakout_accepted,
        "breakout_failed": seq.breakout_failed,
        "transitions": seq.transitions[:40],
        "note": "compact dump; no order identity claimed",
    }


def run_execution_wall_detector(
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    output_dir: Path,
    params: ExecutionWallParams | None = None,
    db: ReadOnlyClickHouse | None = None,
    overwrite: bool = False,
) -> AnalysisResult:
    params = params or ExecutionWallParams()
    start = ensure_utc(start)
    end = ensure_utc(end)
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()) and not overwrite:
        raise FileExistsError(f"{output_dir} exists; pass overwrite=True")
    output_dir.mkdir(parents=True, exist_ok=True)

    owns_db = db is None
    db = db or connect_readonly()
    errors: list[dict[str, Any]] = []
    t0 = time.time()
    max_rss = _rss_mib()

    feed = LiveBookFeed(db, symbol)
    logger.info("bootstrap %s as_of=%s", symbol, start.isoformat())
    try:
        bootstrap_feed_chunked(feed, as_of=start)
    except Exception as exc:  # noqa: BLE001
        errors.append(
            {
                "error_ts": datetime.now(timezone.utc).isoformat(),
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "details": "bootstrap",
            }
        )
        raise

    bids = book_side_to_float_map(feed.book.bids)
    asks = book_side_to_float_map(feed.book.asks)
    tick = params.tick_size or infer_tick_from_levels(
        list(bids) + list(asks), fallback=0.0001
    )
    tracker = ExecutionWallTracker(symbol=symbol, params=params, tick=tick)
    tracker.set_segment("S0001")

    cand_stream = _CsvStream(output_dir / "execution_wall_candidates.csv", _CANDIDATE_FIELDS)
    trans_stream = _CsvStream(output_dir / "execution_wall_transitions.csv", _TRANSITION_FIELDS)
    cand_band_counts: dict[str, int] = defaultdict(int)

    def _on_candidate(row: dict[str, Any]) -> None:
        cand_stream.write(row)
        cand_band_counts[str(row.get("band_label") or "unknown")] += 1

    tracker.attach_writers(
        candidate_writer=_on_candidate,
        transition_writer=trans_stream.write,
    )

    interval = timedelta(milliseconds=params.sample_interval_ms)
    trade_window = timedelta(milliseconds=params.trade_match_window_ms)
    mid_series: list[tuple[datetime, float]] = []
    sample_count = 0
    gap_count = 0
    incomplete_book = 0

    chunk = timedelta(minutes=params.chunk_minutes)
    last_trade_fetch_end = start - timedelta(milliseconds=1)
    trade_buf: list[Any] = []

    t = start
    try:
        while t <= end:
            try:
                for _ in range(10_000):
                    applied = feed.load_deltas_until(t)
                    if applied == 0:
                        break
                else:
                    raise ReplayError(f"stuck draining deltas at {t.isoformat()}")
            except Exception as exc:  # noqa: BLE001 — ReplayError or CH read faults
                gap_count += 1
                errors.append(
                    {
                        "error_ts": t.isoformat(),
                        "error_type": type(exc).__name__,
                        "error_message": str(exc)[:500],
                        "details": "gap_or_storage_fault; attempting re-bootstrap",
                    }
                )
                # ClickHouse checksum/corrupt-part faults: skip forward rather than die.
                skip_ahead = interval
                if "CHECKSUM" in str(exc).upper() or "corrupted data" in str(exc).lower():
                    skip_ahead = max(interval, timedelta(minutes=1))
                try:
                    bootstrap_feed_chunked(feed, as_of=t + skip_ahead)
                    t = t + skip_ahead
                    continue
                except Exception as exc2:  # noqa: BLE001
                    errors.append(
                        {
                            "error_ts": t.isoformat(),
                            "error_type": type(exc2).__name__,
                            "error_message": str(exc2)[:500],
                            "details": "gap recover failed",
                        }
                    )
                    t = t + skip_ahead
                    continue

            mid, best_bid, best_ask = _book_mid(feed)
            if mid is None:
                incomplete_book += 1
                t = t + interval
                continue

            mid_series.append((t, mid))
            bids = book_side_to_float_map(feed.book.bids)
            asks = book_side_to_float_map(feed.book.asks)

            if t > last_trade_fetch_end:
                fetch_end = min(t + chunk, end)
                try:
                    trade_buf = load_trades(
                        db, symbol=symbol, start=last_trade_fetch_end, end=fetch_end
                    )
                    last_trade_fetch_end = fetch_end
                except Exception as exc:  # noqa: BLE001
                    errors.append(
                        {
                            "error_ts": t.isoformat(),
                            "error_type": type(exc).__name__,
                            "error_message": str(exc),
                            "details": "load_trades",
                        }
                    )
                    trade_buf = []

            prev_t = t - interval
            for tr in trade_buf:
                if tr.ts < prev_t - trade_window or tr.ts > t + trade_window:
                    continue
                if (
                    prev_t < tr.ts <= t
                    or abs((tr.ts - t).total_seconds()) <= trade_window.total_seconds()
                ):
                    side_l = tr.side.lower()
                    alignment = "OK"
                    if best_ask is not None and best_bid is not None:
                        if not (best_bid * 0.999 <= tr.price <= best_ask * 1.001):
                            alignment = "AMBIGUOUS"
                    if side_l in {"buy", "b"}:
                        tracker.apply_trade_hit(
                            side="ask",
                            price=tr.price,
                            qty=tr.qty,
                            ts=tr.ts,
                            mid=mid,
                            alignment_status=alignment,
                        )
                    elif side_l in {"sell", "s"}:
                        tracker.apply_trade_hit(
                            side="bid",
                            price=tr.price,
                            qty=tr.qty,
                            ts=tr.ts,
                            mid=mid,
                            alignment_status=alignment,
                        )

            ask_m = score_near_levels(
                side="ask",
                levels=asks,
                mid=mid,
                best_bid=best_bid,
                best_ask=best_ask,
                tick=tick,
                params=params,
                opposite_levels=bids,
            )
            bid_m = score_near_levels(
                side="bid",
                levels=bids,
                mid=mid,
                best_bid=best_bid,
                best_ask=best_ask,
                tick=tick,
                params=params,
                opposite_levels=asks,
            )
            tracker.on_sample(
                ts=t,
                mid=mid,
                metrics=list(ask_m) + list(bid_m),
                sample_interval_ms=params.sample_interval_ms,
            )
            tracker.update_attack_progress(mid=mid)
            sample_count += 1
            max_rss = max(max_rss, _rss_mib())

            if sample_count % 120 == 0:
                logger.info(
                    "progress t=%s samples=%d active=%d completed=%d rss=%.1fMiB",
                    t.isoformat(),
                    sample_count,
                    len(tracker.active),
                    len(tracker.completed),
                    max_rss,
                )
                gc.collect()

            t = t + interval

        tracker.finalize_open(end)
    finally:
        try:
            cand_stream.close()
        except Exception:
            pass
        try:
            trans_stream.close()
        except Exception:
            pass
        if tracker.transition_rows:
            late = output_dir / "execution_wall_transitions_late.csv"
            with late.open("w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=_TRANSITION_FIELDS, extrasaction="ignore")
                w.writeheader()
                for row in tracker.transition_rows:
                    w.writerow(row)

    try:
        ticker_mids = load_ticker_mids(
            db,
            symbol=symbol,
            start=start,
            end=end + timedelta(seconds=max(params.forward_seconds)),
        )
        if len(ticker_mids) > len(mid_series):
            merged = {ts: px for ts, px in mid_series}
            for ts, px in ticker_mids:
                merged.setdefault(ts, px)
            mid_series = sorted(merged.items(), key=lambda x: x[0])
    except Exception as exc:  # noqa: BLE001
        errors.append(
            {
                "error_ts": end.isoformat(),
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "details": "load_ticker_mids",
            }
        )

    sequences = list(tracker.completed)
    break_events = apply_price_break_checks(
        sequences, price_series=mid_series, params=params, tick=tick
    )
    for seq in sequences:
        if seq.breakout_accepted:
            seq.absorption_candidate = False

    absorption = absorption_event_rows(sequences)
    toxicity = toxicity_rows(sequences)
    forward = forward_outcome_rows(sequences, break_events)
    dist = distance_distribution(sequences, params=params)
    cand_dist = [
        {"band_label": k, "candidates": v}
        for k, v in sorted(cand_band_counts.items(), key=lambda kv: kv[0])
    ]
    if not cand_dist:
        cand_dist = candidate_distance_distribution([], params=params)

    structure_csv = load_structure_csv_optional(params.structure_sequences_csv, symbol)
    comparison = structure_vs_execution_comparison(
        execution=sequences,
        structure_csv=structure_csv,
        start=start,
        end=end,
        symbol=symbol,
    )

    n = len(sequences)
    touches = sum(1 for s in sequences if s.touch_status == "TOUCHED" or s.touch_time)
    pulled = sum(1 for s in sequences if s.pulled_before_touch)
    executed = sum(1 for s in sequences if s.executed_qty_estimate > 0)
    absorp_n = sum(1 for s in sequences if s.absorption_candidate)
    accepted = sum(1 for s in sequences if s.breakout_accepted)
    failed = sum(1 for s in sequences if s.breakout_failed)

    top_bands = sorted(dist, key=lambda r: (r.get("touches") or 0), reverse=True)
    top_labels = [r["band_label"] for r in top_bands[:3] if (r.get("touches") or 0) > 0]

    data_quality = [
        {"metric": "sample_count", "value": sample_count, "ok": sample_count > 0},
        {
            "metric": "gap_count",
            "value": gap_count,
            "ok": gap_count < max(1, sample_count // 10),
        },
        {
            "metric": "incomplete_book_samples",
            "value": incomplete_book,
            "ok": incomplete_book < max(1, sample_count // 5) if sample_count else False,
        },
        {
            "metric": "mid_series_points",
            "value": len(mid_series),
            "ok": len(mid_series) >= max(10, sample_count // 2),
        },
        {"metric": "tick_size", "value": tick, "ok": tick > 0},
        {
            "metric": "structure_csv",
            "value": str(structure_csv) if structure_csv else None,
            "ok": structure_csv is not None,
        },
        {"metric": "touch_coverage_vs_structure", "value": touches, "ok": True},
    ]
    dq_ok = all(bool(r["ok"]) for r in data_quality if r["metric"] != "structure_csv")

    case_dumps = select_case_dumps(sequences, break_events=break_events)
    missing_cases = [k for k, v in case_dumps.items() if not v.get("present")]

    runtime = time.time() - t0
    max_rss = max(max_rss, _rss_mib())

    report = {
        "detector_version": DETECTOR_VERSION,
        "symbol": symbol,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "params": params.to_dict(),
        "runtime_sec": round(runtime, 3),
        "max_rss_mib": round(max_rss, 2),
        "summary": {
            "candidate_observations": tracker.candidate_count,
            "sequences": n,
            "touches": touches,
            "touch_rate": (touches / n) if n else None,
            "executed_sequences": executed,
            "execution_rate": (executed / n) if n else None,
            "pulled_before_touch": pulled,
            "pulling_rate": (pulled / n) if n else None,
            "absorption_candidates": absorp_n,
            "accepted_breaks": accepted,
            "failed_breaks": failed,
            "top_interaction_bands": top_labels,
            "data_quality_ok": dq_ok,
            "data_quality_notes": (
                f"gaps={gap_count}; incomplete_book={incomplete_book}; "
                f"missing_case_types={missing_cases}"
            ),
            "thresholds_to_tune": (
                "local_multiple_min, local_percentile_min, local_depth_share_min, "
                "min_lifetime_ms, sample_interval_ms, bucket_mode/ticks, absorption_*, "
                "near_touch_*"
            ),
            "structure_csv": str(structure_csv) if structure_csv else None,
        },
        "missing_case_types": missing_cases,
        "aggressive_side_semantics": {
            "ask_wall": aggressive_side_for_wall("ask"),
            "bid_wall": aggressive_side_for_wall("bid"),
        },
    }

    paths = write_outputs(
        output_dir,
        params=params,
        symbol=symbol,
        start=start,
        end=end,
        candidates=[],
        sequences=sequences,
        transitions=[],
        trade_interactions=tracker.trade_interaction_rows,
        absorption_events=absorption,
        break_events=break_events,
        toxicity=toxicity,
        forward_outcomes=forward,
        distance_dist=dist,
        candidate_dist=cand_dist,
        comparison=comparison,
        data_quality=data_quality,
        errors=errors,
        report=report,
        case_dumps=case_dumps,
        skip_streamed=("execution_wall_candidates.csv", "execution_wall_transitions.csv"),
    )
    paths["execution_wall_candidates.csv"] = output_dir / "execution_wall_candidates.csv"
    paths["execution_wall_transitions.csv"] = output_dir / "execution_wall_transitions.csv"

    if owns_db:
        try:
            db.close()
        except Exception:
            pass

    return AnalysisResult(
        sequences=sequences, output_paths=paths, report=report, errors=errors
    )
