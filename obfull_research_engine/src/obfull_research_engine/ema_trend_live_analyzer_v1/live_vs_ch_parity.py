"""Live fanout trades ↔ CH public_trades_canonical parity (fixture / delayed control)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class LiveVsChParityResult:
    ok: bool
    live_count: int = 0
    ch_count: int = 0
    missing_in_ch: list[str] = field(default_factory=list)
    extra_in_ch: list[str] = field(default_factory=list)
    duplicates_ch: list[str] = field(default_factory=list)
    field_mismatches: list[dict[str, Any]] = field(default_factory=list)
    order_mismatch: bool = False
    ch_visibility_lag_ms: list[float] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "live_count": self.live_count,
            "ch_count": self.ch_count,
            "missing_in_ch": list(self.missing_in_ch),
            "extra_in_ch": list(self.extra_in_ch),
            "duplicates_ch": list(self.duplicates_ch),
            "field_mismatches": list(self.field_mismatches),
            "order_mismatch": self.order_mismatch,
            "ch_visibility_lag_ms_n": len(self.ch_visibility_lag_ms),
            "ch_visibility_lag_ms_p50": _pct(self.ch_visibility_lag_ms, 50),
            "ch_visibility_lag_ms_p95": _pct(self.ch_visibility_lag_ms, 95),
        }


def _pct(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    arr = sorted(xs)
    idx = min(len(arr) - 1, max(0, int(round((p / 100.0) * (len(arr) - 1)))))
    return float(arr[idx])


def _tid(row: dict[str, Any]) -> str:
    return str(row.get("trade_id") or "").strip()


def compare_live_vs_ch(
    live_rows: list[dict[str, Any]],
    ch_rows: list[dict[str, Any]],
) -> LiveVsChParityResult:
    """Match primarily on trade_id; CH is control, not live decision source."""
    live_by = {}
    for r in live_rows:
        tid = _tid(r)
        if tid:
            live_by[tid] = r
    ch_by: dict[str, dict[str, Any]] = {}
    dups: list[str] = []
    for r in ch_rows:
        tid = _tid(r)
        if not tid:
            continue
        if tid in ch_by:
            dups.append(tid)
        ch_by[tid] = r

    missing = sorted(set(live_by) - set(ch_by))
    extra = sorted(set(ch_by) - set(live_by))
    mismatches: list[dict[str, Any]] = []
    lags: list[float] = []
    for tid in sorted(set(live_by) & set(ch_by)):
        lv, ch = live_by[tid], ch_by[tid]
        pairs = (
            ("side", "side", "side"),
            ("price", "price", "price"),
            ("quantity", "quantity", "size"),
        )
        for field, lk, ck in pairs:
            lv_v = lv.get(lk)
            ch_v = ch.get(ck) if ck in ch else ch.get(lk)
            if lv_v is None or ch_v is None:
                continue
            if field == "side":
                if str(lv_v).lower() != str(ch_v).lower():
                    mismatches.append({"trade_id": tid, "field": field, "live": lv_v, "ch": ch_v})
                continue
            try:
                if abs(float(lv_v) - float(ch_v)) > 1e-9:
                    mismatches.append({"trade_id": tid, "field": field, "live": lv_v, "ch": ch_v})
            except (TypeError, ValueError):
                if str(lv_v) != str(ch_v):
                    mismatches.append({"trade_id": tid, "field": field, "live": lv_v, "ch": ch_v})
        try:
            from datetime import datetime

            et = lv.get("exchange_event_time")
            it = ch.get("ingest_timestamp") or ch.get("clickhouse_ingest_timestamp")
            if et and it:

                def _p(x: Any) -> datetime | None:
                    if isinstance(x, datetime):
                        return x
                    s = str(x).replace("Z", "+00:00")
                    try:
                        return datetime.fromisoformat(s)
                    except ValueError:
                        return None

                e_dt, i_dt = _p(et), _p(it)
                if e_dt and i_dt:
                    lags.append((i_dt - e_dt).total_seconds() * 1000.0)
        except Exception:
            pass

    live_order = [_tid(r) for r in live_rows if _tid(r)]
    ch_order = [_tid(r) for r in ch_rows if _tid(r) in live_by]
    order_mismatch = ch_order != [t for t in live_order if t in ch_by]

    ok = not missing and not extra and not dups and not mismatches and not order_mismatch
    return LiveVsChParityResult(
        ok=ok,
        live_count=len(live_by),
        ch_count=len(ch_by),
        missing_in_ch=missing,
        extra_in_ch=extra,
        duplicates_ch=dups,
        field_mismatches=mismatches,
        order_mismatch=order_mismatch,
        ch_visibility_lag_ms=lags,
    )
