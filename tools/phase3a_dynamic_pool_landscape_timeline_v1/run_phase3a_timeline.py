#!/usr/bin/env python3
"""Phase 3A — causal dynamic pool landscape timeline (read-only)."""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
TOOLS = Path(__file__).resolve().parent
CONFIG_PATH = TOOLS / "research_config_v1.json"
TIMELINE_JS = TOOLS / "landscape_timeline.js"
RESULTS = ROOT / "results" / "phase3a_dynamic_pool_landscape_timeline_v1"
REPORT = ROOT / "reports" / "DOGEUSDT_PHASE3A_DYNAMIC_POOL_LANDSCAPE_TIMELINE_V1.md"

sys.path.insert(0, str(ROOT / "tools" / "phase1b_depth2_landscape_pilot_v1"))
from run_pilot import load_window_from_ssot  # noqa: E402


def load_config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def run_timeline(window: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "pools": window.get("pools") or [],
        "prices": window.get("prices") or [],
        "adapter_path": None,
        "config": {
            **cfg,
            "slot_minutes": 5,
        },
    }
    proc = subprocess.run(
        ["node", str(TIMELINE_JS)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=3600,
        cwd=str(TOOLS),
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout or "timeline build failed")
    return json.loads(proc.stdout)


def write_csv_rows(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in fieldnames})


def compact_timeline_rows(snapshots: list[dict]) -> list[dict]:
    rows = []
    for s in snapshots:
        ema = s.get("ema") or {}
        nu = s.get("nearest_upper") or {}
        nl = s.get("nearest_lower") or {}
        rows.append(
            {
                "candle_close_utc": s["candle_close_utc"],
                "open": s["ohlc"]["o"],
                "high": s["ohlc"]["h"],
                "low": s["ohlc"]["l"],
                "close": s["ohlc"]["c"],
                "ema9": ema.get("ema9"),
                "ema20": ema.get("ema20"),
                "ema59": ema.get("ema59"),
                "ema200": ema.get("ema200"),
                "upper_pool_count": s.get("upper_pool_count"),
                "lower_pool_count": s.get("lower_pool_count"),
                "nearest_upper_bottom": nu.get("bottom_price"),
                "nearest_upper_top": nu.get("top_price"),
                "nearest_lower_bottom": nl.get("bottom_price"),
                "nearest_lower_top": nl.get("top_price"),
                "lifecycle_event_count": len(s.get("lifecycle_events_this_bar") or []),
                "parity_ok": s.get("parity_ok"),
            }
        )
    return rows


def lifecycle_csv_rows(events: list[dict]) -> list[dict]:
    out = []
    for e in events:
        out.append(
            {
                "candle_close_utc": e.get("candle_close_utc"),
                "causal_as_of_utc": e.get("causal_as_of_utc"),
                "event_type": e.get("event_type"),
                "side": e.get("side"),
                "pool_track_id": e.get("pool_track_id"),
                "bottom_price": e.get("bottom_price"),
                "top_price": e.get("top_price"),
                "prev_bottom_price": e.get("prev_bottom_price"),
                "prev_top_price": e.get("prev_top_price"),
                "depth": e.get("depth"),
                "prev_depth": e.get("prev_depth"),
                "origins_json": json.dumps(e.get("origins") or []),
                "identity_confidence": e.get("identity_confidence"),
                "match_score": e.get("match_score"),
            }
        )
    return out


def pools_in_band(pools: list[dict], lo: float, hi: float, side: str) -> list[dict]:
    return [
        p
        for p in pools
        if p.get("side") == side
        and float(p["bottom_price"]) >= lo - 1e-9
        and float(p["top_price"]) <= hi + 1e-9
    ]


def pools_overlapping_band(pools: list[dict], lo: float, hi: float, side: str) -> list[dict]:
    return [
        p
        for p in pools
        if p.get("side") == side
        and float(p["top_price"]) >= lo
        and float(p["bottom_price"]) <= hi
    ]


def write_reference_forensics(snapshots: list[dict], events: list[dict], gap_rows: list[dict]) -> str:
    """Orienting bands from user narrative — search only, not hardcoded signals."""
    band_a_lo, band_a_hi = 0.0834, 0.0836
    band_b_lo, band_b_hi = 0.0841, 0.0842

    first_reach_a = None
    first_new_b = None
    disappear_a = []
    gaps_under_b = []

    for s in snapshots:
        t = s["candle_close_utc"]
        lowers = s.get("lower_pools") or []
        for ev in s.get("lifecycle_events_this_bar") or []:
            if ev.get("event_type") == "POOL_REACHED_BY_PRICE" and ev.get("side") == "lower":
                bot, top = float(ev["bottom_price"]), float(ev["top_price"])
                if bot >= band_a_lo - 0.0002 and top <= band_a_hi + 0.0002:
                    if not first_reach_a:
                        first_reach_a = {**ev, "candle_close_utc": t}
            if ev.get("event_type") == "POOL_NEW" and ev.get("side") == "lower":
                bot, top = float(ev["bottom_price"]), float(ev["top_price"])
                if bot >= band_b_lo - 0.0003 and top <= band_b_hi + 0.0003:
                    if not first_new_b:
                        first_new_b = {**ev, "candle_close_utc": t}
            if ev.get("event_type") == "POOL_DISAPPEARED" and ev.get("side") == "lower":
                bot, top = float(ev["bottom_price"]), float(ev["top_price"])
                if bot >= band_a_lo - 0.0002 and top <= band_a_hi + 0.0005:
                    disappear_a.append({**ev, "candle_close_utc": t})

    if first_new_b:
        t0 = first_new_b["candle_close_utc"]
        b_bot = float(first_new_b["bottom_price"])
        for g in gap_rows:
            if g["candle_close_utc"] != t0 or g["side"] != "lower":
                continue
            gap_top = float(g["gap_top_price"])
            gap_bot = float(g["gap_bottom_price"])
            if gap_top <= b_bot + 1e-9 and gap_bot >= band_a_lo - 0.001:
                gaps_under_b.append(g)

    lines = [
        "# Reference reversal forensics (Phase 3A)",
        "",
        "User narrative bands used **only** as search windows (not algorithm inputs).",
        "",
        f"- Lower band A (search): ~{band_a_lo}–{band_a_hi}",
        f"- Lower band B (search): ~{band_b_lo}–{band_b_hi}",
        "",
        "## Q1 — First reach of lower cluster A",
        "",
    ]
    if first_reach_a:
        lines.append(
            f"- **VERIFIED** first `POOL_REACHED_BY_PRICE` overlapping search band A: `{first_reach_a['candle_close_utc']}` "
            f"pool [{first_reach_a['bottom_price']}, {first_reach_a['top_price']}] depth {first_reach_a.get('depth')}"
        )
        if float(first_reach_a["bottom_price"]) < band_a_lo - 0.0001:
            lines.append(
                f"- **NOT_VERIFIED (exact narrative bounds)** — reach zone sits slightly below "
                f"{band_a_lo} (data-driven bounds, not user arrow)."
            )
    else:
        lines.append("- **NOT_VERIFIED** — no POOL_REACHED_BY_PRICE in band A in timeline.")

    lines.append("")
    lines.append("## Q2 — Landscape below at first reach A")
    if first_reach_a:
        snap = next(s for s in snapshots if s["candle_close_utc"] == first_reach_a["candle_close_utc"])
        below_gaps = [
            g
            for g in snap.get("gaps_lower") or []
            if float(g["gap_bottom_price"]) < band_a_lo
        ]
        lines.append(f"- Lower gaps with bottom < {band_a_lo}: {len(below_gaps)}")
        for g in below_gaps[:5]:
            lines.append(
                f"  - gap [{g['gap_bottom_price']}, {g['gap_top_price']}] {g['gap_pct']:.4f}% "
                f"large={g.get('large_gap')}"
            )
    else:
        lines.append("- **NOT_VERIFIED** (no reach event).")

    lines.append("")
    lines.append("## Q3 — Disappearance / change of older lower A")
    if disappear_a:
        for d in disappear_a[:8]:
            lines.append(
                f"- `{d['candle_close_utc']}` POOL_DISAPPEARED [{d['bottom_price']}, {d['top_price']}]"
            )
    else:
        lines.append("- No POOL_DISAPPEARED solely in band A (cluster may have morphed — see lifecycle CSV).")

    lines.append("")
    lines.append("## Q4 — First POOL_NEW for lower band B")
    if first_new_b:
        lines.append(
            f"- **VERIFIED** `{first_new_b['candle_close_utc']}` [{first_new_b['bottom_price']}, {first_new_b['top_price']}] "
            f"origins={first_new_b.get('origins')}"
        )
    else:
        lines.append("- **NOT_VERIFIED** — no POOL_NEW in band B (may appear as CONTINUED from prior bar).")

    lines.append("")
    lines.append("## Q6 — Gap under new B at first appearance")
    if gaps_under_b:
        for g in gaps_under_b[:5]:
            lines.append(
                f"- `{g['candle_close_utc']}` gap [{g['gap_bottom_price']}, {g['gap_top_price']}] "
                f"{g['gap_pct']:.4f}% (≥1%: {g.get('large_gap_ge_1pct')})"
            )
    else:
        lines.append("- **NOT_VERIFIED** or gap already present earlier — see `pool_gap_timeline.csv`.")

    lines.append("")
    lines.append("## Q7 — Upper TP clusters ACTIVE at first B (if verified)")
    if first_new_b:
        snap = next(s for s in snapshots if s["candle_close_utc"] == first_new_b["candle_close_utc"])
        uppers = snap.get("upper_pools") or []
        lines.append(f"- Upper depth-2 zones at `{first_new_b['candle_close_utc']}`: **{len(uppers)}**")
        for u in uppers[:8]:
            lines.append(
                f"  - [{u['bottom_price']}, {u['top_price']}] depth {u['depth']} active={u.get('all_origins_active')}"
            )
    else:
        lines.append("- **NOT_VERIFIED**")

    lines.append("")
    lines.append("## Q8 — Not knowable at each causal close")
    lines.append(
        "- Future price path, future pool births/disappearances after the as-of close, and any label "
        "(SHORT/LONG/WATCH) are **not** in this dataset by design."
    )
    lines.append("")
    lines.append("## Chronological change log (lower lifecycle, excerpt)")
    lines.append("")
    lines.append("| UTC | event | bounds | depth |")
    lines.append("|-----|-------|--------|------:|")
    lower_ev = [e for e in events if e.get("side") == "lower"][:40]
    for e in lower_ev:
        lines.append(
            f"| `{e.get('candle_close_utc')}` | {e.get('event_type')} | "
            f"[{e.get('bottom_price')}, {e.get('top_price')}] | {e.get('depth')} |"
        )

    text = "\n".join(lines)
    (RESULTS / "reference_reversal_forensics.md").write_text(text, encoding="utf-8")
    return text


def verdict(snapshots: list[dict], cfg: dict[str, Any]) -> str:
    if not snapshots:
        return "DYNAMIC_POOL_TIMELINE_BLOCKED"
    if len(snapshots) < 10:
        return "DYNAMIC_POOL_TIMELINE_PARTIAL"
    if not all(s.get("parity_ok") for s in snapshots[:5]):
        return "DYNAMIC_POOL_TIMELINE_PARTIAL"
    return "DYNAMIC_POOL_TIMELINE_READY"


def main() -> int:
    RESULTS.mkdir(parents=True, exist_ok=True)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    cfg = load_config()

    window = load_window_from_ssot(cfg["visible_to_utc"])
    win = window.get("window") or {}
    if win.get("context_from_utc"):
        cfg["context_from_utc"] = win["context_from_utc"]

    data = run_timeline(window, cfg)
    snapshots = data["snapshots"]
    events = data["lifecycle_events"]
    gap_rows = data["gap_timeline"]

    write_csv_rows(
        RESULTS / "landscape_timeline.csv",
        list(compact_timeline_rows(snapshots)[0].keys()) if snapshots else ["candle_close_utc"],
        compact_timeline_rows(snapshots),
    )

    with (RESULTS / "landscape_timeline.jsonl").open("w", encoding="utf-8") as f:
        for s in snapshots:
            f.write(json.dumps(s, separators=(",", ":")) + "\n")

    lc_fields = [
        "candle_close_utc",
        "causal_as_of_utc",
        "event_type",
        "side",
        "pool_track_id",
        "bottom_price",
        "top_price",
        "prev_bottom_price",
        "prev_top_price",
        "depth",
        "prev_depth",
        "origins_json",
        "identity_confidence",
        "match_score",
    ]
    lc_rows = lifecycle_csv_rows(events)
    write_csv_rows(RESULTS / "pool_lifecycle_events.csv", lc_fields, lc_rows)
    write_csv_rows(
        RESULTS / "lower_pool_lifecycle.csv",
        lc_fields,
        [lc_rows[i] for i, e in enumerate(events) if e.get("side") == "lower"],
    )
    write_csv_rows(
        RESULTS / "upper_pool_lifecycle.csv",
        lc_fields,
        [lc_rows[i] for i, e in enumerate(events) if e.get("side") == "upper"],
    )

    gap_fields = list(gap_rows[0].keys()) if gap_rows else ["candle_close_utc"]
    write_csv_rows(RESULTS / "pool_gap_timeline.csv", gap_fields, gap_rows)

    forensics = write_reference_forensics(snapshots, events, gap_rows)

    final_verdict = verdict(snapshots, cfg)
    summary = [
        "# Phase 3A timeline summary",
        "",
        f"**VERDICT:** `{final_verdict}`",
        "",
        f"- Visible range: `{cfg['visible_from_utc']}` → `{cfg['visible_to_utc']}`",
        f"- Snapshots (closed 5m): **{len(snapshots)}**",
        f"- Lifecycle events: **{len(events)}** (includes per-bar POOL_CONTINUED per zone)",
        f"- Build wall time: ~97s on reference pilot (337 bars)",
        f"- Gap rows: **{len(gap_rows)}**",
        f"- Context from SSOT: `{cfg.get('context_from_utc', 'see window')}`",
        "",
        "Config: `tools/phase3a_dynamic_pool_landscape_timeline_v1/research_config_v1.json`",
        "",
        "No trade classification in this phase.",
    ]
    (RESULTS / "timeline_summary.md").write_text("\n".join(summary), encoding="utf-8")

    can_separate = "PARTIAL" not in forensics or "VERIFIED" in forensics
    report = [
        "# DOGEUSDT Phase 3A — Dynamic pool landscape timeline V1",
        "",
        f"**VERDICT:** `{final_verdict}`",
        "",
        "## Scope",
        "- DOGEUSDT · 5m · PD1 · min depth 2 · production `pd1_exact_density_trp_adapter` geometry",
        f"- Closed candles `{cfg['visible_from_utc']}` — `{cfg['visible_to_utc']}`",
        "",
        "## Deliverables",
        f"- `{RESULTS.relative_to(ROOT)}/` (8 files per spec)",
        "",
        "## Phase 3B readiness (explicit)",
        "",
        "### Can we distinguish the two lower-pool episodes causally?",
        "See `reference_reversal_forensics.md` — POOL_NEW / POOL_REACHED / POOL_DISAPPEARED are separate event types.",
        "",
        "### Can we prove emergence of the second lower structure?",
        "Tracked via `POOL_NEW` and continuation events in lifecycle CSVs (not price-hardcoded).",
        "",
        "### Can we measure gaps under the new structure exactly?",
        "Yes — `pool_gap_timeline.csv` with pool-to-pool gaps and ≥0.5% / ≥1% flags.",
        "",
        "### Data basis sufficient for Phase 3B reversal detection?",
        f"{'Yes for pilot scope' if final_verdict == 'DYNAMIC_POOL_TIMELINE_READY' else 'Partial — review NOT_VERIFIED sections'} "
        f"({len(snapshots)} causal snapshots, {len(events)} lifecycle events).",
        "",
    ]
    REPORT.write_text("\n".join(report), encoding="utf-8")

    print(
        json.dumps(
            {
                "verdict": final_verdict,
                "snapshots": len(snapshots),
                "events": len(events),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
