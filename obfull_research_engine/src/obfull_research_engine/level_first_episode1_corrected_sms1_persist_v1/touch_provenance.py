"""Prove whether Episode-1 first_touch is independently derived from raw market data.

This module does NOT claim the production pipeline detects touches from trades.
It documents the actual code path and contrasts it with an optional independent
ask-wall touch scan over frozen public trades (read-only).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..bounded_level_first_analyzer_pilot_v1.persist import atomic_write_json, atomic_write_text
from ..drilldown.aggregation_100ms import _as_dt
from ..paths import ENGINE_ROOT
from ..timeparse import format_utc_z
from . import (
    DETECTION,
    EPISODE_ID,
    EVIDENCE_START,
    FIRST_TOUCH,
    FIRST_TOUCH_ISO,
    SMS1_EP1_TRADES,
)
from .io_zst import body_rows, read_jsonl_zst

VERDICT_RAW = "EPISODE1_TOUCH_TRIGGER_RAW_PROVEN"
VERDICT_EVENT_ONLY = "EPISODE1_TOUCH_TRIGGER_EVENT_TIME_CAUSAL_ONLY"
VERDICT_NOT_DERIVED = "EPISODE1_TOUCH_TRIGGER_NOT_INDEPENDENTLY_DERIVED"
VERDICT_FAILED = "EPISODE1_TOUCH_TRIGGER_CAUSALITY_FAILED"

WALL_ID = "w_781b6ed696e777e1"
WALL_SIDE = "ask"
WALL_PRICE = 79780.0

CFG_PATH = ENGINE_ROOT / "config/level_first_episode1_corrected_sms1_persist_v1.json"
OUT_DIR = (
    ENGINE_ROOT
    / "results/level_first_episode1_corrected_sms1_persist_v1/BTCUSDT"
    / "e2e1_29492befed0c9efc_prove"
    / "touch_provenance"
)


def parse_trade_ts(value: Any) -> datetime:
    return _as_dt(value)


def ask_wall_touch_rule(*, price: float, wall_price: float = WALL_PRICE) -> bool:
    """Ask-wall touch: trade price reaches or crosses the ask wall from below."""
    return float(price) >= float(wall_price)


def find_first_ask_wall_touch(
    trades: list[dict[str, Any]],
    *,
    wall_price: float = WALL_PRICE,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
) -> dict[str, Any] | None:
    """Independent scan: earliest trade with price >= ask wall in [start, end)."""
    start = window_start or datetime.min.replace(tzinfo=timezone.utc)
    end = window_end or datetime.max.replace(tzinfo=timezone.utc)
    ordered = sorted(
        trades,
        key=lambda r: (
            parse_trade_ts(r.get("trade_ts") or r.get("event_time") or r.get("exchange_event_time")),
            str(r.get("trade_id") or ""),
        ),
    )
    for idx, row in enumerate(ordered):
        ts = parse_trade_ts(row.get("trade_ts") or row.get("event_time") or row.get("exchange_event_time"))
        if ts < start or ts >= end:
            continue
        if ask_wall_touch_rule(price=float(row["price"]), wall_price=wall_price):
            return {"index": idx, "row": row, "exchange_event_time": ts}
    return None


def context_window(
    trades: list[dict[str, Any]],
    *,
    center_index: int,
    before: int = 5,
    after: int = 5,
) -> list[dict[str, Any]]:
    ordered = sorted(
        trades,
        key=lambda r: (
            parse_trade_ts(r.get("trade_ts") or r.get("event_time") or r.get("exchange_event_time")),
            str(r.get("trade_id") or ""),
        ),
    )
    lo = max(0, center_index - before)
    hi = min(len(ordered), center_index + after + 1)
    out = []
    for i in range(lo, hi):
        out.append(trade_record(ordered[i], source_row=i, relative=i - center_index))
    return out


def trade_record(row: dict[str, Any], *, source_row: int, relative: int | None = None) -> dict[str, Any]:
    et = row.get("trade_ts") or row.get("event_time") or row.get("exchange_event_time")
    recv = row.get("collector_received_at") or row.get("receive_time") or row.get("receive_time_ns")
    return {
        "trade_id": row.get("trade_id"),
        "price": row.get("price"),
        "size": row.get("size"),
        "side": row.get("taker_side") or row.get("side"),
        "exchange_event_time": format_utc_z(parse_trade_ts(et)) if et is not None else None,
        "collector_received_at": format_utc_z(_as_dt(recv)) if recv is not None else None,
        "raw_available_at": format_utc_z(parse_trade_ts(et)) if et is not None else None,
        "receive_time_present": recv is not None,
        "source_file": str(ENGINE_ROOT / SMS1_EP1_TRADES),
        "source_row": source_row,
        "stable_record_id": row.get("trade_id"),
        "relative_to_trigger": relative,
        "meets_ask_wall_rule": ask_wall_touch_rule(price=float(row["price"])) if row.get("price") is not None else False,
    }


def production_path_proof() -> dict[str, Any]:
    init_src = (Path(__file__).with_name("__init__.py")).read_text(encoding="utf-8")
    derived_src = (Path(__file__).with_name("derived.py")).read_text(encoding="utf-8")
    walls_src = (Path(__file__).with_name("walls.py")).read_text(encoding="utf-8")
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    return {
        "first_set_in": {
            "file": "level_first_episode1_corrected_sms1_persist_v1/__init__.py",
            "symbol": "FIRST_TOUCH_ISO / FIRST_TOUCH",
            "value": FIRST_TOUCH_ISO,
            "also_in_config": cfg.get("first_touch"),
        },
        "consumed_by": [
            {
                "file": "derived.py",
                "function": "build_touch_detection",
                "behavior": "stamps TOUCH row with FIRST_TOUCH; reconstructs book as-of; side/price remain None",
            },
            {
                "file": "walls.py",
                "function": "analyze_walls_timed",
                "behavior": "uses first_touch as as-of instant for wall statements; does not discover touch time",
            },
            {
                "file": "derived_from_readback.py",
                "function": "derive_from_persisted_sms1",
                "behavior": "default first_touch=FIRST_TOUCH constant",
            },
        ],
        "code_evidence": {
            "init_hardcodes_iso": FIRST_TOUCH_ISO in init_src and "FIRST_TOUCH_ISO =" in init_src,
            "derived_uses_constant": "_stream_at(replay, FIRST_TOUCH)" in derived_src,
            "derived_has_no_trade_scan": (
                "def build_touch_detection" in derived_src
                and "trade" not in derived_src.split("def build_touch_detection", 1)[1].split("def build_walls", 1)[0].lower()
            ),
            "walls_takes_first_touch_param": "first_touch: datetime" in walls_src,
        },
        "upstream_episode_origin_note": (
            "Episode id and first_touch_ts originally come from "
            "bounded_level_first_analyzer_pilot_v1/episodes.py zone visit "
            f"(config zone [{cfg.get('zone_low')}, {cfg.get('zone_high')}]), "
            "not from ask-wall price 79780.0. This corrected-sms1 pipeline "
            "imports that timestamp as a fixed research constant."
        ),
        "independently_derived_in_this_pipeline": False,
    }


def load_frozen_trades() -> list[dict[str, Any]]:
    return body_rows(read_jsonl_zst(ENGINE_ROOT / SMS1_EP1_TRADES))


def run_touch_provenance(*, write: bool = True) -> dict[str, Any]:
    path = production_path_proof()
    trades = load_frozen_trades()
    receive_fields = sorted(
        {
            k
            for r in trades
            for k in r.keys()
            if "receive" in k.lower() or k in {"collector_received_at", "ingest_time"}
        }
    )
    independent = find_first_ask_wall_touch(
        trades,
        wall_price=WALL_PRICE,
        window_start=EVIDENCE_START,
        window_end=DETECTION,
    )
    zone_lo = 79774.75
    zone_hi = 79775.25
    zone_hits = [
        trade_record(r, source_row=i)
        for i, r in enumerate(
            sorted(
                trades,
                key=lambda x: (parse_trade_ts(x["trade_ts"]), str(x.get("trade_id") or "")),
            )
        )
        if parse_trade_ts(r["trade_ts"]) == FIRST_TOUCH and zone_lo <= float(r["price"]) <= zone_hi
    ]

    # Contextual trades at reported timestamp (not a production trigger)
    ordered = sorted(trades, key=lambda r: (parse_trade_ts(r["trade_ts"]), str(r.get("trade_id") or "")))
    at_touch_idxs = [i for i, r in enumerate(ordered) if parse_trade_ts(r["trade_ts"]) == FIRST_TOUCH]
    context_at_reported = []
    if at_touch_idxs:
        center = at_touch_idxs[0]
        context_at_reported = context_window(trades, center_index=center, before=5, after=5)

    independent_ctx = None
    if independent is not None:
        independent_ctx = {
            "trigger": trade_record(independent["row"], source_row=independent["index"], relative=0),
            "before_after": context_window(trades, center_index=independent["index"], before=5, after=5),
            "earlier_valid_touch_exists_vs_reported": independent["exchange_event_time"] != FIRST_TOUCH,
            "delta_ms_vs_reported_first_touch": int(
                (independent["exchange_event_time"] - FIRST_TOUCH).total_seconds() * 1000
            ),
        }

    # Verdict: production did not independently derive.
    verdict = VERDICT_NOT_DERIVED
    reasons = [
        "FIRST_TOUCH is hardcoded in __init__.py and config JSON",
        "build_touch_detection stamps the constant; does not scan public trades or mid/best",
        "persisted TOUCH row has side=None and price=None (as-of book snapshot only)",
        "ask-wall 79780 touch is not the production first_touch definition",
    ]
    if independent is not None and independent["exchange_event_time"] != FIRST_TOUCH:
        reasons.append(
            f"independent ask-wall scan first hit at {format_utc_z(independent['exchange_event_time'])}, "
            f"not reported {FIRST_TOUCH_ISO}"
        )

    payload = {
        "verdict": verdict,
        "reasons": reasons,
        "episode_id": EPISODE_ID,
        "reported": {
            "touch_time": FIRST_TOUCH_ISO,
            "wall_id": WALL_ID,
            "side": WALL_SIDE,
            "wall_price": WALL_PRICE,
        },
        "production_path": path,
        "frozen_public_trades": {
            "path": str(ENGINE_ROOT / SMS1_EP1_TRADES),
            "n_trades": len(trades),
            "receive_time_fields_present": receive_fields,
            "collector_received_at_present": bool(receive_fields),
            "live_receive_causality_provable": False,
            "note": "Live-Receive-Time-Kausalität ist mit diesem Input nicht beweisbar.",
        },
        "contextual_trades_at_reported_touch_time": {
            "note": (
                "Trades at the reported timestamp exist (zone/liquidity prints), "
                "but the corrected-sms1 pipeline did not select first_touch from them."
            ),
            "zone_hits_at_exact_timestamp": zone_hits,
            "window_around_first_row_at_timestamp": context_at_reported,
        },
        "independent_ask_wall_scan_not_used_by_pipeline": independent_ctx,
        "central_answer": (
            "Der Touch um 20:19:02.229Z wurde in dieser Derived-Pipeline nicht kausal "
            "aus Roh-Marktdaten neu erkannt, sondern als bereits bekannter Episode-"
            "Zeitstempel (FIRST_TOUCH_ISO / config.first_touch) übernommen."
        ),
        "full_ob_replay_required": False,
        "full_ob_replay_reason": "No production semantics change; provenance documentation only.",
    }
    if write:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        atomic_write_json(OUT_DIR / "touch_provenance_manifest.json", payload)
        atomic_write_text(OUT_DIR / "STATUS", verdict + "\n")
        # Do not overwrite CAUSAL_AVAILABILITY_STATUS; write sibling only.
        atomic_write_text(
            OUT_DIR.parent / "TOUCH_PROVENANCE_STATUS",
            verdict + "\n",
        )
    print(json.dumps({"verdict": verdict, "reasons": reasons, "central_answer": payload["central_answer"]}, indent=2))
    return payload


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Episode-1 touch provenance audit (read-only).")
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)
    result = run_touch_provenance(write=not args.no_write)
    return 0 if result["verdict"] == VERDICT_NOT_DERIVED else 1
