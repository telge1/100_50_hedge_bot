"""Prefix-invariance analysis and legacy test replication."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from orderbook_analyse.btc_doge_current_recheck_v1.runner import (
    _find_segment_for_hour,
    _replay_features_for_segment,
    _row_bucket_ms,
)
from orderbook_analyse.ob200_v3_raw_discovery.files import SegmentRef
from orderbook_analyse.ob_data_source.ndjson_parse import parse_ob200_obj
from orderbook_analyse.orderbook_v2_live.clock import LiveSecondClock
from orderbook_analyse.orderbook_v2_live.raw_archive.events import (
    is_replayable_line,
    line_to_replay_payload,
)
from orderbook_analyse.orderbook_v2_live.raw_archive.replay import iter_segment_lines

from .contract import COMPARE_FIELDS, buckets_equal, bucket_end_ms, finalized_prefix, iso_z, is_bucket_final
from .engine import run_causal_replay


def _raw_dict_from_rows(rows: list[dict]) -> dict[int, dict[str, float]]:
    out: dict[int, dict[str, float]] = {}
    for r in rows:
        if not r.get("is_valid"):
            continue
        ms = _row_bucket_ms(r)
        out[ms] = {f: float(r.get(f, 0)) for f in COMPARE_FIELDS if r.get(f) is not None}
    return out


def legacy_prefix_test(
    seg: SegmentRef,
    hour: datetime,
    symbol: str,
) -> dict[str, Any]:
    """Replicate the broken prefix test from btc_raw_aggregate_parity_audit_v1."""
    mid_ms = int(hour.timestamp() * 1000) + 30 * 60 * 1000
    end_ms = int((hour + timedelta(hours=1)).timestamp() * 1000) - 1
    full_rows = _replay_features_for_segment(seg, end_ms=end_ms)
    full_dict = _raw_dict_from_rows(full_rows)

    clock = LiveSecondClock(symbol)
    prefix_rows: list[dict] = []
    first_divergent_event: int | None = None
    for obj in iter_segment_lines(seg.path):
        if not is_replayable_line(obj):
            continue
        msg = parse_ob200_obj(line_to_replay_payload(obj), expected_symbol=symbol)
        if msg.raw_ts_ms > mid_ms:
            if first_divergent_event is None:
                first_divergent_event = msg.raw_ts_ms
            break
        data = {
            "s": msg.symbol,
            "b": [[format(p, "f"), format(q, "f")] for p, q in msg.bids],
            "a": [[format(p, "f"), format(q, "f")] for p, q in msg.asks],
            "u": msg.update_id,
            "seq": msg.cross_sequence,
        }
        prefix_rows.extend(clock.ingest(msg.message_type, msg.raw_ts_ms, data))
    prefix_rows.extend(clock.close_through(mid_ms))
    prefix_dict = _raw_dict_from_rows(prefix_rows)

    old_filter = {k: v for k, v in full_dict.items() if k <= mid_ms}
    old_pass = prefix_dict == old_filter
    correct_filter = {k: v for k, v in full_dict.items() if bucket_end_ms(k) <= mid_ms}
    correct_pass = prefix_dict == correct_filter

    divergences: list[dict[str, Any]] = []
    all_keys = sorted(set(prefix_dict) | set(old_filter))
    for k in all_keys:
        if prefix_dict.get(k) == old_filter.get(k):
            continue
        be = bucket_end_ms(k)
        is_open_at_mid = not is_bucket_final(k, mid_ms)
        field_diffs: dict[str, tuple[float, float]] = {}
        if k in prefix_dict and k in old_filter:
            for f in COMPARE_FIELDS:
                if f in prefix_dict[k] and f in old_filter[k]:
                    if prefix_dict[k][f] != old_filter[k][f]:
                        field_diffs[f] = (prefix_dict[k][f], old_filter[k][f])
        dtype = (
            "EXPECTED_PROVISIONAL_BUCKET_DIFFERENCE"
            if is_open_at_mid
            else "TRUE_PREFIX_INVARIANCE_FAILURE"
        )
        divergences.append(
            {
                "hour_utc": iso_z(hour),
                "bucket_start_ms": k,
                "bucket_start_utc": iso_z(datetime.fromtimestamp(k / 1000, tz=timezone.utc)),
                "bucket_end_ms": be,
                "as_of_ms": mid_ms,
                "divergence_type": dtype,
                "in_prefix": k in prefix_dict,
                "in_full_old_filter": k in old_filter,
                "is_open_at_as_of": is_open_at_mid,
                "field_diffs": str(field_diffs) if field_diffs else "",
                "first_event_after_as_of_ms": first_divergent_event,
            }
        )

    return {
        "hour_utc": iso_z(hour),
        "as_of_ms": mid_ms,
        "legacy_prefix_pass": old_pass,
        "corrected_prefix_pass": correct_pass,
        "provisional_only_buckets": len([d for d in divergences if d["is_open_at_as_of"]]),
        "true_failure_buckets": len(
            [d for d in divergences if d["divergence_type"] == "TRUE_PREFIX_INVARIANCE_FAILURE"]
        ),
        "divergences": divergences,
    }


def compare_prefix_invariance(
    segments: list[SegmentRef],
    symbol: str,
    T1_ms: int,
    T2_ms: int,
) -> dict[str, Any]:
    """Compare finalized buckets at T1 from replays with as_of T1 vs T2."""
    if T1_ms >= T2_ms:
        raise ValueError("T1 must be < T2")
    r1 = run_causal_replay(segments, symbol=symbol, as_of_exclusive_ms=T1_ms)
    r2 = run_causal_replay(segments, symbol=symbol, as_of_exclusive_ms=T2_ms)
    p1 = finalized_prefix(r1, T1_ms)
    p2 = finalized_prefix(r2, T1_ms)

    mismatches: list[dict[str, Any]] = []
    for bs in sorted(set(p1) | set(p2)):
        b1, b2 = p1.get(bs), p2.get(bs)
        if b1 is None or b2 is None:
            mismatches.append(
                {
                    "bucket_start_ms": bs,
                    "issue": "missing_in_one_run",
                    "in_T1_run": b1 is not None,
                    "in_T2_run": b2 is not None,
                }
            )
            continue
        if not buckets_equal(b1.compare_key(), b2.compare_key()):
            diffs = {
                f: (b1.compare_key().get(f), b2.compare_key().get(f))
                for f in COMPARE_FIELDS
                if f in b1.compare_key()
                and f in b2.compare_key()
                and abs(b1.compare_key()[f] - b2.compare_key()[f]) > 1e-9
            }
            mismatches.append(
                {
                    "bucket_start_ms": bs,
                    "issue": "value_mismatch",
                    "field_diffs": str(diffs),
                    "bucket_end_ms": bucket_end_ms(bs),
                }
            )

    return {
        "T1_ms": T1_ms,
        "T2_ms": T2_ms,
        "T1_utc": iso_z(datetime.fromtimestamp(T1_ms / 1000, tz=timezone.utc)),
        "T2_utc": iso_z(datetime.fromtimestamp(T2_ms / 1000, tz=timezone.utc)),
        "finalized_bucket_count_T1": len(p1),
        "pass": len(mismatches) == 0,
        "mismatch_count": len(mismatches),
        "mismatches": mismatches[:20],
    }


def analyze_all_legacy_windows(
    segments: list[SegmentRef],
    hours: list[datetime],
    symbol: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for hour in hours:
        seg = _find_segment_for_hour(segments, hour)
        if seg is None:
            continue
        result = legacy_prefix_test(seg, hour, symbol)
        for d in result.get("divergences", []):
            rows.append(
                {
                    **d,
                    "symbol": symbol,
                    "legacy_pass": result["legacy_prefix_pass"],
                    "corrected_pass": result["corrected_prefix_pass"],
                }
            )
    return rows
