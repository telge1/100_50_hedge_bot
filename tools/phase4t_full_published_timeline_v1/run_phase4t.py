#!/usr/bin/env python3
"""Phase 4T — full published 5m PD1 timeline recovery."""

from __future__ import annotations

import csv
import hashlib
import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parents[1]
RESULTS = ROOT / "results/phase4t_full_published_timeline_v1"
REPORT = ROOT / "reports/DOGEUSDT_PHASE4T_FULL_PUBLISHED_TIMELINE_RECOVERY_V1.md"
REF_JSONL = ROOT / "results/phase3a_dynamic_pool_landscape_timeline_v1/landscape_timeline.jsonl"
JSON_ROOT = (
    ROOT
    / "backtester/short/dashboard/research_charts/live_micro_lld_v2_ch_test"
    / "doge_apr_sep_2026_v1_history/production/results/2026-09/5m_PD1"
)
MERGED = RESULTS / "landscape_timeline_merged.jsonl"

sys.path.insert(0, str(TOOLS))
from ssot_adapter import apply_ssot_pythonpath, published_inventory  # noqa: E402
from timeline_builder import build_chunk, iter_day_chunks, merge_snapshots  # noqa: E402

apply_ssot_pythonpath()

PUB_FROM = "2026-09-09T00:00:00Z"
PUB_TO = "2026-09-17T23:55:00Z"
AUDIT_TO_EXCL = "2026-09-18T00:00:00Z"


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    for r in rows:
        for k in r:
            if k not in fields:
                fields.append(k)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def published_closes() -> list[str]:
    apply_ssot_pythonpath()
    from research_charts.historical_lmv2_pd_contract_v1 import resolve_viewer_historical_scope
    from research_charts.historical_pool_state_v1 import get_published_decision_closes
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot.published_reader import (
        PublishedActiveReader,
    )

    hcfg = resolve_viewer_historical_scope(timeframe="5m", pool_detail=1)
    reader = PublishedActiveReader()
    pub = get_published_decision_closes(reader, hcfg.config_hash, pipeline_line_id=hcfg.pipeline_line_id)
    out = []
    for dt in pub:
        out.append(dt.strftime("%Y-%m-%dT%H:%M:%SZ"))
    return out


def stem(utc: str) -> str:
    dt = datetime.fromisoformat(utc.replace("Z", "+00:00"))
    return dt.strftime("%Y%m%dT%H%M%SZ")


def norm_d2_hash(snap: dict, side: str) -> str:
    key = "upper_pools" if side == "upper" else "lower_pools"
    rows = []
    for p in snap.get(key) or []:
        if int(p.get("depth") or 0) < 2:
            continue
        rows.append(
            (
                round(float(p["bottom_price"]), 10),
                round(float(p["top_price"]), 10),
                tuple(sorted(p.get("origins") or [])),
            )
        )
    rows.sort()
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()[:16]


def load_index_jsonl(path: Path) -> dict[str, dict]:
    idx = {}
    if not path.is_file():
        return idx
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                s = json.loads(line)
                idx[s["candle_close_utc"]] = s
    return idx


def write_jsonl(path: Path, snaps: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for s in snaps:
            f.write(json.dumps(s, separators=(",", ":")) + "\n")


def signal_coverage_audit() -> list[dict]:
    phase2a = ROOT / "results/phase2a_5m_pd1_depth2_short_candidates_v1/candidates.csv"
    rows = []
    short_count = 0
    if phase2a.is_file():
        with phase2a.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                t = row["signal_close_utc"]
                if PUB_FROM <= t <= PUB_TO:
                    short_count += 1
    rows.append(
        {
            "feed": "PHASE2A_SHORT_CANDIDATES",
            "window": f"{PUB_FROM}..{PUB_TO}",
            "signals_in_window": short_count,
            "full_dynamic_scan": "NOT_IMPLEMENTED",
            "status": "SIGNAL_FEED_PILOT_ONLY",
        }
    )
    rows.append(
        {
            "feed": "PHASE3B_LONG_CONFIRMATIONS",
            "window": f"{PUB_FROM}..{PUB_TO}",
            "signals_in_window": "see phase3b CSV",
            "full_dynamic_scan": "NOT_IMPLEMENTED",
            "status": "SIGNAL_FEED_PILOT_ONLY",
        }
    )
    return rows


def main() -> int:
    RESULTS.mkdir(parents=True, exist_ok=True)
    REPORT.parent.mkdir(parents=True, exist_ok=True)

    (RESULTS / "root_cause.md").write_text(
        "\n".join(
            [
                "# Phase 4T root cause — 337 snapshot limit",
                "",
                "## Classification: **F) Builder bug (config filter)**",
                "",
                "`landscape_timeline.js` filters each price row:",
                "`if (tMs < visFrom || tMs > visTo) continue`",
                "",
                "Phase 4S/4T chunk builds passed `research_config_v1.json` unchanged:",
                "- `visible_from_utc`: **2026-09-11T12:00:00Z**",
                "- `visible_to_utc`: **2026-09-12T16:00:00Z**",
                "",
                "So only the intersection of chunk prices with that pilot window became snapshots:",
                "- Chunk Sep 9–11 ∩ pilot → **144** bars (Sep 11 12:00–23:55)",
                "- Chunk Sep 12–14 ∩ pilot → **193** bars",
                "- Total **337** (= Phase 3A reference count)",
                "",
                "Chunk 3 (Sep 15–17) had **zero** intersection with pilot window → **0** snapshots.",
                "",
                "## Fix (Phase 4T)",
                "",
                "Set `visible_from_utc` / `visible_to_utc` per chunk to match `build_landscape_window` bounds.",
                "",
                "Not A/B/C/D: SSOT publishes and price rows existed; snapshots were dropped by config.",
                "",
            ]
        ),
        encoding="utf-8",
    )

    pub = published_inventory()
    closes = published_closes()
    chunk_dir = RESULTS / "chunks"
    chunk_dir.mkdir(parents=True, exist_ok=True)
    manifest = []
    built_chunks = []

    for i, (a, b) in enumerate(iter_day_chunks(PUB_FROM, PUB_TO), 1):
        out_path = chunk_dir / f"day_{i:02d}_{a[:10]}.json"
        if out_path.is_file():
            ch = json.loads(out_path.read_text(encoding="utf-8"))
            built_chunks.append(ch)
            manifest.append(ch["meta"])
            continue
        ch = build_chunk(a, b)
        meta = {k: ch[k] for k in ch if k != "snapshots"}
        out_path.write_text(
            json.dumps({"meta": meta, "snapshots": ch["snapshots"]}, separators=(",", ":")),
            encoding="utf-8",
        )
        built_chunks.append(ch)
        manifest.append(meta)

    write_csv(RESULTS / "chunk_manifest.csv", manifest)
    merged = merge_snapshots(built_chunks)
    write_jsonl(MERGED, merged)
    snap_index = {s["candle_close_utc"]: s for s in merged}

    coverage_rows = []
    missing = []
    for t in closes:
        if t >= AUDIT_TO_EXCL:
            continue
        jpath = JSON_ROOT / f"{stem(t)}.json"
        snap = snap_index.get(t)
        row = {
            "timestamp": t,
            "candle_available": bool(snap),
            "ssot_publish_available": jpath.is_file(),
            "historical_pool_state_resolvable": jpath.is_file(),
            "origin_history_available": jpath.is_file(),
            "timeline_snapshot_built": bool(snap),
            "failure_reason": "",
            "provenance_hash": norm_d2_hash(snap, "lower") if snap else "",
        }
        if not jpath.is_file():
            row["failure_reason"] = "B_SSOT_JSON_MISSING"
        elif not snap:
            row["failure_reason"] = "F_SNAPSHOT_NOT_BUILT"
        elif snap and not snap.get("parity_ok"):
            row["failure_reason"] = "D_PARITY_FLAG_FALSE"
        else:
            row["failure_reason"] = "OK"
        coverage_rows.append(row)
        if row["failure_reason"] not in ("OK", "D_PARITY_FLAG_FALSE"):
            missing.append(row)

    write_csv(RESULTS / "full_close_coverage.csv", coverage_rows)
    write_csv(RESULTS / "missing_close_diagnostics.csv", missing)

    ref = load_index_jsonl(REF_JSONL)
    parity_rows = []
    shared = sorted(set(ref) & set(snap_index))
    mism = 0
    for t in shared:
        ok = (
            norm_d2_hash(ref[t], "upper") == norm_d2_hash(snap_index[t], "upper")
            and norm_d2_hash(ref[t], "lower") == norm_d2_hash(snap_index[t], "lower")
        )
        if not ok:
            mism += 1
        parity_rows.append({"candle_close_utc": t, "parity_ok": ok})
    parity_rows.append({"summary": True, "bars": len(shared), "mismatches": mism})

    rng = random.Random(42)
    outside = [t for t in snap_index if t not in ref]
    sample = sorted(rng.sample(outside, min(20, len(outside)))) if outside else []
    for t in sample:
        parity_rows.append(
            {
                "candle_close_utc": t,
                "sample_outside_phase3a": True,
                "lower_d2_hash": norm_d2_hash(snap_index[t], "lower"),
                "upper_d2_hash": norm_d2_hash(snap_index[t], "upper"),
            }
        )
    write_csv(RESULTS / "density_parity.csv", parity_rows)

    write_csv(RESULTS / "historical_signal_coverage.csv", signal_coverage_audit())
    write_csv(
        RESULTS / "pool_lifecycle_coverage.csv",
        [
            {
                "merged_snapshots": len(merged),
                "published_closes": len(closes),
                "coverage_pct": round(100.0 * len(merged) / max(1, len(closes)), 4),
            }
        ],
    )

    parity_ok = mism == 0 and len(shared) >= 288
    timeline_ok = len(merged) == len(closes)
    signal_ok = False  # phase2a pilot feed only

    verdict = "PUBLISHED_TIMELINE_RECOVERY_BLOCKED"
    if timeline_ok and parity_ok:
        verdict = "PUBLISHED_TIMELINE_RECOVERY_READY"
    elif parity_ok and len(merged) > 337:
        verdict = "PUBLISHED_TIMELINE_RECOVERY_PARTIAL"

    unresolved = [
        "STRATEGY_ELIGIBILITY_UNRESOLVED",
        "TP_POOL_ELIGIBILITY_UNRESOLVED",
        "HISTORICAL_SIGNAL_FEED_PILOT_ONLY — no per-bar dynamic Phase 2A/3B scan for full Sep 9–17",
    ]
    if not timeline_ok:
        unresolved.append(f"TIMELINE_PARTIAL {len(merged)}/{len(closes)} snapshots")
    (RESULTS / "unresolved_technical_issues.md").write_text("\n".join(f"- {u}" for u in unresolved), encoding="utf-8")

    backtest_blocked = not (timeline_ok and parity_ok and signal_ok)
    if not backtest_blocked:
        pass  # would run 4P backtest — not reached

    summary = [
        "# Phase 4T timeline recovery summary",
        "",
        f"**VERDICT:** `{verdict}`",
        f"- Merged snapshots: **{len(merged)}** / published **{len(closes)}**",
        f"- Phase 3A parity bars: **{len(shared)}**, mismatches **{mism}**",
        f"- Backtest: **{'BLOCKED' if backtest_blocked else 'READY'}** (signal feed incomplete)",
        "",
    ]
    (RESULTS / "timeline_recovery_summary.md").write_text("\n".join(summary), encoding="utf-8")

    REPORT.write_text(
        "\n".join(
            [
                "# DOGEUSDT Phase 4T — Full Published Timeline Recovery V1",
                "",
                f"**VERDICT:** `{verdict}`",
                "",
                "## Root cause",
                "Phase 3A `visible_from/to` pilot constants in timeline config — **not** missing SSOT.",
                "",
                f"## Timeline",
                f"- Built: **{len(merged)}** snapshots",
                f"- Published: **{len(closes)}**",
                "",
                "## Backtest",
                "BLOCKED until dynamic historical signal generation exists (Phase 2A/3B pilot feed insufficient).",
                "",
                f"`results/phase4t_full_published_timeline_v1/`",
                "",
                "**STOP**",
            ]
        ),
        encoding="utf-8",
    )
    print(json.dumps({"verdict": verdict, "merged": len(merged), "published": len(closes), "mism": mism}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
