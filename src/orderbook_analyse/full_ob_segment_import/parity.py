"""Source vs DB parity for imported Full-OB segments."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.continuity_contract import (
    book_content_hash,
)

from .readiness import SegmentCandidate
from .reader import iter_segment_records, summarize_records
from .ch import ch_query


def _as_str(v: Any) -> str:
    if isinstance(v, (bytes, bytearray)):
        return v.decode("utf-8", errors="replace")
    return str(v)


def source_book_hash_sample(rows: list[dict[str, Any]], *, max_checkpoints: int = 5) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        if r.get("record_kind") not in ("INITIAL_CHECKPOINT", "RESYNC_CHECKPOINT"):
            continue
        bids = r.get("bids") or []
        asks = r.get("asks") or []
        h = book_content_hash(bids=bids, asks=asks)
        out.append(
            {
                "record_id": r["record_id"],
                "record_kind": r["record_kind"],
                "book_hash_computed": h,
                "book_hash_stored": r.get("book_hash"),
                "u": r.get("u"),
                "seq": r.get("seq"),
            }
        )
        if len(out) >= max_checkpoints:
            break
    return out


def parity_check_segment(client, database: str, cand: SegmentCandidate) -> dict[str, Any]:
    rows = list(
        iter_segment_records(
            cand.path,
            source_sha256=cand.actual_sha256 or "",
            fight_event_id=cand.fight_event_id,
            symbol=cand.symbol,
            topic=cand.topic,
            segment_id=cand.segment_id,
            segment_index=cand.continuation_index,
            continuation_index=cand.continuation_index,
        )
    )
    summary = summarize_records(rows)
    source_ids = summary["record_ids"]
    db_ids = [
        _as_str(r[0])
        for r in ch_query(
            client,
            f"""
            SELECT record_id FROM {database}.v_full_ob_records_canonical
            WHERE segment_id = {{s:String}}
            ORDER BY record_ordinal
            """,
            {"s": cand.segment_id},
        )
    ]
    mismatches: list[str] = []
    if len(source_ids) != len(db_ids):
        mismatches.append(f"count source={len(source_ids)} db={len(db_ids)}")
    if source_ids != db_ids:
        # find first diff
        for i, (a, b) in enumerate(zip(source_ids, db_ids)):
            if a != b:
                mismatches.append(f"id_mismatch_at_{i}: {a} != {b}")
                break
        if len(source_ids) != len(db_ids):
            mismatches.append("id_list_length_diff")

    # checkpoint book hashes
    src_hashes = source_book_hash_sample(rows)
    for item in src_hashes:
        db_h = ch_query(
            client,
            f"""
            SELECT book_hash, bids, asks FROM {database}.v_full_ob_records_canonical
            WHERE record_id = {{r:String}}
            """,
            {"r": item["record_id"]},
        )
        if not db_h:
            mismatches.append(f"missing_checkpoint_{item['record_id']}")
            continue
        book_hash, bids, asks = db_h[0]
        computed = book_content_hash(bids=bids or [], asks=asks or [])
        if computed != item["book_hash_computed"]:
            mismatches.append(f"book_hash_mismatch_{item['record_id']}")
        if item.get("book_hash_stored") and book_hash and item["book_hash_stored"] != book_hash:
            mismatches.append(f"stored_book_hash_diff_{item['record_id']}")

    kinds_src = ch_query(
        client,
        f"""
        SELECT record_kind, count() FROM {database}.v_full_ob_records_canonical
        WHERE segment_id = {{s:String}} GROUP BY record_kind ORDER BY record_kind
        """,
        {"s": cand.segment_id},
    )
    ok = len(mismatches) == 0
    return {
        "ok": ok,
        "segment_id": cand.segment_id,
        "source_logical_records": len(source_ids),
        "db_logical_records": len(db_ids),
        "parse_rejects": 0,
        "source_book_hashes": src_hashes,
        "db_kind_counts": [{"kind": k, "n": int(n)} for k, n in kinds_src],
        "mismatches": mismatches,
        "source_book_hash_eq_db": all("book_hash_mismatch" not in m for m in mismatches),
    }


def signal_context_join_sql(database: str) -> str:
    """Read-only join templates (not executed automatically against production writes)."""
    return f"""
-- signal-scoped trades (read-only join)
SELECT t.*
FROM orderbook_analysis.public_trades_canonical AS t
INNER JOIN {database}.v_full_ob_signals_canonical AS s
    ON t.symbol = s.symbol
WHERE s.signal_id = {{signal_id:String}}
  AND t.trade_ts >= {{pre_ts:DateTime64(3)}}
  AND t.trade_ts <= {{post_ts:DateTime64(3)}};

-- OI / liquidations: if missing → context_coverage=PARTIAL (application-level)
"""
