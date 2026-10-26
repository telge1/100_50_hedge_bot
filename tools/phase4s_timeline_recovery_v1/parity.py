"""Timeline parity vs Phase 3A reference."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
REF_JSONL = ROOT / "results/phase3a_dynamic_pool_landscape_timeline_v1/landscape_timeline.jsonl"


def load_index(path: Path) -> dict[str, dict]:
    idx: dict[str, dict] = {}
    if not path.is_file():
        return idx
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                s = json.loads(line)
                idx[s["candle_close_utc"]] = s
    return idx


def norm_pool(p: dict) -> dict:
    return {
        "side": p.get("side"),
        "bottom": float(p["bottom_price"]),
        "top": float(p["top_price"]),
        "depth": int(p.get("depth") or 0),
        "origins": sorted(p.get("origins") or []),
    }


def norm_snap(s: dict) -> dict:
    ups = sorted(
        [norm_pool(p) for p in s.get("upper_pools") or [] if int(p.get("depth") or 0) >= 2],
        key=lambda x: (x["bottom"], x["top"]),
    )
    lows = sorted(
        [norm_pool(p) for p in s.get("lower_pools") or [] if int(p.get("depth") or 0) >= 2],
        key=lambda x: (x["bottom"], x["top"]),
    )
    return {
        "candle_close_utc": s["candle_close_utc"],
        "close": float(s["ohlc"]["c"]),
        "upper_d2_count": len(ups),
        "lower_d2_count": len(lows),
        "upper_d2_hash": hashlib_json(ups),
        "lower_d2_hash": hashlib_json(lows),
    }


def hashlib_json(obj: Any) -> str:
    import hashlib

    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()[:16]


def compare_timelines(
    new_index: dict[str, dict],
    ref_index: dict[str, dict],
    from_utc: str,
    to_utc: str,
) -> tuple[list[dict], str]:
    rows = []
    status = "TIMELINE_PARITY_OK"
    shared = sorted(set(new_index) & set(ref_index))
    shared = [t for t in shared if from_utc <= t <= to_utc]
    if not shared:
        return [{"note": "no overlapping timestamps"}], "TIMELINE_PARITY_FAILED"
    mism = 0
    for t in shared:
        rn = norm_snap(new_index[t])
        rr = norm_snap(ref_index[t])
        ok = (
            rn["upper_d2_hash"] == rr["upper_d2_hash"]
            and rn["lower_d2_hash"] == rr["lower_d2_hash"]
            and rn["close"] == rr["close"]
        )
        if not ok:
            mism += 1
        rows.append(
            {
                "candle_close_utc": t,
                "parity_ok": ok,
                "new_upper_hash": rn["upper_d2_hash"],
                "ref_upper_hash": rr["upper_d2_hash"],
                "new_lower_hash": rn["lower_d2_hash"],
                "ref_lower_hash": rr["lower_d2_hash"],
            }
        )
    if mism:
        status = "TIMELINE_PARITY_FAILED"
    rows.append(
        {
            "summary": True,
            "compared_bars": len(shared),
            "mismatches": mism,
            "parity_status": status,
        }
    )
    return rows, status
