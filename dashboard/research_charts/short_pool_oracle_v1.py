"""Shared Short pool oracle helpers (Phase 0) — current path only, no frozen config."""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_repo = Path(__file__).resolve().parents[4]
_dash = _repo / "backtester" / "short"
if str(_repo) not in sys.path:
    sys.path.insert(0, str(_repo))
if str(_dash) not in sys.path:
    sys.path.insert(0, str(_dash))

from pool_pattern.market import ensure_paths

ensure_paths()

from dashboard.research_charts.lld_research_kernel import (
    candle_stamps,
    load_pane_candles,
    scanner_pools_for_index,
    ui_lld_config,
)
from pool_scan.clock import bar_close

PANE_FROM = datetime(2026, 2, 10, tzinfo=timezone.utc)
PANE_FROM_UNIX = int(PANE_FROM.timestamp())
HISTORY_WEEKS = 12
TIMEFRAME = "15m"

ORACLE_FIXTURES: list[tuple[str, str]] = [
    ("XRPUSDT", "2026-06-19T02:00:00+00:00"),
    ("XRPUSDT", "2026-06-18T00:45:00+00:00"),
    ("XRPUSDT", "2026-06-07T05:15:00+00:00"),
    ("ADAUSDT", "2026-06-24T01:45:00+00:00"),
    ("ADAUSDT", "2026-06-07T05:00:00+00:00"),
    ("DOGEUSDT", "2026-06-11T12:00:00+00:00"),
    ("DOGEUSDT", "2026-06-25T17:30:00+00:00"),
]


def parse_decision_iso(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _canonical_rows(rows: list[dict]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda r: r["pool_id"]):
        item: dict[str, Any] = {
            "pool_id": row["pool_id"],
            "side": row["side"],
            "bottom": float(row["bottom"]),
            "top": float(row["top"]),
        }
        known = row.get("known")
        if isinstance(known, datetime):
            item["known"] = known.isoformat()
        break_at = row.get("break_at")
        if isinstance(break_at, datetime):
            item["break_at"] = break_at.isoformat()
        out.append(item)
    return out


def rows_hash(rows: list[dict]) -> str:
    payload = json.dumps(_canonical_rows(rows), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def current_lld_config_snapshot(timeframe: str) -> dict[str, Any]:
    cfg = ui_lld_config(timeframe)
    return {
        "enabled": bool(cfg.enabled),
        "amount_effective": int(cfg.amount),
        "highest_len": int(cfg.highest_len),
        "lowest_len": int(cfg.lowest_len),
    }


def short_pool_oracle_snapshot(symbol: str, decision_iso: str) -> dict[str, Any]:
    decision = parse_decision_iso(decision_iso)
    decision_unix = int(decision.timestamp())
    cfg = ui_lld_config(TIMEFRAME)
    _packed, candles = load_pane_candles(
        symbol,
        TIMEFRAME,
        from_unix=PANE_FROM_UNIX,
        to_unix=decision_unix,
        history_weeks=HISTORY_WEEKS,
    )
    stamps = candle_stamps(candles)
    idx = next(i for i, s in enumerate(stamps) if bar_close(s, TIMEFRAME) == decision)
    available, _ui, proof = scanner_pools_for_index(
        candles, TIMEFRAME, idx, cfg, cache={}
    )
    pool_ids = sorted(r["pool_id"] for r in available)
    return {
        "symbol": symbol,
        "timeframe": TIMEFRAME,
        "decision_timestamp": decision.isoformat(),
        "entry_open": stamps[idx].isoformat(),
        "candle_index": idx,
        "pane_first_timestamp": stamps[0].isoformat(),
        "pane_last_timestamp": stamps[-1].isoformat(),
        "lld_config": current_lld_config_snapshot(TIMEFRAME),
        "available_pool_ids": pool_ids,
        "available_count": len(pool_ids),
        "available_rows_hash": rows_hash(available),
        "proof": proof.as_dict(),
    }


def oracle_snapshot_twice(symbol: str, decision_iso: str) -> tuple[dict[str, Any], dict[str, Any]]:
    return short_pool_oracle_snapshot(symbol, decision_iso), short_pool_oracle_snapshot(
        symbol, decision_iso
    )
