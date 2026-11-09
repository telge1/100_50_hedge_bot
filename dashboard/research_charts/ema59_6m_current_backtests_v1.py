"""EMA59 6M LONG — catalog of frozen research backtests for Research Chart."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .bt_d12_latest_backtester import resolve_pools_ob_repo

STRATEGY_ID = "ema59_long_research_6m_v1"

DEFAULT_BACKTEST_ID = "ema59_6m_juli_p16a_min6"
JULY_2026_ZOOM = {
    "start": "2026-07-01T00:00:00Z",
    "end": "2026-07-31T23:55:00Z",
    "focus": "2026-07-06T13:35:00Z",
}


@dataclass(frozen=True)
class Ema59CurrentBacktestSpec:
    backtest_id: str
    label: str
    source: str
    drawing_prefix: str
    overlay_rel: str | None = None
    backtest_result_rel: str | None = None
    status_tag: str = ""
    run_id: str = ""
    zoom_july_2026: bool = False


CATALOG: tuple[Ema59CurrentBacktestSpec, ...] = (
    Ema59CurrentBacktestSpec(
        backtest_id=DEFAULT_BACKTEST_ID,
        label="EMA59 6M LONG — Juli Origin≥6 (P16A)",
        source="ema59_bt_p16a_6m_min6_origins_v1",
        drawing_prefix="ema59p16a-6-",
        overlay_rel=(
            "results/phase_ema59_bt_p16a_6m_long_research_v1/min_touch_origins_6/overlay_6m_july.json"
        ),
        status_tag="P16A-Min6",
        run_id="BT-P16A-JULY-min6",
        zoom_july_2026=True,
    ),
    Ema59CurrentBacktestSpec(
        backtest_id="ema59_6m_juli_watch_p12",
        label="EMA59 6M LONG — Juli WATCH P12 (Archiv)",
        source="ema59_bt_p12_6m_current_backtest_v1",
        drawing_prefix="ema59p12-",
        overlay_rel="results/phase_ema59_bt_p12_6m_long_watch_v1/overlay_6m_july_watch_p12.json",
        status_tag="WATCH-P12",
        run_id="BT-P12-JULY-2026",
    ),
    Ema59CurrentBacktestSpec(
        backtest_id="ema59_6m_juli_p13e_sl_fallback",
        label="EMA59 6M LONG — Juli P13E SL-Fallback (Archiv)",
        source="ema59_bt_p13e_6m_current_backtest_v1",
        drawing_prefix="ema59p13e-",
        backtest_result_rel="results/phase_ema59_bt_p13e_6m_long_sl_fallback_v1/backtest_result.json",
        status_tag="P13E",
        run_id="BT-P13E-JULY-2026",
    ),
    Ema59CurrentBacktestSpec(
        backtest_id="ema59_6m_juli_p15_min6_variant",
        label="EMA59 6M LONG — Juli P15 Variante C ≥6 (Archiv)",
        source="ema59_bt_p15_min6_variant_v1",
        drawing_prefix="ema59p15c-",
        backtest_result_rel=(
            "results/phase_ema59_bt_p15_origin_min_july_v1/variant_c_min6_origins/backtest_result.json"
        ),
        status_tag="P15-Min6",
        run_id="BT-P15-VARIANT-C",
    ),
)

_CATALOG_BY_ID = {s.backtest_id: s for s in CATALOG}


def list_catalog() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for spec in CATALOG:
        out.append(
            {
                "backtest_id": spec.backtest_id,
                "label": spec.label,
                "is_default": spec.backtest_id == DEFAULT_BACKTEST_ID,
                "trade_count_hint": _trade_count_hint(spec),
            }
        )
    return out


def _trade_count_hint(spec: Ema59CurrentBacktestSpec) -> int | None:
    root = resolve_pools_ob_repo()
    if spec.overlay_rel:
        path = root / spec.overlay_rel
        if path.is_file():
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
                return len(doc.get("records") or doc.get("position_specs") or [])
            except (OSError, json.JSONDecodeError):
                return None
    if spec.backtest_result_rel:
        path = root / spec.backtest_result_rel
        if path.is_file():
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
                return len(doc.get("trades") or [])
            except (OSError, json.JSONDecodeError):
                return None
    return None


def get_spec(backtest_id: str) -> Ema59CurrentBacktestSpec | None:
    return _CATALOG_BY_ID.get(str(backtest_id or "").strip())


def _enrich_overlay_bundle(bundle: dict[str, Any], *, root: Path, overlay_path: Path | None) -> None:
    min_origins: int | None = None
    if overlay_path is not None:
        params_path = overlay_path.parent / "parameters.json"
        if params_path.is_file():
            try:
                min_origins = int(json.loads(params_path.read_text(encoding="utf-8")).get("min_touch_distinct_origins"))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                min_origins = None
    records_by_id = {str(r.get("trade_id")): r for r in bundle.get("records") or []}
    for pos in bundle.get("position_specs") or []:
        meta = pos.setdefault("metadata", {})
        tid = str(meta.get("trade_id") or pos.get("signal_id") or "")
        rec = records_by_id.get(tid) or {}
        if min_origins is not None:
            meta["min_touch_distinct_origins"] = min_origins
        meta["entry_bar_utc"] = rec.get("fill_time_utc") or meta.get("entry_bar_utc")
        meta["entry_price"] = rec.get("entry_price") or pos.get("entry")
        meta["sl_price"] = rec.get("sl_price") or pos.get("stop")
        meta["tp_price"] = rec.get("tp_price") or pos.get("target")
        meta["exit_reason"] = rec.get("exit_reason") or pos.get("exit_reason")
        pnl = rec.get("realized_pnl_pct")
        if pnl is not None:
            meta["gross_return_pct"] = round(float(pnl), 6)


def load_overlay_bundle(backtest_id: str) -> tuple[dict[str, Any] | None, list[str]]:
    spec = get_spec(backtest_id)
    if spec is None:
        return None, ["unknown_backtest_id"]
    root = resolve_pools_ob_repo()
    overlay_path: Path | None = None
    if spec.overlay_rel:
        overlay_path = root / spec.overlay_rel
        if not overlay_path.is_file():
            return None, ["overlay_missing"]
        try:
            bundle = json.loads(overlay_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None, ["overlay_corrupt"]
        _enrich_overlay_bundle(bundle, root=root, overlay_path=overlay_path)
        return bundle, []
    if not spec.backtest_result_rel:
        return None, ["no_artifact"]
    bt_path = root / spec.backtest_result_rel
    if not bt_path.is_file():
        return None, ["backtest_result_missing"]
    try:
        doc = json.loads(bt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, ["backtest_result_corrupt"]
    trades = doc.get("trades") or []
    if not trades:
        return None, ["zero_trades"]
    pkg = root / "tools/phase_ema59_bt_p12_6m_watch_v1"
    if str(pkg) not in sys.path:
        sys.path.insert(0, str(pkg))
    from ema59_research_overlay_v1 import build_overlay_bundle  # noqa: WPS433

    bundle = build_overlay_bundle(
        trades=trades,
        run_id=spec.run_id or spec.backtest_id,
        source=spec.source,
        prefix=spec.drawing_prefix,
        status_tag=spec.status_tag or spec.backtest_id,
    )
    min_o = (doc.get("parameters") or {}).get("min_touch_distinct_origins")
    if min_o is not None:
        for pos in bundle.get("position_specs") or []:
            pos.setdefault("metadata", {})["min_touch_distinct_origins"] = int(min_o)
    return bundle, []


def build_drawing_specs(bundle: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    from datetime import datetime, timezone

    def _dt(v: Any) -> datetime:
        if isinstance(v, datetime):
            return v
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(timezone.utc)

    positions: list[dict[str, Any]] = []
    for spec in bundle.get("position_specs") or []:
        row = dict(spec)
        row["start"] = _dt(row.get("start"))
        row["end"] = _dt(row.get("end"))
        positions.append(row)
    markers = list(bundle.get("marker_specs") or [])
    source = str(bundle.get("source") or "")
    for pos in positions:
        meta = dict(pos.get("metadata") or {})
        meta["origin"] = source
        meta.pop("strategy_id", None)
        pos["metadata"] = meta
    for mk in markers:
        mk_meta = dict(mk.get("metadata") or {})
        mk_meta["origin"] = source
        mk["metadata"] = mk_meta
    return positions, [], markers


def is_current_backtest_drawing(drawing: dict[str, Any], spec: Ema59CurrentBacktestSpec) -> bool:
    meta = drawing.get("metadata") or {}
    if meta.get("origin") == spec.source:
        return True
    did = str(drawing.get("id") or drawing.get("drawing_id") or "")
    return did.startswith(spec.drawing_prefix)


def all_sources() -> list[str]:
    return [s.source for s in CATALOG]
