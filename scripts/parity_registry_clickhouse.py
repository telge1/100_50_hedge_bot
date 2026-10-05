#!/usr/bin/env python3
"""Compare registry snapshots vs ClickHouse *_latest views."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from bot.shadow_signal_registry.ch_sync import fetch_latest_from_ch
from bot.shadow_signal_registry.store import RUNTIME

FLOAT_TOL = 1e-6
FIELDS = (
    "signal_id",
    "allowed",
    "blocked",
    "entry_price",
    "initial_sl",
    "tp",
    "pnl_pct",
    "block_reason",
    "hypothetical",
    "mae_pct",
    "mfe_pct",
)


def _parse_dt(v) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc).replace(microsecond=0)
        return v.astimezone(timezone.utc).replace(microsecond=0)
    return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(timezone.utc).replace(microsecond=0)


def _norm_snapshot(row: dict) -> dict:
    return {
        "signal_id": row["signal_id"],
        "decision_time": _parse_dt(row.get("decision_time")),
        "end_time": _parse_dt(row.get("exit_time")),
        "allowed": int(bool(row.get("allowed"))),
        "blocked": int(bool(row.get("blocked"))),
        "entry_price": float(row.get("entry_price") or 0),
        "initial_sl": float(row.get("initial_sl") or 0),
        "tp": float(row.get("tp") or 0),
        "pnl_pct": row.get("pnl_pct"),
        "block_reason": row.get("block_reason") or "NONE",
        "hypothetical": int(bool(row.get("hypothetical"))),
        "mae_pct": float(row.get("mae_pct") or 0),
        "mfe_pct": float(row.get("mfe_pct") or 0),
    }


def _norm_ch(row: dict) -> dict:
    return {
        "signal_id": row["signal_id"],
        "decision_time": _parse_dt(row.get("decision_time")),
        "end_time": _parse_dt(row.get("end_time")),
        "allowed": int(row.get("allowed") or 0),
        "blocked": int(row.get("blocked") or 0),
        "entry_price": float(row.get("entry_price") or 0),
        "initial_sl": float(row.get("initial_sl") or 0),
        "tp": float(row.get("tp") or 0),
        "pnl_pct": row.get("pnl_pct"),
        "block_reason": row.get("block_reason") or "NONE",
        "hypothetical": int(row.get("hypothetical") or 0),
        "mae_pct": float(row.get("mae_pct") or 0),
        "mfe_pct": float(row.get("mfe_pct") or 0),
    }


def _diff(a: dict, b: dict) -> list[str]:
    diffs = []
    for key in ("decision_time", "end_time", *FIELDS):
        if key in ("decision_time", "end_time"):
            if a.get(key) != b.get(key):
                diffs.append(f"{key}: {a.get(key)} != {b.get(key)}")
            continue
        av, bv = a.get(key), b.get(key)
        if isinstance(av, float) or isinstance(bv, float):
            if abs(float(av or 0) - float(bv or 0)) > FLOAT_TOL:
                diffs.append(f"{key}: {av} != {bv}")
        elif av != bv:
            diffs.append(f"{key}: {av} != {bv}")
    return diffs


def check_side(side: str) -> int:
    snap_path = RUNTIME / f"{side}_snapshot.json"
    snaps = json.loads(snap_path.read_text(encoding="utf-8")) if snap_path.is_file() else []
    ch_rows = {r["signal_id"]: _norm_ch(r) for r in fetch_latest_from_ch(side)}
    diff_count = 0
    for row in snaps:
        sid = row["signal_id"]
        if sid not in ch_rows:
            print(f"MISSING in CH: {sid}")
            diff_count += 1
            continue
        diffs = _diff(_norm_snapshot(row), ch_rows[sid])
        if diffs:
            print(f"DIFF {sid}:", "; ".join(diffs))
            diff_count += 1
    print(f"{side} parity diffs: {diff_count}")
    return diff_count


def main() -> int:
    total = check_side("long") + check_side("short")
    return 0 if total == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
