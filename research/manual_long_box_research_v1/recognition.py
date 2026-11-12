"""Parse long_position drawings into research setups."""

from __future__ import annotations

import math
from typing import Any

from .models import LongBoxReject, LongBoxSetup
from .validation import pct_dist, validate_long_geometry, validate_short_geometry

STRATEGY_ID_PREFIXES = ("ema59", "p17", "btd", "stoch-")


def _is_manual_position_common(d: dict[str, Any]) -> bool:
    if d.get("visible") is False:
        return False
    did = str(d.get("drawing_id") or "")
    if did.startswith("d-"):
        return True
    meta = d.get("metadata") or {}
    if meta.get("manual") is True:
        return True
    src = str(meta.get("source") or "").lower()
    if src in ("user", "manual", "research_manual"):
        return True
    if any(did.startswith(p) for p in STRATEGY_ID_PREFIXES):
        return False
    if meta.get("strategy_id") or meta.get("trade_id"):
        return False
    return did.startswith("d-")


def is_manual_long_box(d: dict[str, Any]) -> bool:
    if d.get("drawing_type") != "long_position":
        return False
    return _is_manual_position_common(d)


def is_manual_short_box(d: dict[str, Any]) -> bool:
    if d.get("drawing_type") != "short_position":
        return False
    return _is_manual_position_common(d)


def recognize_long_boxes(
    payload: dict[str, Any],
    *,
    symbol: str | None = None,
    manual_only: bool = True,
) -> tuple[list[LongBoxSetup], list[LongBoxReject]]:
    setups: list[LongBoxSetup] = []
    rejects: list[LongBoxReject] = []
    drawings = payload.get("drawings")
    if not isinstance(drawings, list):
        rejects.append(LongBoxReject("", "invalid_payload", "drawings_not_array"))
        return setups, rejects

    sym_filter = symbol.upper() if symbol else None
    seen: set[str] = set()

    for row in drawings:
        if not isinstance(row, dict):
            continue
        did = str(row.get("drawing_id") or "")
        if not did or did in seen:
            if did:
                rejects.append(LongBoxReject(did, "duplicate_id"))
            continue
        seen.add(did)

        if manual_only and not is_manual_long_box(row):
            continue
        if row.get("drawing_type") != "long_position":
            continue

        sym = str(row.get("symbol") or "").upper()
        if sym_filter and sym != sym_filter:
            continue

        try:
            entry = float(row["entry_price"])
            stop = float(row["stop_price"])
            target = float(row["target_price"])
            t0 = int(row["start_timestamp"])
            t1 = int(row["end_timestamp"])
        except (KeyError, TypeError, ValueError) as exc:
            rejects.append(LongBoxReject(did, "missing_prices_or_time", str(exc)))
            continue

        ok, reason = validate_long_geometry(stop, entry, target)
        if not ok:
            rejects.append(LongBoxReject(did, reason))
            continue
        if t1 < t0:
            t0, t1 = t1, t0

        setups.append(
            LongBoxSetup(
                drawing_id=did,
                symbol=sym,
                entry_price=entry,
                stop_price=stop,
                target_price=target,
                start_ts=t0,
                end_ts=t1,
                created_on_timeframe=str(row.get("created_on_timeframe") or ""),
                timeframe_scope=str(row.get("timeframe_scope") or "all"),
                risk_pct=round(pct_dist(entry, stop), 6),
                reward_pct=round(pct_dist(entry, target), 6),
                raw=dict(row),
            )
        )

    setups.sort(key=lambda s: (s.start_ts, s.drawing_id))
    return setups, rejects


def recognize_short_boxes(
    payload: dict[str, Any],
    *,
    symbol: str | None = None,
    manual_only: bool = True,
) -> tuple[list[LongBoxSetup], list[LongBoxReject]]:
    setups: list[LongBoxSetup] = []
    rejects: list[LongBoxReject] = []
    drawings = payload.get("drawings")
    if not isinstance(drawings, list):
        rejects.append(LongBoxReject("", "invalid_payload", "drawings_not_array"))
        return setups, rejects

    sym_filter = symbol.upper() if symbol else None
    seen: set[str] = set()

    for row in drawings:
        if not isinstance(row, dict):
            continue
        did = str(row.get("drawing_id") or "")
        if not did or did in seen:
            if did:
                rejects.append(LongBoxReject(did, "duplicate_id"))
            continue
        seen.add(did)

        if manual_only and not is_manual_short_box(row):
            continue
        if row.get("drawing_type") != "short_position":
            continue

        sym = str(row.get("symbol") or "").upper()
        if sym_filter and sym != sym_filter:
            continue

        try:
            entry = float(row["entry_price"])
            stop = float(row["stop_price"])
            target = float(row["target_price"])
            t0 = int(row["start_timestamp"])
            t1 = int(row["end_timestamp"])
        except (KeyError, TypeError, ValueError) as exc:
            rejects.append(LongBoxReject(did, "missing_prices_or_time", str(exc)))
            continue

        ok, reason = validate_short_geometry(stop, entry, target)
        if not ok:
            rejects.append(LongBoxReject(did, reason))
            continue
        if t1 < t0:
            t0, t1 = t1, t0

        setups.append(
            LongBoxSetup(
                drawing_id=did,
                symbol=sym,
                entry_price=entry,
                stop_price=stop,
                target_price=target,
                start_ts=t0,
                end_ts=t1,
                created_on_timeframe=str(row.get("created_on_timeframe") or ""),
                timeframe_scope=str(row.get("timeframe_scope") or "all"),
                risk_pct=round(pct_dist(entry, stop), 6),
                reward_pct=round(pct_dist(entry, target), 6),
                raw=dict(row),
            )
        )

    setups.sort(key=lambda s: (s.start_ts, s.drawing_id))
    return setups, rejects


def setups_invariant_across_timeframe_view(box: LongBoxSetup) -> dict[str, Any]:
    """Explicit record that stored coords are UTC seconds (TF-switch safe)."""
    return {
        "drawing_id": box.drawing_id,
        "entry_price": box.entry_price,
        "stop_price": box.stop_price,
        "target_price": box.target_price,
        "start_ts": box.start_ts,
        "end_ts": box.end_ts,
        "created_on_timeframe": box.created_on_timeframe,
        "note": "geometry_unchanged_on_tf_switch",
    }
