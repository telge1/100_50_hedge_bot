"""D2 landscape metrics (canonical bands only — no micro fallback)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from config import MIN_POOL_DIST_PCT
from config import (
    D2_MIN_DEPTH,
    MAX_DISTANCE_TO_FIRST_D2_UPPER_PCT,
    MIN_D2_BANDS_IN_FIRST_CHANNEL_QUARTER,
)
from pd1_exact_d2_adapter import D2Band, qualify_upper_d2_bands, upper_d2_bands_at_close


@dataclass
class D2LandscapeMetrics:
    d2_band_count: int
    d2_qualified_count: int
    nearest_d2_dist_pct: float | None
    second_d2_dist_pct: float | None
    nearest_d2_depth: int | None
    nearest_d2_width_pct: float | None
    max_gap_d2_pct: float | None
    gate_pass: bool
    gate_reason: str | None
    research_pass: bool
    research_reason: str | None


def analyze_d2_landscape(
    members: list[dict] | None,
    decision_close: datetime,
    entry: float,
    *,
    apply_provisional_research_filter: bool = False,
) -> tuple[D2LandscapeMetrics, list[D2Band], list[dict[str, Any]]]:
    bands, err = upper_d2_bands_at_close(members, decision_close, entry)
    if err:
        m = D2LandscapeMetrics(
            0, 0, None, None, None, None, None, False, err, False, err
        )
        return m, [], []

    qualified = qualify_upper_d2_bands(bands, entry, MIN_POOL_DIST_PCT)
    dists = [(q["lower_bound"] - entry) / entry * 100.0 for q in qualified]
    gaps: list[float] = []
    for i in range(1, len(qualified)):
        gaps.append(
            (float(qualified[i]["lower_bound"]) - float(qualified[i - 1]["upper_bound"]))
            / entry
            * 100.0
        )

    gate_pass = len(qualified) >= 1
    gate_reason = None if gate_pass else "D2_GATE_NO_QUALIFIED_UPPER_BAND"

    research_pass = gate_pass
    research_reason = None
    if apply_provisional_research_filter:
        # Micro-derived thresholds deliberately NOT applied (L1 V3B).
        research_reason = "D2_RESEARCH_FILTER_NOT_CALIBRATED"
        research_pass = False

    n0 = qualified[0] if qualified else None
    m = D2LandscapeMetrics(
        d2_band_count=len(bands),
        d2_qualified_count=len(qualified),
        nearest_d2_dist_pct=dists[0] if dists else None,
        second_d2_dist_pct=dists[1] if len(dists) > 1 else None,
        nearest_d2_depth=int(n0["d2_depth"]) if n0 else None,
        nearest_d2_width_pct=float(n0["width_pct"]) if n0 else None,
        max_gap_d2_pct=max(gaps) if gaps else None,
        gate_pass=gate_pass,
        gate_reason=gate_reason,
        research_pass=research_pass,
        research_reason=research_reason,
    )
    return m, bands, qualified


def apply_d2_gate_to_pending(
    pending: list[dict],
    lines: list,
    *,
    l1_module: Any,
    apply_provisional_research_filter: bool = False,
) -> None:
    from datetime import datetime, timezone

    for row in pending:
        if not row.get("_entry_idx"):
            continue
        members = row.get("_pool_members")
        entry = float(row["entry_price"])
        entry_ts = int(row["_entry_ts"])
        sl = entry * (1.0 - l1_module.SL_PCT / 100.0)
        row["_sl"] = sl
        row["sl_price"] = f"{sl:.10f}"

        dc_s = row.get("_pool_decision_close") or ""
        if not dc_s:
            row["_d2_gate_ok"] = False
            row["_pick_ok"] = False
            row["reject_reason"] = "UNRESOLVED_D2_DECISION_CLOSE"
            continue
        dc = datetime.strptime(dc_s, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)

        m, _bands, qualified = analyze_d2_landscape(
            members,
            dc,
            entry,
            apply_provisional_research_filter=apply_provisional_research_filter,
        )
        row.update(metrics_to_fields(m))
        row["_d2_gate_ok"] = m.gate_pass
        row["_d2_research_pass"] = m.research_pass

        channel_upper, channel_id = l1_module.channel_upper_at(lines, entry_ts, entry)
        lines_by_id = {ln.drawing_id: ln for ln in lines}
        if members is None:
            pick = l1_module.PoolPick(
                None, "NONE", None, channel_id, None, 0, False, "POOL_SSOT_FAIL"
            )
            pick_ok = False
            ch_line = lines_by_id.get(channel_id) if channel_id else None
        else:
            pick = l1_module.pick_tp(entry, qualified, channel_upper, channel_id)
            ch_id = pick.tp_channel_line_id
            ch_line = lines_by_id.get(ch_id) if ch_id else None
            pick_ok = (
                pick.gate_ok
                and not pick.block_reason
                and (l1_module.pool_tp_for_exit(pick) is not None or ch_line is not None)
            )

        row["tp_mode"] = pick.tp_mode
        row["tp_price"] = f"{pick.tp_price:.10f}" if pick.tp_price is not None else ""
        row["tp_pool_id"] = pick.tp_pool_id or ""
        row["tp_channel_line_id"] = pick.tp_channel_line_id or ""
        row["tp_target_dist_pct"] = (
            f"{pick.tp_dist_pct:.4f}" if pick.tp_dist_pct is not None else ""
        )
        row["pool_gate_qualified_count"] = str(pick.gate_count)
        row["pool_ssot"] = "5m_PD1_D2_EXACT"
        row["reject_reason"] = pick.block_reason or row.get("reject_reason") or ""
        row["_pool_tp"] = l1_module.pool_tp_for_exit(pick)
        row["_channel_line"] = ch_line
        row["dynamic_channel_exit"] = "yes" if ch_line is not None else "no"
        row["_pick_ok"] = bool(pick_ok and m.gate_pass)
        if not m.gate_pass and not row.get("reject_reason"):
            row["reject_reason"] = m.gate_reason or "D2_GATE_FAIL"


def _clip_interval(lo: float, hi: float, a: float, b: float) -> tuple[float, float] | None:
    x0, x1 = max(lo, a), min(hi, b)
    if x1 <= x0:
        return None
    return x0, x1


@dataclass
class D2ChannelFilterMetrics:
    first_d2_bottom: float | None
    first_d2_dist_pct: float | None
    first_d2_share_of_channel_pct: float | None
    d2_bands_in_channel: int
    d2_bands_q1: int
    d2_bands_q2: int
    d2_bands_q3: int
    d2_bands_q4: int
    nearness_pass: bool
    stagger_pass: bool
    nearness_reason: str | None
    stagger_reason: str | None


def bands_in_channel_room(
    bands: list[D2Band], entry: float, channel_upper: float | None
) -> list[tuple[D2Band, float, float]]:
    if channel_upper is None or channel_upper <= entry:
        return []
    out: list[tuple[D2Band, float, float]] = []
    for b in bands:
        if b.top <= entry:
            continue
        intr = _clip_interval(b.bottom, b.top, entry, channel_upper)
        if intr:
            out.append((b, intr[0], intr[1]))
    return out


def section_band_counts(
    in_channel: list[tuple[D2Band, float, float]],
    entry: float,
    channel_upper: float,
) -> tuple[int, int, int, int]:
    span = channel_upper - entry
    counts = [0, 0, 0, 0]
    for q in range(4):
        s_lo = entry + span * (q / 4.0)
        s_hi = entry + span * ((q + 1) / 4.0)
        for _, c0, c1 in in_channel:
            if _clip_interval(c0, c1, s_lo, s_hi):
                counts[q] += 1
    return tuple(counts)


def analyze_d2_channel_filters(
    bands: list[D2Band],
    entry: float,
    channel_upper: float | None,
    *,
    max_first_d2_pct: float = MAX_DISTANCE_TO_FIRST_D2_UPPER_PCT,
    min_q1_bands: int = MIN_D2_BANDS_IN_FIRST_CHANNEL_QUARTER,
    check_stagger: bool = True,
    independent_no_channel: bool = False,
) -> D2ChannelFilterMetrics:
    room_pct = (
        (channel_upper - entry) / entry * 100.0
        if channel_upper and channel_upper > entry
        else None
    )
    in_ch = bands_in_channel_room(bands, entry, channel_upper)
    above = [b for b, _, _ in in_ch if b.bottom > entry]
    if independent_no_channel and (channel_upper is None or channel_upper <= entry):
        above = [b for b in bands if b.bottom > entry + 1e-12]
    first_dist: float | None = None
    first_bottom: float | None = None
    if above:
        first_bottom = min(b.bottom for b in above)
        first_dist = (first_bottom - entry) / entry * 100.0

    share = (
        (first_dist / room_pct * 100.0)
        if first_dist is not None and room_pct and room_pct > 0
        else None
    )
    q1 = q2 = q3 = q4 = 0
    if channel_upper and channel_upper > entry:
        q1, q2, q3, q4 = section_band_counts(in_ch, entry, channel_upper)

    if first_dist is None:
        near_ok = False
        near_reason = "REJECTED_FIRST_D2_TOO_FAR"
    elif first_dist > max_first_d2_pct + 1e-12:
        near_ok = False
        near_reason = "REJECTED_FIRST_D2_TOO_FAR"
    else:
        near_ok = True
        near_reason = None

    if not check_stagger:
        stag_ok = True
        stag_reason = None
    elif independent_no_channel and (channel_upper is None or channel_upper <= entry):
        stag_ok = False
        stag_reason = "C_CHANNEL_QUARTER_UNAVAILABLE"
    elif q1 < min_q1_bands:
        stag_ok = False
        stag_reason = "REJECTED_NO_EARLY_D2_STAGGERING"
    else:
        stag_ok = True
        stag_reason = None

    return D2ChannelFilterMetrics(
        first_d2_bottom=first_bottom,
        first_d2_dist_pct=first_dist,
        first_d2_share_of_channel_pct=share,
        d2_bands_in_channel=len(in_ch),
        d2_bands_q1=q1,
        d2_bands_q2=q2,
        d2_bands_q3=q3,
        d2_bands_q4=q4,
        nearness_pass=near_ok,
        stagger_pass=stag_ok,
        nearness_reason=near_reason,
        stagger_reason=stag_reason,
    )


def enrich_d2_channel_filter_fields(
    pending: list[dict],
    upper_line,
    l1_module: Any,
    *,
    independent: bool = False,
) -> None:
    from datetime import datetime, timezone

    channel_line_at_bar = l1_module.channel_line_at_bar
    for row in pending:
        if not row.get("_entry_idx"):
            continue
        entry = float(row["entry_price"])
        entry_ts = int(row["_entry_ts"])
        if independent and "_channel_deckel_at_entry" in row:
            main_up = row.get("_channel_deckel_at_entry")
        else:
            main_up = channel_line_at_bar(upper_line, {"time": float(entry_ts)})
        row["_main_upper_at_entry"] = main_up
        members = row.get("_pool_members")
        dc_s = row.get("_pool_decision_close") or ""
        if not dc_s or members is None:
            row["_d2_nearness_ok"] = False
            row["_d2_stagger_ok"] = False
            row["d2_first_in_channel_pct"] = ""
            row["d2_first_share_channel_pct"] = ""
            row["d2_bands_in_channel"] = "0"
            row["d2_quarter_bands"] = ""
            continue
        dc = datetime.strptime(dc_s, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
        bands, err = upper_d2_bands_at_close(members, dc, entry, min_depth=D2_MIN_DEPTH)
        if err:
            bands = []
        m = analyze_d2_channel_filters(
            bands,
            entry,
            main_up,
            check_stagger=True,
            independent_no_channel=independent,
        )
        row["_d2_nearness_ok"] = m.nearness_pass
        row["_d2_stagger_ok"] = m.stagger_pass
        row["d2_nearness_reason"] = m.nearness_reason or ""
        row["d2_stagger_reason"] = m.stagger_reason or ""
        row["d2_first_in_channel_pct"] = (
            f"{m.first_d2_dist_pct:.6f}" if m.first_d2_dist_pct is not None else ""
        )
        row["d2_first_share_channel_pct"] = (
            f"{m.first_d2_share_of_channel_pct:.4f}" if m.first_d2_share_of_channel_pct is not None else ""
        )
        row["d2_bands_in_channel"] = str(m.d2_bands_in_channel)
        row["d2_quarter_bands"] = f"{m.d2_bands_q1}/{m.d2_bands_q2}/{m.d2_bands_q3}/{m.d2_bands_q4}"


def channel_filter_metrics_to_fields(m: D2ChannelFilterMetrics) -> dict[str, str]:
    return {
        "d2_first_in_channel_pct": (
            f"{m.first_d2_dist_pct:.6f}" if m.first_d2_dist_pct is not None else ""
        ),
        "d2_first_share_channel_pct": (
            f"{m.first_d2_share_of_channel_pct:.4f}" if m.first_d2_share_of_channel_pct is not None else ""
        ),
        "d2_bands_in_channel": str(m.d2_bands_in_channel),
        "d2_quarter_bands": f"{m.d2_bands_q1}/{m.d2_bands_q2}/{m.d2_bands_q3}/{m.d2_bands_q4}",
        "d2_nearness_pass": "yes" if m.nearness_pass else "no",
        "d2_stagger_pass": "yes" if m.stagger_pass else "no",
    }


def metrics_to_fields(m: D2LandscapeMetrics) -> dict[str, str]:
    def f(v: float | None, places: int = 4) -> str:
        return f"{v:.{places}f}" if v is not None else ""

    return {
        "d2_band_count": str(m.d2_band_count),
        "d2_qualified_count": str(m.d2_qualified_count),
        "d2_nearest_dist_pct": f(m.nearest_d2_dist_pct),
        "d2_second_dist_pct": f(m.second_d2_dist_pct),
        "d2_nearest_depth": str(m.nearest_d2_depth or ""),
        "d2_nearest_width_pct": f(m.nearest_d2_width_pct),
        "d2_max_gap_pct": f(m.max_gap_d2_pct),
        "d2_gate_pass": "yes" if m.gate_pass else "no",
        "d2_gate_reason": m.gate_reason or "",
        "d2_research_pass": "yes" if m.research_pass else "no",
        "d2_research_reason": m.research_reason or "",
    }
