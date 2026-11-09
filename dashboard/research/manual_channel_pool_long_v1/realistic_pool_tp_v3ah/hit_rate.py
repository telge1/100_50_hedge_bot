"""Historical pool-target touch rates by distance bucket (causal forward scan)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from line_geometry import LineDef

from zone_density import build_zones_with_density

from support_sim import simulate_exit_v3y

EventKind = Literal[
    "TP_EXECUTABLE",
    "TOUCH_BEFORE_EXIT",
    "SL_FIRST",
    "SUPPORT_BREAK_FIRST",
    "CHANNEL_FIRST",
    "HORIZON_NO_TOUCH",
    "NO_ENTRY",
]

DIST_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("0.5_2", 0.5, 2.0),
    ("2_3", 2.0, 3.0),
    ("3_5", 3.0, 5.0),
    ("5_6", 5.0, 6.0),
    ("6_8", 6.0, 8.0),
    ("8_12", 8.0, 12.0),
)

OBSERVATION_HORIZON_NOTE = (
    "From entry bar (inclusive) through pilot simulation_end (last loaded 5m bar). "
    "No extension beyond L1 pilot candle window."
)


@dataclass
class TargetOutcome:
    touch_event_id: str
    entry_utc: str
    bucket: str
    target_bottom: float
    dist_pct: float
    integrated_density: float
    density_rank: int
    first_touch_bar_idx: int | None
    first_touch_utc: str
    exit_kind: str
    executable_tp: bool
    touch_before_adverse: bool
    bars_to_touch: int | None


def _fmt_ts(ts: int | None) -> str:
    if ts is None:
        return ""
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _first_high_touch_idx(candles: list[dict], start_idx: int, target: float) -> int | None:
    for j in range(start_idx, len(candles)):
        if float(candles[j]["high"]) >= target - 1e-15:
            return j
    return None


def _support_line(row: dict, lines_by_id: dict[str, LineDef]) -> LineDef | None:
    sid = row.get("touch_drawing_id") or ""
    return lines_by_id.get(sid)


def analyze_hit_rates(
    pending: list[dict],
    candles: list[dict],
    lines_by_id: dict[str, LineDef],
    *,
    simulation_end_ts: int,
) -> tuple[list[TargetOutcome], dict[str, dict[str, Any]]]:
    """Enumerate merged zone bottoms per entry; forward outcomes per bucket."""
    rows: list[TargetOutcome] = []
    last_idx = len(candles) - 1
    for row in pending:
        if not row.get("_entry_idx") or row.get("_d2_gate_ok") is not True:
            continue
        eid = row.get("touch_event_id") or ""
        entry = float(row["entry_price"])
        entry_idx = int(row["_entry_idx"])
        dc_s = row.get("_pool_decision_close") or ""
        if not dc_s:
            continue
        dc = datetime.strptime(dc_s, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
        members = row.get("_pool_members")
        scored, _ = build_zones_with_density(members, dc, entry)
        support = _support_line(row, lines_by_id)
        sl = float(row.get("_sl") or entry * 0.998)

        for rec in scored:
            z = rec["zone"]
            target = float(z.bottom)
            dist = rec["dist_from_entry_pct"]
            bucket_lbl = _bucket_for_dist(dist)
            if bucket_lbl is None:
                continue

            touch_idx = _first_high_touch_idx(candles, entry_idx, target)
            exit_kind, exit_ts, _xp, _pnl, exit_idx = simulate_exit_v3y(
                candles,
                entry_idx,
                entry,
                sl,
                target,
                None,
                support,
            )
            executable = exit_kind == "TP"
            touch_before = False
            if touch_idx is not None:
                if exit_idx is None:
                    touch_before = True
                else:
                    touch_before = touch_idx <= exit_idx
            elif executable:
                touch_before = True

            rows.append(
                TargetOutcome(
                    touch_event_id=eid,
                    entry_utc=row.get("entry_utc", ""),
                    bucket=bucket_lbl,
                    target_bottom=target,
                    dist_pct=round(dist, 4),
                    integrated_density=round(rec["integrated_density"], 8),
                    density_rank=rec["density_rank"],
                    first_touch_bar_idx=touch_idx,
                    first_touch_utc=_fmt_ts(candles[touch_idx]["time"] if touch_idx is not None else None),
                    exit_kind=str(exit_kind),
                    executable_tp=executable,
                    touch_before_adverse=touch_before and not executable,
                    bars_to_touch=(touch_idx - entry_idx) if touch_idx is not None else None,
                )
            )

    summary = _summarize_buckets(rows)
    summary["_meta"] = {
        "observation_horizon": OBSERVATION_HORIZON_NOTE,
        "simulation_end_utc": _fmt_ts(simulation_end_ts),
        "candle_bars": last_idx + 1,
    }
    return rows, summary


def _bucket_for_dist(dist: float) -> str | None:
    if dist < 0.5 - 1e-9:
        return None
    if dist <= 2.0 + 1e-9:
        return "0.5_2"
    if dist < 3.0 - 1e-9:
        return "2_3"
    if dist < 5.0 - 1e-9:
        return "3_5"
    if dist < 6.0 - 1e-9:
        return "5_6"
    if dist < 8.0 - 1e-9:
        return "6_8"
    if dist <= 12.0 + 1e-9:
        return "8_12"
    return None


def _summarize_buckets(rows: list[TargetOutcome]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name, _, _ in DIST_BUCKETS:
        sub = [r for r in rows if r.bucket == name]
        n = len(sub)
        touched = [r for r in sub if r.first_touch_bar_idx is not None]
        executable = [r for r in sub if r.executable_tp]
        touch_only = [r for r in sub if r.touch_before_adverse]
        btt = [r.bars_to_touch for r in touched if r.bars_to_touch is not None]
        avg_bars = sum(btt) / len(btt) if btt else None
        out[name] = {
            "candidates": n,
            "touched_high": len(touched),
            "hit_rate_pct": round(100.0 * len(touched) / n, 2) if n else 0.0,
            "executable_tp_exits": len(executable),
            "executable_rate_pct": round(100.0 * len(executable) / n, 2) if n else 0.0,
            "touch_before_adverse_not_tp": len(touch_only),
            "avg_bars_to_first_touch": round(avg_bars, 2) if avg_bars is not None else None,
            "missed_no_touch": n - len(touched),
        }
    return out
