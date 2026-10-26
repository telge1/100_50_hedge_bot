#!/usr/bin/env python3
"""Phase 4X-AB — full May 5m PD1 density continuity audit (read-only)."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "results" / "phase4xab_full_may_density_continuity_v1"
DASH = ROOT / "backtester/short/dashboard"
MULTI = DASH / "research_charts/live_micro_lld_v2_ch_test/doge_multi_month_2026_history"
MAY_DIR = MULTI / "production/results/2026-05/5m_PD1"
ANALYZER = ROOT / "tools/phase1b_depth2_landscape_pilot_v1/analyzer_depth2_landscape.js"
MAY_START = datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)
MAY_END_EXCL = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
REF = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
PILOT_END = datetime(2026, 5, 2, 0, 0, tzinfo=timezone.utc)
LEGACY_MONTHS = ("2026-01", "2026-02", "2026-03", "2026-04")

for _entry in (DASH, Path("/home/telgenbuescher/projects/trading_research_platform")):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))


def _stem(dc: datetime) -> str:
    return dc.strftime("%Y%m%dT%H%M%SZ")


def _closes(start: datetime, end_excl: datetime) -> list[datetime]:
    step = timedelta(minutes=5)
    out = []
    c = start
    while c < end_excl:
        out.append(c)
        c += step
    return out


def wcsv(path: Path, rows: list[dict], fieldnames: list[str] | None = None) -> None:
    if not rows and not fieldnames:
        path.write_text("", encoding="utf-8")
        return
    fields = fieldnames or sorted({k for r in rows for k in r})
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def append_csv(path: Path, row: dict, fieldnames: list[str]) -> None:
    exists = path.is_file()
    with path.open("a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if not exists:
            w.writeheader()
        w.writerow(row)


def origin_month(pid: str) -> str:
    ou = int(pid.rsplit(":", 1)[-1])
    dt = datetime.fromtimestamp(ou, tz=timezone.utc)
    return f"{dt.year:04d}-{dt.month:02d}"


def density_manifest(payload: dict, close_iso: str) -> str:
    import subprocess

    prices = []
    for pid in payload.get("active_pool_ids") or []:
        p = (payload.get("pools") or {}).get(pid) or {}
        prices.extend([float(p.get("bottom_price", 0)), float(p.get("top_price", 0))])
    if not prices:
        return hashlib.sha256(b"EMPTY").hexdigest()
    lo, hi = min(prices) * 0.995, max(prices) * 1.005
    t_ms = int(datetime.fromisoformat(close_iso.replace("Z", "+00:00")).timestamp() * 1000)
    iso = close_iso.replace("+00:00", "Z") if "T" in close_iso else close_iso
    pools = []
    for pid in payload.get("active_pool_ids") or []:
        p = (payload.get("pools") or {}).get(pid) or {}
        parts = pid.split(":")
        origin = f"{parts[0]}:{parts[1]}:{parts[2]}:{parts[4]}:{parts[5]}" if len(parts) >= 6 else pid
        pools.append(
            {
                "pool_id": pid,
                "side": p.get("side"),
                "bottom": float(p.get("bottom_price")),
                "top": float(p.get("top_price")),
                "origin": origin,
                "segments": [[iso, iso]],
            }
        )
    script = f"""
const {{ computeExactBandsForSideWithOrigins }} = require({json.dumps(str(ANALYZER))});
const pools = {json.dumps(pools)};
const t = {t_ms};
const bands = [];
for (const side of ['lower','upper']) {{
  for (const b of computeExactBandsForSideWithOrigins(pools, t, side, {lo}, {hi})) {{
    if (b.depth >= 2) bands.push([side, b.y0, b.y1, b.depth, ...(b.origins||[]).sort()]);
  }}
}}
bands.sort((a,b)=>JSON.stringify(a).localeCompare(JSON.stringify(b)));
console.log(JSON.stringify(bands));
"""
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout)
    canonical = proc.stdout.strip()
    return hashlib.sha256(canonical.encode()).hexdigest()


def record_from_engine(snap, candles, result, decision: datetime, identity: dict) -> dict:
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot.parallel_verify import (
        verification_record,
    )

    rec = verification_record(snap, candles, result, decision)
    rec["frozen_input"] = identity
    return rec


def compare_active(frozen: dict, replay: dict) -> list[str]:
    errs = []
    if frozen.get("engine_input_hash") != replay.get("engine_input_hash"):
        errs.append("engine_input_hash")
    if frozen.get("active_state_fingerprint") != replay.get("active_state_fingerprint"):
        errs.append("active_state_fingerprint")
    fa = sorted(frozen.get("active_pool_ids") or [])
    ra = sorted(replay.get("active_pool_ids") or [])
    if fa != ra:
        errs.append("active_pool_ids")
    fp, rp = frozen.get("pools") or {}, replay.get("pools") or {}
    for pid in set(fa) & set(ra):
        for k in ("side", "bottom_price", "top_price"):
            if fp.get(pid, {}).get(k) != rp.get(pid, {}).get(k):
                errs.append(f"pool.{k}.{pid}")
    return errs


def run_segment(
    parity_start: datetime,
    parity_end_excl: datetime,
    *,
    resume: bool,
    progress_path: Path,
) -> dict[str, Any]:
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot.extract import (
        build_active_state_at_decision_close,
    )
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot.rolling_calendar import (
        HISTORY_POLICY_ROLLING_4M,
        rolling_history_from_close,
    )
    from research_charts.live_micro_lld_v2_ch_test.immutable_candle_snapshot_v1 import (
        ohlcv_content_hash,
        require_snapshot,
        revision_metadata_hash,
    )
    from research_charts.live_micro_lld_v2_ch_test.phase11m.session import ReusedSnapshotSession

    manifest = json.loads(list((MULTI / "inputs").glob("*.manifest.json"))[0].read_text())
    sid, ph = manifest["snapshot_id"], manifest["ohlcv_content_hash"]
    rows = require_snapshot(MULTI / "inputs", sid, ohlcv_hash=ph)
    identity = {
        "snapshot_id": sid,
        "price_hash": ph,
        "revision_hash": revision_metadata_hash(rows),
        "clickhouse_candle_reads": 0,
    }
    session = ReusedSnapshotSession(rows, snapshot_id=sid, ohlcv_hash=ph, timeframe="5m")
    session.max_1m_bars = 500_000

    parity_closes = _closes(parity_start, parity_end_excl)
    resume_from = None
    if resume and progress_path.is_file():
        prog = json.loads(progress_path.read_text())
        resume_from = prog.get("last_completed_stem")

    active_fields = [
        "decision_close_utc",
        "stem",
        "active_count",
        "active_parity",
        "engine_input_hash_match",
        "fingerprint_match",
        "legacy_jan_apr_count",
        "origins_gt_168h",
        "active_errors",
        "frozen_density_manifest",
        "replay_density_manifest",
        "density_parity",
    ]
    density_fields = ["decision_close_utc", "stem", "density_parity", "frozen_manifest", "replay_manifest"]
    parity_path = OUT / "active_origin_parity_by_close.csv"
    density_path = OUT / "density_parity_by_close.csv"

    legacy_tracker: dict[str, dict] = {}
    may_start_frozen = MAY_DIR / f"{_stem(MAY_START)}.json"
    if may_start_frozen.is_file():
        ms = json.loads(may_start_frozen.read_text(encoding="utf-8"))
        for pid in ms.get("active_pool_ids") or []:
            if origin_month(pid) in LEGACY_MONTHS:
                legacy_tracker[pid] = {
                    "first_seen": MAY_START.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "lost_in_replay": "",
                    "lost_in_frozen": "",
                }

    mismatches = 0
    density_mismatches = 0
    processed = 0
    t0 = time.time()

    # One ReusedSnapshotSession; each close is independent LMV2 prefix (no ACTIVE carry-over).
    for dc in parity_closes:
        stem = _stem(dc)
        if resume_from and stem <= resume_from:
            continue
        frozen_path = MAY_DIR / f"{stem}.json"
        if not frozen_path.is_file():
            append_csv(
                OUT / "origin_lifecycle_discrepancies.csv",
                {"stem": stem, "issue": "MISSING_FROZEN_FILE"},
                ["stem", "issue"],
            )
            mismatches += 1
            continue

        frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
        hist = rolling_history_from_close(dc, 4)
        snap, candles, result = build_active_state_at_decision_close(
            "DOGEUSDT",
            "5m",
            hist,
            dc,
            pool_detail=1,
            history_policy=HISTORY_POLICY_ROLLING_4M,
            candle_session=session,
        )
        replay = record_from_engine(snap, candles, result, dc, identity)
        close_iso = dc.strftime("%Y-%m-%dT%H:%M:%SZ")
        errs = compare_active(frozen, replay)
        active_ok = not errs

        active_ids = set(replay.get("active_pool_ids") or [])
        frozen_ids = set(frozen.get("active_pool_ids") or [])
        if legacy_tracker:
            for pid in list(legacy_tracker.keys()):
                if legacy_tracker[pid]["lost_in_replay"] == "" and pid not in active_ids:
                    legacy_tracker[pid]["lost_in_replay"] = close_iso
                if legacy_tracker[pid]["lost_in_frozen"] == "" and pid not in frozen_ids:
                    legacy_tracker[pid]["lost_in_frozen"] = close_iso

        gt168 = sum(
            1
            for pid in active_ids
            if (dc - datetime.fromtimestamp(int(pid.rsplit(":", 1)[-1]), tz=timezone.utc)).total_seconds()
            > 168 * 3600
        )
        legacy_count = sum(1 for pid in active_ids if origin_month(pid) in LEGACY_MONTHS)

        fm = rm = ""
        density_ok = True
        try:
            if active_ok:
                fm = rm = density_manifest(replay, close_iso)
            else:
                fm = density_manifest(frozen, close_iso)
                rm = density_manifest(replay, close_iso)
                density_ok = fm == rm
        except Exception as exc:
            density_ok = False
            append_csv(
                OUT / "exact_density_differences.csv",
                {"stem": stem, "error": str(exc)[:500]},
                ["stem", "error", "detail"],
            )

        if not density_ok:
            density_mismatches += 1
            append_csv(
                OUT / "exact_density_differences.csv",
                {
                    "stem": stem,
                    "frozen_manifest": fm,
                    "replay_manifest": rm,
                    "active_errors": "|".join(errs),
                },
                ["stem", "frozen_manifest", "replay_manifest", "active_errors", "error", "detail"],
            )

        if not active_ok:
            mismatches += 1
            append_csv(
                OUT / "origin_lifecycle_discrepancies.csv",
                {"stem": stem, "issue": "|".join(errs)},
                ["stem", "issue"],
            )

        append_csv(
            parity_path,
            {
                "decision_close_utc": close_iso,
                "stem": stem,
                "active_count": len(active_ids),
                "active_parity": "PASS" if active_ok else "FAIL",
                "engine_input_hash_match": frozen.get("engine_input_hash") == replay.get("engine_input_hash"),
                "fingerprint_match": frozen.get("active_state_fingerprint")
                == replay.get("active_state_fingerprint"),
                "legacy_jan_apr_count": legacy_count,
                "origins_gt_168h": gt168,
                "active_errors": "|".join(errs),
                "frozen_density_manifest": fm,
                "replay_density_manifest": rm,
                "density_parity": "PASS" if density_ok else "FAIL",
            },
            active_fields,
        )
        append_csv(
            density_path,
            {
                "decision_close_utc": close_iso,
                "stem": stem,
                "density_parity": "PASS" if density_ok else "FAIL",
                "frozen_manifest": fm,
                "replay_manifest": rm,
            },
            density_fields,
        )

        processed += 1
        progress_path.write_text(
            json.dumps(
                {
                    "last_completed_stem": stem,
                    "processed": processed,
                    "mismatches": mismatches,
                    "density_mismatches": density_mismatches,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    elapsed = time.time() - t0
    return {
        "processed": processed,
        "mismatches": mismatches,
        "density_mismatches": density_mismatches,
        "elapsed_sec": round(elapsed, 1),
        "legacy_tracker": legacy_tracker,
    }


def coverage_csv() -> list[dict]:
    expected = _closes(MAY_START, MAY_END_EXCL)
    stems = {p.stem.replace(".json", "") for p in MAY_DIR.glob("*.json")}
    rows = []
    for dc in expected:
        stem = _stem(dc)
        rows.append(
            {
                "decision_close_utc": dc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "stem": stem,
                "frozen_present": "YES" if stem in stems else "NO",
            }
        )
    return rows


def write_docs(result: dict, phase: str, verdict: str) -> None:
    (OUT / "replay_provenance.md").write_text(
        """# Replay provenance — Phase 4X-AB

- Engine: `build_active_state_at_decision_close` / `live_micro_lld_v2@trp` (Phase 4X-Z / 4X-AA path)
- Input: `cfae2571…` immutable parquet (`doge_multi_month_2026_history/inputs/`)
- Session: single `ReusedSnapshotSession` over May closes in chronological order (candle cache only; no ACTIVE seed)
- Density: `analyzer_depth2_landscape.js` + production adapter semantics (min depth 2)
- Frozen: read-only `production/results/2026-05/5m_PD1/`
""",
        encoding="utf-8",
    )
    (OUT / "historical_input_limitations.md").write_text(
        """# Historical input limitations

Unchanged from Phase 4X-AA:

- Immutable 1m from **2025-12-11**; per-close replay before ~**2026-04-30** not feasible on this file.
- May closes use rolling `history_from_close` (4 calendar months); Jan–Apr legacy origins appear via prefix at each May decision close.
- No claim of completeness for origins requiring 1m data before immutable left edge.
""",
        encoding="utf-8",
    )
    (OUT / "final_density_quality_verdict.md").write_text(
        f"""# Final density quality verdict

**Phase:** {phase}  
**VERDICT:** `{verdict}`

```json
{json.dumps(result, indent=2)}
```
""",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pilot-only", action="store_true")
    parser.add_argument("--full-may", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    cov = coverage_csv()
    wcsv(OUT / "may_close_coverage.csv", cov)
    present = sum(1 for r in cov if r["frozen_present"] == "YES")
    if present != 8928:
        write_docs({"frozen_count": present}, "preflight", "REPLAY_BLOCKED")
        print("REPLAY_BLOCKED missing frozen", present)
        return 1

    progress = OUT / "progress.json"
    if args.pilot_only or not args.full_may:
        print("Running 24h pilot May1 00:00 .. May2 00:00 ...")
        if not args.resume:
            for p in (
                "active_origin_parity_by_close.csv",
                "density_parity_by_close.csv",
                "origin_lifecycle_discrepancies.csv",
                "exact_density_differences.csv",
                "progress.json",
            ):
                (OUT / p).unlink(missing_ok=True)
        pilot = run_segment(MAY_START, PILOT_END, resume=args.resume, progress_path=progress)
        legacy_rows = [
            {
                "pool_id": pid,
                **meta,
                "replay_frozen_loss_mismatch": meta["lost_in_replay"] != meta["lost_in_frozen"],
            }
            for pid, meta in pilot.get("legacy_tracker", {}).items()
        ]
        wcsv(OUT / "legacy_origin_survival.csv", legacy_rows)
        if pilot["mismatches"] or pilot["density_mismatches"]:
            verdict = "MAY_DENSITY_CONTINUITY_FAILED"
            write_docs(pilot, "pilot_24h", verdict)
            print(verdict, pilot)
            return 1
        if not args.full_may:
            write_docs(pilot, "pilot_24h", "MAY_DENSITY_CONTINUITY_PARTIAL")
            print("PILOT_PASS", pilot)
            return 0

    print("Running full May ...")
    full = run_segment(MAY_START, MAY_END_EXCL, resume=args.resume, progress_path=progress)
    legacy_rows = [
        {
            "pool_id": pid,
            **meta,
            "replay_frozen_loss_mismatch": meta["lost_in_replay"] != meta["lost_in_frozen"],
        }
        for pid, meta in full.get("legacy_tracker", {}).items()
    ]
    wcsv(OUT / "legacy_origin_survival.csv", legacy_rows)
    blockers = [r for r in legacy_rows if r.get("replay_frozen_loss_mismatch")]
    if blockers:
        for r in blockers[:50]:
            append_csv(
                OUT / "origin_lifecycle_discrepancies.csv",
                {"stem": "legacy", "issue": f"legacy_loss_mismatch {r['pool_id']}"},
                ["stem", "issue"],
            )

    verdict = "MAY_DENSITY_CONTINUITY_VERIFIED"
    if full["mismatches"] or full["density_mismatches"] or blockers:
        verdict = "MAY_DENSITY_CONTINUITY_FAILED"
    else:
        parity_rows = list(csv.DictReader((OUT / "active_origin_parity_by_close.csv").open()))
        unique_stems = {r["stem"] for r in parity_rows}
        if len(unique_stems) < 8928:
            verdict = "MAY_DENSITY_CONTINUITY_PARTIAL"

    write_docs(full, "full_may", verdict)
    report = f"""# DOGEUSDT – PHASE 4X-AB FULL MAY 5M PD1 DENSITY CONTINUITY V1

**VERDICT: `{verdict}`**

- Frozen coverage: **{present}/8928**
- Processed: **{full['processed']}**
- ACTIVE mismatches: **{full['mismatches']}**
- Density mismatches: **{full['density_mismatches']}**
- Legacy loss mismatches: **{len(blockers)}**

Artifacts: `results/phase4xab_full_may_density_continuity_v1/`

**STOP.**
"""
    (ROOT / "reports/DOGEUSDT_PHASE4XAB_FULL_MAY_DENSITY_CONTINUITY_V1.md").write_text(report, encoding="utf-8")
    print(verdict, full)
    return 0 if verdict == "MAY_DENSITY_CONTINUITY_VERIFIED" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise
