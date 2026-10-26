"""BT-D7 pool landscape short research → TRP drawings (read-only run artifacts)."""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STRATEGY_ID = "pool_landscape_v1_candidate_bt_d7"
DISPLAY_NAME = "Pool Landscape SHORT · Depth 2 (Research)"
BACKTESTER_SOURCE = "bt_d7_short_research_overlay_v1"
DRAWING_PREFIX = "btd10-short-"
HLINE_PREFIX = "btd10-pending-"
ARROW_PREFIX = "btd10-marker-"

REPO_ROOT = Path(__file__).resolve().parents[4]
BT_D10_TOOLS = REPO_ROOT / "tools/phase_bt_d10_existing_viewer_overlay_integration_v1"

ALLOWED_RESULT_ROOTS = (
    REPO_ROOT / "results/phase_bt_d6_config_experiments_v1",
    REPO_ROOT / "results/phase_bt_d7_config_driven_short_pnl_integration_v1",
)

RUN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,120}$", re.I)
EXPECTED_STRATEGY = "pool_landscape_v1_candidate"


def _ensure_bt_d10_import() -> None:
    p = str(BT_D10_TOOLS)
    if p not in sys.path:
        sys.path.insert(0, p)


def _parse_utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _validate_manifest(manifest: dict[str, Any]) -> list[str]:
    errs: list[str] = []
    if str(manifest.get("strategy_id") or "") != EXPECTED_STRATEGY:
        errs.append("strategy_id_mismatch")
    dc = manifest.get("density_contract") or {}
    if str(dc.get("timeframe") or "5m") != "5m":
        errs.append("timeframe_not_5m")
    if int(dc.get("pool_detail") or 0) != 1:
        errs.append("pool_detail_not_pd1")
    if str(dc.get("method") or "") not in ("exact_unique_origin", ""):
        if dc.get("method") and dc.get("method") != "exact_unique_origin":
            errs.append("density_method_not_exact_unique_origin")
    if int(dc.get("min_unique_origins") or 2) < 2:
        errs.append("min_unique_origins_below_2")
    return errs


def discover_runs() -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for root in ALLOWED_RESULT_ROOTS:
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*/integration_result.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            manifest = raw.get("manifest") or {}
            if _validate_manifest(manifest):
                continue
            run_id = path.parent.name
            pnl = raw.get("pnl") or {}
            signals = raw.get("signals") or []
            trades = raw.get("trades") or []
            runs.append(
                {
                    "run_id": run_id,
                    "config_hash": str(raw.get("config_hash") or manifest.get("config_hash") or "")[:16],
                    "strategy_version": str(manifest.get("strategy_version") or ""),
                    "symbol": "DOGEUSDT",
                    "signal_count": len(signals),
                    "trade_count": len(trades),
                    "closed_trades": int(pnl.get("closed_trades") or len(trades)),
                    "net_pnl_usdt": pnl.get("net_pnl_usdt"),
                    "return_pct": pnl.get("total_return_pct"),
                    "upper_gap_min_pct": manifest.get("upper_gap_min_pct"),
                    "backtest_start": (manifest.get("backtest") or {}).get("start_utc")
                    if isinstance(manifest.get("backtest"), dict)
                    else None,
                    "integration_path": str(path.relative_to(REPO_ROOT)),
                }
            )
    runs.sort(key=lambda r: r["run_id"])
    return runs


def resolve_run_path(run_id: str) -> Path | None:
    rid = str(run_id or "").strip()
    if not rid or not RUN_ID_RE.match(rid):
        return None
    for root in ALLOWED_RESULT_ROOTS:
        candidate = (root / rid / "integration_result.json").resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            continue
        if candidate.is_file():
            return candidate
    return None


def load_validated_run(
    run_id: str, *, symbol: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None, list[str]]:
    sym = str(symbol or "").strip().upper()
    if sym != "DOGEUSDT":
        return None, None, None, ["symbol_not_allowed"]
    path = resolve_run_path(run_id)
    if path is None:
        return None, None, None, ["run_not_found"]
    raw = json.loads(path.read_text(encoding="utf-8"))
    manifest = raw.get("manifest") or {}
    errs = _validate_manifest(manifest)
    if errs:
        return None, None, None, errs
    _ensure_bt_d10_import()
    from overlay_export_v1 import export_overlay_bundle  # noqa: WPS433

    bundle = export_overlay_bundle(raw, integration_path=path, symbol=sym)
    meta = build_summary(raw, bundle, path)
    return raw, bundle, meta, []


def build_summary(raw: dict[str, Any], bundle: dict[str, Any], path: Path) -> dict[str, Any]:
    manifest = raw.get("manifest") or {}
    pnl = raw.get("pnl") or {}
    signals = raw.get("signals") or []
    trades = raw.get("trades") or []
    times = [s.get("decision_close_utc") for s in signals if s.get("decision_close_utc")]
    time_span = {}
    if times:
        time_span = {"start": min(times), "end": max(times)}
    return {
        "run_id": path.parent.name,
        "strategy_id": manifest.get("strategy_id"),
        "strategy_version": manifest.get("strategy_version"),
        "strategy_freeze_status": manifest.get("strategy_freeze_status"),
        "config_hash": raw.get("config_hash") or manifest.get("config_hash"),
        "upper_gap_min_pct": manifest.get("upper_gap_min_pct"),
        "tp_selection_mode": manifest.get("tp_selection_mode"),
        "depth": 2,
        "timeframe": "5m",
        "pool_detail": 1,
        "time_span": time_span,
        "signal_count": len(signals),
        "filled_trade_count": len(trades),
        "closed_trades": int(pnl.get("closed_trades") or 0),
        "net_pnl_usdt": pnl.get("net_pnl_usdt"),
        "return_pct": pnl.get("total_return_pct"),
        "win_rate": pnl.get("win_rate"),
        "max_drawdown_pct": pnl.get("max_drawdown_pct"),
        "contract_version": bundle.get("contract_version"),
        "integration_path": str(path.relative_to(REPO_ROOT)),
    }


def build_drawing_specs(bundle: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (position_specs, hline_specs, marker_specs)."""
    _ensure_bt_d10_import()
    from trp_position_adapter_v1 import overlay_bundle_to_position_specs  # noqa: WPS433

    positions = overlay_bundle_to_position_specs(bundle)
    hlines: list[dict[str, Any]] = []
    markers: list[dict[str, Any]] = []
    sym = str(bundle.get("symbol") or "DOGEUSDT").upper()

    for rec in bundle.get("records") or []:
        lifecycle = str(rec.get("lifecycle_state") or "")
        sid = str(rec.get("signal_id") or "")
        safe = sid.replace(":", "").replace("|", "_").replace("+", "")

        if lifecycle in ("PENDING", "SIGNAL", "CANCELLED", "EXPIRED"):
            continue

        if lifecycle in ("CLOSED_TP", "CLOSED_SL", "CLOSED", "FILLED_OPEN"):
            fill_t = rec.get("fill_time_utc")
            if fill_t:
                markers.append(
                    {
                        "drawing_id": f"{ARROW_PREFIX}{safe}-fill",
                        "kind": "fill",
                        "symbol": sym,
                        "timestamp": fill_t,
                        "price": rec.get("fill_price") or rec.get("entry_price"),
                        "label": "Fill",
                        "metadata": {"origin": BACKTESTER_SOURCE},
                    }
                )
            exit_t = rec.get("exit_time_utc")
            if exit_t and lifecycle.startswith("CLOSED"):
                markers.append(
                    {
                        "drawing_id": f"{ARROW_PREFIX}{safe}-exit",
                        "kind": "exit",
                        "symbol": sym,
                        "timestamp": exit_t,
                        "price": rec.get("exit_price"),
                        "label": str(rec.get("exit_reason") or "Exit"),
                        "metadata": {"origin": BACKTESTER_SOURCE},
                    }
                )

    for pos in positions:
        pos["metadata"] = dict(pos.get("metadata") or {})
        pos["metadata"].update(
            {
                "realized_net_pnl_usdt": next(
                    (
                        r.get("realized_net_pnl_usdt")
                        for r in bundle.get("records") or []
                        if r.get("trade_id") == pos["metadata"].get("trade_id")
                    ),
                    None,
                ),
                "realized_pnl_pct": next(
                    (
                        r.get("realized_pnl_pct")
                        for r in bundle.get("records") or []
                        if r.get("trade_id") == pos["metadata"].get("trade_id")
                    ),
                    None,
                ),
            }
        )

    return positions, hlines, markers
