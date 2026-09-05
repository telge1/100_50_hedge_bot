"""Phase B: coverage / overlap inventory for closed Raw-OB200 + trades."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from orderbook_analyse.aggressor_efficiency_flip.timeutil import iso_z
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.edge_catalog import DEFAULT_RAW_ROOT
from orderbook_analyse.ob200_v3_raw_discovery.files import list_closed_segments


def build_coverage_inventory(
    *,
    raw_root: Path = DEFAULT_RAW_ROOT,
    symbols: tuple[str, ...] = ("BTCUSDT", "DOGEUSDT"),
    day: datetime | None = None,
) -> list[dict[str, Any]]:
    """List closed hour segments and note research readiness (no CH writes)."""
    day = day or datetime(2026, 8, 29, tzinfo=timezone.utc)
    day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    segs = list_closed_segments(raw_root, symbols=symbols, start=day_start, end=day_end)
    rows: list[dict[str, Any]] = []
    by_hour: dict[tuple[str, str], dict[str, Any]] = {}
    for s in segs:
        if s.is_boundary_stub or s.duration_sec < 60:
            continue
        hour_key = s.start_utc.strftime("%Y-%m-%dT%H:00:00Z")
        key = (s.symbol, hour_key)
        by_hour[key] = {
            "symbol": s.symbol,
            "hour_start": hour_key,
            "ob200_path": str(s.path),
            "ob200_start": iso_z(s.start_utc),
            "ob200_end": iso_z(s.end_utc),
            "ob200_duration_s": s.duration_sec,
            "tmp_excluded": True,
            "public_trades_expected": True,  # canonical CH available historically for these days
            "aef_compatible": True,
            "forward_horizon_60m_requires_end": iso_z(s.end_utc + timedelta(hours=1)),
            "notes": "closed zst only; trade coverage validated at run time via CH read",
        }
    # Pair BTC/DOGE hours
    hours = sorted({h for _, h in by_hour})
    for h in hours:
        btc = by_hour.get(("BTCUSDT", h))
        doge = by_hour.get(("DOGEUSDT", h))
        rows.append(
            {
                "hour_start": h,
                "btc_ob200": bool(btc),
                "doge_ob200": bool(doge),
                "both_symbols": bool(btc and doge),
                "btc_duration_s": (btc or {}).get("ob200_duration_s"),
                "doge_duration_s": (doge or {}).get("ob200_duration_s"),
                "safe_research_hour": bool(btc and doge),
                "estimated_aef_events_order": "~5-25 compressions/hour/symbol (unfitted)",
                "estimated_high_matches_order": "sparse; smoke had ~10 HIGH / 40m",
                "full_60m_outcome_ok_if_next_hour_exists": True,
                "estimated_runtime_s_per_hour_pair": 15,
                "estimated_ch_queries_per_hour_pair": 2,
            }
        )
    return rows


def recommend_expand_window(inventory: list[dict[str, Any]]) -> dict[str, Any]:
    safe = [r for r in inventory if r.get("safe_research_hour")]
    if not safe:
        return {"allowed": False, "reason": "no_paired_btc_doge_hours"}
    # Prefer contiguous closed hours on 2026-08-29 with room for 60m forward:
    # last usable event hour needs next hour present for trades — use hours before last.
    hours = [r["hour_start"] for r in safe]
    if len(hours) < 3:
        return {"allowed": False, "reason": "too_few_hours", "hours": hours}
    # Bounded expand: 10:00–14:00Z with OB load from 09:00, events 10–13, forward into 14
    start = "2026-08-29T10:00:00Z"
    end = "2026-08-29T13:00:00Z"
    data_end = "2026-08-29T14:00:00Z"
    covered = [h for h in hours if start <= h < end]
    return {
        "allowed": len(covered) >= 2,
        "reason": "bounded_btc_doge_closed_hours" if len(covered) >= 2 else "insufficient_overlap",
        "proposed_event_window": [start, end],
        "proposed_data_end_for_60m": data_end,
        "ob_warmup_start": "2026-08-29T09:00:00Z",
        "safe_hours_in_window": covered,
        "estimated_runtime_s": 15 * max(1, len(covered)),
        "estimated_queries": 2 * 2,  # symbols × windows (may batch)
        "universe51": False,
        "open_tmp": False,
    }
