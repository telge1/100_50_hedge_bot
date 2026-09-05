"""Coverage cost estimates for expansion cases — availability only, no outcomes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from orderbook_analyse.aggressor_efficiency_flip.timeutil import iso_z, parse_utc
from orderbook_analyse.liquidity_pool_entry_contract_expansion_freeze_v1 import (
    RESULTS_DIR_REL,
)
from orderbook_analyse.liquidity_pool_six_case_wall_trade_reaction_sample_v1 import (
    MAX_POST_START_S,
    PRE_START_S,
)
from orderbook_analyse.ob200_v3_raw_discovery.files import list_closed_segments

DEFAULT_RAW_ROOT = Path("data/orderbook_raw_shadow/ob200_v3")
TIMEFRAMES = ("5m", "15m", "30m", "1h")
CASE_05_RUNTIME_S = 485.0
CASE_05_QUERY_HINT = {
    "public_trades_select": 1,
    "raw_ob_reconstruction": 1,
    "lld_packs": len(TIMEFRAMES),
}


def _utc(ts: str | datetime) -> datetime:
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc)
    return parse_utc(str(ts))


def audit_window(ref_ts: str) -> tuple[datetime, datetime]:
    ref = _utc(ref_ts)
    return ref - timedelta(seconds=PRE_START_S), ref + timedelta(seconds=MAX_POST_START_S)


def estimate_raw_ob_seconds(
    *,
    symbol: str,
    reference_ts: str,
    raw_root: Path = DEFAULT_RAW_ROOT,
) -> dict[str, Any]:
    start, end = audit_window(reference_ts)
    segs = list_closed_segments(raw_root, symbols=(symbol,), start=start, end=end)
    covered = 0
    paths: list[str] = []
    for seg in segs:
        if seg.is_boundary_stub:
            continue
        overlap_start = max(start, seg.start_utc)
        overlap_end = min(end, seg.end_utc)
        if overlap_end > overlap_start:
            covered += int((overlap_end - overlap_start).total_seconds())
            paths.append(str(seg.path))
    window_s = int((end - start).total_seconds()) + 1
    return {
        "symbol": symbol,
        "reference_ts": reference_ts,
        "window_start": iso_z(start),
        "window_end": iso_z(end),
        "window_seconds": window_s,
        "raw_ob200_seconds_covered": covered,
        "raw_ob200_coverage_ratio": round(covered / window_s, 4) if window_s else 0.0,
        "segment_paths": paths[:5],
        "segment_count": len(paths),
        "tmp_excluded": True,
    }


def estimate_coverage_batch(
    cases: list[dict[str, Any]],
    *,
    raw_root: Path = DEFAULT_RAW_ROOT,
) -> dict[str, Any]:
    per_case = [
        estimate_raw_ob_seconds(
            symbol=str(c["symbol"]),
            reference_ts=str(c["reference_ts"]),
            raw_root=raw_root,
        )
        for c in cases
    ]
    avg_ob = sum(x["raw_ob200_seconds_covered"] for x in per_case) / max(len(per_case), 1)
    est_case_s = CASE_05_RUNTIME_S
    total_s = est_case_s * len(cases)
    return {
        "per_case": per_case,
        "summary": {
            "case_count": len(cases),
            "avg_raw_ob200_seconds": round(avg_ob, 1),
            "public_trades_availability": "orderbook_analysis.public_trades_canonical (SELECT-only)",
            "lld_pack_timeframes": list(TIMEFRAMES),
            "lld_pack_availability": "chart_backend_lld per timeframe at reference_ts",
            "estimated_runtime_per_case_s": est_case_s,
            "estimated_total_runtime_s": round(total_s, 1),
            "estimated_total_runtime_min": round(total_s / 60.0, 1),
            "estimated_disk_per_case_mb": 25,
            "estimated_total_disk_mb": 25 * len(cases),
            "batch_cache_reuse": (
                "Shared Raw-OB200 segment reads and LLD pack loads across cases "
                "within same UTC hour; wall_first_seen tables reusable per symbol-day."
            ),
            "query_pattern_hint": CASE_05_QUERY_HINT,
            "audit_window": {"pre_s": PRE_START_S, "max_post_s": MAX_POST_START_S},
            "results_dir": RESULTS_DIR_REL,
        },
    }
