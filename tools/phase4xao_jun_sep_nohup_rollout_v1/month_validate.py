#!/usr/bin/env python3
"""Per-month 4X-AB style ACTIVE + exact density validation (read-only)."""

from __future__ import annotations

import csv
import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
AO_TOOL = Path(__file__).resolve().parent
POOLS_DASH = ROOT / "backtester/short/dashboard"
MULTI = POOLS_DASH / "research_charts/live_micro_lld_v2_ch_test/doge_multi_month_2026_history"
ANALYZER = ROOT / "tools/phase1b_depth2_landscape_pilot_v1/analyzer_depth2_landscape.js"
LEGACY_MONTHS = ("2026-01", "2026-02", "2026-03", "2026-04")


def _ensure_paths() -> None:
    for mod in list(sys.modules):
        if mod.startswith("research_charts"):
            del sys.modules[mod]
    sys.path = [p for p in sys.path if p != str(POOLS_DASH)]
    for p in (ROOT, Path("/home/telgenbuescher/projects/trading_research_platform"), POOLS_DASH):
        if p.is_dir() and str(p) not in sys.path:
            sys.path.insert(0, str(p))
    from research_charts.trp_import import ensure_trp_on_path

    ensure_trp_on_path()


def month_window(month: str) -> tuple[datetime, datetime]:
    y, m = int(month[:4]), int(month[5:7])
    start = datetime(y, m, 1, tzinfo=timezone.utc)
    if m == 12:
        end = datetime(y + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(y, m + 1, 1, tzinfo=timezone.utc)
    return start, end


def _stem(dc: datetime) -> str:
    return dc.strftime("%Y%m%dT%H%M%SZ")


def _closes(start: datetime, end_excl: datetime) -> list[datetime]:
    step = timedelta(minutes=5)
    out, c = [], start
    while c < end_excl:
        out.append(c)
        c += step
    return out


def origin_month(pid: str) -> str:
    ou = int(pid.rsplit(":", 1)[-1])
    dt = datetime.fromtimestamp(ou, tz=timezone.utc)
    return f"{dt.year:04d}-{dt.month:02d}"


def density_manifest(payload: dict, close_iso: str) -> str:
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
    return hashlib.sha256(proc.stdout.strip().encode()).hexdigest()


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


def record_from_engine(snap, candles, result, decision: datetime, identity: dict) -> dict:
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot.parallel_verify import verification_record

    rec = verification_record(snap, candles, result, decision)
    rec["frozen_input"] = identity
    return rec


def validate_month(
    month: str,
    frozen_dir: Path,
    out_dir: Path,
    *,
    resume: bool,
    log_fn: Any = print,
) -> dict[str, Any]:
    """Full close walk; writes parity_index.json on PASS."""
    _ensure_paths()
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot.extract import (
        build_active_state_at_decision_close,
    )
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot.rolling_calendar import (
        HISTORY_POLICY_ROLLING_4M,
        rolling_history_from_close,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    progress_path = out_dir / "validation_progress.json"
    parity_csv = out_dir / "active_origin_parity_by_close.csv"
    manifest_path = out_dir / "parity_index.json"

    start, end_excl = month_window(month)
    parity_closes = _closes(start, end_excl)
    stems_on_disk = {p.stem for p in frozen_dir.glob("*.json")}
    missing_calendar = [_stem(dc) for dc in parity_closes if _stem(dc) not in stems_on_disk]
    if missing_calendar:
        return {
            "month": month,
            "verdict": "MONTH_VALIDATION_BLOCKED",
            "reason": f"missing {len(missing_calendar)} frozen files",
            "sample": missing_calendar[:5],
        }

    if str(AO_TOOL) not in sys.path:
        sys.path.insert(0, str(AO_TOOL))
    from september_immutable_binding_v1 import load_session_for_month

    session, identity, resolved = load_session_for_month(month)
    log_fn(
        f"immutable input month={month} mode={resolved.binding_mode} "
        f"snapshot_id={identity['snapshot_id'][:16]}… dir={resolved.inputs_dir}"
    )

    resume_from = None
    if resume and progress_path.is_file():
        resume_from = json.loads(progress_path.read_text()).get("last_completed_stem")

    parity_index: dict[str, dict[str, str]] = {}
    if resume and manifest_path.is_file():
        parity_index = json.loads(manifest_path.read_text())

    mismatches = 0
    density_mismatches = 0
    processed = 0
    t0 = time.time()
    write_header = not parity_csv.is_file() or not resume

    with parity_csv.open("a" if resume and parity_csv.is_file() else "w", encoding="utf-8", newline="") as f:
        fields = [
            "decision_close_utc",
            "stem",
            "active_parity",
            "density_parity",
            "frozen_density_manifest",
            "replay_density_manifest",
            "active_errors",
        ]
        w = csv.DictWriter(f, fieldnames=fields)
        if write_header:
            w.writeheader()

        for dc in parity_closes:
            stem = _stem(dc)
            if resume_from and stem <= resume_from:
                continue
            frozen_path = frozen_dir / f"{stem}.json"
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
            try:
                fm = density_manifest(frozen, close_iso)
                rm = density_manifest(replay, close_iso) if not active_ok else fm
                density_ok = fm == rm
            except Exception as exc:
                density_ok = False
                fm, rm = "", ""
                log_fn(f"density error {stem}: {exc}")

            if not active_ok:
                mismatches += 1
            if not density_ok:
                density_mismatches += 1

            w.writerow(
                {
                    "decision_close_utc": close_iso,
                    "stem": stem,
                    "active_parity": "PASS" if active_ok else "FAIL",
                    "density_parity": "PASS" if density_ok else "FAIL",
                    "frozen_density_manifest": fm,
                    "replay_density_manifest": rm,
                    "active_errors": "|".join(errs),
                }
            )
            if active_ok and density_ok:
                parity_index[stem] = {
                    "frozen_density_manifest": fm,
                    "active_parity": "PASS",
                    "density_parity": "PASS",
                }
            processed += 1
            if processed % 100 == 0:
                manifest_path.write_text(json.dumps(parity_index, sort_keys=True), encoding="utf-8")
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
                log_fn(f"validate {month} stem={stem} processed={processed}")

    manifest_path.write_text(json.dumps(parity_index, sort_keys=True), encoding="utf-8")
    wall = time.time() - t0
    if mismatches or density_mismatches:
        return {
            "month": month,
            "verdict": "MONTH_VALIDATION_BLOCKED",
            "active_mismatches": mismatches,
            "density_mismatches": density_mismatches,
            "processed": processed,
            "wall_sec": wall,
        }
    return {
        "month": month,
        "verdict": "MONTH_VALIDATION_PASS",
        "closes": len(parity_closes),
        "parity_index_stems": len(parity_index),
        "wall_sec": wall,
        "manifest": str(manifest_path),
    }
