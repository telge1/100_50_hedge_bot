from __future__ import annotations

from typing import Any

from .config import INCOMPLETE_JOIN
from .timeutil import parse_ts


def _num(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _close(a: object, b: object, tol: float = 1e-8) -> bool:
    na, nb = _num(a), _num(b)
    if na is None or nb is None:
        return str(a) == str(b)
    return abs(na - nb) <= tol * max(1.0, abs(na), abs(nb))


def join_signals_outcomes(
    signals: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
) -> dict[str, Any]:
    sig_by_id: dict[str, dict[str, Any]] = {}
    dup_signal_ids: list[str] = []
    for row in signals:
        sid = str(row.get("signal_id") or "")
        if sid in sig_by_id:
            dup_signal_ids.append(sid)
        else:
            sig_by_id[sid] = row
    out_by_id: dict[str, dict[str, Any]] = {}
    dup_outcome_ids: list[str] = []
    for row in outcomes:
        sid = str(row.get("signal_id") or "")
        if sid in out_by_id:
            dup_outcome_ids.append(sid)
        else:
            out_by_id[sid] = row

    sig_ids = set(sig_by_id)
    out_ids = set(out_by_id)
    matched = sorted(sig_ids & out_ids)
    missing_outcome = sorted(sig_ids - out_ids)
    extra_outcome = sorted(out_ids - sig_ids)
    field_mismatches: list[dict[str, Any]] = []
    pairs: list[dict[str, Any]] = []
    for sid in matched:
        s = sig_by_id[sid]
        o = out_by_id[sid]
        diffs: dict[str, Any] = {}
        if str(s.get("symbol")) != str(o.get("symbol")):
            diffs["symbol"] = [s.get("symbol"), o.get("symbol")]
        if str(s.get("timeframe")) != str(o.get("timeframe")):
            diffs["timeframe"] = [s.get("timeframe"), o.get("timeframe")]
        if str(s.get("direction") or "").upper() != str(o.get("direction") or "").upper():
            diffs["direction"] = [s.get("direction"), o.get("direction")]
        if not _close(s.get("entry_price"), o.get("entry_price")):
            diffs["entry_price"] = [s.get("entry_price"), o.get("entry_price")]
        tp_s = s.get("tp_price") if s.get("tp_price") is not None else s.get("take_profit")
        tp_o = o.get("tp_price") if o.get("tp_price") is not None else o.get("take_profit")
        if not _close(tp_s, tp_o):
            diffs["tp_price"] = [tp_s, tp_o]
        sl_s = s.get("sl_price") if s.get("sl_price") is not None else s.get("initial_sl_price")
        sl_o = o.get("initial_sl_price") if o.get("initial_sl_price") is not None else o.get("sl_price")
        if not _close(sl_s, sl_o):
            diffs["sl_price"] = [sl_s, sl_o]
        if str(s.get("entry_time")) != str(o.get("entry_time")):
            diffs["entry_time"] = [s.get("entry_time"), o.get("entry_time")]
        if diffs:
            field_mismatches.append({"signal_id": sid, "diffs": diffs})
        pairs.append({"signal": s, "outcome": o})

    complete = (
        not missing_outcome
        and not extra_outcome
        and not field_mismatches
        and not dup_signal_ids
        and not dup_outcome_ids
        and len(matched) == len(signals) == len(outcomes)
    )
    audit = {
        "tier_a_signals": len(signals),
        "outcomes": len(outcomes),
        "unique_signal_ids": len(sig_ids),
        "unique_outcome_ids": len(out_ids),
        "matched_ids": len(matched),
        "signals_without_outcome": missing_outcome[:50],
        "signals_without_outcome_count": len(missing_outcome),
        "outcomes_without_source_signal": extra_outcome[:50],
        "outcomes_without_source_signal_count": len(extra_outcome),
        "duplicate_signal_ids": dup_signal_ids[:20],
        "duplicate_outcome_ids": dup_outcome_ids[:20],
        "field_mismatch_count": len(field_mismatches),
        "field_mismatches": field_mismatches[:50],
        "complete": complete,
        "blocker": None if complete else INCOMPLETE_JOIN,
    }
    return {"pairs": pairs, "audit": audit}
