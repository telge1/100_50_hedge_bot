"""BT-P17G — P17 Landscape LONG entry + SL chart overlay (July 2026, read-only)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from .bt_d12_latest_backtester import resolve_pools_ob_repo

SOURCE = "ema59_p17_landscape_long_overlay_v1"
STRATEGY_ID = "p17_landscape_long_july_2026_research"
CONTRACT_VERSION = "p17_july_chart_overlay_v1"
DISPLAY_NAME = "P17 Landscape LONG – Entry + SL (July 2026)"
SYMBOL = "DOGEUSDT"
TIMEFRAME = "5m"
PREFIX = "p17-long-"
ENTRY_COLOR = "#22c55e"
SL_COLOR = "#ef4444"
LINK_COLOR = "#94a3b8"

ARTIFACT_REL = Path(
    "results/phase_ema59_bt_p17_landscape_long_research_v1/p17_july_2026_chart_overlay_v1.json"
)


def artifact_path() -> Path:
    return resolve_pools_ob_repo() / ARTIFACT_REL


def _parse_utc(s: str) -> datetime:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)


@lru_cache(maxsize=1)
def load_chart_payload() -> dict[str, Any] | None:
    path = artifact_path()
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return raw if isinstance(raw, dict) else None


def artifacts_available() -> dict[str, Any]:
    path = artifact_path()
    payload = load_chart_payload()
    cands = (payload or {}).get("candidates") or []
    return {
        "artifact_path": str(path),
        "artifact_ok": payload is not None and bool(cands),
        "candidate_count": len(cands),
        "contract_version": CONTRACT_VERSION,
    }


def _entry_tooltip(c: dict[str, Any]) -> str:
    lines = [
        f"P17 LONG · {c.get('entry_utc')}",
        f"Entry {c.get('entry_price')} · SL {c.get('sl_price')} ({c.get('sl_distance_pct', 0):.2f}%)",
        f"Cross {c.get('cross_id')} · {c.get('mode', 'diagnostic').upper()}",
        f"A {c.get('near_a_utc')} · B {c.get('bullish_b_utc')}",
        f"Support {c.get('support_lo')} – {c.get('support_hi')}",
        f"Origins {c.get('distinct_origins')} · I {c.get('weighted_density_i')}",
        "Entry/SL-Diagnose — kein TP, kein PnL",
    ]
    return "\n".join(str(x) for x in lines)


def build_overlay_objects(
    candidates: list[dict[str, Any]],
    *,
    symbol: str,
    mode: str = "diagnostic",
) -> list[Any]:
    from .trp_import import load_trp

    trp = load_trp()
    OverlayMarker = trp["OverlayMarker"]
    OverlayLine = trp["OverlayLine"]
    OverlayStyle = trp["OverlayStyle"]
    ensure_utc = trp["ensure_utc"]
    sym = str(symbol or "").strip().upper()
    out: list[Any] = []
    for c in candidates:
        if mode == "single_entry" and c.get("mode") == "diagnostic" and c.get("diagnostic_only"):
            continue
        eid = str(c.get("event_id") or c.get("entry_utc") or "")
        entry_close = str(c.get("entry_utc") or "")
        if not entry_close:
            continue
        chart_open_dt = _parse_utc(entry_close) - timedelta(minutes=5)
        entry_px = c.get("entry_price")
        sl_px = c.get("sl_price")
        try:
            entry_f = float(entry_px) if entry_px is not None else None
        except (TypeError, ValueError):
            entry_f = None
        try:
            sl_f = float(sl_px) if sl_px is not None else None
        except (TypeError, ValueError):
            sl_f = None
        hhmm = entry_close[11:16] if len(entry_close) >= 16 else entry_close
        meta = {
            "origin": SOURCE,
            "strategy_id": STRATEGY_ID,
            "contract_version": CONTRACT_VERSION,
            "kind": "P17_LONG_ENTRY",
            "event_id": eid,
            "cross_id": c.get("cross_id"),
            "zone_id": c.get("zone_id"),
            "entry_decision_close_utc": entry_close,
            "near_a_utc": c.get("near_a_utc"),
            "bullish_b_utc": c.get("bullish_b_utc"),
            "mode": c.get("mode") or mode,
            "diagnostic_only": bool(c.get("diagnostic_only", True)),
            "validation_status": c.get("validation_status"),
            "support_lo": c.get("support_lo"),
            "support_hi": c.get("support_hi"),
            "sl_price": sl_f,
            "sl_distance_pct": c.get("sl_distance_pct"),
            "structural_sl_raw": c.get("structural_sl_raw"),
            "sl_cap_applied": c.get("sl_cap_applied"),
            "ignored_mini_pools": c.get("ignored_mini_pools"),
            "anchor_lo": c.get("anchor_lo"),
            "anchor_hi": c.get("anchor_hi"),
            "research_note": "P17 structural SL only — no TP / no exit / no PnL",
            "tooltip": _entry_tooltip(c),
        }
        out.append(
            OverlayMarker(
                overlay_id=f"{PREFIX}{eid}-entry",
                symbol=sym,
                timestamp=ensure_utc(chart_open_dt),
                price=entry_f,
                position="below",
                shape="arrow_up",
                text=f"P17 LONG {hhmm}",
                size=10.0,
                style=OverlayStyle(color=ENTRY_COLOR, width=1.2),
                timeframe_scope=TIMEFRAME,
                visible=True,
                z_order=48,
                metadata=meta,
            )
        )
        if sl_f is not None:
            dist = c.get("sl_distance_pct")
            dist_s = f"{float(dist):.2f}%" if dist is not None else "—"
            out.append(
                OverlayLine(
                    overlay_id=f"{PREFIX}{eid}-sl",
                    symbol=sym,
                    kind="horizontal",
                    price=sl_f,
                    style=OverlayStyle(color=SL_COLOR, width=1.2, opacity=0.9, line_style="dashed"),
                    label_text=f"SL {sl_f:.5f} (−{dist_s})",
                    timeframe_scope=TIMEFRAME,
                    visible=True,
                    z_order=47,
                    metadata={**meta, "kind": "P17_STRUCTURAL_SL"},
                )
            )
        if entry_f is not None and sl_f is not None and entry_f > sl_f:
            out.append(
                OverlayLine(
                    overlay_id=f"{PREFIX}{eid}-risk-link",
                    symbol=sym,
                    kind="segment",
                    start_timestamp=ensure_utc(chart_open_dt),
                    end_timestamp=ensure_utc(chart_open_dt + timedelta(minutes=5)),
                    start_price=entry_f,
                    end_price=sl_f,
                    price=entry_f,
                    style=OverlayStyle(color=LINK_COLOR, width=1.0, opacity=0.35, line_style="dotted"),
                    label_text="",
                    timeframe_scope=TIMEFRAME,
                    visible=True,
                    z_order=46,
                    metadata={**meta, "kind": "P17_ENTRY_SL_LINK"},
                )
            )
    return out


def build_summary(payload: dict[str, Any]) -> dict[str, Any]:
    cands = payload.get("candidates") or []
    times = [str(c.get("entry_utc") or "") for c in cands if c.get("entry_utc")]
    return {
        "contract_version": CONTRACT_VERSION,
        "strategy_id": STRATEGY_ID,
        "display_name": DISPLAY_NAME,
        "symbol": SYMBOL,
        "timeframe": TIMEFRAME,
        "candidate_count": len(cands),
        "artifact_path": str(artifact_path()),
        "default_mode": payload.get("default_mode") or "diagnostic",
        "time_span": {"start": min(times), "end": max(times)} if times else {},
        "funnel_diagnostic": payload.get("funnel_diagnostic"),
    }


def build_jump_list(payload: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for c in payload.get("candidates") or []:
        entry_close = str(c.get("entry_utc") or "")
        if not entry_close:
            continue
        out.append(
            {
                "event_id": c.get("event_id"),
                "label": f"{c.get('cross_id')} · {entry_close[11:16]} · {c.get('entry_price')}",
                "entry_decision_close_utc": entry_close,
                "chart_open_utc": (_parse_utc(entry_close) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:00Z"),
            }
        )
    return out
