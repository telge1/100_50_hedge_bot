"""P17AB — Research chart overlay from frozen P17AA Bar-C executables (no signal recompute)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from .bt_d12_latest_backtester import resolve_pools_ob_repo

SOURCE = "p17aa_bar_c_funnel_020_v1"
STRATEGY_ID = "p17_landscape_long_july_2026_research"
DRAWING_PREFIX = "p17ab-"
LAYER_DISPLAY_NAME = "P17 – 0.20% Upper-Gate Diagnostic"
P17AA_REL = "results/phase_ema59_bt_p17_landscape_long_research_v1/p17aa_bar_c_funnel_020_v1.json"

FIRST_IN_EPISODE_UTCS = frozenset(
    {
        "2026-07-07T11:10:00Z",
        "2026-07-08T09:25:00Z",
        "2026-07-13T14:00:00Z",
        "2026-07-17T02:00:00Z",
        "2026-07-29T16:50:00Z",
    }
)

FilterMode = Literal["first_only", "all"]


def p17aa_results_path(root: Path | None = None) -> Path:
    repo = root or resolve_pools_ob_repo()
    return repo / P17AA_REL


def load_p17aa_executables(root: Path | None = None) -> list[dict[str, Any]]:
    path = p17aa_results_path(root)
    if not path.is_file():
        return []
    doc = json.loads(path.read_text(encoding="utf-8"))
    rows = list(doc.get("nine_executables") or [])
    if len(rows) != 9:
        raise ValueError(f"P17AA expected 9 executables, got {len(rows)}")
    return rows


def _parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)


def _episode_tag(row: dict[str, Any]) -> str:
    utc = str(row.get("entry_utc") or "")
    if utc in FIRST_IN_EPISODE_UTCS:
        return "FIRST_IN_EPISODE"
    return "DIAGNOSTIC_REPEAT"


def filter_executables(rows: list[dict[str, Any]], mode: FilterMode) -> list[dict[str, Any]]:
    if mode == "all":
        return list(rows)
    return [r for r in rows if str(r.get("entry_utc") or "") in FIRST_IN_EPISODE_UTCS]


def candidate_to_position_spec(row: dict[str, Any], *, seq: int) -> dict[str, Any] | None:
    entry = row.get("entry_price")
    sl = row.get("sl_price")
    tp_lo = row.get("upper_lo")
    entry_utc = str(row.get("entry_utc") or "")
    if entry is None or sl is None or tp_lo is None or not entry_utc:
        return None
    start = _parse_dt(entry_utc)
    end = start + timedelta(minutes=30)
    cross = str(row.get("cross_id") or "")
    ep_tag = _episode_tag(row)
    sl_mode = str(row.get("sl_mode") or "")
    sl_dist = row.get("sl_distance_pct")
    tp_dist = row.get("tp_distance_pct")
    lo = row.get("lower_lo")
    hi = row.get("lower_hi")
    ulo = row.get("upper_lo")
    uhi = row.get("upper_hi")
    dist_lbl = ""
    if sl_dist is not None and tp_dist is not None:
        dist_lbl = f" · SL −{float(sl_dist):.2f}% · TP +{float(tp_dist):.2f}%"
    safe = entry_utc.replace(":", "").replace("-", "")
    drawing_id = f"{DRAWING_PREFIX}pos-{seq:02d}-{safe}"
    status = (
        f"{LAYER_DISPLAY_NAME} · {cross} · {ep_tag} · DIAGNOSTIC_ONLY · {sl_mode}{dist_lbl}\n"
        f"Entry {float(entry):.6f} · SL {float(sl):.5f} · TP lo {float(tp_lo):.6f}\n"
        f"Lower [{lo}–{hi}] orig={row.get('lower_origins')} w={row.get('lower_width_pct')} "
        f"i={row.get('lower_density_i')}\n"
        f"Upper [{ulo}–{uhi}] · Bullish {row.get('bullish_confirm_utc')}"
    )
    return {
        "drawing_id": drawing_id,
        "drawing_type": "long_position",
        "symbol": "DOGEUSDT",
        "timeframe": "5m",
        "start": start.isoformat().replace("+00:00", "Z"),
        "end": end.isoformat().replace("+00:00", "Z"),
        "entry": float(entry),
        "stop": float(sl),
        "target": float(tp_lo),
        "signal_id": f"p17ab-{seq:02d}",
        "direction": "LONG",
        "source": SOURCE,
        "exit_reason": "DIAGNOSTIC_ONLY",
        "realized_pnl_pct": None,
        "metadata": {
            "origin": SOURCE,
            "strategy_id": STRATEGY_ID,
            "layer": LAYER_DISPLAY_NAME,
            "status_label": status,
            "research_disclaimer": "DIAGNOSTIC_ONLY — geplanter Entry/SL/TP, kein Trade-Verlauf",
            "diagnostic_only": True,
            "episode_tag": ep_tag,
            "cross_id": cross,
            "entry_utc": entry_utc,
            "chart_open_utc": row.get("chart_open_utc"),
            "sl_mode": sl_mode,
            "sl_distance_pct": sl_dist,
            "tp_distance_pct": tp_dist,
            "lower_lo": lo,
            "lower_hi": hi,
            "lower_origins": row.get("lower_origins"),
            "lower_width_pct": row.get("lower_width_pct"),
            "lower_density_i": row.get("lower_density_i"),
            "upper_lo": ulo,
            "upper_hi": uhi,
            "armed_bar_utc": row.get("armed_bar_utc"),
            "bullish_confirm_utc": row.get("bullish_confirm_utc"),
            "upper_gate_status": row.get("upper_gate_status"),
            "visual_box_end_utc": end.isoformat().replace("+00:00", "Z"),
            "no_exit_simulated": True,
        },
    }


def candidate_to_entry_marker(row: dict[str, Any], *, seq: int) -> dict[str, Any]:
    entry_utc = str(row.get("entry_utc") or "")
    cross = str(row.get("cross_id") or "")
    ep_tag = _episode_tag(row)
    safe = entry_utc.replace(":", "").replace("-", "")
    return {
        "drawing_id": f"{DRAWING_PREFIX}entry-{seq:02d}-{safe}",
        "kind": "entry_long",
        "symbol": "DOGEUSDT",
        "timestamp": entry_utc,
        "price": row.get("entry_price"),
        "label": f"{cross} Bar-C · {ep_tag}",
        "metadata": {
            "origin": SOURCE,
            "cross_id": cross,
            "episode_tag": ep_tag,
            "color": "green",
        },
    }


def build_overlay_bundle(*, filter_mode: FilterMode = "first_only", root: Path | None = None) -> dict[str, Any]:
    rows = load_p17aa_executables(root)
    picked = filter_executables(rows, filter_mode)
    positions = []
    markers = []
    for i, row in enumerate(picked, start=1):
        pos = candidate_to_position_spec(row, seq=i)
        if pos:
            positions.append(pos)
        markers.append(candidate_to_entry_marker(row, seq=i))
    return {
        "contract_version": "p17ab_diagnostic_chart_overlay_v1",
        "display_name": LAYER_DISPLAY_NAME,
        "source_p17aa": P17AA_REL,
        "filter_mode": filter_mode,
        "symbol": "DOGEUSDT",
        "timeframe": "5m",
        "source": SOURCE,
        "research_disclaimer": "DIAGNOSTIC_ONLY — keine Exits/PnL",
        "candidates": picked,
        "position_specs": positions,
        "marker_specs": markers,
    }


def build_drawing_specs(
    bundle: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    positions = []
    for spec in bundle.get("position_specs") or []:
        row = dict(spec)
        row["start"] = _parse_dt(row["start"])
        row["end"] = _parse_dt(row["end"])
        positions.append(row)
    markers = list(bundle.get("marker_specs") or [])
    hlines: list[dict[str, Any]] = []
    for pos in positions:
        pos.setdefault("metadata", {})
        pos["metadata"]["origin"] = SOURCE
        pos["metadata"]["strategy_id"] = STRATEGY_ID
    for mk in markers:
        mk.setdefault("metadata", {})["origin"] = SOURCE
    return positions, hlines, markers


def expected_position_count(filter_mode: str = "first_only") -> int:
    mode = filter_mode if filter_mode in ("first_only", "all") else "first_only"
    try:
        bundle = build_overlay_bundle(filter_mode=mode)  # type: ignore[arg-type]
    except (OSError, ValueError, json.JSONDecodeError):
        return 0
    return len(bundle.get("position_specs") or [])


__all__ = [
    "SOURCE",
    "STRATEGY_ID",
    "DRAWING_PREFIX",
    "LAYER_DISPLAY_NAME",
    "FIRST_IN_EPISODE_UTCS",
    "build_overlay_bundle",
    "build_drawing_specs",
    "expected_position_count",
    "load_p17aa_executables",
]
