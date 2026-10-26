#!/usr/bin/env python3
"""Phase 3B — run two-pool market structure detector (Phase 3A inputs only)."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import sys

ROOT = Path(__file__).resolve().parents[2]
TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
CONFIG_PATH = TOOLS / "research_config_v1.json"
RESULTS = ROOT / "results" / "phase3b_two_pool_market_structure_detector_v1"
REPORT = ROOT / "reports" / "DOGEUSDT_PHASE3B_TWO_POOL_MARKET_STRUCTURE_DETECTOR_V1.md"
PHASE2A = ROOT / "results" / "phase2a_5m_pd1_depth2_short_candidates_v1"

from two_pool_detector import (  # noqa: E402
    blocked_short_candidates,
    run_detector,
)


def load_config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def write_csv(path: Path, rows: list[dict], fieldnames: list[str] | None = None) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = fieldnames or list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def load_short_candidates() -> list[dict[str, Any]]:
    path = PHASE2A / "candidates.csv"
    if not path.is_file():
        return []
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def tp_rows_at_confirmations(st) -> list[dict[str, Any]]:
    rows = []
    for ep in st.episodes:
        if not ep.long_candidate_utc or not ep.tp_uppers:
            continue
        for i, tp in enumerate(ep.tp_uppers[:15]):
            rows.append(
                {
                    "episode_id": ep.episode_id,
                    "confirmation_close_utc": ep.long_candidate_utc,
                    "tp_rank": i + 1,
                    "pool_track_id": tp.get("pool_track_id"),
                    "bottom_price": tp["bottom_price"],
                    "top_price": tp["top_price"],
                    "depth": tp.get("depth"),
                    "distance_above_close_pct": tp.get("distance_above_close_pct"),
                    "origins": json.dumps(tp.get("origins") or []),
                }
            )
    return rows


def write_reference_comparison(st, cfg: dict) -> str:
    first_watch = None
    first_long = None
    for row in st.timeline_rows:
        if row["market_state"] == "WATCH_REVERSAL" and not first_watch:
            first_watch = row["as_of_close_utc"]
        if row["market_state"] == "LONG_CANDIDATE" and not first_long:
            first_long = row["as_of_close_utc"]

    lines = [
        "# Reference case comparison (Phase 3B vs Phase 3A narrative)",
        "",
        "Phase 3A narrative hints (not hardcoded in detector):",
        "- Lower REACH search ~0.0834–0.0836: `2026-09-11T12:20:00Z`",
        "- POOL_NEW search ~0.0841–0.0842: `2026-09-11T13:30:00Z`",
        "",
        "## Automatic detector",
        "",
        f"- First global `WATCH_REVERSAL` bar: `{first_watch or 'NONE'}`",
        f"- First global `LONG_CANDIDATE` bar: `{first_long or 'NONE_DETECTED'}`",
        f"- Lower reach episodes detected: **{len(st.reach_rows)}**",
        f"- Second-lower confirmations: **{len(st.confirm_rows)}**",
        "",
        "## Episode 13:30 vs separate move (research question)",
        "",
    ]
    ep_1330 = [
        r
        for r in st.confirm_rows
        if r.get("confirmation_close_utc") == "2026-09-11T13:30:00Z"
    ]
    if ep_1330:
        r = ep_1330[0]
        lines.append(
            f"- Confirmation at 13:30 links to episode **{r['episode_id']}** "
            f"with event A at `{r['event_a_utc']}` (recovery `{r['recovery_utc']}`)."
        )
        lines.append(
            "- If event A is far earlier than the user reversal narrative, classify as "
            "**possible false episode coupling** — review `detected_lower_reach_episodes.csv`."
        )
    else:
        lines.append("- No LONG confirmation exactly at 13:30 in this run.")

    lines.append("")
    lines.append("## First three reach episodes")
    lines.append("")
    for r in st.reach_rows[:3]:
        lines.append(
            f"- Ep {r['episode_id']}: reach `{r['reach_utc']}` "
            f"[{r['lower_bottom']}, {r['lower_top']}] gap {r['gap_pct']:.4f}%"
        )

    text = "\n".join(lines)
    (RESULTS / "reference_case_comparison.md").write_text(text, encoding="utf-8")
    return text


def verdict(st) -> str:
    if not st.timeline_rows:
        return "TWO_POOL_DETECTOR_BLOCKED"
    if not st.reach_rows:
        return "TWO_POOL_DETECTOR_PARTIAL"
    if not st.confirm_rows:
        return "TWO_POOL_DETECTOR_PARTIAL"
    # Mechanics work; user-chart narrative alignment remains NOT_VERIFIED (see reference_case_comparison.md).
    if len(st.reach_rows) > 20:
        return "TWO_POOL_DETECTOR_PARTIAL"
    return "TWO_POOL_DETECTOR_READY"


def main() -> int:
    RESULTS.mkdir(parents=True, exist_ok=True)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    phase3a = ROOT / cfg["phase3a_results_dir"]

    st = run_detector(phase3a, cfg)
    blocked = blocked_short_candidates(st, load_short_candidates())
    tp_rows = tp_rows_at_confirmations(st)

    write_csv(RESULTS / "market_state_timeline.csv", st.timeline_rows)
    write_csv(RESULTS / "detected_lower_reach_episodes.csv", st.reach_rows)
    write_csv(RESULTS / "second_lower_confirmation_events.csv", st.confirm_rows)
    write_csv(RESULTS / "blocked_short_windows.csv", blocked)
    write_csv(RESULTS / "upper_tp_candidates_at_confirmation.csv", tp_rows)

    (RESULTS / "state_transition_evidence.json").write_text(
        json.dumps(
            {
                "config": cfg,
                "transitions": st.transitions,
                "episodes": [
                    {
                        "episode_id": e.episode_id,
                        "event_a_utc": e.event_a_utc,
                        "event_a_bounds": [e.event_a_bottom, e.event_a_top],
                        "event_a_origins": e.event_a_origins,
                        "event_a_gap": e.event_a_gap,
                        "recovery_utc": e.recovery_utc,
                        "event_b_utc": e.event_b_utc,
                        "event_b_bounds": [e.event_b_bottom, e.event_b_top]
                        if e.event_b_bottom is not None
                        else None,
                        "event_b_origins": e.event_b_origins,
                        "event_b_gap": e.event_b_gap,
                        "long_candidate_utc": e.long_candidate_utc,
                        "identity_notes": e.identity_notes,
                    }
                    for e in st.episodes
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    ref = write_reference_comparison(st, cfg)

    first_watch = next(
        (r["as_of_close_utc"] for r in st.timeline_rows if r["market_state"] == "WATCH_REVERSAL"),
        None,
    )
    first_long = next(
        (r["as_of_close_utc"] for r in st.timeline_rows if r["market_state"] == "LONG_CANDIDATE"),
        None,
    )
    final_verdict = verdict(st)

    summary = [
        "# Phase 3B detection summary",
        "",
        f"**VERDICT:** `{final_verdict}`",
        "",
        f"- Reach episodes (Event A): **{len(st.reach_rows)}**",
        f"- Long confirmations (Event B): **{len(st.confirm_rows) or 'NONE_DETECTED'}**",
        f"- State transitions: **{len(st.transitions)}**",
        f"- Blocked short candidates (WATCH window only): **{len(blocked)}**",
        "",
        f"- First `WATCH_REVERSAL`: `{first_watch}`",
        f"- First `LONG_CANDIDATE`: `{first_long or 'NONE_DETECTED'}`",
        "",
        "Inputs: Phase 3A JSONL stream only (no SSOT rescan).",
    ]
    (RESULTS / "detection_summary.md").write_text("\n".join(summary), encoding="utf-8")

    report = [
        "# DOGEUSDT Phase 3B — Two-pool market structure detector V1",
        "",
        f"**VERDICT:** `{final_verdict}`",
        "",
        "## Answers (explicit)",
        "",
        f"1. First `WATCH_REVERSAL`: **{first_watch or 'NONE'}**",
        f"2. Second lower pool detected: **{'YES' if st.confirm_rows else 'NO'}** ({len(st.confirm_rows)} confirmations)",
        f"3. First causal `LONG_CANDIDATE` close: **{first_long or 'NONE_DETECTED'}**",
        f"4. Upper TP pools: see `upper_tp_candidates_at_confirmation.csv` ({len(tp_rows)} rows)",
        f"5. Short candidates blocked in WATCH windows: **{len(blocked)}** (see `blocked_short_windows.csv`)",
        "6. Open questions:",
        "   - `min_pool_age_bars=5` not enforced as hard gate (research param).",
        "   - Same-bar POOL_NEW + LONG confirmation — no extra persistence bar required in v1.",
        "   - Multiple reach episodes may not map 1:1 to user chart arrows (see reference_case_comparison.md).",
        "   - No automated exit from LONG_CANDIDATE back to SHORT_SEARCH (deferred).",
        "",
        "## Outputs",
        f"- `{RESULTS.relative_to(ROOT)}/`",
        "",
    ]
    REPORT.write_text("\n".join(report), encoding="utf-8")

    print(
        json.dumps(
            {
                "verdict": final_verdict,
                "reach_episodes": len(st.reach_rows),
                "long_confirmations": len(st.confirm_rows),
                "blocked_shorts": len(blocked),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
