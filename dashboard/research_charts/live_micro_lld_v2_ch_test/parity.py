"""Compare engine output to CH rows or a second engine pass (expect zero diffs)."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any, Sequence

from indicators.live_micro_lld_v2.models import LiquidityPoolV2

from .identity import config_hash as full_config_hash
from .serialize import pool_fingerprint, row_fingerprint


def compare_engine_pools_to_rows(
    pools: Sequence[LiquidityPoolV2],
    rows: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    eng_fps = sorted(pool_fingerprint(p) for p in pools)
    row_fps = sorted(row_fingerprint(r) for r in rows)
    eng_ids = {p.pool_id for p in pools}
    row_ids = {r["pool_id"] for r in rows}

    report: dict[str, Any] = {
        "engine_pool_count": len(pools),
        "row_pool_count": len(rows),
        "pool_count_match": len(pools) == len(rows),
        "pool_id_match": eng_ids == row_ids,
        "missing_in_rows": sorted(eng_ids - row_ids),
        "extra_in_rows": sorted(row_ids - eng_ids),
        "fingerprint_match": eng_fps == row_fps,
        "engine_only_fps": [],
        "row_only_fps": [],
    }
    if not report["fingerprint_match"]:
        c_eng = Counter(eng_fps)
        c_row = Counter(row_fps)
        report["engine_only_fps"] = list((c_eng - c_row).elements())[:5]
        report["row_only_fps"] = list((c_row - c_eng).elements())[:5]
    return report


def compare_as_of_snapshots(
    engine_dicts: list[dict[str, Any]],
    reader_dicts: list[dict[str, Any]],
) -> dict[str, Any]:
    from .serialize import _norm_iso

    def snap_key(d: dict[str, Any]) -> tuple:
        return (
            d["pool_id"],
            round(float(d["top_price"]), 8),
            round(float(d["bottom_price"]), 8),
            round(float(d["strength"]), 8),
            str(d.get("strength_class")),
            _norm_iso(d.get("known_at")),
            _norm_iso(d.get("break_at")),
            bool(d.get("active")),
        )

    e = sorted(snap_key(d) for d in engine_dicts)
    r = sorted(snap_key(d) for d in reader_dicts)
    return {
        "count_match": len(e) == len(r),
        "engine_count": len(e),
        "reader_count": len(r),
        "snapshot_match": e == r,
    }


def run_parity_check(
    symbol: str,
    timeframe: str,
    from_close: datetime,
    to_close: datetime,
    *,
    pool_detail: int = 10,
    run_id: str,
) -> dict[str, Any]:
    from .batch import _load_candles_for_window, run_engine_on_candles
    from .reader import LiveMicroLldV2ChTestReader

    candles, _meta = _load_candles_for_window(
        symbol, timeframe, from_close, to_close, pool_detail
    )
    result, _cfg = run_engine_on_candles(candles, pool_detail)
    pools = list(result.pools)

    reader = LiveMicroLldV2ChTestReader()
    rows = reader.load_all_pools(run_id)

    core = compare_engine_pools_to_rows(pools, rows)

    from indicators.live_micro_lld_v2.as_of import pools_as_of

    raw_dicts = [p.to_dict() for p in pools]
    mid_ts = candles[len(candles) // 2].timestamp
    eng_asof = pools_as_of(raw_dicts, mid_ts)
    read_asof = reader.pools_visible_at(run_id, mid_ts)

    asof_report = compare_as_of_snapshots(eng_asof, read_asof)

    def field_ok(name: str) -> str:
        if name == "pool_count":
            return "PASS" if core["pool_count_match"] and core["pool_id_match"] else "FAIL"
        if name == "bounds" or name == "strength":
            return "PASS" if core["fingerprint_match"] else "FAIL"
        if name == "break" or name == "invalidation":
            return "PASS" if core["fingerprint_match"] else "FAIL"
        return "NOT_VERIFIED"

    return {
        "core": core,
        "as_of_mid_candle": mid_ts.isoformat(),
        "historical_asof": asof_report,
        "summary": {
            "ENGINE_VS_CH_PARITY": "PASS" if core["fingerprint_match"] else "FAIL",
            "POOL_ID_PARITY": "PASS" if core["pool_id_match"] else "FAIL",
            "BOUNDS_PARITY": field_ok("bounds"),
            "STRENGTH_PARITY": field_ok("strength"),
            "BREAK_PARITY": field_ok("break"),
            "INVALIDATION_PARITY": field_ok("invalidation"),
            "ACTIVE_STATE_PARITY": "PASS" if core["fingerprint_match"] else "FAIL",
            "HISTORICAL_ASOF_PARITY": "PASS" if asof_report["snapshot_match"] else "FAIL",
        },
    }
