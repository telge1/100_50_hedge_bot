"""Classify published closes against a composite candle window. No candle fallback."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("naive datetime")
    return dt.astimezone(timezone.utc)


def decision_bar_inside(decision_close: datetime, range_start: datetime, range_end_exclusive: datetime) -> bool:
    dc = _utc(decision_close)
    first = dc - timedelta(minutes=5)
    last = dc - timedelta(minutes=1)
    return first >= _utc(range_start) and last < _utc(range_end_exclusive)


def classify_published_closes(rows: list[dict], *, range_start: datetime, range_end_exclusive: datetime) -> dict:
    ready = []
    failed = []
    for row in rows:
        reasons = []
        if int(row["snapshot_ids"]) != 1 or int(row["fingerprints"]) != 1:
            reasons.append("AMBIGUOUS_PUBLICATION")
        if row.get("fingerprint_match") is False:
            reasons.append("MISSING_MEMBERSHIP")
        members = int(row["member_rows"])
        catalog = int(row["catalog_rows"])
        if members != catalog and not (members == 0 and catalog == 0 and row.get("empty_membership_proven") is True):
            reasons.append("MISSING_MEMBERSHIP" if catalog == 0 else "MISSING_CATALOG")
        known = row.get("min_known_at")
        if known is not None and _utc(known) < _utc(range_start):
            reasons.append("INSUFFICIENT_CANDLE_COVERAGE")
        if not decision_bar_inside(row["decision_close"], range_start, range_end_exclusive):
            reasons.append("INSUFFICIENT_CANDLE_COVERAGE")
        if reasons:
            failed.append({"decision_close": row["decision_close"], "reasons": reasons})
        else:
            ready.append(row["decision_close"])
    return {"ready": len(ready), "failed": failed, "total": len(rows)}
