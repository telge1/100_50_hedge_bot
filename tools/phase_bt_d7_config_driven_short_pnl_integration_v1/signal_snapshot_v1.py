"""Causal signal decision snapshot (planned entry/SL/TP at signal time)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from bands_adapter_v1 import snapshot_to_intervals
from signal_engine_v1 import ShortSignalV1


def _parse_utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _find_band(snapshot: dict[str, Any], side: str, pool_lo: float, pool_hi: float) -> dict[str, Any] | None:
    key = f"{pool_lo:.17g}|{pool_hi:.17g}"
    for b in snapshot.get("upper_bands" if side == "upper" else "lower_bands") or []:
        bk = f"{float(b['lower_price']):.17g}|{float(b['upper_price']):.17g}"
        if bk == key:
            return b
    return None


def build_signal_snapshot(
    config: dict[str, Any],
    sig: ShortSignalV1,
    density_snapshot: dict[str, Any],
    *,
    config_hash: str,
    density_provenance: dict[str, Any] | None = None,
    provenance_tag: str = "LIVE_AT_SIGNAL",
) -> dict[str, Any]:
    """Planned levels from existing strategy_rules only (no new rules)."""
    import sys
    from pathlib import Path

    p4d = Path(__file__).resolve().parents[2] / "phase4d_pool_landscape_backtester_v2"
    if str(p4d) not in sys.path:
        sys.path.insert(0, str(p4d))
    from strategy_rules import select_short_tp_depth2, short_sl  # noqa: WPS433

    entry = float(sig.entry_level)
    sl_pct = float(config["risk"]["sl_above_upper_top_pct"])
    min_tp = float(config["take_profit"]["min_distance_pct"])
    sl_planned = short_sl(sig.pool_upper, sl_pct)
    intervals = snapshot_to_intervals(density_snapshot)
    tp_iv, tp_reason = select_short_tp_depth2(entry, intervals, min_tp)
    tp_top = None
    tp_bottom = None
    if tp_iv:
        tp_top = float(tp_iv.get("top") or tp_iv.get("top_price"))
        tp_bottom = float(tp_iv.get("bottom") or tp_iv.get("bottom_price"))
    tp_planned = tp_top

    upper_b = _find_band(density_snapshot, "upper", sig.pool_lower, sig.pool_upper)
    target_lower = None
    if tp_iv and tp_bottom is not None and tp_top is not None:
        target_lower = _find_band(density_snapshot, "lower", tp_bottom, tp_top)

    expiry_bars = config["short_entry"].get("pending_limit_expiry_bars")
    pending_expiry = None
    if expiry_bars is not None and int(expiry_bars) > 0:
        pending_expiry = _iso(_parse_utc(sig.order_active_from_utc) + timedelta(minutes=5 * int(expiry_bars)))

    sid = f"{config_hash[:12]}:{sig.decision_close_utc}:{sig.pool_key}"

    snap = {
        "signal_id": sid,
        "signal_time_utc": sig.decision_close_utc,
        "side": "SHORT",
        "decision_close_utc": sig.decision_close_utc,
        "entry_price_planned": entry,
        "entry_price_reference": config["short_entry"]["entry_price_reference"],
        "entry_offset_pct": float(config["short_entry"].get("entry_offset_pct") or 0),
        "stop_loss_price_planned": sl_planned,
        "take_profit_price_planned": tp_planned,
        "take_profit_selection_reason": tp_reason,
        "entry_upper_band_bottom": sig.pool_lower,
        "entry_upper_band_top": sig.pool_upper,
        "entry_upper_origin_ids": (upper_b or {}).get("origin_ids"),
        "target_lower_band_bottom": tp_bottom,
        "target_lower_band_top": tp_top,
        "target_lower_origin_ids": (target_lower or {}).get("origin_ids") if target_lower else None,
        "upper_gap_pct": sig.gap_min_pct_used,
        "pending_start_utc": sig.order_active_from_utc,
        "pending_expiry_utc": pending_expiry,
        "actual_order_status": "PENDING_PLACED",
        "config_hash": config_hash,
        "density_provenance": density_provenance or {
            "pipeline_line_id": density_snapshot.get("pipeline_line_id"),
            "config_hash": density_snapshot.get("config_hash"),
            "decision_close_utc": sig.decision_close_utc,
        },
        "snapshot_provenance": provenance_tag,
    }
    return snap


def snapshot_from_legacy_signal(
    config: dict[str, Any],
    signal_row: dict[str, Any],
    density_snapshot: dict[str, Any],
    *,
    config_hash: str,
    provenance_tag: str = "RECONSTRUCTED",
) -> dict[str, Any]:
    """Reconstruct planned snapshot for BT-D9-era rows (deterministic rules + as-of density)."""
    sig = ShortSignalV1(
        decision_close_utc=str(signal_row["decision_close_utc"]),
        order_active_from_utc=str(signal_row["order_active_from_utc"]),
        pool_lower=float(signal_row["pool_lower"]),
        pool_upper=float(signal_row["pool_upper"]),
        pool_key=f"{float(signal_row['pool_lower']):.17g}|{float(signal_row['pool_upper']):.17g}",
        entry_level=float(signal_row.get("entry_level") or signal_row["pool_lower"]),
        gap_min_pct_used=float(signal_row.get("gap_min_pct_used") or config["short_entry"]["upper_gap_min_pct"]),
        require_large_upper_gap=True,
        experimental_selection=str(signal_row.get("experimental_selection") or ""),
    )
    return build_signal_snapshot(
        config,
        sig,
        density_snapshot,
        config_hash=config_hash,
        provenance_tag=provenance_tag,
    )
