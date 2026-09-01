"""Dashboard adapter for Nested Ask Pool Edge Short V1 (research overlays only)."""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .oa_import import ensure_oa_on_path
from .trp_import import load_trp

STRATEGY_ID = "a_plus_nested_ask_pool_edge_short_v1"
BACKTESTER_SOURCE = "a_plus_nested_ask_pool_edge_short_v1"
RESULTS_ROOT = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "a_plus_nested_ask_pool_edge_short_v1"
)
DISPLAY_NAME = "Nested Ask Pool Edge Short V1 — Research, SHORT only"

# Engine/pandas dumps sometimes emit bare NaN / Infinity tokens.
_NONFINITE_JSON_RE = re.compile(r"\b(-?Infinity|NaN)\b")


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _utc(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "nat", "none", "null"}:
        return None
    try:
        return _utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        return None


def _finite_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num):
        return None
    return num


def json_safe(value: Any) -> Any:
    """Replace non-finite floats so FastAPI/Starlette JSON encoding cannot 500."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [json_safe(v) for v in value]
    return value


def _load_json_file(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = json.loads(_NONFINITE_JSON_RE.sub("null", raw))
    if not isinstance(data, dict):
        return {}
    cleaned = json_safe(data)
    return cleaned if isinstance(cleaned, dict) else {}


def ensure_nested_on_path() -> None:
    ensure_oa_on_path()


def run_nested_ask_pool_backtest(
    *,
    symbol: str,
    start: str,
    end: str,
    show_rejected: bool = False,
) -> dict[str, Any]:
    """Thin wrapper — invokes orderbook_analyse research_entry only."""
    ensure_nested_on_path()
    from orderbook_analyse.a_plus_nested_ask_pool_edge_short_v1.research_entry import (
        run_single_symbol_research_backtest,
    )

    return run_single_symbol_research_backtest(
        symbol=symbol,
        start=start,
        end=end,
        out_dir=RESULTS_ROOT,
        show_rejected_overlays=show_rejected,
    )


def load_run_payload(run_dir: Path | str) -> dict[str, Any]:
    p = Path(run_dir)
    overlay: dict[str, Any] = {}
    op = p / "dashboard_overlay_payload.json"
    if op.is_file():
        overlay = _load_json_file(op)
    provenance: dict[str, Any] = {}
    pp = p / "dashboard_provenance.json"
    if pp.is_file():
        provenance = _load_json_file(pp)
    manifest: dict[str, Any] = {}
    mp = p / "manifest.json"
    if mp.is_file():
        manifest = _load_json_file(mp)
    summary = json_safe(manifest.get("summary") or provenance.get("summary") or {})
    return {
        "meta": {
            "symbol": provenance.get("symbol") or (overlay.get("symbol")),
            "strategy_id": STRATEGY_ID,
            "run_id": provenance.get("run_id") or manifest.get("run_id"),
            "import_path": str(p),
            "source": BACKTESTER_SOURCE,
            "start_utc": provenance.get("start_utc"),
            "end_utc": provenance.get("end_utc"),
            "research_note": "Research simulation — keine ausgeführten Live-Trades",
        },
        "summary": summary if isinstance(summary, dict) else {},
        "provenance": provenance,
        "overlay": overlay,
        "markers": list(overlay.get("specs") or []),
        "strategy_id": STRATEGY_ID,
    }


def build_overlay_objects(marker_specs: list[dict[str, Any]], *, symbol: str) -> list[Any]:
    trp = load_trp()
    OverlayLine = trp["OverlayLine"]
    OverlayMarker = trp["OverlayMarker"]
    OverlayStyle = trp["OverlayStyle"]
    ensure_utc = trp["ensure_utc"]
    out: list[Any] = []
    sym = str(symbol or "").upper()

    for spec in marker_specs:
        kind = str(spec.get("kind") or "")
        meta = json_safe(spec.get("meta") or {})
        if not isinstance(meta, dict):
            meta = {}
        if kind in {"NAP_PENDING_LIMIT", "NAP_SL", "NAP_TP"} or spec.get("line_kind") == "segment":
            st = _parse_ts(spec.get("start_timestamp"))
            et = _parse_ts(spec.get("end_timestamp"))
            if st is None or et is None:
                continue
            sp = _finite_float(
                spec.get("start_price") if spec.get("start_price") is not None else spec.get("price")
            )
            ep = _finite_float(spec.get("end_price") if spec.get("end_price") is not None else sp)
            if sp is None or ep is None:
                continue
            out.append(
                OverlayLine(
                    overlay_id=str(spec["overlay_id"]),
                    symbol=sym,
                    kind="segment",
                    start_timestamp=ensure_utc(st),
                    end_timestamp=ensure_utc(et),
                    start_price=sp,
                    end_price=ep,
                    price=sp,
                    style=OverlayStyle(color=spec.get("color") or "#e67e22", width=2.0, opacity=0.9),
                    label_text=str(spec.get("label_text") or spec.get("text") or ""),
                    timeframe_scope="all",
                    visible=True,
                    z_order=42 if kind == "NAP_PENDING_LIMIT" else 40,
                    metadata={
                        "origin": BACKTESTER_SOURCE,
                        "strategy_id": STRATEGY_ID,
                        "kind": kind,
                        "tooltip": spec.get("tooltip"),
                        "research_note": "Research simulation — keine ausgeführten Live-Trades",
                        **meta,
                    },
                )
            )
            continue

        ts = _parse_ts(spec.get("timestamp"))
        if ts is None:
            continue
        px = _finite_float(spec.get("price"))
        # Exit markers without a finite price still render via TRP position=above.
        position = str(spec.get("position") or "at_price")
        if px is None:
            position = "above"
        out.append(
            OverlayMarker(
                overlay_id=str(spec["overlay_id"]),
                symbol=sym,
                timestamp=ensure_utc(ts),
                price=px,
                position=position,
                shape=str(spec.get("shape") or "arrow_down"),
                text=str(spec.get("text") or ""),
                size=10.0 if kind == "NAP_FILL" else 8.0,
                style=OverlayStyle(color=spec.get("color") or "#d62728", width=1.0),
                timeframe_scope="all",
                visible=True,
                z_order=46 if kind == "NAP_FILL" else 44,
                metadata={
                    "origin": BACKTESTER_SOURCE,
                    "strategy_id": STRATEGY_ID,
                    "kind": kind,
                    "tooltip": spec.get("tooltip"),
                    "research_note": "Research simulation — keine ausgeführten Live-Trades",
                    **meta,
                },
            )
        )
    return out


def ui_summary_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    summary = payload.get("summary") or {}
    structural = summary.get("structural_sl") or {}
    n_cands = int(summary.get("candidates") or 0)
    n_fills = int(summary.get("fills_strict") or 0)
    n = int(structural.get("n") or n_fills or 0)
    return json_safe(
        {
            "candidates": n_cands,
            "pending_orders": n_cands,
            "fills": n_fills,
            "fill_rate": summary.get("fill_rate_vs_candidates"),
            "tp": structural.get("tp_first"),
            "sl": structural.get("sl_first"),
            "ambiguous": summary.get("ambiguous"),
            "winrate": structural.get("winrate"),
            "net_expectancy": structural.get("expectancy_net_pct"),
            "profit_factor": structural.get("profit_factor"),
            "net_pnl": structural.get("net_pnl_pct_sum"),
            "cost_pct": (payload.get("provenance") or {}).get("frozen_config", {}).get(
                "roundtrip_cost_pct_baseline"
            ),
            "symbol": (payload.get("meta") or {}).get("symbol"),
            "run_id": (payload.get("meta") or {}).get("run_id"),
            "start_utc": (payload.get("meta") or {}).get("start_utc"),
            "end_utc": (payload.get("meta") or {}).get("end_utc"),
            "sample_note": "Kleine Stichprobe — nur deskriptiv" if n < 30 else None,
            "research_note": "Research simulation — keine ausgeführten Live-Trades",
        }
    )
