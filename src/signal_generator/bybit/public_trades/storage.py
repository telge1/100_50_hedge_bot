"""Conservative ClickHouse storage projection from a completed pilot."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

FULL_BACKFILL_FILES = 12680
MERGE_RESERVE_FACTOR = Decimal("1.5")
MAX_SAFE_USE_BYTES = 430 * 1024 * 1024 * 1024
HARD_STOP_FREE_BYTES = 80 * 1024 * 1024 * 1024
PLAN_GZ_BYTES_CONSERVATIVE = int(143.5 * 1024**3)


def bytes_per_trade(ch_bytes: int, logical_rows: int) -> float | None:
    if logical_rows <= 0 or ch_bytes <= 0:
        return None
    return ch_bytes / logical_rows


def project_storage(
    *,
    doge_rows: int,
    lit_rows: int,
    combined_rows: int,
    combined_ch_bytes: int | None,
    combined_physical_rows: int | None,
    combined_gz_bytes: int,
    free_bytes_now: int,
    first_import_ch_bytes: int | None = None,
    full_files: int = FULL_BACKFILL_FILES,
    pilot_files: int = 4,
) -> dict[str, Any]:
    """Project 12,680-file ClickHouse usage.

    ``combined_ch_bytes`` may include unremerged ReplacingMergeTree duplicates.
    Prefer ``first_import_ch_bytes`` (logical==physical) when available, else
    scale current disk by logical/physical.
    """
    logical = combined_rows
    clean_bytes = first_import_ch_bytes
    if clean_bytes is None and combined_ch_bytes and combined_physical_rows and logical:
        clean_bytes = int(combined_ch_bytes * (logical / combined_physical_rows))
    comb_bpt = bytes_per_trade(clean_bytes or 0, logical)
    doge_bpt = comb_bpt
    lit_bpt = comb_bpt
    gz_ratio = None
    if combined_gz_bytes > 0 and clean_bytes:
        gz_ratio = clean_bytes / combined_gz_bytes
    gz_bytes_per_trade = (combined_gz_bytes / logical) if logical else None
    scaled_from_pilot_files = None
    if clean_bytes and pilot_files:
        scaled_from_pilot_files = int(clean_bytes / pilot_files * full_files)
    from_gz = int(gz_ratio * PLAN_GZ_BYTES_CONSERVATIVE) if gz_ratio else None
    est_rows_from_gz = None
    from_rows = None
    if gz_bytes_per_trade and comb_bpt:
        est_rows_from_gz = PLAN_GZ_BYTES_CONSERVATIVE / gz_bytes_per_trade
        from_rows = int(comb_bpt * est_rows_from_gz)
    candidates = [v for v in (scaled_from_pilot_files, from_gz, from_rows) if v]
    conservative_ch = max(candidates) if candidates else 0
    with_merge = int(conservative_ch * float(MERGE_RESERVE_FACTOR))
    remaining = free_bytes_now - with_merge
    blocked = (
        comb_bpt is None
        or with_merge > MAX_SAFE_USE_BYTES
        or remaining < HARD_STOP_FREE_BYTES
    )
    return {
        "doge_rows": doge_rows,
        "lit_rows": lit_rows,
        "combined_logical_rows": logical,
        "combined_physical_rows": combined_physical_rows,
        "clean_ch_bytes": clean_bytes,
        "doge_bytes_per_trade": doge_bpt,
        "lit_bytes_per_trade": lit_bpt,
        "combined_bytes_per_trade": comb_bpt,
        "clickhouse_to_gz_ratio": gz_ratio,
        "gz_bytes_per_trade": gz_bytes_per_trade,
        "estimated_full_rows_from_gz": est_rows_from_gz,
        "projected_ch_from_pilot_file_scale_bytes": scaled_from_pilot_files,
        "projected_ch_from_gz_ratio_bytes": from_gz,
        "projected_ch_from_scaled_rows_bytes": from_rows,
        "conservative_clickhouse_bytes": conservative_ch,
        "merge_reserve_factor": float(MERGE_RESERVE_FACTOR),
        "conservative_with_merge_reserve_bytes": with_merge,
        "free_bytes_now": free_bytes_now,
        "max_safe_use_bytes": MAX_SAFE_USE_BYTES,
        "hard_stop_free_bytes": HARD_STOP_FREE_BYTES,
        "projected_remaining_free_bytes": remaining,
        "full_backfill_blocked_storage": blocked,
        "note": (
            "Pilot days 2026-08-15/16 are weekend (low activity). File-scale from "
            "DOGE+LIT therefore underestimates BTC/ETH. Conservative projection "
            "uses max(file-scale, gz-ratio*143.5GiB, gz-implied rows * bytes/trade) "
            "then *1.5 merge reserve."
        ),
    }
