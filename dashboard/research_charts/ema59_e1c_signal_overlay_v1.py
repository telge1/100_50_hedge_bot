"""Phase E3B — E1C EMA59 −2σ long-cross signals → Research Chart OverlayMarkers (read-only)."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .bt_d12_latest_backtester import resolve_pools_ob_repo

CONTRACT_VERSION = "ema59_e1c_july_chart_overlay_v1"
STRATEGY_ID = "ema59_e1c_long_signals_july_2026_research"
BACKTESTER_SOURCE = "ema59_e1c_signal_overlay_v1"
PAYLOAD_KIND = "ema59_e1c_july_chart_integration_v1"
WORKSPACE_SNAP_KEY = "ema59_e1c_july_research"
DISPLAY_NAME = "EMA59 −2σ Signale — Juli 2026"
SYMBOL = "DOGEUSDT"
TIMEFRAME = "5m"
VARIANT = "E1C_V_MONTHLY_FIXED"
ANALYSIS_MONTH = "2026-07"
EXPECTED_SIGNAL_COUNT = 20

POOLS_REL = Path("results/phase_e1c_ema59_6month_stddev_v1/long_crosses_E1C_V_MONTHLY_FIXED.json")
BUNDLED_NAME = "e1c_ema59_monthly_fixed_long_crosses.json"
BUNDLED_SHA256 = "91a10d56d284b1cac9a9ac7258654d6cf4d8670adfbf6468f87f03c4938251b1"
MARKER_OVERLAY_PREFIX = "e1c59-july-"
MARKER_COLOR = "#22c55e"


def _bundled_path() -> Path:
    return Path(__file__).resolve().parent / "data" / BUNDLED_NAME


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_utc(s: str) -> datetime:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)


def _overlay_id_for_row(row: dict[str, Any], index: int) -> str:
    open_u = str(row.get("candle_open_utc") or "")
    slug = re.sub(r"[^0-9TZtz]", "", open_u) or str(index)
    return f"{MARKER_OVERLAY_PREFIX}{slug}"


def resolve_source_path() -> tuple[Path, bool]:
    """Return (path, from_pools_repo). Prefer pools+ob live file, else bundled."""
    pools = resolve_pools_ob_repo() / POOLS_REL
    bundled = _bundled_path()
    if pools.is_file():
        return pools, True
    if bundled.is_file():
        return bundled, False
    return pools, True


def load_long_cross_rows() -> tuple[list[dict[str, Any]], Path, str, list[str]]:
    path, _from_pools = resolve_source_path()
    errs: list[str] = []
    if not path.is_file():
        return [], path, "", errs + ["ledger_not_found"]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return [], path, "", errs + ["ledger_read_error"]
    if not isinstance(raw, list):
        return [], path, "", errs + ["ledger_not_array"]
    digest = sha256_file(path)
    if path == _bundled_path() and digest != BUNDLED_SHA256:
        errs.append(f"bundled_sha256_mismatch:{digest}")
    rows = [
        r
        for r in raw
        if isinstance(r, dict)
        and str(r.get("variant") or "") == VARIANT
        and str(r.get("analysis_month") or "") == ANALYSIS_MONTH
    ]
    rows.sort(key=lambda r: str(r.get("candle_open_utc") or ""))
    if len(rows) != EXPECTED_SIGNAL_COUNT:
        errs.append(f"signal_count:{len(rows)}")
    return rows, path, digest, errs


def build_marker_specs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for i, row in enumerate(rows):
        open_u = str(row.get("candle_open_utc") or "")
        decision_u = str(row.get("decision_close_utc") or "")
        if not open_u or not decision_u:
            continue
        close_px = row.get("close")
        try:
            price = float(close_px) if close_px is not None else None
        except (TypeError, ValueError):
            price = None
        specs.append(
            {
                "overlay_id": _overlay_id_for_row(row, i),
                "timestamp": open_u,
                "price": price,
                "position": "below",
                "shape": "arrow_up",
                "text": "EMA59 −2σ",
                "color": MARKER_COLOR,
                "decision_close_utc": decision_u,
                "ema_dist_pct": row.get("ema_dist_pct"),
                "thr_long": row.get("thr_long"),
                "candle_open_utc": open_u,
            }
        )
    return specs


def build_overlay_markers(marker_specs: list[dict[str, Any]], *, symbol: str) -> list[Any]:
    from .trp_import import load_trp

    trp = load_trp()
    OverlayMarker = trp["OverlayMarker"]
    OverlayStyle = trp["OverlayStyle"]
    ensure_utc = trp["ensure_utc"]
    sym = str(symbol or "").strip().upper()
    out = []
    for spec in marker_specs:
        meta = {
            "origin": BACKTESTER_SOURCE,
            "strategy_id": STRATEGY_ID,
            "contract_version": CONTRACT_VERSION,
            "kind": "E1C_LONG_CROSS",
            "variant": VARIANT,
            "analysis_month": ANALYSIS_MONTH,
            "candle_open_utc": spec.get("candle_open_utc"),
            "decision_close_utc": spec.get("decision_close_utc"),
            "ema_dist_pct": spec.get("ema_dist_pct"),
            "thr_long": spec.get("thr_long"),
            "research_note": "E1C signal only — not a trade entry",
        }
        out.append(
            OverlayMarker(
                overlay_id=str(spec["overlay_id"]),
                symbol=sym,
                timestamp=ensure_utc(_parse_utc(str(spec["timestamp"]))),
                price=spec.get("price"),
                position=str(spec.get("position") or "below"),
                shape=str(spec.get("shape") or "arrow_up"),
                text=str(spec.get("text") or ""),
                size=9.0,
                style=OverlayStyle(color=str(spec.get("color") or MARKER_COLOR), width=1.0),
                timeframe_scope=TIMEFRAME,
                visible=True,
                z_order=43,
                metadata=meta,
            )
        )
    return out


def build_summary(
    rows: list[dict[str, Any]],
    *,
    ledger_path: Path,
    source_sha256: str,
    marker_count: int,
) -> dict[str, Any]:
    times = [str(r.get("candle_open_utc") or "") for r in rows if r.get("candle_open_utc")]
    decisions = [str(r.get("decision_close_utc") or "") for r in rows if r.get("decision_close_utc")]
    return {
        "contract_version": CONTRACT_VERSION,
        "strategy_id": STRATEGY_ID,
        "symbol": SYMBOL,
        "timeframe": TIMEFRAME,
        "variant": VARIANT,
        "analysis_month": ANALYSIS_MONTH,
        "signal_count": len(rows),
        "marker_count": marker_count,
        "expected_signal_count": EXPECTED_SIGNAL_COUNT,
        "display_name": DISPLAY_NAME,
        "ledger_path": str(ledger_path),
        "source_sha256": source_sha256,
        "payload_kind": PAYLOAD_KIND,
        "time_span": {"start": min(times), "end": max(times)} if times else {},
        "decision_close_span": {"start": min(decisions), "end": max(decisions)} if decisions else {},
    }
