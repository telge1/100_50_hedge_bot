"""Deterministic Episode-1 detail parity for ClickHouse silver v1_2 level_changes + checkpoints."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

import zstandard as zstd

from .helpers import iso_to_ns_exact


# Canonical LC field order for hashing / equality (research-comparable fields only).
CANONICAL_LC_FIELDS = (
    "event_time_ns",
    "update_id",
    "seq",
    "side",
    "price",
    "old_size",
    "new_size",
    "change_type",
)

CANONICAL_SORT_KEY = CANONICAL_LC_FIELDS  # documented sort = field tuple order

DEFAULT_REFERENCE_LEVEL_CHANGES = (
    "/home/telgenbuescher/projects/orderbook_analyse/obfull_research_engine/results/"
    "level_first_episode1_corrected_sms1_persist_v1/BTCUSDT/csp1_13058debfd4bba04/"
    "build_primary/level_changes.jsonl.zst"
)
DEFAULT_REFERENCE_BOOK_RESETS = (
    "/home/telgenbuescher/projects/orderbook_analyse/obfull_research_engine/results/"
    "level_first_episode1_corrected_sms1_persist_v1/BTCUSDT/csp1_13058debfd4bba04/"
    "build_primary/book_resets.jsonl.zst"
)


class DetailParityError(RuntimeError):
    """STOP_CLICKHOUSE_V1_2_DETAIL_PARITY_*."""


def dec(value: Any) -> Decimal:
    """Exact decimal from int/float/str without binary float re-round trips when str given."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise TypeError("bool is not a numeric size/price")
    if isinstance(value, int):
        return Decimal(value)
    # Prefer str(float) path only when already float; CH should pass toString().
    return Decimal(str(value))


def canon_dec_str(value: Any) -> str:
    """Canonical decimal text: no scientific notation, no trailing zeros."""
    d = dec(value).normalize()
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def change_type_from_sizes(old_size: Any, new_size: Any) -> str:
    old = dec(old_size)
    new = dec(new_size)
    if old <= 0 and new > 0:
        return "ADD"
    if old > 0 and new <= 0:
        return "DELETE"
    if old > 0 and new > 0 and old != new:
        return "UPDATE"
    raise DetailParityError(
        f"STOP_CLICKHOUSE_V1_2_DETAIL_PARITY_CHANGE_TYPE: "
        f"cannot map sizes old={old} new={new}"
    )


def map_level_kind(level_kind: str, old_size: Any, new_size: Any) -> str:
    """Map Episode-1 level_kind onto ADD/UPDATE/DELETE; sizes are authoritative."""
    mapped = change_type_from_sizes(old_size, new_size)
    kind = str(level_kind or "")
    expected = {
        "LEVEL_ADD": "ADD",
        "LEVEL_REMOVE": "DELETE",
        "LEVEL_INCREASE": "UPDATE",
        "LEVEL_DECREASE": "UPDATE",
    }.get(kind)
    if expected is not None and expected != mapped:
        raise DetailParityError(
            f"STOP_CLICKHOUSE_V1_2_DETAIL_PARITY_CHANGE_TYPE: "
            f"level_kind={kind} disagrees with sizes→{mapped}"
        )
    return mapped


def event_time_to_ns(value: Any) -> int:
    if isinstance(value, int):
        return int(value)
    s = str(value).strip()
    if not s.endswith("Z") and "+" not in s[10:] and s.count("-") <= 2:
        # ISO without Z
        if "T" in s:
            s = s + "Z"
    return iso_to_ns_exact(s)


def canonical_lc_row(
    *,
    event_time_ns: int,
    update_id: int,
    seq: int,
    side: str,
    price: Any,
    old_size: Any,
    new_size: Any,
    change_type: str,
) -> dict[str, Any]:
    return {
        "event_time_ns": int(event_time_ns),
        "update_id": int(update_id),
        "seq": int(seq),
        "side": str(side),
        "price": dec(price),
        "old_size": dec(old_size),
        "new_size": dec(new_size),
        "change_type": str(change_type),
    }


def sort_canonical_lcs(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    def key(r: dict[str, Any]) -> tuple:
        return (
            int(r["event_time_ns"]),
            int(r["update_id"]),
            int(r["seq"]),
            str(r["side"]),
            dec(r["price"]),
            dec(r["old_size"]),
            dec(r["new_size"]),
            str(r["change_type"]),
        )

    return sorted(rows, key=key)


def hash_canonical_lcs(rows: list[dict[str, Any]]) -> str:
    h = hashlib.sha256()
    for r in sort_canonical_lcs(rows):
        line = "|".join(
            [
                str(r["event_time_ns"]),
                str(r["update_id"]),
                str(r["seq"]),
                str(r["side"]),
                canon_dec_str(r["price"]),
                canon_dec_str(r["old_size"]),
                canon_dec_str(r["new_size"]),
                str(r["change_type"]),
            ]
        )
        h.update(line.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def load_reference_level_changes(
    path: str | Path,
    *,
    analysis_start_ns: int,
    analysis_end_ns: int,
) -> list[dict[str, Any]]:
    raw = zstd.ZstdDecompressor().decompress(Path(path).read_bytes())
    out: list[dict[str, Any]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        if obj.get("record_type") == "header":
            continue
        et = event_time_to_ns(obj["event_time"])
        if not (analysis_start_ns <= et < analysis_end_ns):
            continue
        out.append(
            canonical_lc_row(
                event_time_ns=et,
                update_id=int(obj["u"]),
                seq=int(obj["seq"]),
                side=str(obj["side"]),
                price=obj["price"],
                old_size=obj["old_size"],
                new_size=obj["new_size"],
                change_type=map_level_kind(obj.get("level_kind"), obj["old_size"], obj["new_size"]),
            )
        )
    return out


def load_clickhouse_level_changes(
    client: Any,
    *,
    database: str,
    table: str,
    symbol: str,
    analysis_start_ns: int,
    analysis_end_ns: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (canonical_rows, raw_meta_rows with epoch/ordinal/receive)."""
    rows = client.query(
        f"""
        SELECT
          event_time_ns,
          update_id,
          seq,
          side,
          toString(price),
          toString(old_size),
          toString(new_size),
          change_type,
          replay_epoch,
          receive_time_ns,
          source_record_ordinal,
          apply_order
        FROM {database}.{table} FINAL
        WHERE symbol = {{symbol:String}}
          AND event_time_ns >= {{start_ns:UInt64}}
          AND event_time_ns < {{end_ns:UInt64}}
        """,
        parameters={
            "symbol": symbol.upper(),
            "start_ns": int(analysis_start_ns),
            "end_ns": int(analysis_end_ns),
        },
    ).result_rows
    canon: list[dict[str, Any]] = []
    meta: list[dict[str, Any]] = []
    for r in rows:
        c = canonical_lc_row(
            event_time_ns=int(r[0]),
            update_id=int(r[1]),
            seq=int(r[2]),
            side=str(r[3]),
            price=r[4],
            old_size=r[5],
            new_size=r[6],
            change_type=str(r[7]),
        )
        canon.append(c)
        meta.append(
            {
                **c,
                "replay_epoch": int(r[8]),
                "receive_time_ns": int(r[9]),
                "source_record_ordinal": int(r[10]),
                "apply_order": int(r[11]),
            }
        )
    return canon, meta


@dataclass
class FieldDiffStats:
    missing: int = 0
    extra: int = 0
    duplicate_ref: int = 0
    duplicate_ch: int = 0
    field_mismatches: dict[str, int] = field(default_factory=dict)
    first_mismatch: dict[str, Any] | None = None


def compare_canonical_lc_lists(
    reference: list[dict[str, Any]],
    clickhouse: list[dict[str, Any]],
) -> dict[str, Any]:
    ref_s = sort_canonical_lcs(reference)
    ch_s = sort_canonical_lcs(clickhouse)
    ref_hash = hash_canonical_lcs(ref_s)
    ch_hash = hash_canonical_lcs(ch_s)

    def row_key(r: dict[str, Any]) -> tuple:
        return (
            int(r["event_time_ns"]),
            int(r["update_id"]),
            int(r["seq"]),
            str(r["side"]),
            canon_dec_str(r["price"]),
            canon_dec_str(r["old_size"]),
            canon_dec_str(r["new_size"]),
            str(r["change_type"]),
        )

    ref_c = Counter(row_key(r) for r in ref_s)
    ch_c = Counter(row_key(r) for r in ch_s)
    dup_ref = sum(v - 1 for v in ref_c.values() if v > 1)
    dup_ch = sum(v - 1 for v in ch_c.values() if v > 1)
    missing = sum((ref_c - ch_c).values())
    extra = sum((ch_c - ref_c).values())

    stats = FieldDiffStats(
        missing=int(missing),
        extra=int(extra),
        duplicate_ref=int(dup_ref),
        duplicate_ch=int(dup_ch),
    )

    # Positional compare on sorted lists (same length expected).
    n = min(len(ref_s), len(ch_s))
    for i in range(n):
        a, b = ref_s[i], ch_s[i]
        for f in CANONICAL_LC_FIELDS:
            av, bv = a[f], b[f]
            if isinstance(av, Decimal) or isinstance(bv, Decimal):
                ok = dec(av) == dec(bv)
            else:
                ok = av == bv
            if not ok:
                stats.field_mismatches[f] = stats.field_mismatches.get(f, 0) + 1
                if stats.first_mismatch is None:
                    stats.first_mismatch = {
                        "index": i,
                        "field": f,
                        "reference": str(av),
                        "clickhouse": str(bv),
                        "reference_row": {k: str(a[k]) for k in CANONICAL_LC_FIELDS},
                        "clickhouse_row": {k: str(b[k]) for k in CANONICAL_LC_FIELDS},
                    }

    if len(ref_s) != len(ch_s):
        stats.field_mismatches["_row_count"] = abs(len(ref_s) - len(ch_s))
        if stats.first_mismatch is None:
            stats.first_mismatch = {
                "field": "_row_count",
                "reference": len(ref_s),
                "clickhouse": len(ch_s),
            }

    ok = (
        len(ref_s) == len(ch_s)
        and missing == 0
        and extra == 0
        and ref_hash == ch_hash
        and not stats.field_mismatches
        and dup_ref == 0
        and dup_ch == 0
    )
    return {
        "ok": ok,
        "reference_rows": len(ref_s),
        "clickhouse_rows": len(ch_s),
        "missing_rows": stats.missing,
        "extra_rows": stats.extra,
        "duplicate_reference_rows": stats.duplicate_ref,
        "duplicate_clickhouse_rows": stats.duplicate_ch,
        "field_mismatches": dict(stats.field_mismatches),
        "first_mismatch": stats.first_mismatch,
        "reference_hash": ref_hash,
        "clickhouse_hash": ch_hash,
        "canonical_fields": list(CANONICAL_LC_FIELDS),
        "sort_key": list(CANONICAL_SORT_KEY),
    }


def load_reference_checkpoints(
    path: str | Path,
    *,
    bronze_start_ns: int,
    analysis_end_ns: int,
) -> list[dict[str, Any]]:
    raw = zstd.ZstdDecompressor().decompress(Path(path).read_bytes())
    out: list[dict[str, Any]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        if obj.get("record_type") == "header":
            continue
        et = event_time_to_ns(obj["event_time"])
        if et < bronze_start_ns or et >= analysis_end_ns:
            continue
        if str(obj.get("event_type") or "") != "checkpoint":
            continue
        out.append(
            {
                "event_time_ns": et,
                "update_id": int(obj["update_id"]),
                "seq": int(obj["sequence_id"]),
                "replay_epoch": int(obj["replay_epoch"]),
                "n_bid_levels": int(obj["n_bid_levels"]),
                "n_ask_levels": int(obj["n_ask_levels"]),
                "book_hash": str(obj["book_map_sha256"]),
                "identity_reset": bool(obj.get("identity_reset")),
            }
        )
    out.sort(key=lambda r: (r["event_time_ns"], r["update_id"], r["seq"]))
    return out


def load_clickhouse_checkpoints(
    client: Any,
    *,
    database: str,
    table: str,
    symbol: str,
) -> list[dict[str, Any]]:
    rows = client.query(
        f"""
        SELECT
          checkpoint_time_ns,
          update_id,
          seq,
          replay_epoch,
          bid_level_count,
          ask_level_count,
          book_hash,
          source_record_ordinal
        FROM {database}.{table} FINAL
        WHERE symbol = {{symbol:String}}
        ORDER BY checkpoint_time_ns, update_id, seq
        """,
        parameters={"symbol": symbol.upper()},
    ).result_rows
    out = []
    for r in rows:
        bh = r[6]
        if isinstance(bh, (bytes, bytearray)):
            bh = bh.decode("utf-8")
        out.append(
            {
                "event_time_ns": int(r[0]),
                "update_id": int(r[1]),
                "seq": int(r[2]),
                "replay_epoch": int(r[3]),
                "n_bid_levels": int(r[4]),
                "n_ask_levels": int(r[5]),
                "book_hash": str(bh),
                "source_record_ordinal": int(r[7]),
            }
        )
    return out


def compare_checkpoints(
    reference: list[dict[str, Any]],
    clickhouse: list[dict[str, Any]],
) -> dict[str, Any]:
    if len(reference) != len(clickhouse):
        return {
            "ok": False,
            "reason": f"checkpoint count ref={len(reference)} ch={len(clickhouse)}",
            "reference": reference,
            "clickhouse": clickhouse,
        }
    # Absolute replay_epoch values differ (episode-global vs local build). Compare order + delta.
    ref_epochs = [r["replay_epoch"] for r in reference]
    ch_epochs = [r["replay_epoch"] for r in clickhouse]
    ref_delta = [ref_epochs[i] - ref_epochs[0] for i in range(len(ref_epochs))]
    ch_delta = [ch_epochs[i] - ch_epochs[0] for i in range(len(ch_epochs))]
    if ref_delta != ch_delta:
        return {
            "ok": False,
            "reason": "STOP_CLICKHOUSE_V1_2_DETAIL_PARITY_EPOCH: relative epoch sequence mismatch",
            "reference_epochs": ref_epochs,
            "clickhouse_epochs": ch_epochs,
            "reference_relative": ref_delta,
            "clickhouse_relative": ch_delta,
        }
    # Each checkpoint must bump epoch by +1 versus previous (canonical identity reset).
    if len(ch_epochs) >= 2 and any(
        ch_epochs[i] != ch_epochs[i - 1] + 1 for i in range(1, len(ch_epochs))
    ):
        return {
            "ok": False,
            "reason": "STOP_CLICKHOUSE_V1_2_DETAIL_PARITY_EPOCH: checkpoint did not increment epoch by 1",
            "clickhouse_epochs": ch_epochs,
        }

    mismatches: list[dict[str, Any]] = []
    for i, (a, b) in enumerate(zip(reference, clickhouse)):
        for f in ("event_time_ns", "update_id", "seq", "n_bid_levels", "n_ask_levels", "book_hash"):
            if a[f] != b[f]:
                mismatches.append(
                    {
                        "index": i,
                        "field": f,
                        "reference": a[f],
                        "clickhouse": b[f],
                    }
                )
        if not a.get("identity_reset", True):
            mismatches.append({"index": i, "field": "identity_reset", "reference": a.get("identity_reset")})

    return {
        "ok": not mismatches,
        "count": len(reference),
        "reference_epochs": ref_epochs,
        "clickhouse_epochs": ch_epochs,
        "relative_epochs": ch_delta,
        "mismatches": mismatches,
        "pairs": [
            {
                "event_time_ns": a["event_time_ns"],
                "update_id": a["update_id"],
                "seq": a["seq"],
                "book_hash": a["book_hash"],
                "n_bid_levels": a["n_bid_levels"],
                "n_ask_levels": a["n_ask_levels"],
                "reference_replay_epoch": a["replay_epoch"],
                "clickhouse_replay_epoch": b["replay_epoch"],
            }
            for a, b in zip(reference, clickhouse)
        ],
        "epoch_explanation": (
            "Two full-book checkpoints produce two replay epochs: the pre-window warm-up "
            "checkpoint initializes epoch E0 and the in-window periodic checkpoint performs "
            "an identity reset to E0+1. Absolute epoch integers may differ between Episode-1 "
            "(episode-global) and the silver pilot (build-local) while relative +1 steps match. "
            "This matches Episode-1 book_resets identity_reset semantics and is not a false epoch."
        ),
    }


def assert_lc_epoch_consistent_with_checkpoints(
    lc_meta: list[dict[str, Any]],
    checkpoints: list[dict[str, Any]],
) -> dict[str, Any]:
    """All analysis LCs must remain in the epoch opened by the first checkpoint until the second."""
    if len(checkpoints) < 1:
        raise DetailParityError("STOP_CLICKHOUSE_V1_2_DETAIL_PARITY_EPOCH: no checkpoints")
    epochs = sorted({int(c["replay_epoch"]) for c in checkpoints})
    first_epoch = int(checkpoints[0]["replay_epoch"])
    # LCs in analysis window should all carry first checkpoint's epoch (deltas before 2nd reset).
    bad = [r for r in lc_meta if int(r["replay_epoch"]) != first_epoch]
    if bad:
        raise DetailParityError(
            "STOP_CLICKHOUSE_V1_2_DETAIL_PARITY_EPOCH: "
            f"{len(bad)} level_changes not on start epoch {first_epoch}; first={bad[0]}"
        )
    return {
        "ok": True,
        "analysis_lc_epoch": first_epoch,
        "checkpoint_epochs": [int(c["replay_epoch"]) for c in checkpoints],
    }
