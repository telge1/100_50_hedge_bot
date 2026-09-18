"""V1 event audit against Silver 100ms mids (read-only)."""

from __future__ import annotations

import csv
import json
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

from obfull_research_engine.mp_edge_event_study_v1.schema import MidTick
from obfull_research_engine.mp_edge_event_study_v1.util import NS, format_ns_z
from obfull_research_engine.timeparse import format_utc_z

from .sides import break_side_for, fade_side_for, trade_side_for


@dataclass
class AuditRow:
    event_id: str
    label_v1: str
    event_role: str
    fade_side_v1: str
    confluence_class: str
    first_touch_ts: str
    zone_low: float
    zone_high: float
    mid_30s_before: float | None
    approach_from_expected_side: bool | None
    would_be_armed_origin_2bps: bool | None
    likely_profile_activation_touch: bool | None
    penetration_ts: str
    first_acceptance_30s_ts: str
    reclaim_within_120s: bool | None
    reclaim_held_15s: bool | None
    v1_label_plausible: str
    v1_outcome_direction_correct: bool
    correct_trade_side: str
    notes: str


def _bisect_mids(mids: Sequence[MidTick], ts_ns: int) -> int:
    lo, hi = 0, len(mids)
    while lo < hi:
        mid = (lo + hi) // 2
        if mids[mid].ts_ns < ts_ns:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _mid_at_or_before(mids: Sequence[MidTick], ts_ns: int) -> MidTick | None:
    i = _bisect_mids(mids, ts_ns + 1) - 1
    if i < 0:
        return None
    return mids[i]


def audit_v1_sample(
    events_csv: Path,
    mids: Sequence[MidTick],
    *,
    out_csv: Path,
    out_md: Path,
) -> dict[str, Any]:
    rows_in = list(csv.DictReader(events_csv.open(encoding="utf-8")))
    by_label: dict[str, list[dict[str, Any]]] = {"TRUE_BREAK": [], "FAILED_BREAK": [], "ABSORB": []}
    for r in rows_in:
        lab = r["label"]
        if lab in by_label:
            by_label[lab].append(r)

    def prefer_confl(cands: list[dict[str, Any]], n: int, role: str | None = None) -> list[dict[str, Any]]:
        filt = [c for c in cands if role is None or c["event_role"] == role]
        filt.sort(
            key=lambda e: (
                0 if str(e["confluence_class"]).startswith(("C2", "C3")) else 1,
                int(e["first_touch_ts_ns"]),
            )
        )
        return filt[:n]

    sample: list[dict[str, Any]] = []
    sample.extend(prefer_confl(by_label["TRUE_BREAK"], 5, "UPPER"))
    sample.extend(prefer_confl(by_label["TRUE_BREAK"], 5, "LOWER"))
    sample.extend(by_label["FAILED_BREAK"])  # all
    sample.extend(prefer_confl(by_label["ABSORB"], 5))

    # de-dup by event_id preserving order
    seen: set[str] = set()
    uniq: list[dict[str, Any]] = []
    for r in sample:
        if r["event_id"] in seen:
            continue
        seen.add(r["event_id"])
        uniq.append(r)

    audits: list[AuditRow] = []
    wrong_dir = 0
    for r in uniq:
        role = r["event_role"]
        label = r["label"]
        touch_ns = int(r["first_touch_ts_ns"])
        lo = float(r["confluence_low"])
        hi = float(r["confluence_high"])
        look_ns = touch_ns - 30 * NS
        before = _mid_at_or_before(mids, look_ns)
        mid_before = before.mid if before else None
        expected_below = role == "UPPER"
        approach_ok = None
        armed_ok = None
        if mid_before is not None:
            if expected_below:
                approach_ok = mid_before < lo
                armed_ok = mid_before < lo and (lo - mid_before) / lo * 1e4 >= 2.0
            else:
                approach_ok = mid_before > hi
                armed_ok = mid_before > hi and (mid_before - hi) / hi * 1e4 >= 2.0

        # profile activation heuristic: mid at touch already beyond/inside and 30s before also inside/beyond
        at_touch = _mid_at_or_before(mids, touch_ns)
        profile_act = None
        if at_touch and mid_before is not None:
            inside_now = lo <= at_touch.mid <= hi or (
                (role == "UPPER" and at_touch.mid > hi) or (role == "LOWER" and at_touch.mid < lo)
            )
            inside_before = lo <= mid_before <= hi or (
                (role == "UPPER" and mid_before > hi) or (role == "LOWER" and mid_before < lo)
            )
            profile_act = bool(inside_now and inside_before and not approach_ok)

        # scan forward 120s from touch for pen / acceptance / reclaim
        end_scan = touch_ns + 180 * NS
        i0 = _bisect_mids(mids, touch_ns)
        pen_ts = None
        accept_ts = None
        beyond_run = None
        reclaim_ts = None
        reclaim_held = None
        for j in range(i0, len(mids)):
            t = mids[j]
            if t.ts_ns > end_scan:
                break
            if not t.valid:
                continue
            if role == "UPPER":
                beyond = t.mid > hi
                pen_bps = (t.mid - hi) / hi * 1e4 if beyond else 0.0
                reclaimed = t.mid < lo
            else:
                beyond = t.mid < lo
                pen_bps = (lo - t.mid) / lo * 1e4 if beyond else 0.0
                reclaimed = t.mid > hi
            if pen_ts is None and pen_bps >= 2.0:
                pen_ts = t.ts_ns
                beyond_run = t.ts_ns
            if pen_ts is not None:
                if beyond:
                    if beyond_run is None:
                        beyond_run = t.ts_ns
                    elif accept_ts is None and (t.ts_ns - beyond_run) / NS >= 30:
                        accept_ts = beyond_run + 30 * NS
                else:
                    beyond_run = None
                if reclaimed and reclaim_ts is None and (t.ts_ns - pen_ts) / NS <= 120:
                    reclaim_ts = t.ts_ns
                    # check hold 15s
                    hold_ok = True
                    hold_end = t.ts_ns + 15 * NS
                    for k in range(j, len(mids)):
                        u = mids[k]
                        if u.ts_ns > hold_end:
                            break
                        if role == "UPPER":
                            if not (u.mid < lo):
                                hold_ok = False
                                break
                        else:
                            if not (u.mid > hi):
                                hold_ok = False
                                break
                    reclaim_held = hold_ok

        correct_side, _ = trade_side_for(role, label)
        v1_dir_ok = (r["fade_side"] == correct_side) if label != "TRUE_BREAK" else False
        if label == "TRUE_BREAK":
            wrong_dir += 1
            # V1 always stored fade_side and used it for outcomes
            v1_dir_ok = False

        plausible = "UNKNOWN"
        if label == "TRUE_BREAK" and reclaim_held:
            plausible = "NO_SHOULD_BE_FAILED_BREAK_IF_RECLAIM_HELD"
        elif label == "TRUE_BREAK" and accept_ts and not reclaim_held:
            plausible = "MAYBE_EARLY_TRUE_BREAK_WITHOUT_120S_HORIZON"
        elif label == "ABSORB" and approach_ok is False:
            plausible = "QUESTIONABLE_WRONG_APPROACH"
        elif label == "FAILED_BREAK" and reclaim_held:
            plausible = "YES"
        elif label == "ABSORB" and approach_ok:
            plausible = "PLAUSIBLE_IF_NO_PEN"
        else:
            plausible = "REVIEW"

        notes = []
        if label == "TRUE_BREAK":
            notes.append("V1_OUTCOME_USED_FADE_SIDE_FACHLICH_UNGUELTIG")
            notes.append(f"correct_trade_side={correct_side}")
        if approach_ok is False:
            notes.append("wrong_approach_30s_before")
        if profile_act:
            notes.append("possible_profile_activation_touch")

        audits.append(
            AuditRow(
                event_id=r["event_id"],
                label_v1=label,
                event_role=role,
                fade_side_v1=r["fade_side"],
                confluence_class=r["confluence_class"],
                first_touch_ts=format_ns_z(touch_ns),
                zone_low=lo,
                zone_high=hi,
                mid_30s_before=mid_before,
                approach_from_expected_side=approach_ok,
                would_be_armed_origin_2bps=armed_ok,
                likely_profile_activation_touch=profile_act,
                penetration_ts=format_ns_z(pen_ts) if pen_ts else "",
                first_acceptance_30s_ts=format_ns_z(accept_ts) if accept_ts else "",
                reclaim_within_120s=bool(reclaim_ts) if pen_ts else None,
                reclaim_held_15s=reclaim_held,
                v1_label_plausible=plausible,
                v1_outcome_direction_correct=v1_dir_ok,
                correct_trade_side=correct_side,
                notes=";".join(notes),
            )
        )

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(asdict(audits[0]).keys()) if audits else ["event_id"])
        w.writeheader()
        for a in audits:
            w.writerow(asdict(a))

    n_wrong_approach = sum(1 for a in audits if a.approach_from_expected_side is False)
    n_tb = sum(1 for a in audits if a.label_v1 == "TRUE_BREAK")
    lines = [
        "# V1 Event Audit",
        "",
        "## Critical outcome finding",
        "",
        "V1 `compute_outcomes` passes `ev.fade_side` into MFE/MAE and TP/SL for **all** labels,",
        "including `TRUE_BREAK`. Correct trade directions:",
        "",
        "- UPPER+ABSORB/FAILED_BREAK → SHORT",
        "- UPPER+TRUE_BREAK → LONG",
        "- LOWER+ABSORB/FAILED_BREAK → LONG",
        "- LOWER+TRUE_BREAK → SHORT",
        "",
        "**Therefore all V1 TRUE_BREAK TP/SL/MFE/MAE results are FACHLICH_UNGUELTIG.**",
        "",
        "## ABSORB V1 definition (actual code)",
        "",
        "Touch + never `min_penetration_bps` beyond outer edge + fade distance",
        "`>= min_penetration_bps` past inner edge. No approach/arming filter.",
        "",
        "## TRUE_BREAK V1 timing",
        "",
        "Finalizes after continuous `true_break_acceptance_s` (30s) beyond the zone",
        "without waiting for a later reclaim inside a longer horizon — early lock-in.",
        "",
        f"## Sample size: {len(audits)} (TB_UPPER/LOWER up to 5 each, all FAILED_BREAK, 5 ABSORB)",
        "",
        f"- TRUE_BREAK in sample with wrong V1 outcome direction: {n_tb}/{n_tb}",
        f"- Sample events with wrong approach (30s before): {n_wrong_approach}",
        "",
        "## Rows",
        "",
    ]
    for a in audits:
        lines.append(
            f"- `{a.label_v1}` {a.event_role} {a.confluence_class} touch={a.first_touch_ts} "
            f"approach_ok={a.approach_from_expected_side} armed2bps={a.would_be_armed_origin_2bps} "
            f"reclaim120={a.reclaim_within_120s} held={a.reclaim_held_15s} "
            f"v1_dir_ok={a.v1_outcome_direction_correct} correct_trade={a.correct_trade_side} "
            f"plausible={a.v1_label_plausible} notes={a.notes}"
        )
    lines.append("")
    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return {
        "n_audited": len(audits),
        "true_break_outcome_direction_correct": False,
        "n_wrong_approach_in_sample": n_wrong_approach,
        "v1_true_break_outcomes_invalid": True,
    }
