"""Fill vs cancel chronology (BT-D42) — evidence only, no PnL."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

BT_D7 = Path(__file__).resolve().parents[1] / "phase_bt_d7_config_driven_short_pnl_integration_v1"
if str(BT_D7) not in sys.path:
    sys.path.insert(0, str(BT_D7))

from sell_limit_fill_v1 import evaluate_short_sell_limit_fill  # noqa: E402

ChronologyStatus = Literal[
    "NO_TOUCH",
    "CANCELLED_BEFORE_TOUCH",
    "TOUCHED_BEFORE_CANCEL",
    "PRICE_TOUCHED_FILL_NOT_PROVEN",
    "EXECUTION_POSSIBLE",
    "EXECUTION_UNRESOLVED",
    "STILL_PENDING_NO_TOUCH",
    "STILL_PENDING_TOUCH_UNRESOLVED",
]

FillBranch = Literal["UNFILLED_PENDING", "POSSIBLY_FILLED", "NOT_APPLICABLE_ENDED"]


def _parse_utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def activation_contract_note() -> str:
    return (
        "BT-D41 earliest_eligible_execution_utc equals the first 5m decision-close key T+5m "
        "(SSOT bar timestamp). evaluate_short_sell_limit_fill treats bar_utc >= order_active_from_utc "
        "as active for that close-labelled candle — not the originating decision bar."
    )


@dataclass
class OrderSegmentV1:
    order_id: str
    decision_close_utc: str
    pending_active_from_utc: str
    planned_entry: float
    planned_sl: float
    planned_tp: float | None
    pool_lower: float
    pool_upper: float
    pool_key: str
    origin_ids: str
    selection_reason: str | None
    scanner_setup_id: str | None
    end_decision_close_utc: str | None = None
    end_event_type: str | None = None
    end_cancel_reason: str | None = None
    levels_history: list[dict[str, Any]] = field(default_factory=list)


def build_order_segments(events: list[dict[str, Any]]) -> list[OrderSegmentV1]:
    """Reconstruct order segments from BT-D41 lifecycle_events.csv rows."""
    by_id: dict[str, OrderSegmentV1] = {}
    segments: list[OrderSegmentV1] = []
    active_id: str | None = None

    for row in events:
        et = row["event_type"]
        oid = row.get("active_order_id") or ""

        if et == "CREATE" and oid:
            seg = OrderSegmentV1(
                order_id=oid,
                decision_close_utc=row["decision_close_utc"],
                pending_active_from_utc=row["earliest_eligible_execution_utc"],
                planned_entry=float(row["planned_entry"]),
                planned_sl=float(row["planned_sl"]),
                planned_tp=float(row["planned_tp"]) if row.get("planned_tp") else None,
                pool_lower=float(row["pool_lower"]),
                pool_upper=float(row["pool_upper"]),
                pool_key=str(row["pool_key"]),
                origin_ids=str(row.get("origin_ids") or ""),
                selection_reason=row.get("selection_reason"),
                scanner_setup_id=None,
            )
            detail = row.get("detail") or ""
            if "scanner_setup_id=" in detail:
                seg.scanner_setup_id = detail.split("scanner_setup_id=", 1)[-1]
            seg.levels_history.append(
                {
                    "decision_close_utc": row["decision_close_utc"],
                    "planned_entry": seg.planned_entry,
                    "planned_sl": seg.planned_sl,
                    "planned_tp": seg.planned_tp,
                }
            )
            by_id[oid] = seg
            segments.append(seg)
            active_id = oid
            continue

        if et == "PLANNED_SCANNER" and active_id and active_id in by_id:
            detail = row.get("detail") or ""
            if "scanner_setup_id=" in detail:
                by_id[active_id].scanner_setup_id = detail.split("scanner_setup_id=", 1)[-1]

        if active_id and active_id in by_id and et == "KEEP":
            seg = by_id[active_id]
            if row.get("planned_entry"):
                seg.planned_entry = float(row["planned_entry"])
                seg.planned_sl = float(row["planned_sl"])
                seg.planned_tp = float(row["planned_tp"]) if row.get("planned_tp") else None
                seg.levels_history.append(
                    {
                        "decision_close_utc": row["decision_close_utc"],
                        "planned_entry": seg.planned_entry,
                        "planned_sl": seg.planned_sl,
                        "planned_tp": seg.planned_tp,
                    }
                )

        if active_id and active_id in by_id and et in ("CANCEL", "REPLACE"):
            seg = by_id[active_id]
            seg.end_decision_close_utc = row["decision_close_utc"]
            seg.end_event_type = et
            seg.end_cancel_reason = row.get("cancel_reason")
            active_id = None

    return segments


def _entry_for_bar(seg: OrderSegmentV1, bar_utc: str) -> float:
    entry = seg.planned_entry
    for h in seg.levels_history:
        if h["decision_close_utc"] <= bar_utc:
            entry = float(h["planned_entry"])
    return entry


def _bars_between(
    prices: dict[str, dict[str, float]],
    start_utc: str,
    end_utc: str | None,
) -> list[str]:
    keys = sorted(prices.keys())
    out = []
    for t in keys:
        if t < start_utc:
            continue
        if end_utc is not None and t > end_utc:
            break
        out.append(t)
    return out


def _classify_touch_5m(
    *,
    limit: float,
    bar_utc: str,
    active_from: str,
    ohlc: dict[str, float],
    fill_model: str,
) -> dict[str, Any]:
    dec = evaluate_short_sell_limit_fill(
        fill_model=fill_model,
        limit_price=limit,
        bar_utc=bar_utc,
        order_active_from_utc=active_from,
        ohlc=ohlc,
    )
    h = float(ohlc["h"])
    o = float(ohlc["o"])
    high_touch = h >= limit
    return {
        "bar_utc": bar_utc,
        "open": o,
        "high": h,
        "low": float(ohlc["l"]),
        "close": float(ohlc["c"]),
        "limit": limit,
        "high_touch": high_touch,
        "causal_v2_filled": dec.filled,
        "causal_v2_rule_id": dec.rule_id,
        "causal_v2_ambiguous": dec.ambiguous,
        "causal_v2_note": dec.note,
    }


def _first_1m_touch(
    candles_1m: list[dict[str, Any]],
    *,
    start_utc: str,
    end_utc: str,
    limit: float,
) -> dict[str, Any] | None:
    start_dt = _parse_utc(start_utc)
    end_dt = _parse_utc(end_utc)
    for c in candles_1m:
        ct = c["close_time"]
        if isinstance(ct, datetime):
            close_dt = ct if ct.tzinfo else ct.replace(tzinfo=timezone.utc)
        else:
            close_dt = _parse_utc(str(ct))
        if close_dt < start_dt:
            continue
        if close_dt > end_dt:
            break
        if float(c["high"]) >= limit:
            return {
                "touch_1m_close_utc": _iso(close_dt),
                "open": c["open"],
                "high": c["high"],
                "low": c["low"],
                "close": c["close"],
            }
    return None


def analyze_segment(
    seg: OrderSegmentV1,
    prices_5m: dict[str, dict[str, float]],
    *,
    fill_model: str = "causal_sell_limit_v2",
    candles_1m: list[dict[str, Any]] | None = None,
    pilot_end_utc: str,
) -> dict[str, Any]:
    active_from = seg.pending_active_from_utc
    cancel_close = seg.end_decision_close_utc
    eval_end = cancel_close if cancel_close else pilot_end_utc

    touch_rows: list[dict[str, Any]] = []
    first_touch_bar: str | None = None
    first_causal_fill_bar: str | None = None
    first_high_touch_bar: str | None = None

    for bar in _bars_between(prices_5m, active_from, eval_end):
        ohlc = prices_5m.get(bar)
        if not ohlc:
            continue
        lim = _entry_for_bar(seg, bar)
        row = _classify_touch_5m(
            limit=lim,
            bar_utc=bar,
            active_from=active_from,
            ohlc=ohlc,
            fill_model=fill_model,
        )
        row["order_id"] = seg.order_id
        row["data_granularity"] = "5m"
        if row["high_touch"] and first_high_touch_bar is None:
            first_high_touch_bar = bar
        if row["causal_v2_filled"] and first_causal_fill_bar is None:
            first_causal_fill_bar = bar
        if row["high_touch"] or row["causal_v2_filled"]:
            touch_rows.append(row)
            if first_touch_bar is None:
                first_touch_bar = bar

    touch_1m: dict[str, Any] | None = None
    if candles_1m and eval_end:
        lim0 = _entry_for_bar(seg, active_from)
        touch_1m = _first_1m_touch(
            candles_1m,
            start_utc=active_from,
            end_utc=eval_end,
            limit=lim0,
        )

    cancel_effective = cancel_close
    limitations: list[str] = []
    cancel_assumes_unfilled = True
    fill_branch: FillBranch = "UNFILLED_PENDING"
    chronology: ChronologyStatus = "NO_TOUCH"

    if not first_high_touch_bar:
        chronology = "NO_TOUCH" if cancel_close else "STILL_PENDING_NO_TOUCH"
        fill_branch = "UNFILLED_PENDING" if cancel_close else "NOT_APPLICABLE_ENDED"
        if cancel_close:
            chronology = "CANCELLED_BEFORE_TOUCH"
    elif cancel_close is None:
        chronology = "STILL_PENDING_TOUCH_UNRESOLVED"
        fill_branch = "POSSIBLY_FILLED"
        cancel_assumes_unfilled = False
    elif first_high_touch_bar < cancel_close:
        cancel_assumes_unfilled = False
        fill_branch = "POSSIBLY_FILLED"
        if first_causal_fill_bar and first_causal_fill_bar <= cancel_close:
            chronology = "EXECUTION_POSSIBLE"
        else:
            chronology = "PRICE_TOUCHED_FILL_NOT_PROVEN"
    elif first_high_touch_bar == cancel_close:
        chronology = "EXECUTION_UNRESOLVED"
        fill_branch = "POSSIBLY_FILLED"
        cancel_assumes_unfilled = False
        limitations.append("5m_touch_same_bar_as_cancel_close")
        if touch_1m:
            t1m = touch_1m["touch_1m_close_utc"]
            if t1m < cancel_close:
                cancel_assumes_unfilled = False
                limitations.append("1m_touch_before_5m_cancel_close")
                if first_causal_fill_bar:
                    chronology = "EXECUTION_POSSIBLE"
                else:
                    chronology = "TOUCHED_BEFORE_CANCEL"
            else:
                limitations.append("1m_does_not_resolve_intrabar_ordering")
    else:
        chronology = "CANCELLED_BEFORE_TOUCH"
        fill_branch = "UNFILLED_PENDING"

    return {
        "order_id": seg.order_id,
        "decision_close_utc": seg.decision_close_utc,
        "pending_active_from_utc": active_from,
        "cancel_effective_decision_close_utc": cancel_effective,
        "end_event_type": seg.end_event_type,
        "end_cancel_reason": seg.end_cancel_reason,
        "planned_entry": seg.planned_entry,
        "planned_sl": seg.planned_sl,
        "planned_tp": seg.planned_tp,
        "pool_lower": seg.pool_lower,
        "pool_upper": seg.pool_upper,
        "origin_ids": seg.origin_ids,
        "selection_reason": seg.selection_reason,
        "scanner_setup_id": seg.scanner_setup_id,
        "first_high_touch_bar_5m": first_high_touch_bar,
        "first_causal_v2_fill_bar_5m": first_causal_fill_bar,
        "first_touch_1m_close_utc": touch_1m["touch_1m_close_utc"] if touch_1m else None,
        "chronology_status": chronology,
        "fill_branch": fill_branch,
        "bt41_cancel_conditional_on_unfilled": cancel_assumes_unfilled,
        "activation_contract": activation_contract_note(),
        "limitations": "|".join(limitations) if limitations else "",
        "touch_evidence_rows": touch_rows,
    }
