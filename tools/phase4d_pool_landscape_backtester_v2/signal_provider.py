"""Load Phase 2A shorts and Phase 3B experimental longs (read-only)."""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class RawSignal:
    candidate_id: str
    direction: str
    signal_close_utc: str
    order_active_from_utc: str
    signal_source: str
    pool_lower: float
    pool_upper: float
    pool_identity: str
    landscape_intervals: list[dict]
    upper_tp_rows: list[dict]
    market_state_experimental: str


def in_pilot(t: str, cfg: dict) -> bool:
    return cfg["pilot_from_utc"] <= t <= cfg["pilot_to_utc"]


def load_market_states() -> dict[str, str]:
    path = ROOT / "results/phase3b_two_pool_market_structure_detector_v1/market_state_timeline.csv"
    out = {}
    if path.is_file():
        with path.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                out[row["as_of_close_utc"]] = row["market_state"]
    return out


def load_short_signals(cfg: dict) -> list[RawSignal]:
    phase2a = ROOT / "results/phase2a_5m_pd1_depth2_short_candidates_v1"
    states = load_market_states()
    out: list[RawSignal] = []
    with (phase2a / "candidates.csv").open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            sig = row["signal_close_utc"]
            if not in_pilot(sig, cfg):
                continue
            prof = json.loads(
                (phase2a / "candidate_landscape_profiles" / f"{row['candidate_id']}.json").read_text()
            )
            cand = prof.get("candidate") or {}
            out.append(
                RawSignal(
                    candidate_id=row["candidate_id"],
                    direction="SHORT",
                    signal_close_utc=sig,
                    order_active_from_utc=row["entry_open_utc"],
                    signal_source="PHASE2A_RAW_SHORT",
                    pool_lower=float(row["upper_pool_lower"]),
                    pool_upper=float(row["upper_pool_upper"]),
                    pool_identity="|".join(cand.get("upper_pool_origins") or []),
                    landscape_intervals=cand.get("landscape_intervals") or [],
                    upper_tp_rows=[],
                    market_state_experimental=states.get(sig, "UNRESOLVED"),
                )
            )
    return out


def load_long_signals(cfg: dict) -> list[RawSignal]:
    conf_path = ROOT / "results/phase3b_two_pool_market_structure_detector_v1/second_lower_confirmation_events.csv"
    tp_path = ROOT / "results/phase3b_two_pool_market_structure_detector_v1/upper_tp_candidates_at_confirmation.csv"
    states = load_market_states()
    tp_by_key: dict[tuple[str, str], list[dict]] = {}
    with tp_path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            key = (row["episode_id"], row["confirmation_close_utc"])
            tp_by_key.setdefault(key, []).append(row)

    out: list[RawSignal] = []
    with conf_path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            close = row["confirmation_close_utc"]
            if not in_pilot(close, cfg):
                continue
            ep = row["episode_id"]
            key = (ep, close)
            uppers = []
            for t in tp_by_key.get(key, []):
                uppers.append(
                    {
                        "side": "upper",
                        "bottom_price": float(t["bottom_price"]),
                        "top_price": float(t["top_price"]),
                        "depth": int(t.get("depth") or 2),
                        "origins": json.loads(t.get("origins") or "[]"),
                    }
                )
            cid = f"L3B_E{int(ep):02d}_{close.replace(':', '').replace('-', '')}"
            out.append(
                RawSignal(
                    candidate_id=cid,
                    direction="LONG",
                    signal_close_utc=close,
                    order_active_from_utc=row["earliest_entry_open_utc"],
                    signal_source="PHASE3B_EXPERIMENTAL_LONG",
                    pool_lower=float(row["second_lower_bottom"]),
                    pool_upper=float(row["second_lower_top"]),
                    pool_identity=row.get("second_pool_track_id") or "",
                    landscape_intervals=[],
                    upper_tp_rows=uppers,
                    market_state_experimental=states.get(close, "LONG_CANDIDATE"),
                )
            )
    return out


def load_all_signals(cfg: dict) -> list[RawSignal]:
    return load_short_signals(cfg) + load_long_signals(cfg)
